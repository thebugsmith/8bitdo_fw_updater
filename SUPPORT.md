# Model support in this snapshot

**Tested (user-reported):** `ultimate2c-wireless` (type 75) and `ultimate2c-wireless-receiver` (type 76). These are marked `tested` in `models` and plans. Other implemented routes remain hardware untested. The automated live checks recorded here exercised type-75 enumeration, identity/version and full readback/block-CRC comparison without changing its firmware; the receiver's tested label is based on the user's report, not a new automated flash in this session.

For normal app vs boot/recovery choices, see the [connection-mode table in README.md](README.md#app-mode-or-boot-mode) or run `guide --model MODEL`. `models` deliberately stays a compact status table. `flash`/`update` detect the mode; `plan --mode` does not switch hardware.

The live product-list API returned nine controller families. Its product-detail endpoints linked five receiver models. Receiver rows are separate firmware targets even when they belong to the same controller family.

| Model slug | Official type | Firmware / protocol PID | Transfer route |
|---|---:|---|---|
| `64` | 78 | `3019` | Controller pages in app mode; contiguous boot write |
| `arcade` | 101 | `600B` | Boot transfer, version and boot metadata |
| `arcade-receiver` | 102 | `600C` | Boot transfer, version and boot metadata |
| `lite2` | 47 | `5112` | Boot transfer and version metadata |
| `pro3` | 86 | `6009` + secondary `600E` | Dual section; secondary written after main |
| `pro3-receiver` | 87 | `600A` | Receiver initialization, prefix skip, save/CRC |
| `sn30pro` | 9 | `6001`; stock container PID `0000` | Shared legacy bootloader `5750` for PID-zero stock image |
| `ultimate2-bluetooth` | 97 | `600F` + secondary `6010` | Dual section; secondary written after main |
| `ultimate2-bluetooth-receiver` | 98 | `6011` | Receiver initialization, prefix skip, save/CRC |
| `ultimate2-wireless` | 94 | `6012` | Controller pages in app mode; contiguous boot write |
| `ultimate2-wireless-receiver` | 95 | `6013` | Same packet family, separate receiver image |
| `ultimate2c-wireless` | 75 | `301B` | **Tested (user-reported)**; controller route, boot path omits save |
| `ultimate2c-wireless-receiver` | 76 | `301C` | **Tested (user-reported)**; controller packet family, separate receiver image |
| `ultimate2c-wired` | 77 | `301B` in downloaded firmware | Controller route; explicitly select wired catalog |

Additional entries from the inspected bundle:

| Model slug | Official type | PID | Status |
|---|---:|---|---|
| `sf30pro` | 9 | `6000` | Alias family using the same sampled stock firmware and legacy route |
| `ultimate2c-bluetooth` | 108 | `301A` | Explicit selector/flash route in code; absent from the live product-list response; official file inspected |
| `sn30pro-24g` | 123 | `207C` | Page-update branch implemented; API returned no firmware; no real-file validation |
| `sn30pro-24g-receiver` | 124 | `2079` | Page-update branch implemented; API returned no firmware; no real-file validation |
| `ultimate3c-plus` | 127 | `2066` | Catalog and info only; **flashing blocked**, because no dispatcher branch exists in this web build |

The bundled metadata also names many older products, Xbox controllers, keyboards and adapters that have no corresponding update dispatch in this web app. A name in that table is not evidence of working web-updater support. Those products are not advertised as flashable here.

## Identity caveats

- USB enumeration PID and firmware PID can differ. The common `05C4` boot interface is not a model identity; the CLI sends the website's identity query and checks the result before writing.
- Wired and wireless Ultimate 2C containers both use `301B` in the downloaded samples, although the web metadata labels the wired model `301D`. The actual wired USB/protocol identity has not been captured here; `301D` is absent from the website USB filters and is not accepted by this CLI. A wired device using that ID will be refused. Software cannot distinguish those variants from that shared firmware PID alone. The selected `--model` chooses the correct catalog; users must supply the correct image for custom flashing.
- SN30/SF30 stock images can carry PID zero and use a shared `5750` bootloader. Their selected family is checked, but that boot PID cannot prove which physical variant is attached.
- Regional/special editions that reuse the same ID and firmware format follow the same route; individual editions have not been tested. Firmware requiring a new format or protocol is intentionally not inferred from a product name.

The current online support list can change. `catalog --receivers` shows the live filtered list; `models` shows the implemented snapshot. These are deliberately separate concepts.
