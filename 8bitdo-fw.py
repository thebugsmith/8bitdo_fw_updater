#!/usr/bin/env python3
"""
8bitdo-fw.py - firmware updater for 8BitDo controllers, from the terminal.

Reimplementation of the HID protocol used by https://web.8bitdo.com/update-firmware
(WebHID). No browser, no extra Python packages: talks straight to /dev/hidraw*.

Commands:
  list                      list connected 8BitDo HID devices
  info   [--pid PID]        read the device's firmware id + installed version
  update [FILE.dat] [--pid PID] [--dry-run] [--yes]
                            flash a firmware .dat (auto-downloads the latest if
                            FILE is omitted)
  backup [OUT.bin]          dump the raw application flash to a file

The protocol (from the site's bundle):
  request  = [tag u8][cmd u16][params u16][len u16][crc u16][total_len u32][offset u32][data...]
             tag 0x05 = app mode, 0x04 = boot mode. Payload is 63 bytes and is sent
             as HID output report 0x81 in app mode (report 0 in the bootloader).
  response = [tag u8][status u16][cmd_echo u16][len u16][crc u16][total_len u32][offset u32][data...]

  cmd 0x0008 read mapped PID           cmd 0x0005 read flash
  cmd 0x00C3 read block CRC16          cmd 0x0004 erase block
  cmd 0x0003 write flash chunk (46B)   cmd 0x00C4 save flash info
  cmd 0x0007 reset device
"""
import json
import os
import select
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_ID = 0x2DC8

# --- packet field values ------------------------------------------------------
TAG_APP = 0x05
TAG_BOOT = 0x04
CMD_READ_PID = 0x0008
CMD_READ_FLASH = 0x0005
CMD_READ_CRC = 0x00C3
CMD_ERASE = 0x0004
CMD_WRITE = 0x0003
CMD_SAVE = 0x00C4
CMD_RESET = 0x0007
CMD_READ_PID_ALT = 0x2100   # receivers/dongles answer this instead of 0x0008

# The nine products the vendor site lists, with the adapter PID where one exists.
SITE_PRODUCTS = [
    ("64 Bluetooth Controller", 0x3019, None),
    ("Arcade Controller", 0x600b, 0x600c),
    ("Lite 2 Bluetooth Gamepad", 0x5112, None),
    ("Pro 3 Bluetooth Gamepad", 0x6009, 0x600a),
    ("SN30 Pro Bluetooth Gamepad", 0x6001, None),
    ("Ultimate 2 Bluetooth Controller", 0x600f, 0x6011),
    ("Ultimate 2 Wireless Controller", 0x6012, 0x6013),
    ("Ultimate 2C Wired Controller", 0x301d, None),
    ("Ultimate 2C Wireless Controller", 0x301b, 0x301c),
]
# app-mode PIDs this tool drives end to end (vendor `wt` list minus the ones it
# also puts in the bootloader-jump list)
APP_MODE_PIDS = {12313, 12315, 24594, 24595, 12316}
TWO_SECTION_PIDS = {24585, 24591}      # vendor `te` pairs: second image at 0x40000000

# PIDs the vendor updater first switches into the bootloader (its `x` list).
# Those need a mode-jump packet plus the boot-mode interface - not implemented.
BOOT_JUMP_PIDS = {24586, 24593, 20754, 12314, 24587, 24588, 24577, 24576}

BLOCK = 4096
CHUNK = 46
HEADER_SIZE = 28


# --- crc16 (poly 0xA001 reflected, init 0xFFFF) -------------------------------
def crc16(data):
    table = (0x0000, 0xA001)
    crc = 0xFFFF
    for b in data:
        for _ in range(8):
            crc = (crc >> 1) ^ table[(crc ^ b) & 1]
            b >>= 1
    return crc


# --- product table ------------------------------------------------------------
def find_data(name):
    """Data files may sit next to the scripts or in a dt/ subdir; search both."""
    here = HERE
    up = os.path.dirname(HERE)
    for d in (here, os.path.join(here, "dt"), up, os.path.join(up, "dt")):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(here, name)


def fw_dir():
    """Firmware cache, always inside the script's own directory."""
    d = os.path.join(HERE, "fw")
    os.makedirs(d, exist_ok=True)
    return d


def load_products():
    with open(find_data("products.json")) as f:
        return {p["Pid"]: p for p in json.load(f)}


def load_filters():
    with open(find_data("hidfilters.json")) as f:
        return json.load(f)


# --- hidraw discovery ---------------------------------------------------------
def parse_report_ids(desc):
    """Return (input_report_id, output_report_id) from a report descriptor."""
    rid = 0
    rin = rout = None
    i = 0
    while i < len(desc):
        b = desc[i]
        size = (0, 1, 2, 4)[b & 3]
        tag = b & 0xFC
        if tag == 0x84:  # Report ID
            rid = desc[i + 1]
        elif tag == 0x80 and rin is None:  # Input
            rin = rid
        elif tag == 0x90 and rout is None:  # Output
            rout = rid
        i += 1 + size
    return rin, rout


def list_hidraw():
    """Yield dicts describing every hidraw node belonging to an 8BitDo device."""
    out = []
    base = "/sys/class/hidraw"
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base), key=lambda s: int(s.replace("hidraw", "") or 0)):
        uevent = os.path.join(base, name, "device", "uevent")
        try:
            with open(uevent) as f:
                props = dict(l.strip().split("=", 1) for l in f if "=" in l)
        except OSError:
            continue
        hid_id = props.get("HID_ID", "")
        try:
            bus, vid, pid = hid_id.split(":")
        except ValueError:
            continue
        if int(vid, 16) != VENDOR_ID:
            continue
        node = "/dev/" + name
        try:
            with open(os.path.join(base, name, "device", "report_descriptor"), "rb") as f:
                desc = f.read()
        except OSError:
            desc = b""
        rin, rout = parse_report_ids(desc)
        out.append(dict(node=node, pid=int(pid, 16), name=props.get("HID_NAME", ""),
                        input_id=rin, output_id=rout, desc=desc))
    return out


def top_usage(desc):
    """(usage_page, usage) of the descriptor's first application collection."""
    page = usage = None
    i = 0
    while i < len(desc):
        b = desc[i]
        size = (0, 1, 2, 4)[b & 3]
        tag = b & 0xFC
        val = int.from_bytes(desc[i + 1:i + 1 + size], "little") if size else 0
        if tag == 0x04 and page is None:       # Usage Page
            page = val
        elif tag == 0x08 and usage is None:    # Usage
            usage = val
        elif tag == 0xA0:                      # Collection
            break
        i += 1 + size
    return page, usage


def pick_device(pid=None):
    """Find the HID interface the firmware protocol runs on.

    The site's filter list is product-specific (some products expose the vendor
    collection on usage page 0xFF7A, others on the plain desktop page), so use
    the same table when it has an entry for this pid.
    """
    devices = list_hidraw()
    if not devices:
        raise SystemExit("No 8BitDo HID device found (is the controller plugged in via USB?)")
    if pid is not None:
        devices = [d for d in devices if d["pid"] == pid]
        if not devices:
            raise SystemExit(f"No 8BitDo device with product id {pid:#06x}")
    cand = [d for d in devices
            if d["input_id"] is not None and d["output_id"] is not None]
    if not cand:
        raise SystemExit("No 8BitDo interface with an output report found")
    for d in cand:
        d["usage"] = top_usage(d["desc"])
    # 1. exact match against the site's HID filter table
    filters = [f for f in load_filters() if f.get("productId") == cand[0]["pid"]]
    for f in filters:
        if "UsagePage" not in f:
            continue
        want = (f["UsagePage"], f.get("Usage"))
        for d in cand:
            if d["usage"] == want:
                return d
    # 2. the known firmware interface shape: 63-byte vendor reports
    for d in cand:
        if (d["input_id"], d["output_id"]) == (2, 0x81):
            return d
    return cand[0]


# --- transport ----------------------------------------------------------------
class Device:
    def __init__(self, path, out_id, in_id=None, timeout=3.0, usb_pid=None):
        self.path = path
        self.usb_pid = usb_pid
        self.out_id = out_id if out_id else 0
        self.timeout = timeout
        try:
            self.fd = os.open(path, os.O_RDWR)
        except PermissionError:
            raise SystemExit(
                f"{path}: permission denied.\n"
                "hidraw nodes are root-only by default, so a udev rule is needed\n"
                "(MODE matters — the uaccess tag alone only grants an ACL for a\n"
                "logged-in local seat, not for SSH/headless use):\n\n"
                '  echo \'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2dc8", '
                'MODE="0666", TAG+="uaccess"\' \\\n'
                "    | sudo tee /etc/udev/rules.d/99-8bitdo-hidraw.rules\n"
                "  sudo udevadm control --reload-rules && "
                "sudo udevadm trigger --subsystem-match=hidraw\n\n"
                "or just run this command with sudo.")
        self.log = []

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def reconnect(self, wait=8.0):
        """Re-open after a reset. Some receivers reboot mid-erase, which kills
        the old fd; the node may also come back under a different hidraw name."""
        self.close()
        end = time.time() + wait
        last = None
        while time.time() < end:
            try:
                dev = pick_device(self.usb_pid)
            except SystemExit as e:
                last = e
                time.sleep(0.15)
                continue
            try:
                self.fd = os.open(dev["node"], os.O_RDWR)
            except OSError as e:
                last = e
                time.sleep(0.15)
                continue
            self.path = dev["node"]
            return True
        raise IOError(f"device did not come back after reset ({last})")

    def _resilient(self, fn, attempts=4):
        """Run fn(), reconnecting and retrying if the device drops."""
        for i in range(attempts):
            try:
                return fn()
            except (OSError, TimeoutError):
                if i == attempts - 1:
                    raise
                self.reconnect()

    def _write(self, payload):
        data = payload if self.out_id == 0 else bytes([self.out_id]) + payload
        os.write(self.fd, data)

    def _read(self, timeout):
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                return None
            data = os.read(self.fd, 128)
            if data:
                return data

    def xfer(self, tag, cmd, params=0, data=b"", length=None, total_len=0,
             offset=0, timeout=None, verbosity=0, with_crc=True):
        """Send one packet, return (status, payload_bytes)."""
        p = bytearray(63)
        p[0] = tag
        struct.pack_into("<HHHHII", p, 1, cmd, params,
                         len(data) if length is None else length,
                         crc16(data) if (with_crc and data) else 0,
                         total_len, offset)
        p[17:17 + len(data)] = data
        self._write(bytes(p))
        raw = self._read(self.timeout if timeout is None else timeout)
        if raw is None:
            raise TimeoutError(f"no response to cmd {cmd:#06x} (offset {offset:#x})")
        body = raw[1:]  # drop report id
        if len(body) < 17:
            raise IOError(f"short response: {raw.hex()}")
        status, echo, ln, _crc, total, off = struct.unpack_from("<HHHHII", body, 1)
        if verbosity > 1:
            print(f"    [cmd {cmd:#06x} -> status {status} echo {echo:#06x} "
                  f"len {ln} total {total} off {off:#x}]")
        if status != 0:
            raise IOError(f"device returned status {status} for cmd {cmd:#06x}")
        return status, bytes(body[17:17 + max(ln, 0)]) if ln else bytes(body[17:])

    # -- protocol helpers ------------------------------------------------------
    def read_pid(self):
        _, d = self.xfer(TAG_APP, CMD_READ_PID, total_len=4)
        return struct.unpack_from("<H", d, 0)[0]

    def read_flash(self, offset, length):
        """Read `length` bytes of flash; the device answers at most 63-17 bytes."""
        out = bytearray()
        while len(out) < length:
            n = min(46, length - len(out))
            _, d = self.xfer(TAG_APP, CMD_READ_FLASH, length=n,
                             total_len=n, offset=offset + len(out))
            if not d:
                raise IOError(f"empty read at {offset + len(out):#x}")
            out += d[:n]
        return bytes(out)

    def read_version(self, offset=0x6000):
        """The site reads 16 bytes at 0x6000 and takes u16 @2 (main) and @3 (beta)."""
        d = self.read_flash(offset, 16)
        return struct.unpack_from("<H", d, 2)[0], struct.unpack_from("<H", d, 4)[0]

    def block_crc(self, address, length, blocksize=BLOCK):
        payload = struct.pack("<III", address, length, blocksize)
        def go():
            _, d = self.xfer(TAG_APP, CMD_READ_CRC, data=payload, length=12,
                             total_len=12, offset=address)
            return struct.unpack_from("<H", d, 0)[0]
        return self._resilient(go)

    def erase(self, address, length):
        self._resilient(lambda: self.xfer(TAG_APP, CMD_ERASE, total_len=length,
                                          offset=address, timeout=10.0))

    def write(self, blob, address, total_len):
        n = 0
        while n < len(blob):
            chunk = blob[n:n + CHUNK]
            # a reset mid-block resumes at the current chunk, not the block
            self._resilient(lambda c=chunk, o=address + n: self.xfer(
                TAG_APP, CMD_WRITE, data=c, total_len=total_len, offset=o,
                timeout=5.0))
            n += len(chunk)

    def save(self, length, version):
        self.xfer(TAG_APP, CMD_SAVE, data=struct.pack("<II", length, version),
                  length=8, total_len=0, offset=0)

    def reset(self):
        p = bytearray(63)
        p[0] = TAG_APP
        struct.pack_into("<HHHHII", p, 1, CMD_RESET, 0, 0, 0, 0, 0)
        self._write(bytes(p))
        self._read(1.0)


# --- firmware file ------------------------------------------------------------
def parse_dat(raw):
    """Return (first_header, section_count, payload_offset, payload_len)."""
    version, addr, length, pid = struct.unpack_from("<IIII", raw, 0)
    sections = []
    off = 0
    while off + HEADER_SIZE <= len(raw):
        v, a, l, p = struct.unpack_from("<IIII", raw, off)
        if not l or off + HEADER_SIZE + l > len(raw):
            break
        sections.append(dict(version=v, addr=a, length=l, pid=p))
        off += HEADER_SIZE + l
    if not sections:
        raise SystemExit("not a valid 8BitDo firmware file")
    return dict(version=version, addr=addr, length=length, pid=pid), sections


def fmt_version(v):
    if v >= 1000:
        s = f"{v:04d}"
        return f"{s[0]}.{s[1:3]}.{s[3]}"
    return f"{v // 100}.{v % 100:02d}"


# --- commands -----------------------------------------------------------------
def cmd_list(args):
    prods = load_products()
    devs = list_hidraw()
    if not devs:
        print("no 8BitDo HID devices found")
        return
    for d in devs:
        p = prods.get(d["pid"], {})
        print(f'{d["node"]:16} {VENDOR_ID:04x}:{d["pid"]:04x}  '
              f'{p.get("EnName", d["name"]):45} '
              f'in={d["input_id"] and hex(d["input_id"])} out='
              f'{d["output_id"] is not None and hex(d["output_id"])}')


def read_pid_or_die(d):
    try:
        return d.read_pid()
    except (TimeoutError, OSError) as e:
        hint = ""
        try:
            _, raw = d.xfer(TAG_APP, CMD_READ_PID_ALT, params=1, total_len=4,
                            timeout=2.0)
            hint = (f"\n  it answered 0x2100 instead: {raw[:16].hex()}\n"
                    "  that is the receiver/dongle PID query - the dongle update\n"
                    "  flow is not implemented here.")
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(
            "the device did not answer the app-mode PID query (0x0008).\n"
            "Either it is in bootloader mode, or it is one of the products that\n"
            "must be switched to the bootloader before flashing, or it is a\n"
            "USB dongle/adapter. None of those are supported yet - use the\n"
            "official updater for this one." + hint) from e


def refuse_if_boot_jump(usb_pid):
    if usb_pid in BOOT_JUMP_PIDS:
        p = load_products().get(usb_pid, {})
        name = p.get("EnName") or p.get("Name") or "this device"
        raise SystemExit(
            f"{name} ({usb_pid:#06x}) is flashed by the vendor tool only after a\n"
            "jump-to-bootloader command, over the boot-mode HID interface.\n"
            "That path is not implemented here, so nothing was touched.\n"
            "Use https://web.8bitdo.com/update-firmware for this device.")


def product_status(pid):
    if pid == 0x301c:
        return "supported (tested)"
    if pid in BOOT_JUMP_PIDS:
        return "needs bootloader jump"
    if pid in TWO_SECTION_PIDS:
        return "two-section image"
    if pid in APP_MODE_PIDS:
        return "supported (app-mode)"
    return "not dispatched by the site"


def cmd_products(args):
    """The nine products the vendor site lists, with firmware type and status."""
    prods = load_products()
    print(f"{'product':32} {'type':>4} {'usb pid':>8} {'adapter':>8}  status")
    print("-" * 92)
    for name, pid, adapter in SITE_PRODUCTS:
        p = prods.get(pid, {})
        ftype = p.get("type", "?")
        ad = f"{adapter:#06x}" if adapter else "-"
        print(f"{name:32} {ftype:>4} {pid:#08x} {ad:>8}  {product_status(pid)}")
        if adapter:
            print(f"{'  └ adapter':32} {prods.get(adapter, {}).get('type', '?'):>4} "
                  f"{adapter:#08x} {'-':>8}  {product_status(adapter)}")
    print("\nstatus 'supported (tested)' = flashed on hardware; 'app-mode' = same "
          "code path,\nnot exercised here; the rest need flows this tool does not "
          "implement yet.")


def cmd_info(args):
    prods = load_products()
    dev = pick_device(args.pid)
    refuse_if_boot_jump(dev["pid"])
    d = Device(dev["node"], dev["output_id"], usb_pid=dev["pid"])
    try:
        pid = read_pid_or_die(d)
        p = prods.get(pid, {})
        try:
            main, beta = d.read_version()
        except (TimeoutError, IOError):
            main = beta = None
        print(f'interface : {dev["node"]} ({VENDOR_ID:04x}:{dev["pid"]:04x})')
        print(f'usb pid   : {dev["pid"]:#06x}')
        print(f'device pid: {pid:#06x}  ({p.get("EnName", "unknown product")})')
        print(f'fw type   : {p.get("type")}')
        if main is not None:
            print(f'firmware  : {fmt_version(main)} (raw {main})'
                  + (f' beta {fmt_version(beta)}' if beta else ''))
    finally:
        d.close()


def infer_layout(pid):
    """Best guess at (flash address, length): from a matching fw/*.dat header."""
    d = fw_dir()
    best = (0, 0)
    if os.path.isdir(d):
        for f in os.listdir(d):
            if not f.endswith(".dat"):
                continue
            try:
                h, _ = parse_dat(open(os.path.join(d, f), "rb").read())
            except Exception:  # noqa: BLE001
                continue
            if h["pid"] == pid and h["length"] > best[1]:
                best = (h["addr"] or 0x6000, h["length"])
    return best


def cmd_backup(args):
    """Dump the application region. Reads use absolute flash addresses."""
    dev = pick_device(args.pid)
    d = Device(dev["node"], dev["output_id"], usb_pid=dev["pid"])
    try:
        pid = d.read_pid()
        addr, length = infer_layout(pid)
        if args.address is not None:
            addr = args.address
        if args.length is not None:
            length = args.length
        if not length:
            raise SystemExit("unknown flash layout for this device - "
                             "pass --address and --length")
        out = args.output or f"backup-{pid:04x}.bin"
        data = bytearray()
        t0 = time.time()
        last = -1
        while len(data) < length:
            n = min(46, length - len(data))
            data += d.read_flash(addr + len(data), n)
            pct = len(data) * 100 // length
            if pct != last:
                last = pct
                sys.stderr.write(f"\r  reading {pct:3d}%  ({len(data)}/{length})")
        sys.stderr.write("\n")
        with open(out, "wb") as f:
            f.write(bytes(data))
        print(f"wrote {out} ({len(data)} bytes from flash {addr:#x}) "
              f"in {time.time()-t0:.1f}s")
    finally:
        d.close()


def do_update(d, header, payload, dry_run=False, rewrite=False):
    # the 64 controller keeps its app at desAddress; everything else starts at 0
    addr = header["addr"] if header["pid"] == 12313 else 0
    length = header["length"]
    version = header["version"]
    total = length
    done = 0
    t0 = time.time()
    while done < length:
        block = payload[done:done + BLOCK]
        want = crc16(block)
        try:
            have = d.block_crc(addr, len(block))
        except (TimeoutError, IOError) as e:
            print(f"  CRC read failed at {addr:#x}: {e}")
            return False
        if want == have and not rewrite:
            print(f"  {addr:#08x}  {len(block):5d}B  crc {want:#06x}  unchanged")
            done += BLOCK
            addr += BLOCK
            continue
        why = "rewrite (forced)" if want == have else f"!= {have:#06x}"
        print(f"  {addr:#08x}  {len(block):5d}B  crc {want:#06x} {why}  erase+write",
              end="", flush=True)
        if dry_run:
            print("  (dry run)")
            done += BLOCK
            addr += BLOCK
            continue
        d.erase(addr, len(block))
        d.write(block, addr, total)
        check = d.block_crc(addr, len(block))
        ok = check == want
        print(f"  -> {check:#06x} {'OK' if ok else 'MISMATCH'}")
        if not ok:
            return False
        done += BLOCK
        addr += BLOCK
        pct = min(100, done * 100 // length)
        sys.stderr.write(f"\r  flashing {pct}%  ")
    sys.stderr.write("\r  flashed 100%% in %.1fs\n" % (time.time() - t0))
    if dry_run:
        print("  dry run: not saving")
        return True
    d.save(length, version)
    print(f"  saved flash info (length {length}, version {fmt_version(version)})")
    d.reset()
    print("  device reset")
    return True


def resolve_firmware(args, dev_pid, product):
    """Return the .dat to flash: explicit path, or the newest official build."""
    if args.file:
        return args.file
    import subprocess
    ftype = product.get("type")
    if ftype is None:
        raise SystemExit("unknown product; pass a .dat file explicitly")
    outdir = fw_dir()
    print(f"no file given - fetching the latest official build (firmware_type {ftype})")
    r = subprocess.run([sys.executable, os.path.join(HERE, "8bitdo-download.py"),
                        "--type", str(ftype), "--latest", "-o", outdir])
    if r.returncode != 0:
        raise SystemExit("firmware download failed")
    files = [os.path.join(outdir, f) for f in os.listdir(outdir) if f.endswith(".dat")]
    if not files:
        raise SystemExit("no firmware downloaded")
    return max(files, key=os.path.getmtime)


def cmd_update(args):
    prods = load_products()
    dev = pick_device(args.pid)
    refuse_if_boot_jump(dev["pid"])
    d = Device(dev["node"], dev["output_id"], usb_pid=dev["pid"])
    try:
        pid = read_pid_or_die(d)
        p = prods.get(pid, {})
        print(f'device: {p.get("EnName", "unknown")}  pid {pid:#06x}')

        path = resolve_firmware(args, pid, p)
        raw = open(path, "rb").read()
        header, sections = parse_dat(raw)
        print(f'file  : {path}')
        print(f'        v{fmt_version(header["version"])}  {len(raw)} bytes, '
              f'{len(sections)} section(s), pid {header["pid"]:#06x}')
        if header["pid"] and header["pid"] != pid:
            raise SystemExit(f'firmware is for pid {header["pid"]:#06x}, '
                             f'device reports {pid:#06x} - refusing')
        try:
            main, _ = d.read_version()
            print(f'installed: {fmt_version(main)}  ->  '
                  f'target: {fmt_version(header["version"])}')
            if main == header["version"] and not args.force:
                print("already up to date (use --force to reflash)")
                return
        except (TimeoutError, IOError):
            pass
        status = product_status(pid)
        if pid not in APP_MODE_PIDS and not args.allow_unknown:
            raise SystemExit(
                f"mapped pid {pid:#06x} -> {status}.\n"
                "This tool only writes devices on the app-mode path; anything else\n"
                "needs a vendor flow it does not implement, and a wrong guess\n"
                "writes to the wrong flash region. Nothing was touched.\n"
                "Use https://web.8bitdo.com/update-firmware, or pass\n"
                "--allow-unknown if you accept the risk on a device you can recover.")

        payload = raw[HEADER_SIZE:HEADER_SIZE + header["length"]]
        if not args.yes and not args.dry_run:
            if input("proceed with flashing? [y/N] ").strip().lower() not in ("y", "yes"):
                return
        ok = do_update(d, header, payload, dry_run=args.dry_run,
                       rewrite=args.rewrite)
        print("done" if ok else "FAILED - re-run, or use the official updater")
        sys.exit(0 if ok else 1)
    finally:
        d.close()


def cmd_verify(args):
    """Compare a .dat against the device flash without writing anything."""
    dev = pick_device(args.pid)
    d = Device(dev["node"], dev["output_id"], usb_pid=dev["pid"])
    try:
        pid = d.read_pid()
        header, _ = parse_dat(open(args.file, "rb").read())
        raw = open(args.file, "rb").read()
        payload = raw[HEADER_SIZE:HEADER_SIZE + header["length"]]
        bad = 0
        addr = 0
        done = 0
        while done < header["length"]:
            block = payload[done:done + BLOCK]
            want = crc16(block)
            have = d.block_crc(addr, len(block))
            flag = "ok  " if want == have else "DIFF"
            bad += want != have
            print(f"  {addr:#08x}  {len(block):5d}B  file {want:#06x}  "
                  f"device {have:#06x}  {flag}")
            done += BLOCK
            addr += BLOCK
        print(f'{args.file}: {"identical to device flash" if not bad else f"{bad} block(s) differ"}')
        sys.exit(0 if not bad else 1)
    finally:
        d.close()


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="8BitDo firmware updater (HID, no browser)")
    ap.add_argument("--pid", type=lambda s: int(s, 0), help="select device by USB pid")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("products")
    sub.add_parser("info")
    b = sub.add_parser("backup")
    b.add_argument("output", nargs="?")
    b.add_argument("--length", type=lambda s: int(s, 0),
                   help="bytes to dump (default: from a matching fw/*.dat)")
    b.add_argument("--address", type=lambda s: int(s, 0),
                   help="flash address to start at (default 0x6000)")
    u = sub.add_parser("update")
    u.add_argument("file", nargs="?")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("-y", "--yes", action="store_true")
    u.add_argument("-f", "--force", action="store_true",
                   help="reflash even if the version matches")
    u.add_argument("--allow-unknown", action="store_true",
                   help="write even if this PID is outside the implemented path")
    u.add_argument("--rewrite", action="store_true",
                   help="rewrite every block even when its CRC already matches")
    v = sub.add_parser("verify")
    v.add_argument("file")
    args = ap.parse_args(argv)
    {"list": cmd_list, "products": cmd_products, "info": cmd_info, "backup": cmd_backup,
     "update": cmd_update, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    main(sys.argv[1:])
