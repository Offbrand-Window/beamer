"""CoreBluetooth peripheral implementation, with native notification backpressure."""
import sys

if sys.platform == "darwin":
    import asyncio
    import logging

    import CoreBluetooth as CB
    import objc
    from Foundation import NSData, NSObject
    from libdispatch import dispatch_queue_create

    from core.bluetooth_transport import RX_UUID, TX_UUID

    LOGGER = logging.getLogger("Beamer")


    class BluetoothPeripheralDelegate(NSObject, protocols=[objc.protocolNamed("CBPeripheralManagerDelegate")]):
        def peripheralManagerDidUpdateState_(self, manager):
            owner = self.owner
            if owner is None:
                return
            owner.loop.call_soon_threadsafe(owner._state, int(manager.state()))

        def peripheralManager_didAddService_error_(self, _manager, _service, error):
            owner = self.owner
            if owner is None:
                return
            owner.loop.call_soon_threadsafe(owner._service_added, str(error) if error else None)

        def peripheralManagerDidStartAdvertising_error_(self, _manager, error):
            owner = self.owner
            if owner is None:
                return
            owner.loop.call_soon_threadsafe(owner._advertised, str(error) if error else None)

        def peripheralManager_central_didSubscribeToCharacteristic_(self, _manager, central, _characteristic):
            owner = self.owner
            if owner is None:
                return
            owner.loop.call_soon_threadsafe(owner._subscribed, central)

        def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, _manager, central, _characteristic):
            owner = self.owner
            if owner is None:
                return
            if owner.central is not None and central.identifier() == owner.central.identifier():
                owner.loop.call_soon_threadsafe(owner.disconnected.set)

        def peripheralManagerIsReadyToUpdateSubscribers_(self, _manager):
            owner = self.owner
            if owner is None:
                return
            owner.loop.call_soon_threadsafe(owner.writable.set)

        def peripheralManager_didReceiveWriteRequests_(self, manager, requests):
            owner = self.owner
            if owner is None:
                return
            # The OS delivers these on our serial dispatch queue, preserving packet order.
            status = CB.CBATTErrorSuccess
            packets = []
            for request in requests:
                central = owner.central
                if central is None or request.central().identifier() != central.identifier():
                    status = CB.CBATTErrorInsufficientAuthorization
                    break
                if request.offset() != 0 or str(request.characteristic().UUID().UUIDString()).lower() != RX_UUID:
                    status = CB.CBATTErrorInvalidOffset
                    break
                data = bytes(request.value() or b"")
                if not 1 <= len(data) <= 244:
                    status = CB.CBATTErrorInvalidAttributeValueLength
                    break
                packets.append(data)
            if status == CB.CBATTErrorSuccess:
                for data in packets:
                    owner.receive(data)
            # Write Without Response has no ATT response. Rejected packets get no stream ACK.

    class MacPeripheral:
        def __init__(self, service, receive):
            self.service, self.receive = service, receive
            self.loop = asyncio.get_running_loop()
            self.disconnected = asyncio.Event()
            self.writable = asyncio.Event()
            self.started = self.loop.create_future()
            self.powered = self.loop.create_future()
            self.added = self.loop.create_future()
            self.central = None
            self.manager = None
            self.delegate = BluetoothPeripheralDelegate.alloc().init()
            self.delegate.owner = self
            self.queue = dispatch_queue_create(b"beamer.bluetooth.peripheral", None)

        def _state(self, state):
            if state == CB.CBManagerStatePoweredOn:
                if not self.powered.done():
                    self.powered.set_result(None)
            elif state in (CB.CBManagerStateUnauthorized, CB.CBManagerStateUnsupported, CB.CBManagerStatePoweredOff):
                message = {CB.CBManagerStateUnauthorized: "Allow Bluetooth for Beamer Bluetooth in System Settings > Privacy & Security > Bluetooth.",
                           CB.CBManagerStateUnsupported: "This Mac does not support Bluetooth Low Energy.",
                           CB.CBManagerStatePoweredOff: "Turn Bluetooth on in System Settings."}[state]
                if not self.powered.done():
                    self.powered.set_exception(OSError(message))
                self.disconnected.set()

        def _service_added(self, error):
            if not self.added.done():
                if error:
                    self.added.set_exception(OSError(error))
                else:
                    self.added.set_result(None)

        def _advertised(self, error):
            if not self.started.done():
                if error:
                    self.started.set_exception(OSError(error))
                else:
                    self.started.set_result(None)

        def _subscribed(self, central):
            if self.central is None:
                self.central = central
                try:
                    self.manager.setDesiredConnectionLatency_forCentral_(
                        CB.CBPeripheralManagerConnectionLatencyLow, central)
                    LOGGER.info("Mac Bluetooth low-latency connection preference requested")
                except Exception as exc:
                    LOGGER.info("Mac Bluetooth low-latency preference unavailable: %s", exc)

        @property
        def packet_size(self):
            return min(244, int(self.central.maximumUpdateValueLength())) if self.central is not None else 20

        async def start(self):
            self.manager = CB.CBPeripheralManager.alloc().initWithDelegate_queue_(self.delegate, self.queue)
            await asyncio.wait_for(self.powered, 12)
            service = CB.CBMutableService.alloc().initWithType_primary_(CB.CBUUID.UUIDWithString_(self.service), True)
            self.rx = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
                CB.CBUUID.UUIDWithString_(RX_UUID), CB.CBCharacteristicPropertyWriteWithoutResponse, None, CB.CBAttributePermissionsWriteable)
            self.tx = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
                CB.CBUUID.UUIDWithString_(TX_UUID), CB.CBCharacteristicPropertyNotify, None, CB.CBAttributePermissionsReadable)
            service.setCharacteristics_([self.rx, self.tx])
            self.manager.addService_(service)
            await asyncio.wait_for(self.added, 10)
            # Advertising the service UUID is essential: long local names can crowd it out.
            self.manager.startAdvertising_({CB.CBAdvertisementDataServiceUUIDsKey: [CB.CBUUID.UUIDWithString_(self.service)]})
            await asyncio.wait_for(self.started, 10)

        async def send(self, data):
            if self.central is None:
                raise ConnectionError("No Bluetooth subscriber")
            value = NSData.dataWithBytes_length_(data, len(data))
            while not self.disconnected.is_set():
                self.writable.clear()
                if self.manager.updateValue_forCharacteristic_onSubscribedCentrals_(value, self.tx, [self.central]):
                    return
                await asyncio.wait_for(self.writable.wait(), 2)
            raise ConnectionError("Bluetooth disconnected")

        async def stop(self):
            if self.manager is not None:
                self.manager.stopAdvertising()
                self.manager.removeAllServices()
                self.manager.setDelegate_(None)
            self.delegate.owner = None
            self.central = None
