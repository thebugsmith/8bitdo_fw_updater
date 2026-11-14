# Windows and macOS support

The firmware protocol is shared across operating systems. Version 0.2 adds host-specific setup, launchers, default API platform selection, portable interface IDs and diagnostics around the native HIDAPI backends. The protocol packet reference tests still pass unchanged.

**Validation boundary:** host logic was tested with mocks on Linux. No native Windows or macOS execution, USB permission test, hotplug test or physical flash has been performed here. The CI matrix is provided but has not been run. This remains experimental on every OS.

## Windows

Use native Windows Python, preferably Python 3.12 x64, and extract the package before running it. A native Windows ARM64 Python environment would need a compatible HIDAPI wheel; none is bundled or promised. A Linux Python interpreter inside WSL is a separate environment and does not automatically have native Windows HID access.

Open PowerShell in the extracted `eightbitdo-cli` folder:

```powershell
py -3 install.py
.\eightbitdo.cmd doctor
.\eightbitdo.cmd guide --model ultimate2-wireless
.\eightbitdo.cmd devices
```

If the `py` launcher is not installed, use `python install.py` with the intended native interpreter. If you have several Python versions, select one explicitly, for example `py -3.12 install.py`.

Copy a `device_id` returned by `devices`. The ID below is a placeholder:

```powershell
.\eightbitdo.cmd info --device YOUR_DEVICE_ID
.\eightbitdo.cmd inspect "C:\Firmware Files\custom.dat"
.\eightbitdo.cmd flash --model ultimate2-wireless --device YOUR_DEVICE_ID "C:\Firmware Files\custom.dat"
```

The last command shows a plan. To transfer:

```powershell
.\eightbitdo.cmd flash --model ultimate2-wireless --device YOUR_DEVICE_ID --trace flash.jsonl --yes "C:\Firmware Files\custom.dat"
```

No PowerShell script-policy change is required. The `.cmd` launcher invokes the environment's Python directly. The same launcher also works in CMD.

For official firmware:

```powershell
.\eightbitdo.cmd versions --model ultimate2-wireless
.\eightbitdo.cmd download --model ultimate2-wireless --version 1.10 --output stock.dat
.\eightbitdo.cmd flash --model ultimate2-wireless --device YOUR_DEVICE_ID --yes stock.dat
```

### Windows access and drivers

Use the normal Windows HID driver. This CLI uses HIDAPI, not a libusb/WinUSB flashing backend. Replacing the HID driver with Zadig/WinUSB can make the required interface unavailable. Close the browser updater, 8BitDo software and other applications using that controller before opening it here.

Each Windows HID top-level collection can have a different path. The CLI keeps distinct paths separate and applies usage-page/usage filters. Long paths are retained for `--path` compatibility, but `--device` avoids copying JSON-escaped backslashes. Do not copy a JSON string's escaped representation directly into a shell path argument.

Windows can pad an output write to the interface's maximum report size and return a count larger than the submitted buffer. The transport accepts that documented behavior. It still waits for the device protocol's acknowledgement; a successful Windows write alone is not treated as delivery confirmation.

## macOS, Intel or Apple Silicon

Use a native Python matching the machine's architecture. Python 3.12 is a convenient default. The published HIDAPI 0.15.0 wheels include separate Intel and Apple Silicon builds; the installer lets pip select a compatible one. It does not require Homebrew or install system-wide libraries when a matching wheel is available.

In Terminal, change to the extracted `eightbitdo-cli` folder:

```bash
python3 install.py
sh ./eightbitdo doctor
sh ./eightbitdo guide --model ultimate2-wireless
sh ./eightbitdo devices
```

Using the reported interface ID:

```bash
sh ./eightbitdo info --device YOUR_DEVICE_ID
sh ./eightbitdo flash --model ultimate2-wireless --device YOUR_DEVICE_ID "$HOME/Downloads/custom.dat"
sh ./eightbitdo flash --model ultimate2-wireless --device YOUR_DEVICE_ID --trace flash.jsonl --yes "$HOME/Downloads/custom.dat"
```

Only the final command permits flashing. The launcher does not require activation or an executable bit when invoked with `sh`.

### macOS USB access

Unlock the Mac and approve the USB accessory if prompted, especially on an Apple Silicon laptop. Charging alone does not prove data access. A bootloader can re-enumerate as a different device, so watch for another prompt during reconnection. Close other controller/updater applications first. The CLI does not disable macOS protections, change accessory policy, install a kernel extension or forcibly seize an interface through an extra platform API.

HIDAPI supplies a native IOKit-related path, not `/dev/hidrawN`. Use `--device ID` or the exact native path displayed by `devices`. Raw path bytes are preserved when opening the interface.

### Ultimate 2 Bluetooth controller on macOS

The official product endpoint for `80ND` sets the macOS web connection path to boot mode. For this model, the CLI rejects app-mode flashing **before opening the HID interface**. It uses this offline model rule even for local custom firmware:

1. Power the controller off.
2. Hold **L1 + R1** while pressing **Home** to power on.
3. When the power LED blinks, connect the controller to the Mac by USB.
4. Run `devices` again and use the new boot interface ID.

```bash
sh ./eightbitdo guide --model ultimate2-bluetooth --boot
sh ./eightbitdo devices
sh ./eightbitdo flash --model ultimate2-bluetooth --device YOUR_BOOT_DEVICE_ID --yes custom.dat
```

This rule applies to the **Ultimate 2 Bluetooth controller**, not the similarly named Ultimate 2 Wireless controller or the Bluetooth model's separate receiver. The `guide` command obtains current instructions; the offline check reflects the verified source snapshot.

## Linux backend

On Linux the `hidapi` wheel includes both `hid` (libusb) and `hidraw` modules. The CLI prefers **`hidraw`**, so paths are `/dev/hidrawN` and the usage-page metadata can select the firmware interface. It uses that same backend to open the device. A distro build exposing hidraw through `hid` is also accepted; libusb-only enumeration gets an actionable diagnostic rather than unusable paths.

Do not choose an arbitrary `/dev/hidrawN`: `devices` labels the updater collection. `info` auto-selects when exactly one updater exists. Use `devices --json` / `info --json` for the structured form.

## Troubleshooting

| Symptom | Next check |
|---|---|
| Cannot load `hid`, a DLL, or a native extension | Run `doctor` with the same launcher; check the Python architecture and package version. Reinstall into a fresh environment for that OS if needed. |
| Installer finds no compatible wheel | Use Python 3.12 with a supported native architecture. The installer intentionally does not compile native code automatically. |
| Existing `.venv` belongs to another machine or OS | Move it aside, then run `install.py` locally. Do not copy virtualenvs between OSes. |
| No devices | Use a USB data cable, the model's connection mode, and `guide`; on macOS approve the accessory. |
| Multiple updater interfaces | Select the full `device_id` from `devices`. No interface is silently chosen. |
| Access denied / open failed | Close competing applications and reconnect. `doctor` provides the OS-specific access hint. |
| Boot reconnect timed out | Check for an accessory prompt, run `devices` again, then retry with the boot interface's new ID. |
| Python `hid` module has no `device` | Install the `hidapi` distribution, not the separate distribution named `hid`. |

The installer only installs the USB dependency into this folder's environment. Neither it nor diagnostics installs firmware. `--yes` remains required for flashing. `download`/`update` default to `firmware/MODEL-vVERSION.dat` beside the script; `--output` is optional and existing files require a separate overwrite confirmation.

## References checked during this update

- [cython-hidapi platform support](https://github.com/trezor/cython-hidapi/blob/master/README.rst)
- [HIDAPI native platform support](https://github.com/libusb/hidapi/wiki)
- [Windows write padding and return counts](https://github.com/libusb/hidapi/wiki/Known-Issues)
- [HIDAPI 0.15.0 distribution metadata](https://pypi.org/project/hidapi/0.15.0/)
- [Apple accessory access](https://support.apple.com/102282)
- 8BitDo product-query API: `POST https://support.8bitdo.com/restapi/link/v1/client/productquery`, JSON `{"product_code":"80ND"}`. Its platform mode and boot instructions were rechecked on 13 November 2026 (Asia/Kolkata).
