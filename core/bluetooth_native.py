"""Native BLE peripheral adapters. Imported lazily only for the Advertise role.

Windows uses WinRT directly and never renames/restarts the Bluetooth adapter or
requests administrator rights. macOS uses CoreBluetooth on its own dispatch queue.
"""
import asyncio
import sys
import uuid

from core.bluetooth_transport import RX_UUID, TX_UUID


def make_peripheral(service, receive):
    if sys.platform == "darwin":
        from core.bluetooth_mac import MacPeripheral
        return MacPeripheral(service, receive)
    if sys.platform == "win32":
        return WindowsPeripheral(service, receive)
    raise OSError("Bluetooth advertising in this prototype supports Windows and macOS.")


class WindowsPeripheral:
    def __init__(self, service, receive):
        self.service, self.receive = service, receive
        self.loop = asyncio.get_running_loop()
        self.disconnected = asyncio.Event()
        self.packet_size = 20
        self.client = None
        self.provider = None
        self.callbacks = []
        self.tasks = set()

    async def start(self):
        from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothError
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCharacteristicProperties,
            GattLocalCharacteristicParameters,
            GattProtectionLevel,
            GattServiceProvider,
            GattServiceProviderAdvertisingParameters,
        )
        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None or not adapter.is_peripheral_role_supported:
            raise OSError("This Windows Bluetooth adapter cannot advertise. Choose Connect here and Advertise on the other computer.")
        result = await GattServiceProvider.create_async(uuid.UUID(self.service))
        if result.error != BluetoothError.SUCCESS:
            raise OSError(f"Windows could not create a Bluetooth service ({result.error})")
        self.provider = result.service_provider
        rx_params = GattLocalCharacteristicParameters()
        rx_params.characteristic_properties = GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
        rx_params.write_protection_level = GattProtectionLevel.PLAIN
        tx_params = GattLocalCharacteristicParameters()
        tx_params.characteristic_properties = GattCharacteristicProperties.NOTIFY
        tx_params.read_protection_level = GattProtectionLevel.PLAIN
        rx = await self.provider.service.create_characteristic_async(uuid.UUID(RX_UUID), rx_params)
        tx = await self.provider.service.create_characteristic_async(uuid.UUID(TX_UUID), tx_params)
        if rx.error != BluetoothError.SUCCESS or tx.error != BluetoothError.SUCCESS:
            raise OSError("Windows could not create Bluetooth input channels")
        self.rx, self.tx = rx.characteristic, tx.characteristic

        def subscribed(sender, _args):
            clients = list(sender.subscribed_clients)
            self.loop.call_soon_threadsafe(self._subscribed, clients)

        def written(_sender, args):
            # Take the deferral synchronously, before returning to WinRT's event dispatcher.
            deferral = args.get_deferral()
            def schedule():
                task = asyncio.create_task(self._written(args, deferral))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            self.loop.call_soon_threadsafe(schedule)

        self.callbacks = [(self.tx, self.tx.add_subscribed_clients_changed(subscribed), "subscribed"),
                          (self.rx, self.rx.add_write_requested(written), "write")]
        params = GattServiceProviderAdvertisingParameters()
        params.is_connectable, params.is_discoverable = True, True
        self.provider.start_advertising(params)
        # StartAdvertising returns before the radio accepts or rejects the advertisement.
        for _ in range(100):
            status = int(self.provider.advertisement_status)
            if status in (2, 4):  # Started (possibly without the optional local name)
                return
            if status == 3:  # Aborted
                raise OSError("Bluetooth advertising was rejected by Windows. Check Bluetooth permissions and adapter support.")
            await asyncio.sleep(0.1)
        raise OSError("Bluetooth advertising timed out")

    def _subscribed(self, clients):
        if self.client is None and clients:
            self.client = clients[0]
            self.packet_size = min(244, int(self.client.session.max_pdu_size) - 3)
        elif self.client is not None:
            own = self.client.session.device_id.id
            if not any(c.session.device_id.id == own for c in clients):
                self.disconnected.set()

    async def _written(self, args, deferral):
        from winrt.windows.storage.streams import DataReader
        try:
            request = await args.get_request_async()
            if request is None:
                return
            if self.client is None or args.session.device_id.id != self.client.session.device_id.id:
                request.respond_with_protocol_error(0x08)  # insufficient authorization
                return
            if request.offset != 0 or request.value.length > 244:
                request.respond_with_protocol_error(0x0d)
                return
            reader = DataReader.from_buffer(request.value)
            data = bytearray(reader.unconsumed_buffer_length)
            reader.read_bytes(data)
            self.receive(data)
            # RX only exposes Write Without Response; the stream ACK supplies delivery confirmation.
        except Exception:
            self.disconnected.set()
        finally:
            deferral.complete()

    async def send(self, data):
        from winrt.windows.storage.streams import DataWriter
        if self.client is None or self.disconnected.is_set():
            raise ConnectionError("Bluetooth subscriber disconnected")
        writer = DataWriter()
        writer.write_bytes(data)
        result = await self.tx.notify_value_for_subscribed_client_async(writer.detach_buffer(), self.client)
        if int(result.status) != 0:
            raise ConnectionError(f"Windows Bluetooth notification failed ({result.status})")

    async def stop(self):
        if self.provider is not None:
            self.provider.stop_advertising()
        for obj, token, kind in self.callbacks:
            if kind == "subscribed":
                obj.remove_subscribed_clients_changed(token)
            else:
                obj.remove_write_requested(token)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.callbacks.clear()
