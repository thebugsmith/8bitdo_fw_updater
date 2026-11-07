8bitdo-fw-update is a Python toolset for updating 8BitDo controllers from a
terminal — Linux, Windows and macOS.

| script | what it does |
|---|---|
| `8bitdo-fw.py` | talks the updater's HID protocol to the controller: list, info, verify, backup, update |
| `8bitdo-download.py` | pulls official firmware `.dat` files from 8BitDo's public API |

| platform | transport | requirements |
|---|---|---|
| Linux | `/dev/hidraw*` | `python3` only |
| Windows 10/11 | hidapi | `py -m pip install hidapi` |
| macOS | hidapi | `python3 -m pip install hidapi` |

The backend is picked automatically (`/sys/class/hidraw` ⇒ hidraw, otherwise
hidapi). Force one with `8BITDO_BACKEND=hidraw` or `8BITDO_BACKEND=hidapi` —
useful when testing the Windows path on a Linux box.


Tested on:

* **8BitDo Ultimate 2C Wireless Controller**, 1.06 → 1.09
* **8BitDo Ultimate 2C Wireless Controller Adapter** (`0x301c`), 1.03 full
  erase/write rewrite, verified block-by-block

Should work on 2C wired version as well. Other controller models have different flow of updating the firmware, so it won't work on them. Downloading firmware for other models is possible regardless.

I will try to add support for other models in the future, but I don't have them to test on.

## Permissions (Linux only)

`/dev/hidraw*` is root-only by default, so a udev rule is needed exactly like
for the browser updater:

```console
$ sudo cp dt/99-8bitdo-hidraw.rules /etc/udev/rules.d/
$ sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw
```

`MODE` is the part that does the work. `TAG+="uaccess"` only attaches a
systemd-logind ACL, which exists for a logged-in local seat — over SSH or on a
headless box there is no ACL and the open still fails. The rule is per-vendor,
so it covers every 8BitDo product. Replug the controller (or run the `trigger`
above) after adding it; the script prints these instructions itself if it hits
a permission error.

## Windows / macOS

No udev rule, no admin rights, no driver swap — the pad's vendor collection is a
plain HID interface, so `hidapi` opens it directly. Do **not** run Zadig or bind
WinUSB to it; that breaks the gamepad side.

```powershell
PS> py -m pip install hidapi
PS> py .\8bitdo-fw.py list                 # backend: hidapi
PS> py .\8bitdo-fw.py info
PS> py .\8bitdo-fw.py update .\fw\Ultimate 2C_v1.09_75.dat -y
```

Two things bite on Windows:

* **Something else may hold the pad.** Steam / Steam Input, DS4Windows, x360ce
  and vendor utilities open HID devices exclusively. Quit them if `info` reports
  no device or opening fails.
* **The pad must be on the USB-C cable**, not the 2.4 GHz dongle — over the
  dongle the tool sees the adapter (`0x301c`) and would flash the dongle.

The update path resets the device and re-enumerates; on Windows that can take a
second or two longer than on Linux. `tests/test_transport.py` runs the whole
hidapi path (enumerate → pick → open → write → read → parse) against a fake HID
device, so this code is exercised without hardware — but it has not yet been
run on a real Windows host, so treat the first run as a test, keep the pad on a
cable, and use `verify` afterwards.

## Usage

```console
$ ./8bitdo-fw.py list
/dev/hidraw1  2dc8:310a  8BitDo Ultimate 2C Wireless Controller  in=0x1  out=0x1
/dev/hidraw2  2dc8:310a  8BitDo Ultimate 2C Wireless Controller  in=0x2  out=0x81

$ ./8bitdo-fw.py info
interface : /dev/hidraw2 (2dc8:310a)
usb pid   : 0x310a
device pid: 0x301b  (8BitDo Ultimate 2C Wireless Controller)
fw type   : 75
firmware  : 1.09 (raw 109)

$ ./8bitdo-fw.py update            # fetch latest for this device and flash it
$ ./8bitdo-fw.py update file.dat    # flash a specific file
$ ./8bitdo-fw.py verify file.dat    # compare a file against the device, read-only
$ ./8bitdo-fw.py backup dump.bin   # dump application flash (read-only)
```

## Downloading firmware

`firmware_type` is a per-product number; `products.json` maps every known
product/PID to one.

```console
$ ./8bitdo-download.py --list-products
$ ./8bitdo-download.py --type 75              # list builds for the Ultimate 2C Wireless
$ ./8bitdo-download.py --type 75 --latest -o fw
$ ./8bitdo-download.py --all -o fw            # every build of every product
$ ./8bitdo-download.py --scan 1-255           # probe the whole type range
```

The API it uses:

```
POST https://support.8bitdo.com/restapi/link/v1/client/firmware/select
     {"firmware_type": 75, "beta": 0}
  -> {"code":200,"data":[{file_version, fileURL, fileSize, md5,
                          created_at, readme_en, beta}, ...]}
```

Note: the `md5` field in that response is not trustworthy (it is identical
across different builds). The scripts check `fileSize` and treat md5 as a hint.
Firmware URLs point at `support.8bitdo.com/bd-uploads/files/...` and need no
auth.

## The protocol

Everything below came out of the site's JS bundle of the updater site
(`connectDevice-*.js` / `index-*.js`).

### Transport

WebHID, vendor id `0x2dc8`. The firmware interface is a 63-byte vendor
collection (for the Ultimate 2C it is usage page `0xFF7A`, report id `0x81` out,
`0x02` in). `hidfilters.json` is the site's own filter table, used to pick the
right interface per product.

In app mode the HID output report is `0x81`; in the bootloader the report id is
`0` (send the bare 63-byte payload).

```
request   [tag u8][cmd u16][params u16][len u16][crc u16][total u32][offset u32][data…]
response  [tag u8][status u16][cmd echo u16][len u16][crc u16][total u32][offset u32][data…]
```

All little-endian. `tag` is `0x05` in app mode, `0x04` in the bootloader.
`crc` is CRC-16/ARC (poly `0xA001`, init `0xFFFF`) over `data` only — it is
**required** on write packets; the device answers status `1` without it.
`status 0` means OK.

| cmd | meaning | fields |
|---|---|---|
| `0x0008` | read mapped device PID | `total=4` |
| `0x0005` | read flash | `len`/`total` = bytes, `offset` = **absolute** flash address |
| `0x00C3` | read block CRC-16 | `data={address,len,block_size}` |
| `0x0004` | erase block | `total` = length, `offset` = address |
| `0x0003` | write chunk | `data` = ≤46 bytes, `total` = firmware length, `offset` = address + position |
| `0x00C4` | save flash info | `len=8`, `data={firmware_len u32, version u32}` |
| `0x0007` | reset device | — |

### Firmware file

28-byte header, then the encrypted payload:

```
u32 version      e.g. 109 -> v1.09
u32 desAddress   0x6000
u32 desLen       74752
u32 pid          the *mapped* pid, 0x301b for the 2C Wireless
… 12 bytes zero …
payload          desLen bytes (encrypted — see 8cryptdo/)
```

More sections can follow; the updater only ever flashes section 1 for products
in its "single section" list (`gt` in the bundle), which is where the 2C
Wireless sits.

### Update sequence

The web app does it in place, in app mode, no bootloader round-trip:

1. `read pid` (`0x0008`) → mapped pid (`0x301b`), look up `firmware_type`.
2. `POST /client/firmware/select` → build list; user picks one.
3. For each 4096-byte block of the payload, at device address `0`
   (erase/write/CRC addresses are *application-relative*: address 0 lands at
   flash `0x6000`; `cmd 0x0005` reads absolute flash, so a dump starts at
   `0x6000`):
   - compute CRC-16 of the block, ask the device for its CRC (`0x00C3`);
   - equal → skip (this is why a reflash is fast and why an interrupted
     update can resume);
   - different → erase (`0x0004`), write in 46-byte chunks (`0x0003`), verify
     CRC again.
4. Save `{desLen, version}` (`0x00C4`).
5. Reset (`0x0007`); the controller re-enumerates.

The tool additionally prints per-block results and aborts on a write that does
not read back correctly.

**Receivers reboot when erased.** The `0x301c` resets ~1 s after a block is
erased (its firmware evidently checks itself), which kills the hidraw fd
mid-write. The updater therefore reconnects on I/O error and resumes at the
*current 46-byte chunk* rather than restarting the block; without that, flashing
a dongle fails on the first write packet and leaves the block half-written.

## Safety notes

* The updater never touches the bootloader region, so a failed or interrupted
  write is recoverable — re-run `update`, or use the official site.
* `verify` and `backup` are read-only; use them first on a new device.
* Flashing rewrites address 0 of the application region while the controller
  runs from it. That is exactly what the vendor's tool does, but don't unplug
  mid-write.

## Sources

* `products.json` / `hidfilters.json` extracted from the site bundle.
* `scan.txt` — `--scan 1-255` output (stopped at type 130): 103 `firmware_type`
  values actually serve builds, i.e. the set of products with downloadable
  firmware. Most modern controllers live in the 40-130 range.
