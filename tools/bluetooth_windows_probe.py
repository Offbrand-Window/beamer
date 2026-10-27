"""Verify the installed WinRT projection without requiring a runner's Bluetooth radio."""
import sys

if sys.platform != "win32":
    raise SystemExit("Run this probe on Windows.")

from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothError
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattCharacteristicProperties,
    GattClientNotificationResult,
    GattLocalCharacteristic,
    GattLocalCharacteristicParameters,
    GattProtectionLevel,
    GattServiceProvider,
    GattServiceProviderAdvertisingParameters,
    GattSubscribedClient,
    GattWriteRequest,
    GattWriteRequestedEventArgs,
)
from winrt.windows.storage.streams import DataReader, DataWriter

required = {
    BluetoothAdapter: ("get_default_async", "is_peripheral_role_supported"),
    GattServiceProvider: ("create_async", "start_advertising", "stop_advertising", "advertisement_status"),
    GattLocalCharacteristic: ("notify_value_for_subscribed_client_async", "add_write_requested",
                              "remove_write_requested", "add_subscribed_clients_changed",
                              "remove_subscribed_clients_changed", "subscribed_clients"),
    GattSubscribedClient: ("session",),
    GattWriteRequestedEventArgs: ("get_deferral", "get_request_async", "session"),
    GattWriteRequest: ("offset", "value", "respond_with_protocol_error"),
    GattClientNotificationResult: ("status",),
}
for cls, names in required.items():
    for name in names:
        assert hasattr(cls, name), f"{cls.__name__}.{name} is missing"

params = GattLocalCharacteristicParameters()
params.characteristic_properties = GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
params.write_protection_level = GattProtectionLevel.PLAIN
advertising = GattServiceProviderAdvertisingParameters()
advertising.is_connectable, advertising.is_discoverable = True, True
assert int(BluetoothError.SUCCESS) == 0
writer = DataWriter()
writer.write_bytes(b"Bluetooth probe")
reader = DataReader.from_buffer(writer.detach_buffer())
value = bytearray(reader.unconsumed_buffer_length)
reader.read_bytes(value)
assert value == b"Bluetooth probe"
print("Native WinRT contract and buffer round trip passed. Physical radio is untested.")
