# Beamer Bluetooth — experimental desktop fork

Based on [Toby Kalkman's Beamer](https://github.com/kalkman-code/beamer), under its
GPL-3.0-only license. The original copyright, license, and platform input adapters
are retained. This fork adds a Bluetooth Low Energy connection beneath Beamer's
encrypted protocol. It is a prototype, not an upstream release.

## What is implemented

- Windows → Mac, Mac → Windows, Windows → Windows, and Mac → Mac use the same
  transport. Either computer can supply input; radio role does not select direction.
- Mouse movement, buttons, scrolling, keyboard input, and Beamer's existing
  semantic modifier/shortcut translation.
- Beamer's existing screen map, edge crossing, and return shortcuts.
- Text clipboard in both directions, up to **16 KiB of UTF-8** per copy. Larger
  entries are skipped whole; images and files are excluded in Bluetooth mode.
- Offline setup by exchanging private setup codes. Neither a router nor internet
  access is required for a paired Bluetooth connection. Dependencies are needed
  when building from source.
- Authentication and encryption use Beamer's existing v6 protocol: random pairing
  tokens, ephemeral X25519, and ChaCha20-Poly1305. Discovery does not establish trust.
- Bluetooth is explicit per peer. A failed Bluetooth link never falls back to Wi-Fi.

macOS 13+ and Windows 10/11 are the targets. macOS has native CoreBluetooth
advertising; Windows uses native WinRT GATT APIs. Windows adapters must support
the BLE peripheral role to advertise. A Windows PC that cannot advertise can
still **Connect** to an advertising Mac or another compatible Windows PC.
Linux central connections may work through Bleak, but Linux advertising is not
implemented in this prototype. Android, iOS, and other mobile systems are not supported.

## Run it

Quit the original Beamer first. This fork has separate settings and a separate Mac
bundle identity, but two apps capturing the same mouse simultaneously are not useful.

**Mac:** use the supplied `Beamer Bluetooth.app`, or build with
`bash tools/build_bluetooth_mac.sh`. The result is
`mac_app/dist/Beamer Bluetooth.app`. The build is for the Mac's native architecture;
build on Intel for Intel Macs and Apple Silicon for Apple Silicon Macs. The local
build is ad-hoc signed, without Apple notarization. Open it through Finder's Open
action if macOS asks for approval. Grant Accessibility and Input Monitoring in
the app, relaunch, and allow Bluetooth when requested.

**Windows:** download `Beamer-Bluetooth-Windows` from a successful
[Bluetooth prototype workflow](https://github.com/Offbrand-Window/beamer/actions/workflows/bluetooth-build.yml).
Extract and run `Beamer.exe`. It is an unsigned prototype. Alternatively, install
Python 3.13, extract this repository, and double-click `Run Bluetooth Windows.cmd`.
To build the executable yourself:

```powershell
py -3.13 -m venv win_app\.venv
win_app\.venv\Scripts\python -m pip install -r win_app\requirements-win.txt
cd win_app
.venv\Scripts\python -m PyInstaller --noconfirm build.spec
```

1. Open **Bluetooth setup…** on Overview on both computers.
2. Use **Copy my code** on each computer. Transfer those codes privately to the
   other computer and paste into **Other computer's private code**. Each computer
   needs the other's code. You can transfer text through a private channel or a
   USB drive; this is a one-time setup exchange, not a network dependency.
3. Choose **Advertise** on the Mac and **Connect** on Windows. For same-platform
   pairs choose Advertise on one, Connect on the other. Save on both.
4. Allow Bluetooth access on both computers. Arrange their screens in Beamer's
   existing screen map. Enable the desired sending/receiving directions.
5. Move through the configured screen edge, or use Beamer's input-switch shortcut.
   The existing shortcut also brings input home. A disconnected link releases held input.

These codes authorize input and clipboard access. Keep them private. Remove a
computer from Beamer's machines list to revoke its pairing. To change radio roles,
open setup again, paste the other code, and save the opposite role on each side.

## Testing and limits

The automated tests exercise offline pairing, encrypted bidirectional byte
streams, fragmentation at the minimum BLE MTU, bounded buffers, invalid packet
ordering, stale connection packets, clipboard limits, and read/disconnect behavior.
They do **not** establish real-radio performance, Windows driver compatibility,
or physical PC-to-Mac input behavior. Those require testing the builds on two computers.

Bluetooth is not guaranteed to be smoother than Wi-Fi. The app uses Write Without
Response, bounded delivery windows, native notification backpressure, and movement
coalescing to avoid long queues. Actual latency depends on the adapter, connection
interval, negotiated MTU, interference, and OS scheduling.

If the connection fails, check Beamer's status and logs. "Other computer not found"
usually means mismatched codes, two Connect roles, disabled Bluetooth, or missing
Bluetooth permission. On Windows, an unsupported advertising adapter reports that
you should use Connect instead. Try both directions and test with Wi-Fi disabled
to confirm Bluetooth carries the input.

The upstream LAN mode is still available for ordinary LAN-paired peers. Its pairing
and input ports in this fork are 24821 and 24820. Neither is used to carry a
Bluetooth-paired peer's input. The app can still make ordinary update requests if
you explicitly enable its upstream update checker; it defaults off in this fork.

## Developer checks

```sh
python -m unittest core.tests.test_bluetooth -v
python -m unittest discover -s core/tests -t . -q
cd mac_app
python -m unittest discover -s tests -q
```

Settings: `~/Library/Application Support/Beamer Bluetooth/settings.json` on Mac;
`%LOCALAPPDATA%\Beamer Bluetooth\settings.json` on Windows.
Mac logs: `~/Library/Logs/Beamer Bluetooth/Beamer.log`.
Setup secrets and pairing tokens stay in the private settings file; do not upload it.
