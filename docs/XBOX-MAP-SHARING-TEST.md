# OpenCE custom-map download validation

`scripts/xbox/smoke-map-sharing.py` drives two actual native Mac HaloPad
processes through upstream's built-in `HALO_NETWORK_TEST` host/join route.
The stand-in `network-bot.py` cannot verify map downloading: it does not run
the map-sharing client or load the downloaded map.

The fixture is the user's local Custom Edition Blood Gulch (cache version
609) in the Xbox edition's `custom_maps` folder. This exercises the same
loader, transfer and resource-map route as community maps. It does not prove
every community map is compatible. The script makes independent APFS copies
(ordinary copies when cloning is unavailable), creates isolated saves, and
hashes original inputs before and after. Never publish its output directory:
it contains private game files, generated saves and local network logs.

Build the native Mac candidate first. The smoke checks the executable and
guest against `build-mac.json`, then requires its revision to match
`config/xbox-release.json`. It refuses existing output folders and busy test
ports. The default null renderer keeps windows hidden; it is real engine
networking and gameplay-state evidence, not rendering or physical-device
acceptance. Add `--render` deliberately to open the pair's windows.

Two distinct, already configured private IPv4 addresses on this Mac that
can exchange UDP in both directions are required. The engine reserves
`127.0.0.1` to mean itself. On macOS arbitrary `127.0.0.2`/`.3` addresses are
not necessarily bound. A Wi-Fi address and a USB link-local address can both
bind while the reverse UDP route fails. The smoke checks that round trip
before copying fixtures or launching either game. Check the current
interfaces rather than reusing an address from an old log. When there is
no routable local pair, use two physical devices for the transfer checks;
this script does not create interfaces or change system network settings.

```sh
python3 scripts/xbox/smoke-map-sharing.py \
  --host-address <first-local-private-address> \
  --client-address <second-local-private-address> \
  --out ref/xbox-build/map-sharing-206-<new-run-name>
```

No public lobby, online tunnel, UPnP request, clipboard invitation, update
check or voice capture is enabled. Map sharing, downloading and seeding retain their default
settings. Only the host's upload cap is raised to make the resource transfer
bounded; the interruption case lowers it to capture a partial file.

| Case | Required evidence |
| --- | --- |
| Missing map | Actual download log, identical map hash, no remaining `.part`, and two live players over multiple ticks in both processes. |
| Missing resources | Start with empty `custom_maps`; verify map and all three CE resources against originals, then the same real gameplay gate. |
| Cached rejoin | Restart both processes using the downloaded files; join with no download log and unchanged hashes. |
| Same-name collision | Existing independent CE map remains byte-identical; the host's version appears under the expected content-hash suffix and plays. |
| Interrupted transfer | Stop after at least two pieces have reached disk; no completed map exists. Restart and require an explicit resumed-pieces log, completed file hash and gameplay. |

Every case also verifies its stock map copies and unrelated save sentinel.
All processes started by the test are terminated and reaped, including when
a session raises an error. Logs and `result.json` remain for inspection.
`--case` can select a bounded case; `--timeout` changes the per-session limit.

Before release acceptance, additionally verify the actual iPhone/iPad join
UI and progress display on a preservation-backed installation, background /
resume behavior, a real internet tunnel and other-player seeding. Existing
player maps, saved profiles and campaign saves need device readback checks.
This script does not install onto or change a physical device.
