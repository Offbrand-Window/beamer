# 4 ms batching experiment

Branch: `bluetooth-4ms-test`
Version: `1.5.14-bluetooth.4ms-test`

This branch changes the Bluetooth sender's motion batching interval from 8 ms
(up to 125 updates/second) to 4 ms (up to 250 updates/second). It retains V3's
connection preferences, packet format, queue limits, and delivery checks.
The active interval is logged when each Bluetooth writer starts.

It is an experiment. The higher rate can reduce batching delay, but can also
increase queuing on a saturated connection. The actual update rate still depends
on the radio and available queue credits. No real-device improvement is claimed.

For a Windows-primary → Mac test, replace only the Windows sender and keep Mac
V3 running. Saved pairings and screen arrangement remain compatible. Compare it
with the V3 Windows executable under the same conditions, then switch back to V3
if cursor feel or tail latency worsens. End-to-end ping does not directly measure
the pointer's batching delay, so cursor feel and drag accuracy matter as well.

The stable prototype branch remains `bluetooth-transport` at V3. No upstream pull
request should be opened until the comparison determines which behavior to keep.
