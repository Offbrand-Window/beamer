"""Connection-policy requests and diagnostic behavior, without a physical radio."""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from core.bluetooth_native import WindowsConnectionPreference, prefer_low_latency


class ConnectionPolicyTests(unittest.TestCase):
    def requester(self, **extra):
        lease = SimpleNamespace(status="success", close=mock.Mock())
        requester = SimpleNamespace(request_preferred_connection_parameters=mock.Mock(return_value=lease), **extra)
        return requester, lease

    def test_request_is_retained_until_explicit_close_and_closed_once(self):
        requester, lease = self.requester()
        preferred = object()
        policy = WindowsConnectionPreference(requester, preferred)
        requester.request_preferred_connection_parameters.assert_called_once_with(preferred)
        self.assertIs(policy.request, lease)
        lease.close.assert_not_called()
        policy.close()
        policy.close()
        lease.close.assert_called_once()
        self.assertIsNone(policy.request)

    def test_diagnostics_report_negotiated_units_and_do_not_query_per_packet(self):
        getter = mock.Mock(return_value=SimpleNamespace(connection_interval=12, connection_latency=0, link_timeout=400))
        requester, _ = self.requester(get_connection_parameters=getter)
        policy = WindowsConnectionPreference(requester, object())
        with mock.patch("core.bluetooth_native.time.monotonic", return_value=0):
            policy.log_parameters()
            policy.log_parameters()
        self.assertEqual(policy.last_parameters, (15.0, 0, 4000))
        getter.assert_called_once()
        with mock.patch("core.bluetooth_native.time.monotonic", return_value=6):
            policy.log_parameters()
        self.assertEqual(getter.call_count, 2)
        policy.close()

    def test_missing_or_failing_parameter_getter_is_nonfatal(self):
        for extra in ({}, {"get_connection_parameters": mock.Mock(side_effect=OSError("unsupported"))}):
            requester, _ = self.requester(**extra)
            policy = WindowsConnectionPreference(requester, object())
            policy.log_parameters()
            policy.close()

    def test_missing_bleak_backend_does_not_prevent_connecting(self):
        with prefer_low_latency(SimpleNamespace()) as policy:
            self.assertIsNone(policy)

    @unittest.skipUnless(sys.platform == "darwin", "CoreBluetooth is macOS only")
    def test_mac_requests_low_latency_once_per_subscriber(self):
        import CoreBluetooth as CB

        from core.bluetooth_mac import MacPeripheral
        backend = MacPeripheral.__new__(MacPeripheral)
        backend.central, backend.manager = None, mock.Mock()
        central = SimpleNamespace(maximumUpdateValueLength=lambda: 244)
        backend._subscribed(central)
        backend._subscribed(central)
        backend.manager.setDesiredConnectionLatency_forCentral_.assert_called_once_with(
            CB.CBPeripheralManagerConnectionLatencyLow, central)
        self.assertIs(backend.central, central)
        self.assertEqual(backend.packet_size, 244)

    @unittest.skipUnless(sys.platform == "darwin", "CoreBluetooth is macOS only")
    def test_mac_refused_preference_does_not_abort_the_link(self):
        from core.bluetooth_mac import MacPeripheral
        backend = MacPeripheral.__new__(MacPeripheral)
        backend.central, backend.manager = None, mock.Mock()
        backend.manager.setDesiredConnectionLatency_forCentral_.side_effect = OSError("unsupported")
        central = object()
        backend._subscribed(central)
        self.assertIs(backend.central, central)
