# 8BitDo firmware CLI

An independent Python CLI for the firmware-update workflows at <https://web.8bitdo.com/>. 

List official versions, download firmware, and update controllers or receivers using official or compatible custom `.dat` files. Not affiliated with 8BitDo.

**Tested models:** `ultimate2c-wireless` (type 75) and `ultimate2c-wireless-receiver` (type 76) on linux. Other implemented models remain untested; catalog-only models cannot be flashed. See [SUPPORT.md](SUPPORT.md) for model-specific limitations.

If you want to try out the this tool on untested models, please report your results to the issue tracker. 

## Install

Python 3.10+ is required. Extract or clone this folder.
```
git clone https://github.com/thebugsmith/8bitdo_fw_updater.git
cd 8bitdo_fw_updater
```

| OS | Install | Run |
|---|---|---|
| Linux / macOS | `python3 install.py` | `sh ./eightbitdo models` |
| Windows | `py -3 install.py` | `.\eightbitdo.cmd models` |

The installer creates a local `.venv` and installs `hidapi`. It does not change system Python, USB drivers, or permissions. This is a firmware-only tool: the gamepad tester and its dependencies have been removed.

Alternatively, activate your own virtualenv and run:

```sh
python -m pip install -r requirements.txt
python eightbitdo.py --help
```

[PLATFORMS.md](PLATFORMS.md) covers Windows/macOS setup and troubleshooting.

### Linux permissions

For the active local desktop user:

```sh
python eightbitdo.py udev | sudo tee /etc/udev/rules.d/70-eightbitdo.rules >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
```

Reconnect the USB device. SSH/headless sessions need an administrator-managed group rule and group membership; the `uaccess` desktop rule alone does not grant headless access. Do not run the entire updater as root merely to work around permissions.

## Discover devices and models

```sh
python eightbitdo.py models
python eightbitdo.py devices
python eightbitdo.py info
python eightbitdo.py doctor
```

`models` is a compact table: model slug, firmware type, PID, and `tested` / `untested` / `unsupported`. Connection instructions live in `guide`, not under every table row.

`devices` shows native HID paths and identifies the updater interface. On Linux these are real `/dev/hidrawN` paths, using HIDAPI's hidraw backend rather than libusb's opaque paths. Keyboard/gamepad collections belonging to the same controller are labelled as non-updater interfaces.

`info` automatically selects the updater if exactly one exists. Select an interface explicitly when needed:

```sh
python eightbitdo.py info --path /dev/hidraw2
python eightbitdo.py info --device YOUR_DEVICE_ID
```

Paths and device IDs can change after reconnection. Use the values from `devices`; `/dev/hidraw2` is only an example. `devices --json` and `info --json` provide structured output. Neither `devices` nor `doctor` opens hardware; `info` sends read-only identity/version queries.

equence.

## Local/custom firmware

```sh
python eightbitdo.py inspect custom.dat
python eightbitdo.py plan --model ultimate2c-wireless custom.dat
python eightbitdo.py flash --model ultimate2c-wireless custom.dat
python eightbitdo.py flash --model ultimate2c-wireless --yes custom.dat
```

Only the final command permits hardware writes. `flash` works offline and accepts a model-compatible `.dat` container; it does not require official hashes or an official version. A raw `.bin` cannot simply be renamed to `.dat`. Container and target checks do not prove patched machine code will boot.

`extract custom.dat --directory extracted` copies section payloads without decrypting or modifying them. `--trace flash.jsonl` on flash/update records raw USB traffic, including firmware bytes and potentially device identifiers. Do not publish traces without reviewing them.

The updater stops on invalid acknowledgements and does not blindly replay erase/write commands. A successful transfer still needs a reconnect, `info`, and a physical controls check. Keep a known-good stock image and the correct recovery instructions available. Never disconnect during flashing.

## Official firmware

```sh
python eightbitdo.py versions --model ultimate2c-wireless
python eightbitdo.py versions --model ultimate2c-wireless --beta
python eightbitdo.py download --model ultimate2c-wireless --version 1.09
python eightbitdo.py update --model ultimate2c-wireless
```
It will be downloaded to the firmware as:

```text
firmware/ultimate2c-wireless-v109.dat
firmware/ultimate2-wireless-v110.dat
```

The name uses the model slug and the selected version with dots removed. `--output` overrides the entire destination; a relative override is relative to your current working directory:

```sh
python eightbitdo.py download --model ultimate2c-wireless --output my-firmware.dat
python eightbitdo.py update --model ultimate2c-wireless --output /path/to/stock.dat
```

`update` without `--yes` downloads and displays the plan for the connected device; it does not open the USB interface. To actually install:

```sh
python eightbitdo.py update --model ultimate2c-wireless --yes
```

Omitting `--version` selects the first eligible official release. Use `--beta` to include beta entries. If a version is ambiguous, choose the exact `--key` from `versions`. An older available version permits a downgrade, subject to device restrictions. Controller and receiver files are different: use the `-receiver` model when updating a receiver.

### Catalog and network behaviour

```sh
python eightbitdo.py catalog
python eightbitdo.py catalog --receivers
```

The second command needs one detail request per product family. It deduplicates families and runs up to four requests concurrently, with immediate progress on **stderr**; stdout remains valid JSON. It fetches live data rather than serving a stale cache. Slow servers or retries can still delay completion.

API and file requests send browser-style User-Agent, Accept, Accept-Language, Origin and Referer headers. TCP connection attempts and TLS handshakes use a five-second maximum timeout; reads use twenty seconds, with up to two retries for transient failures. An unresponsive Cloudflare address was observed delaying sequential connections; the separate connect timeout limits that wait without pinning an IP or disabling TLS.

**Download hash checks are disabled.** Stale catalog hashes are ignored, without mismatch warnings. `--strict-md5` and `--sha256` are not options. Printed local SHA-256 values are informational only. HTTPS verification, file-size limits, container structure, model/PID and destination checks remain, as do the USB protocol's CRCs and device-side firmware checksums.

## App mode versus boot mode

- **app**: normal controller firmware over a USB cable, Bluetooth mode, or XInput/DInput selection.
- **boot**: bootloader/recovery firmware-update interface.
- `flash` and `update` detect the connected mode. Required boot transitions happen only after `--yes`.
- `plan --mode auto` chooses the model/host default without opening USB. `--mode app` or `--mode boot` changes only the offline plan; it does not switch hardware.

| Family | Normal connection |
|---|---|
| Ultimate 2C Wireless and its receiver | **app**; connect the controller directly by USB to update the controller. |
| Ultimate 2 Wireless and receiver; 64 controller | **app**; boot is for an already-entered recovery/update interface. |
| Pro 3 controller | **app**; both firmware sections are handled. |
| Ultimate 2 Bluetooth controller | **app** on Linux/Windows; enter **boot first on macOS**. |
| Lite 2, Ultimate 2C Bluetooth, Arcade controller/receiver, Pro 3 receiver, Ultimate 2 Bluetooth receiver | Start in app where available; the updater switches to boot for transfer. |
| SN30/SF30 Pro | Boot transfer; PID-zero stock images require the legacy `5750` bootloader. |
| Ultimate 2C Wired | Route implemented, real USB identity unverified. Metadata `301D` is not an accepted USB filter; sampled firmware uses `301B`. |

```sh
python eightbitdo.py guide --model ultimate2c-wireless
python eightbitdo.py guide --model ultimate2c-wireless --boot
```

Guides use the host platform automatically; override with `--platform linux`, `windows`, or `macos`. Recovery button combinations are model-specific. If the API has no recovery instructions, consult the model's manual-do not borrow a different controller's s

## Tests and publication

```sh
python -m unittest discover -s tests -v
```

Tests include 22 complete packet sequences compared with the inspected website routines, malformed firmware, target checks, failure handling, platform adapters, catalog concurrency, download naming and overwrite prompts. The two tested model labels are based on user reports. Automated live checks exercised the type-75 controller's identity/version and readback; they did not perform erase/write operations. Other models and native Windows/macOS hardware paths remain unverified here.

The CI configuration is provided in `.github/workflows/tests.yml`; local success is not a claim that GitHub Actions has run. See [GITHUB_FILES.md](GITHUB_FILES.md) for the exact publication file list and exclusions. No official firmware or vendor JavaScript bundles should be committed.

[PROTOCOL.md](PROTOCOL.md), [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json), and [REA_ANALYSIS.json](REA_ANALYSIS.json) retain protocol/source provenance. These are developer references, not runtime dependencies.

## Licence and provenance

The scripts and documentation here are MIT. The .dat images are byte-patches of 8BitDo's own firmware and therefore embed their code. They are shared for personal, interoperability use only; 8BitDo's firmware remains 8BitDo's. No affiliation with 8BitDo. Flash at your own risk: a failed flash is recoverable via the LB+RB bootloader, but a misapplied patch may brick the device.