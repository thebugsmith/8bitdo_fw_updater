#!/usr/bin/env python3
"""Independent, experimental CLI for web.8bitdo.com's firmware updater.

Protocol evidence: web build index-BXAl9J5z / connectDevice-CqNxlsyD.
Tested model status is user-reported; other routes remain experimental. See README.md.
Python 3.10+; hidapi is needed only for USB.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import dataclasses
import hashlib
import http.client
import importlib.metadata
import json
import math
import os
import platform
import re
from pathlib import Path
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

VERSION = "0.2.2-experimental"
API = "https://support.8bitdo.com/restapi/link/v1"
VID = 0x2DC8
MAX_FILE = 16 * 1024 * 1024
FILE_HEADER = struct.Struct("<7I")
WIRE_HEADER = struct.Struct("<4H2I")
PAGE = 4096
CHUNK = 46
ACK = 0x0000
WRITE_PLAIN, WRITE, ERASE, READ, RESET = 2, 3, 4, 5, 7
READ_PID, VERSION_CMD, INFO, INIT, SAVE4K, CRC, SAVE = 8, 32, 192, 193, 194, 195, 196
VERSION_ADDRESS = 0x01017040
BOOT_FLAG_ADDRESS = 0x010170C0
BOOT_FLAG = 0x20241220
APP_ADDRESS = 0x01018000
SECONDARY_ADDRESS = 0x40000000


class Error(Exception):
    """An actionable error, safe to print without a traceback."""


class ProtocolError(Error):
    pass


class ReplyTimeout(ProtocolError):
    pass


class DeviceOpenError(ProtocolError):
    pass


def host_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise Error(f"Unsupported host platform: {sys.platform}. Use native Windows, macOS or Linux Python")


def usb_access_hint() -> str:
    host = host_platform()
    common = "Close the browser updater, 8BitDo software and other apps using this interface. "
    if host == "windows":
        return common + "Use native Windows Python and the Windows HID driver. Reconnect by USB; do not replace the HID driver with WinUSB/Zadig."
    if host == "macos":
        return common + "Unlock the Mac and allow the USB accessory if macOS asks. Reconnect and follow the model's macOS connection guide."
    return common + ("Check the hidraw permissions using the udev instructions, then unplug and reconnect the device. "
                     "The uaccess rule covers a local desktop seat; SSH/headless needs an administrator-managed "
                     "group rule and membership in that group.")


def configure_stdio():
    # In particular, redirected output on Windows must not fail on a device's
    # Unicode name. StringIO/test streams may not implement reconfigure().
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, "reconfigure", None)
        if configure:
            try:
                configure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


@dataclasses.dataclass(frozen=True)
class Model:
    slug: str
    name: str
    firmware_type: int
    pid: int
    product_code: str
    route: str
    secondary_pid: int = 0
    selectable: bool = True


# Each entry is an explicit routed model, not the much broader metadata table
# in the web bundle. 301B is also the observed firmware PID for the wired 2C.
MODEL_LIST = (
    Model("64", "64 Bluetooth Controller", 78, 0x3019, "80NE", "controller"),
    Model("arcade", "Arcade Controller", 101, 0x600B, "80FG", "receiver"),
    Model("arcade-receiver", "Arcade Controller Receiver", 102, 0x600C, "80FG", "receiver"),
    Model("lite2", "Lite 2 Bluetooth Gamepad", 47, 0x5112, "80KA", "receiver"),
    Model("pro3", "Pro 3 Bluetooth Gamepad", 86, 0x6009, "80GQ", "dual", 0x600E),
    Model("pro3-receiver", "Pro 3 Receiver", 87, 0x600A, "80GQ", "receiver"),
    Model("sn30pro", "SN30 Pro Bluetooth Gamepad", 9, 0x6001, "80DA", "sn30"),
    Model("sf30pro", "SF30 Pro Bluetooth Gamepad", 9, 0x6000, "80DA", "sn30"),
    Model("ultimate2-bluetooth", "Ultimate 2 Bluetooth Controller", 97, 0x600F, "80ND", "dual", 0x6010),
    Model("ultimate2-bluetooth-receiver", "Ultimate 2 Bluetooth Receiver", 98, 0x6011, "80ND", "receiver"),
    Model("ultimate2-wireless", "Ultimate 2 Wireless Controller", 94, 0x6012, "81HE", "controller"),
    Model("ultimate2-wireless-receiver", "Ultimate 2 Wireless Receiver", 95, 0x6013, "81HE", "controller"),
    Model("ultimate2c-wireless", "Ultimate 2C Wireless Controller", 75, 0x301B, "81HD", "controller"),
    Model("ultimate2c-wireless-receiver", "Ultimate 2C Wireless Receiver", 76, 0x301C, "81HD", "controller"),
    Model("ultimate2c-wired", "Ultimate 2C Wired Controller", 77, 0x301B, "82CD", "controller"),
    Model("ultimate2c-bluetooth", "Ultimate 2C Bluetooth Gamepad", 108, 0x301A, "80NC", "receiver", selectable=False),
    Model("sn30pro-24g", "SN30 Pro 2.4G (bundle-only)", 123, 0x207C, "81DA", "pages", selectable=False),
    Model("sn30pro-24g-receiver", "SN30 Pro 2.4G Receiver (bundle-only)", 124, 0x2079, "81DA", "pages", selectable=False),
    Model("ultimate3c-plus", "Ultimate 3C+ (catalog only; no update dispatch)", 127, 0x2066, "81DA", "unsupported", selectable=False),
)
MODELS = {m.slug: m for m in MODEL_LIST}
# User-reported hardware testing; this session's checks are read-only.
TESTED_MODELS = frozenset({"ultimate2c-wireless", "ultimate2c-wireless-receiver"})
# (USB PID, usage page, usage); None means the site's unnumbered boot mode.
USB_FILTERS = (
    (0x05C4, None, None), (0x301A, 1, 5), (0x301C, 0xFFA0, 1),
    (0x3109, 0x008C, 1), (0x310A, 0xFF7A, 1),
    (0x310B, 0xFF7A, 1), (0x310B, 0xFF7B, 1),
    (0x3208, None, None), (0x5112, 1, 5), (0x5750, None, None),
    (0x6009, 1, 5), (0x600F, 1, 5), (0x6013, 0xFFA0, 1),
    (0x600A, 1, 5), (0x3019, 1, 5), (0x6001, 1, 5), (0x6000, 1, 5),
    (0x207B, None, None), (0x2078, None, None),
    (0x207C, 0xFFA0, 1), (0x207A, 0xFFA0, 1), (0x2079, 0xFFA0, 1),
    (0x2066, 0xFFA0, 1),
)
DIRECT_PID = {0x5112, 0x301A, 0x6009, 0x3019, 0x6001, 0x6000}
JUMP_5100 = {0x5112, 0x301A, 0x6001, 0x6000}
JUMP_0050 = {0x600B, 0x600C, 0x3019}
INIT_RECEIVERS = {0x600A, 0x6011}
OLD_VERSION = {0x5112, 0x3109, 0x600C, 0x310B, 0x6001, 0x6000}
SKIP_SAVE_BOOT = {0x301B, 0x301A, 0x301D, 0x207C, 0x2079}
SERIAL_TABLE = (
    409564901, 3402005197, 955416558, 172574961, 3076224851, 529529436,
    3220754243, 3297993063, 2461860848, 1433650925, 1473807451, 2188482476,
    3105423817, 957869566, 3168154703, 3483247541,
)


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS (poly A001, init FFFF), used for wire payloads."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def word_sum(data: bytes) -> int:
    if len(data) % 4:
        raise Error("Firmware checksum needs a whole number of 32-bit words")
    return sum(v[0] for v in struct.iter_unpack("<I", data)) & 0xFFFFFFFF


def take_u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise ProtocolError(f"Short response: need uint16 at {offset}, got {len(data)} bytes")
    return struct.unpack_from("<H", data, offset)[0]


def take_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ProtocolError(f"Short response: need uint32 at {offset}, got {len(data)} bytes")
    return struct.unpack_from("<I", data, offset)[0]


@dataclasses.dataclass(frozen=True)
class Segment:
    version: int
    address: int
    size: int
    pid: int
    reserved1: int
    revision: int
    reserved2: int
    offset: int
    payload: bytes = dataclasses.field(repr=False)

    def describe(self) -> dict:
        return {"version_raw": self.version, "version": f"{self.version / 100:.2f}",
                "pid": f"0x{self.pid:04x}", "address": f"0x{self.address:08x}",
                "size": self.size, "header_offset": self.offset,
                "reserved1": f"0x{self.reserved1:08x}", "revision": self.revision,
                "reserved2": self.reserved2,
                "payload_sha256": hashlib.sha256(self.payload).hexdigest()}


@dataclasses.dataclass(frozen=True)
class Firmware:
    data: bytes = dataclasses.field(repr=False)
    segments: tuple[Segment, ...]

    @classmethod
    def parse(cls, data: bytes) -> Firmware:
        if not 28 < len(data) <= MAX_FILE:
            raise Error(f"Firmware must be 29..{MAX_FILE} bytes")
        parts = []
        pos = 0
        while pos < len(data):
            if len(parts) == 2:
                raise Error("More than two firmware sections; format not verified")
            if pos + FILE_HEADER.size > len(data):
                raise Error(f"Truncated header / unexplained trailing bytes at {pos}")
            fields = FILE_HEADER.unpack_from(data, pos)
            version, address, size, pid, _, _, _ = fields
            end = pos + FILE_HEADER.size + size
            if size == 0 or end > len(data):
                raise Error(f"Section at {pos} has invalid/truncated length {size}")
            if address + size > 0x100000000 or pid > 0xFFFF:
                raise Error("Firmware address or PID is out of range")
            parts.append(Segment(*fields, pos, data[pos + FILE_HEADER.size:end]))
            pos = end
        return cls(data, tuple(parts))

    @classmethod
    def read(cls, path: str | Path) -> Firmware:
        path = Path(path)
        if path.stat().st_size > MAX_FILE:
            raise Error("Firmware file exceeds size limit")
        return cls.parse(path.read_bytes())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def main(self) -> Segment:
        return self.segments[-1]

    def validate(self, model: Model) -> None:
        if model.route == "unsupported":
            raise Error(f"{model.name}: no flash dispatch exists in the inspected web build")
        main = self.main
        if model.route == "dual":
            if len(self.segments) != 2 or self.segments[0].pid != model.secondary_pid:
                raise Error("This model requires both sections in their original order")
            if main.pid != model.pid:
                raise Error("Main-section PID does not match selected model")
            if main.address != APP_ADDRESS or self.segments[0].address != SECONDARY_ADDRESS + 0x4000:
                raise Error("Dual-section destination addresses differ from verified format")
        else:
            if len(self.segments) != 1:
                raise Error("Selected model requires a single firmware section")
            allowed = {model.pid}
            if model.route == "sn30":
                allowed = {0, 0x6000, 0x6001}
            if main.pid not in allowed:
                raise Error(f"Firmware PID 0x{main.pid:04x} != model PID 0x{model.pid:04x}")
        # Device bootloader interpretation, not an arbitrary write-address interface.
        expected_addresses = {
            0x301B: {0x6000}, 0x301C: {0x6000},
            0x6012: {0x5000}, 0x6013: {0x5000}, 0x3019: {0x01078000},
            0x5112: {APP_ADDRESS}, 0x301A: {APP_ADDRESS},
            0x600B: {APP_ADDRESS}, 0x600C: {APP_ADDRESS},
            0x600A: {0x3000}, 0x6011: {0x3000},
        }
        if model.pid in expected_addresses and main.address not in expected_addresses[model.pid]:
            raise Error("Destination address differs from the verified model format")
        if model.route == "sn30" and main.address != 0x08003400:
            raise Error("SN30/SF30 firmware address differs from verified legacy format")
        if model.pid == 0x600C:
            # boot header uses the opaque checksum from the supplied firmware.
            # It is not necessarily a simple additive or host-CRC value.
            if main.size < 4:
                raise Error("Arcade receiver image is too short")

    def describe(self) -> dict:
        return {"size": len(self.data), "sha256": self.sha256,
                "segments": [s.describe() for s in self.segments]}


def packet(command: int, *, params: int = 0, data: bytes = b"", length: int | None = None,
           total: int = 0, offset: int = 0, channel: int = 5) -> bytes:
    if len(data) > CHUNK:
        raise Error("Payload exceeds 46 bytes")
    if length is None:
        length = len(data)
    if not all(0 <= v <= 0xFFFF for v in (command, params, length)):
        raise Error("16-bit packet field out of range")
    if not all(0 <= v <= 0xFFFFFFFF for v in (total, offset)) or not 0 <= channel < 256:
        raise Error("Packet field out of range")
    head = WIRE_HEADER.pack(command, params, length, crc16(data) if data else 0, total, offset)
    return (bytes([channel]) + head + data).ljust(63, b"\0")


@dataclasses.dataclass(frozen=True)
class Reply:
    raw: bytes
    command: int
    params: int
    length: int
    crc: int
    total: int
    offset: int
    body: bytes


def decode_reply(raw: bytes, mode: str) -> Reply:
    off = 1 if mode == "app" else 2
    if len(raw) < off + 16:
        raise ProtocolError(f"Truncated {mode} response ({len(raw)} bytes)")
    fields = WIRE_HEADER.unpack_from(raw, off)
    return Reply(raw, *fields, raw[off + 16:])


def legacy_packet(command: int, data: bytes | None = None, *, family: int = 22) -> bytes:
    if data is not None and len(data) > 55:
        raise Error("Legacy payload too large")
    command_length = 1 if data is None else len(data) + 3
    buf = bytearray(63)
    buf[0:6] = bytes([command_length + 4, 0, family, command_length & 255,
                      command_length >> 8, command])
    if data is not None:
        struct.pack_into("<H", buf, 6, len(data))
        buf[8:8 + len(data)] = data
    return bytes(buf)


def path_text(value) -> str:
    return os.fsdecode(value)


def import_hid():
    try:
        if sys.platform.startswith("linux"):
            try:
                # Linux wheels ship both backends. `hid` uses libusb, whose
                # opaque USB paths and zero usage fields break our filters.
                # Use the same native hidraw nodes as 8bitdo-fw.py, without
                # detaching a kernel driver or probing unrelated interfaces.
                import hidraw as hid
            except ImportError:
                # Some distro builds expose the hidraw backend as `hid`.
                import hid
        else:
            import hid
    except (ImportError, OSError) as exc:
        raise Error("Cannot load HIDAPI. Run install.py using native Python for your OS/CPU, "
                    f"or install 'hidapi' in the active virtualenv. Loader detail: {exc}") from exc
    if not hasattr(hid, "device") or not hasattr(hid, "enumerate"):
        raise Error(f"Loaded module {getattr(hid, '__name__', 'hid')!r} lacks device/enumerate. "
                    "Install the 'hidapi' distribution; remove conflicting same-named modules/packages.")
    return hid


def device_path_bytes(info: dict) -> bytes:
    """Use the exact bytes returned by HIDAPI, including opaque platform paths."""
    if "path_hex" in info:
        return bytes.fromhex(info["path_hex"])
    return os.fsencode(info["path"])


def device_id(info: dict) -> str:
    return hashlib.sha256(device_path_bytes(info)).hexdigest()[:12]


def usb_mode(info: dict) -> str | None:
    if info.get("vendor_id") != VID or info.get("bus_type") == 2:
        return None  # Never update over Bluetooth.
    for pid, page, usage in USB_FILTERS:
        if info.get("product_id") == pid:
            if page is None:
                return "boot"
            if info.get("usage_page") == page and info.get("usage") == usage:
                return "app"
    return None


def enumerate_devices() -> list[dict]:
    result = {}
    try:
        entries = import_hid().enumerate(VID, 0)
    except OSError as exc:
        raise Error(f"HID enumeration failed: {exc}. {usb_access_hint()}") from exc
    for item in entries:
        mode = usb_mode(item)
        native_path = os.fsencode(item["path"])
        if (sys.platform.startswith("linux") and not native_path.startswith(b"/dev/")
                and item.get("usage_page") == 0 and item.get("usage") == 0):
            raise Error("Linux HIDAPI selected the libusb backend with no usage metadata. "
                        "Install a hidraw-enabled 'hidapi' wheel in this environment "
                        "(python -m pip install --upgrade --force-reinstall -r requirements.txt); "
                        "devices must show /dev/hidraw paths. No interfaces were opened.")
        entry = {**item, "mode": mode, "path": path_text(native_path),
                 "path_hex": native_path.hex(), "device_id": device_id(item)}
        # Some backends enumerate several collections for the same native path.
        # Keep the matching collection; distinct Windows TLC paths stay distinct.
        old = result.get(native_path)
        if old is None or (old["mode"] is None and mode is not None):
            result[native_path] = entry
    return list(result.values())


def select_device(devices: list[dict], path: str | None = None, identifier: str | None = None) -> dict:
    if path is not None and identifier is not None:
        raise Error("Select either --device or --path, not both")
    matching = [d for d in devices if d["mode"] and (path is None or d["path"] == path)
                and (identifier is None or device_id(d) == identifier.lower())]
    if len(matching) != 1:
        if not matching:
            available = ", ".join(str(d["path"]) for d in devices if d["mode"]) or "none"
            if path is not None and any(d["path"] == path for d in devices):
                raise Error(f"{path} is not an updater interface; do not use the keyboard/gamepad collection. "
                            f"Available updater paths: {available}")
            raise Error(f"No matching USB HID interface. Available updater paths: {available}. "
                        "Run devices; follow guide --model MODEL and connect by USB data cable")
        raise Error("Multiple updater interfaces found. Select --device ID or --path from devices")
    return matching[0]


def mode_description(mode: str | None) -> str:
    return {"app": "app (normal USB firmware; not a phone/desktop app)",
            "boot": "boot (bootloader/recovery firmware-update interface)"}.get(
                mode, "not an updater interface (gamepad/keyboard or unsupported collection)")


def info_models(pid: int, device: dict) -> list[Model]:
    candidates = [m for m in MODEL_LIST if m.pid == pid]
    # Both 2C firmware containers carry 301B. A protocol PID alone cannot
    # identify the catalog. Use the exact USB product name only as a display
    # hint; never use it instead of check_identity() to authorize a flash.
    if len(candidates) > 1:
        name = (device.get("product_string") or "").removeprefix("8BitDo ").casefold()
        named = [m for m in candidates if m.name.casefold() == name]
        if len(named) == 1:
            return named
    return candidates


def connection_guidance(model: Model) -> str:
    if model.route == "unsupported":
        return "Identification/catalog only; no firmware update mode is implemented."
    if model.slug == "ultimate2c-wired":
        return ("Connect the wired controller directly to USB. Normal app-mode transfer is implemented, "
                "but this variant's real USB identity is unverified. The catalog file uses 0x301b; "
                "metadata also lists 0x301d, which is not an accepted updater USB PID. "
                "If devices has no matching interface, stop; do not substitute the wireless catalog.")
    connection = ("Turn the controller off; connect the receiver directly to USB. "
                  if "receiver" in model.slug else "Connect a USB data cable directly to the controller, not through its receiver. ")
    if model.slug == "ultimate2-bluetooth":
        return connection + ("Linux/Windows: start in app mode. macOS: enter boot mode first "
                             "(power off, hold L1 + R1 and press Home, then connect USB).")
    if model.route == "sn30":
        return connection + ("Normal app connection is detected; flashing automatically switches to boot. "
                             "PID-zero stock files need the legacy 0x5750 bootloader. See guide --boot for manual entry.")
    if model.route == "receiver":
        return connection + ("Start in app mode; flashing automatically switches to boot. "
                             "If already in boot mode, use that interface. See guide --boot for recovery.")
    return connection + ("Use app mode for normal updates. Boot mode is for an already-entered recovery/update interface; "
                         "see guide --boot for model-specific entry instructions.")


def validate_host_mode(model: Model, mode: str):
    # Public productquery snapshot for 80ND: softtype 7 => soft_isboot 1.
    # Keep this offline so local custom firmware never needs a network request.
    if host_platform() == "macos" and model.slug == "ultimate2-bluetooth" and mode != "boot":
        raise Error("Ultimate 2 Bluetooth requires boot mode for the documented macOS path. "
                    "Power off; hold L1 + R1 while pressing Home, then connect USB when the power LED blinks. "
                    "Run devices again. The guide command can retrieve the current instructions")


def doctor() -> int:
    """Diagnose dependencies and enumerate only; never opens a HID device."""
    host = host_platform()
    result = {"cli_version": VERSION, "host": host, "os_version": platform.platform(),
              "cpu": platform.machine(), "python": platform.python_version(),
              "python_executable": sys.executable, "python_bits": struct.calcsize("P") * 8,
              "in_virtualenv": sys.prefix != sys.base_prefix,
              "hardware_opened": False, "hidapi": {}, "devices": [],
              "access_hint": usb_access_hint()}
    try:
        result["hidapi"]["package_version"] = importlib.metadata.version("hidapi")
    except importlib.metadata.PackageNotFoundError:
        result["hidapi"]["package_version"] = None
    status = 0
    try:
        hid = import_hid()
        result["hidapi"].update(loadable=True, module_path=getattr(hid, "__file__", None),
                                native_version=hid.version_str() if hasattr(hid, "version_str") else None)
        result["devices"] = enumerate_devices()
        result["updater_interfaces"] = sum(bool(d["mode"]) for d in result["devices"])
    except (Error, OSError) as exc:
        result["hidapi"]["error"] = str(exc)
        status = 1
    result["note"] = "Enumeration does not verify device access or flashing. Run info for a read-only identity query."
    json_print(result)
    return status


class HidTransport:
    """HIDAPI adapter. Public recv() returns WebHID-style data (without report ID)."""
    def __init__(self, info: dict, trace: str | None = None):
        self.info = info
        self.mode = info["mode"]
        self.report_id = 0x81 if self.mode == "app" else 0
        self.log = None
        self.device = None
        try:
            self.log = open(trace, "a", encoding="utf-8") if trace else None
            self.device = import_hid().device()
            self.device.open_path(device_path_bytes(info))
            self.device.set_nonblocking(0)
        except OSError as exc:
            self.close()
            raise DeviceOpenError(f"Cannot open USB interface {device_id(info)}: {exc}. {usb_access_hint()}") from exc
        except Exception:
            self.close()
            raise

    def record(self, direction: str, data: bytes):
        if self.log:
            self.log.write(json.dumps({"time": time.time(), "direction": direction,
                                       "mode": self.mode, "hidapi_bytes": data.hex()}) + "\n")
            self.log.flush()

    def drain(self):
        # Bounded even if an app-mode controller continuously streams input.
        for _ in range(128):
            if not self.device.read(65, 1):
                return

    def send(self, payload: bytes):
        wire = bytes([self.report_id]) + payload
        self.record("out", wire)
        written = self.device.write(wire)
        if written < len(wire):
            raise ProtocolError(f"Short HID write: {written}/{len(wire)} bytes")

    def recv(self, timeout: float) -> bytes:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            raw = bytes(self.device.read(65, max(1, int((end - time.monotonic()) * 1000))))
            if not raw:
                continue
            self.record("in", raw)
            if self.mode == "app":
                # Numbered HID reports include their ID in hid_read(). Boot mode
                # unnumbered reports do NOT get an artificial 00 removed here.
                if raw[0] in (3, 4):
                    continue
                return raw[1:]
            return raw
        raise ReplyTimeout("Timed out waiting for USB reply")

    def close(self):
        if getattr(self, "device", None) is not None:
            device = self.device
            self.device = None
            try:
                device.close()
            except OSError:
                pass  # An unplugged interface may already be invalid.
        if self.log:
            self.log.close()
            self.log = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Protocol:
    def __init__(self, transport, timeout: float = 1.5):
        self.io = transport
        self.mode = transport.mode
        self.timeout = timeout

    def raw(self, payload: bytes, timeout: float | None = None) -> bytes:
        self.io.drain()
        self.io.send(payload)
        return self.io.recv(self.timeout if timeout is None else timeout)

    def request(self, command: int, *, timeout: float | None = None, **fields) -> Reply:
        raw = self.raw(packet(command, **fields), timeout)
        return decode_reply(raw, self.mode)

    def ack(self, command: int, **fields) -> bytes:
        reply = self.request(command, **fields)
        if reply.command != ACK or reply.params != command:
            raise ProtocolError(f"Unexpected acknowledgement for 0x{command:04x}: "
                                f"cmd=0x{reply.command:04x}, params=0x{reply.params:04x}")
        return reply.body

    def read_data(self, command: int, **fields) -> bytes:
        reply = self.request(command, **fields)
        if reply.params != command:
            raise ProtocolError(f"Response belongs to 0x{reply.params:04x}, expected 0x{command:04x}")
        if reply.command != ACK:
            raise ProtocolError(f"Device returned status 0x{reply.command:04x} for 0x{command:04x}")
        return reply.body

    def read_flash(self, address: int, size: int) -> bytes:
        if not 1 <= size <= CHUNK:
            raise Error("Read size must be 1..46")
        return self.read_data(READ, length=size, total=size, offset=address)

    def init(self, params: int = 0) -> bytes:
        body = self.ack(INIT, params=params)
        return body[2:] if self.mode == "app" else body

    def erase(self, address: int, size: int):
        self.ack(ERASE, total=size, offset=address, timeout=12)

    def write(self, data: bytes, address: int, total: int, progress: Callable[[int], None]):
        for pos in range(0, len(data), CHUNK):
            chunk = data[pos:pos + CHUNK]
            self.ack(WRITE, data=chunk, total=total, offset=address + pos)
            progress(len(chunk))

    def crc(self, address: int, size: int, block: int = PAGE, extra: bytes = b"") -> int:
        payload = struct.pack("<3I", address, size, block) + extra
        data = self.read_data(CRC, data=payload, total=len(payload), offset=address)
        return take_u16(data, 0) if self.mode == "app" else take_u32(data, 0)

    def save(self, segment: Segment):
        self.ack(SAVE, data=struct.pack("<2I", segment.size, segment.version))

    def reset(self):
        # The web app sends reset without waiting for an ACK (device disappears).
        self.io.send(packet(RESET))

    def jump(self, pid: int, usb_pid: int):
        command = 0x50 if pid in JUMP_0050 else 0x5100 if usb_pid in JUMP_5100 else 12
        self.io.drain()
        self.io.send(packet(command, channel=0 if pid in JUMP_0050 else 4))

    def legacy(self, command: int, data: bytes | None = None, *, ack: bool = True) -> bytes:
        payload = legacy_packet(command, data)
        if not ack:
            self.io.send(payload)
            return b""
        try:
            response = self.raw(payload, 3)
        except ReplyTimeout:
            # Sequential legacy writes have no address; never replay a timed-out
            # data packet. The site's recovery packet asks for transfer status.
            response = self.raw(legacy_packet(0, family=29), 3)
        if command in (0, 1, 6) and (len(response) < 6 or response[5] != 20):
            raise ProtocolError(f"Legacy command {command} was not acknowledged")
        return response


def old_query(proto: Protocol, command: int = 0x2100, **fields) -> bytes:
    raw = proto.raw(packet(command, **fields))
    return raw[1:] if raw and raw[0] == 0x22 else raw


def app_check(proto: Protocol) -> bytes:
    raw = proto.raw(packet(0x0100, channel=4))
    if len(raw) < 23 or raw[0:2] != b"\x04\x04" or raw[4] != 1:
        raise ProtocolError("Unexpected APP_CHECK_DEVICE response")
    return raw


def identify(proto: Protocol, usb_pid: int) -> int:
    if usb_pid == 0x5750:
        return usb_pid  # Shared legacy bootloader; model must be selected by user.
    if usb_pid in DIRECT_PID:
        return usb_pid
    try:
        if usb_pid in (0x301C, 0x3109):
            raw = old_query(proto, params=1, length=4, total=4)
            return take_u16(raw, 6 if usb_pid == 0x3109 else 4)
        return take_u32(proto.read_data(READ_PID, length=4, total=4), 0)
    except (ProtocolError, OSError):
        if proto.mode != "app":
            raise
        return take_u16(app_check(proto), 21)


def read_version(proto: Protocol, usb_pid: int, pid: int) -> tuple[int, int] | None:
    if pid == 0x5750:
        raw = proto.legacy(4)
        return take_u16(raw, 8), take_u16(raw, 10)
    if proto.mode == "boot" and pid in {m.pid for m in MODEL_LIST if m.route in ("receiver", "sn30")}:
        return None  # This branch does not read a version in the website.
    if proto.mode == "app" and pid in (0x600B, 0x3019):
        raw = old_query(proto, 0x21)
        return take_u16(raw, 17), take_u16(raw, 18)
    if proto.mode == "app" and pid in OLD_VERSION:
        try:
            raw = old_query(proto, params=1)
            return take_u16(raw, 0), take_u16(raw, 2)
        except (ProtocolError, OSError):
            raw = app_check(proto)
            return take_u16(raw, 17), raw[19]
    if usb_pid in (0x310A, 0x301C):
        # Use the validated flash-read response body, as 8bitdo-fw.py does.
        # Adjacent u16 fields are at +2 and +4, not overlapping +2 and +3.
        raw = proto.read_flash(0x6000, 16)
        return take_u16(raw, 2), take_u16(raw, 4)
    if pid in (0x207C, 0x2079):
        raw = proto.read_data(VERSION_CMD)
        return take_u16(raw, 0), take_u16(raw, 2)
    if pid == 0x2066:
        raw = proto.read_data(INFO)
        return take_u16(raw, 4), take_u16(raw, 6)
    if proto.mode == "app":
        raw = app_check(proto)
        return take_u16(raw, 17), raw[19]
    raw = proto.read_flash(VERSION_ADDRESS, 4)
    return take_u16(raw, 0), take_u16(raw, 4)


def check_identity(model: Model, pid: int):
    if pid == model.pid or (model.route == "sn30" and pid in (0x5750, 0x6000, 0x6001)):
        return
    raise Error(f"Connected device identifies as 0x{pid:04x}; selected {model.slug} "
                f"expects 0x{model.pid:04x}. No firmware was erased")


def flash_plan(model: Model, fw: Firmware, mode: str) -> dict:
    fw.validate(model)
    if mode == "auto":
        mode = "boot" if host_platform() == "macos" and model.slug == "ultimate2-bluetooth" else "app"
    validate_host_mode(model, mode)
    initial_mode = mode
    steps = []
    if mode == "app" and model.route in ("receiver", "sn30"):
        steps.append("Send model-specific boot command; wait for newly enumerated USB interface; recheck identity")
        mode = "boot"
    main = fw.main
    if model.route == "sn30":
        if main.pid != 0:
            steps.append("If USB boot PID is 5750, use the legacy sequence below; otherwise erase/write the declared image through the identified standard bootloader, then reset")
        steps += ["Read legacy version/ID data", "Send first 11 header bytes",
                  f"Write {main.size} payload bytes in 32-byte packets",
                  "Send three source-compatible serial check bytes", "Send legacy reset"]
    elif model.route == "receiver":
        if model.pid in INIT_RECEIVERS:
            steps += ["Initialize receiver; use returned file skip length",
                      "Erase/write payload after skipped prefix at address 0",
                      "Save first 4 KiB and compare its CRC with the device"]
        else:
            steps += [f"Erase/write {main.size} bytes at 0x{main.address:08x}"]
            if model.pid in (0x5112, 0x600B, 0x600C):
                steps += [f"Erase 32 bytes and write version at 0x{VERSION_ADDRESS:08x}"]
            if model.pid in (0x600B, 0x600C):
                steps += [f"Erase/write 12-byte boot metadata at 0x{BOOT_FLAG_ADDRESS:08x}"]
        steps.append("Send reset; reconnect to confirm operation")
    else:
        if mode == "app" or model.route == "pages":
            address = main.address if model.pid == 0x3019 else 0
            steps += [f"Compare/erase/write 4 KiB pages starting at 0x{address:08x}; final chunk may be shorter"]
        else:
            address = APP_ADDRESS if model.route == "dual" else main.address
            steps += [f"Erase {main.size} bytes at 0x{main.address:08x}",
                      f"Write {main.size} bytes at 0x{address:08x}"]
        if model.route == "dual":
            steps += ["Initialize secondary target with parameter 0x8000 in app mode",
                      f"Erase/write {fw.segments[0].size} secondary bytes at 0x40000000"]
        if not (mode == "boot" and model.pid in SKIP_SAVE_BOOT):
            steps.append("Save main image length and version")
        steps.append("Send reset; reconnect to confirm operation")
    return {"model": model.slug, "hardware_tested": model.slug in TESTED_MODELS,
            "testing_source": "user-reported" if model.slug in TESTED_MODELS else None, "firmware": fw.describe(),
            "initial_mode": initial_mode, "effective_mode": mode,
            "connection_guidance": connection_guidance(model), "steps": steps}


def program(proto: Protocol, model: Model, fw: Firmware, *, usb_pid: int,
            progress: Callable[[int], None] = lambda _: None):
    """Program an already identified/validated device. Never performs a blind retry."""
    fw.validate(model)
    main = fw.main
    if model.route == "sn30" and main.pid == 0 and usb_pid != 0x5750:
        raise Error("PID-zero SN30/SF30 image requires the 0x5750 legacy bootloader")
    if usb_pid == 0x5750:
        if model.route != "sn30" or proto.mode != "boot":
            raise Error("0x5750 requires the SN30/SF30 legacy transfer route")
        # Source GET_verID_DATA uses a six-byte output, unlike firmware packets.
        ids = proto.raw(bytes([5, 0, 26, 1, 0, 0]))
        values = [take_u32(ids, x) for x in (0, 4, 8)]
        proto.legacy(1, fw.data[:11])
        for pos in range(0, len(main.payload), 32):
            data = main.payload[pos:pos + 32]
            proto.legacy(0, data)
            progress(len(data))
        # The shipped app passes a 3-element JS Array to Uint8Array.set(),
        # truncating each XOR word to one byte. Preserve this observable quirk.
        encoded = bytes((n ^ SERIAL_TABLE[n & 15]) & 255 for n in values)
        proto.legacy(6, encoded)
        proto.legacy(2, ack=False)
        return
    if model.route in ("receiver", "sn30"):
        if proto.mode != "boot":
            raise Error("This update path requires a bootloader reconnect")
        payload = main.payload
        address = main.address
        if model.pid in INIT_RECEIVERS:
            skip = take_u32(proto.init(), 0)
            if not 0 <= skip < len(payload) or skip % 4:
                raise ProtocolError(f"Invalid receiver initialization skip length: {skip}")
            payload = payload[skip:]
            address = 0
            if len(payload) < PAGE or len(payload) % 4:
                raise Error("Initialized receiver payload must be word-aligned and at least 4 KiB")
        proto.erase(address, len(payload))
        proto.write(payload, address, len(payload), progress)
        if model.pid in INIT_RECEIVERS:
            proto.ack(SAVE4K)
            extra = struct.pack("<2I", word_sum(payload), len(payload))
            actual = proto.crc(0, PAGE, 0, extra)
            expected = crc16(payload[:PAGE])
            if actual != expected:
                raise ProtocolError(f"Receiver verification failed: {actual:#x} != {expected:#x}; reset not sent")
        elif model.pid in (0x5112, 0x600B, 0x600C):
            proto.erase(VERSION_ADDRESS, 32)
            proto.ack(WRITE_PLAIN, data=struct.pack("<I", main.version), total=4, offset=VERSION_ADDRESS)
            if model.pid in (0x600B, 0x600C):
                proto.erase(BOOT_FLAG_ADDRESS, 12)
                proto.ack(WRITE_PLAIN, data=struct.pack("<3I", BOOT_FLAG, main.size - 4, main.reserved1),
                          total=12, offset=BOOT_FLAG_ADDRESS)
        proto.reset()
        return
    if model.route == "pages":
        proto.init()
    if proto.mode == "app" or model.route == "pages":
        base = main.address if model.pid == 0x3019 else 0
        for pos in range(0, main.size, PAGE):
            chunk = main.payload[pos:pos + PAGE]
            address = base + pos
            # Original app reads a full page even for its shorter final chunk.
            # Such a tail must be rewritten rather than comparing unlike CRCs.
            current_crc = proto.crc(address, PAGE)
            if len(chunk) == PAGE and current_crc == crc16(chunk):
                progress(len(chunk))
                continue
            proto.erase(address, len(chunk))
            proto.write(chunk, address, main.size, progress)
    else:
        address = APP_ADDRESS if model.route == "dual" else main.address
        proto.erase(main.address, main.size)
        proto.write(main.payload, address, main.size, progress)
    if model.route == "dual":
        if proto.mode == "app":
            proto.init(0x8000)
            time.sleep(1)
        secondary = fw.segments[0]
        proto.erase(SECONDARY_ADDRESS, secondary.size)
        proto.write(secondary.payload, SECONDARY_ADDRESS, secondary.size, progress)
    if not (proto.mode == "boot" and model.pid in SKIP_SAVE_BOOT):
        proto.save(main)
    proto.reset()


def reconnect(previous: dict, before: list[dict], model: Model, timeout: float,
              trace: str | None) -> tuple[dict, HidTransport, int]:
    """Only consider newly arrived boot interfaces, never another existing device."""
    existing = {d["path"] for d in before if d["path"] != previous["path"]}
    end = time.monotonic() + timeout
    disappeared = False
    while time.monotonic() < end:
        devices = enumerate_devices()
        if not any(d["path"] == previous["path"] and d["product_id"] == previous["product_id"] for d in devices):
            disappeared = True
        candidates = [d for d in devices if d["mode"] == "boot" and d["path"] not in existing
                      and (disappeared or d["product_id"] != previous["product_id"])]
        if len(candidates) > 1:
            raise Error("Multiple new boot interfaces appeared. Run devices, then select --path explicitly")
        if candidates:
            candidate = candidates[0]
            old_serial, new_serial = previous.get("serial_number"), candidate.get("serial_number")
            if old_serial and new_serial and old_serial != new_serial:
                raise Error("Serial changed across reboot; select the boot device explicitly with --path")
            io = None
            try:
                io = HidTransport(candidate, trace)
                pid = identify(Protocol(io), candidate["product_id"])
                check_identity(model, pid)
                return candidate, io, pid
            except (ProtocolError, OSError):
                if io:
                    io.close()
            except BaseException:
                if io:
                    io.close()
                raise
        time.sleep(0.25)
    raise Error("Bootloader did not reappear. Reconnect in update mode, run devices, and retry with --path")


class OfficialAPI:
    # The official updater is a browser application hosted on
    # https://web.8bitdo.com; the API and firmware CDN expect that client's
    # identity, so the CLI presents a plausible browser rather than a bot-ish
    # one. sec-ch-* hints and fake encodings are deliberately not sent.
    USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
    BROWSER_HEADERS = {"User-Agent": USER_AGENT,
                       "Accept-Language": "en-US,en;q=0.9",
                       "Origin": "https://web.8bitdo.com",
                       "Referer": "https://web.8bitdo.com/"}
    API_HEADERS = {**BROWSER_HEADERS, "Accept": "application/json, text/plain, */*"}
    DOWNLOAD_HEADERS = {**BROWSER_HEADERS, "Accept": "application/octet-stream, */*"}
    RETRY_BACKOFF = 1.0
    RETRY_AFTER_CAP = 10.0

    class _BoundedHTTPSConnection(http.client.HTTPSConnection):
        """Cloudflare's edge set includes blackholed IPs; create_connection
        tries addresses in DNS order with the full socket timeout each, so a
        dead first IP stalls a request for the whole read budget. Bound only
        the connect+handshake phase; body reads keep the full timeout."""
        CONNECT_TIMEOUT = 5.0

        def connect(self):
            read_timeout = self.timeout
            self.timeout = (min(self.CONNECT_TIMEOUT, read_timeout)
                            if isinstance(read_timeout, (int, float)) else self.CONNECT_TIMEOUT)
            try:
                super().connect()
            finally:
                self.timeout = read_timeout
            self.sock.settimeout(read_timeout)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(OfficialAPI._BoundedHTTPSConnection, req,
                                context=self._context)

    def __init__(self, timeout: float = 20, retries: int = 2):
        self.timeout = timeout
        self.retries = retries
        self._opener = None

    @property
    def opener(self):
        # Instance-scoped and created lazily (never install_opener): no
        # process-wide state, and no SSL context is built until first use.
        if self._opener is None:
            self._opener = urllib.request.build_opener(self._HTTPSHandler())
        return self._opener

    @staticmethod
    def _transient(exc: Exception) -> bool:
        """Only transient failures (network, timeout, 429, 5xx) may be retried."""
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code == 429 or exc.code >= 500
        return True

    def _pause(self, exc: Exception, retry: int) -> float | None:
        """Delay before the next attempt; None means do not retry at all."""
        if isinstance(exc, urllib.error.HTTPError) and exc.headers is not None:
            value = exc.headers.get("Retry-After")
            if value is not None:
                try:
                    seconds = float(value)
                except ValueError:
                    seconds = None
                if seconds is not None and math.isfinite(seconds) and seconds > 0:
                    # A wait longer than our cap means the server wants us
                    # gone for a while: abort instead of retrying too early.
                    return seconds if seconds <= self.RETRY_AFTER_CAP else None
        return min(self.RETRY_BACKOFF * retry, self.RETRY_AFTER_CAP)

    def _fetch(self, build_request: Callable[[], urllib.request.Request],
               read: Callable[[object], bytes]) -> bytes:
        """Open and read, retrying transient failures including mid-read ones."""
        for retry in range(self.retries + 1):
            try:
                with self.opener.open(build_request(), timeout=self.timeout) as response:
                    return read(response)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()  # Release the error body/socket promptly.
                delay = self._pause(exc, retry + 1)
                if retry >= self.retries or not self._transient(exc) or delay is None:
                    raise
                time.sleep(delay)
        raise Error("Retry loop exhausted")  # Unreachable; guards against edits.

    def request(self, endpoint: str, body: dict):
        headers = {**self.API_HEADERS, "Content-Type": "application/json;charset=utf-8"}
        payload = json.dumps(body).encode()
        try:
            raw = self._fetch(lambda: urllib.request.Request(API + endpoint, payload, headers),
                              lambda response: response.read(4 * 1024 * 1024 + 1))
            if len(raw) > 4 * 1024 * 1024:
                raise Error("Catalog response too large")
            value = json.loads(raw)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            raise Error(f"Catalog request failed: {exc}") from exc
        if not isinstance(value, dict) or value.get("code", 200) != 200:
            raise Error(f"Catalog API error: {value}")
        return value.get("data")

    def products(self, platform: str | None = None) -> list[dict]:
        platform = platform or host_platform()
        softtype = {"windows": 6, "macos": 7, "linux": 8}[platform]
        data = self.request("/client/productlist", {"product_name": "", "current": 1,
                                                    "pageSize": 9999, "softtype": softtype})
        rows = data.get("data", []) if isinstance(data, dict) else []
        permitted = {m.firmware_type for m in MODEL_LIST if m.selectable}
        # The site's local selection table contains type 108, even if its API
        # currently omits it. The CLI can still explicitly select that model.
        permitted.add(108)
        return [r for r in rows if r.get("firmware_type") in permitted]

    def product(self, code: str) -> dict:
        data = self.request("/client/productquery", {"product_code": code})
        if not isinstance(data, dict):
            raise Error("Unexpected product detail response")
        return data

    def versions(self, model: Model, beta: bool = False) -> list[dict]:
        data = self.request("/client/firmware/select", {"firmware_type": model.firmware_type,
                                                      "beta": int(beta)})
        if not isinstance(data, list):
            raise Error("Unexpected firmware list response")
        return [r for r in data if (beta or not r.get("beta")) and r.get("exists", True)]

    def download(self, row: dict) -> bytes:
        url = row.get("fileURL", "")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname == "8bitdo.com" or parsed.hostname.endswith(".8bitdo.com")
        ):
            raise Error("Firmware URL is not an HTTPS 8BitDo URL")
        try:
            data = self._fetch(lambda: urllib.request.Request(
                url, headers=dict(self.DOWNLOAD_HEADERS)), self._copy)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise Error(f"Firmware download failed: {exc}") from exc
        if len(data) > MAX_FILE:
            raise Error("Firmware download too large")
        if row.get("fileSize") and len(data) != int(row["fileSize"]):
            raise Error("Downloaded size differs from official catalog")
        # The catalog's hash fields are stale for reference samples (and the
        # website itself only checks size), so they are ignored entirely.
        # run() still prints the downloaded bytes' SHA-256 for identification.
        Firmware.parse(data)
        return data

    def _copy(self, response, chunk: int = 65536) -> bytes:
        """Read up to MAX_FILE+1 bytes in chunks, reporting progress on stderr."""
        data = bytearray()
        while len(data) <= MAX_FILE:
            block = response.read(chunk)
            if not block:
                break
            data += block
            print(f"\rDownloading: {len(data)} bytes", end="", file=sys.stderr, flush=True)
        print(f"\rDownloaded {len(data)} bytes", file=sys.stderr, flush=True)
        return bytes(data)


def choose_version(rows: list[dict], version: str | None, key: str | None) -> dict:
    if key:
        choices = [r for r in rows if str(r.get("firmware_key")) == key]
    elif version:
        choices = [r for r in rows if str(r.get("file_version")) == version]
    else:
        choices = rows[:1]  # Preserve the site's catalog ordering.
    if len(choices) != 1:
        raise Error("Firmware selection is empty or ambiguous. Use versions, then --key")
    return choices[0]


def download_path(model: Model, row: dict, output: str | None) -> Path:
    if output is not None:
        return Path(output).expanduser()
    version = str(row.get("file_version", ""))
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
        raise Error("Catalog version cannot form a safe filename; specify --output explicitly")
    return Path(__file__).resolve().parent / "firmware" / f"{model.slug}-v{version.replace('.', '')}.dat"


def confirm_download_overwrite(path: Path) -> bool:
    if path.is_symlink():
        raise Error(f"Refusing to replace a symlink: {path}")
    if not path.exists():
        return False
    if not path.is_file():
        raise Error(f"Output is not a regular file: {path}")
    print(f"{path} already exists. Overwrite? [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        raise Error("Cancelled; existing file kept. No firmware downloaded or flashed")
    return True


def save_download(path: Path, data: bytes, overwrite: bool):
    if not overwrite:
        write_new(path, data)  # Exclusive create also handles files appearing mid-download.
        return
    temporary = None
    try:
        # Stage a fully validated image before replacing the approved old file.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".firmware-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_new(path: str | Path, data: bytes):
    path = Path(path)
    # Never overwrite a supplied/custom firmware or an existing trace artifact.
    with path.open("xb") as file:
        file.write(data)


def json_print(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=path_text))


def positive(value: str) -> float:
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise argparse.ArgumentTypeError("Value must be finite and positive")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("models", help="Show implemented model routes and limitations")
    devices = sub.add_parser("devices", help="List USB HID paths and updater modes without sending commands")
    devices.add_argument("--json", action="store_true", help="Machine-readable interface details")
    sub.add_parser("doctor", help="Check host/dependencies and enumerate interfaces without opening them")
    catalog = sub.add_parser("catalog", help="Fetch the current web-app product list")
    catalog.add_argument("--platform", choices=("windows", "macos", "linux"), default=host_platform())
    catalog.add_argument("--receivers", action="store_true", help="Also fetch each product's receiver entries")
    for name in ("guide", "versions", "download", "plan", "flash", "update"):
        parser = sub.add_parser(name, help={"guide": "Connection instructions from the official API",
                 "versions": "Official firmware versions, release notes, and keys",
                 "download": "Download official firmware without opening USB",
                 "plan": "Offline validation and description; never opens USB",
                 "flash": "Install a local model-matching .dat file",
                 "update": "Download official firmware and install it"}[name])
        parser.add_argument("--model", required=True, choices=tuple(MODELS))
        if name in ("versions", "download", "update"):
            parser.add_argument("--beta", action="store_true", help="Include beta firmware")
        if name in ("download", "update"):
            group = parser.add_mutually_exclusive_group()
            group.add_argument("--version", dest="fw_version")
            group.add_argument("--key", help="Exact firmware_key from versions")
            parser.add_argument("--output", help="Override download path (default: firmware/MODEL-vVERSION.dat beside the script)")
        if name in ("plan", "flash"):
            parser.add_argument("file", help="Local .dat firmware container")
        if name == "plan":
            parser.add_argument("--mode", choices=("auto", "app", "boot"), default="auto",
                                help="Plan only: auto uses the model/host default; app=normal USB, boot=recovery. "
                                     "flash/update detect the actual interface; this flag never switches a controller.")
        if name in ("flash", "update"):
            selector = parser.add_mutually_exclusive_group()
            selector.add_argument("--path", help="Exact native interface path from devices")
            selector.add_argument("--device", help="12-character device_id from devices (all operating systems)")
            parser.add_argument("--yes", action="store_true", help="Confirm the displayed plan and permit device writes")
            parser.add_argument("--trace", help="Append timestamped HID transaction JSON lines to this file")
            parser.add_argument("--reconnect-timeout", type=positive, default=30.0)
        if name == "guide":
            parser.add_argument("--platform", choices=("windows", "macos", "linux"), default=host_platform())
            parser.add_argument("--boot", action="store_true", help="Show recovery/update-mode instructions if supplied")
    info = sub.add_parser("info", help="Read protocol identity and available firmware version")
    selector = info.add_mutually_exclusive_group()
    selector.add_argument("--path")
    selector.add_argument("--device")
    info.add_argument("--trace")
    info.add_argument("--json", action="store_true", help="Machine-readable identity and version")
    inspect = sub.add_parser("inspect", help="Inspect local .dat structure and SHA-256 offline")
    inspect.add_argument("file")
    extract = sub.add_parser("extract", help="Extract exact payload bytes; does not decrypt or modify them")
    extract.add_argument("file")
    extract.add_argument("--directory", required=True, help="A new destination directory")
    sub.add_parser("udev", help="Print a Linux USB HID permission rule; does not install it")
    return p


def flash_cli(args, model: Model, fw: Firmware) -> int:
    fw.validate(model)  # Complete local validation before opening a device.
    before = enumerate_devices()
    selected = select_device(before, args.path, args.device)
    validate_host_mode(model, selected["mode"])
    plan = flash_plan(model, fw, selected["mode"])
    json_print({"device": selected["path"], **plan})
    if not args.yes:
        print("Plan only. Add --yes to permit flashing. No USB device was opened.", file=sys.stderr)
        return 0
    if args.trace and Path(args.trace).resolve() == Path(getattr(args, "file", args.output if hasattr(args, "output") else "")).resolve():
        raise Error("Trace path must differ from firmware path")
    io = HidTransport(selected, args.trace)
    transferred = 0
    total = sum(s.size for s in fw.segments)
    last_progress = -1

    def progress(count):
        nonlocal transferred, last_progress
        transferred += count
        percentage = min(100, transferred * 100 // total)
        if percentage != last_progress:
            print(f"\rTransfer {percentage:3d}% ({transferred}/{total} bytes)", end="", file=sys.stderr, flush=True)
            last_progress = percentage

    try:
        proto = Protocol(io)
        pid = identify(proto, selected["product_id"])
        check_identity(model, pid)
        if selected["mode"] == "app" and model.route in ("receiver", "sn30"):
            print("Switching to bootloader...", file=sys.stderr)
            proto.jump(pid, selected["product_id"])
            io.close()
            selected, io, pid = reconnect(selected, before, model, args.reconnect_timeout, args.trace)
            proto = Protocol(io)
        if proto.mode == "app" and selected["product_id"] in DIRECT_PID:
            # Stop normal reports only after the user elects to flash.
            io.send(packet(7, channel=4))
            time.sleep(0.05)
        program(proto, model, fw, usb_pid=selected["product_id"], progress=progress)
    except BaseException:
        print("\nUpdate stopped. Reset was not deliberately sent after failure. Keep the log; "
              "reconnect in update mode before retrying.", file=sys.stderr)
        raise
    finally:
        io.close()
    print("\nTransfer acknowledged; reset sent. Reconnect and check the reported version and controls.", file=sys.stderr)
    return 0


def run(args) -> int:
    command = args.command
    if command == "models":
        print(f"{'MODEL':36} {'TYPE':>4}  {'PID':4}  STATUS")
        for m in MODEL_LIST:
            state = "unsupported" if m.route == "unsupported" else "tested" if m.slug in TESTED_MODELS else "untested"
            print(f"{m.slug:36} {m.firmware_type:4}  {m.pid:04x}  {state}")
        return 0
    if command == "devices":
        devices = enumerate_devices()
        if args.json:
            json_print(devices)
        elif not devices:
            print("No 8BitDo HID interfaces found. Connect by USB data cable and run guide --model MODEL.")
        else:
            for device in devices:
                print(f"{device['path']} ({device['vendor_id']:04x}:{device['product_id']:04x}) "
                      f"{device.get('product_string') or ''}")
                print(f"  {mode_description(device['mode'])}; device_id={device_id(device)}")
        return 0
    if command == "doctor":
        return doctor()
    if command == "udev":
        if host_platform() != "linux":
            raise Error("udev rules apply only to Linux. Run doctor for this host's USB guidance")
        print('SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2dc8", MODE="0660", TAG+="uaccess"')
        return 0
    if command in ("inspect", "extract"):
        fw = Firmware.read(args.file)
        if command == "inspect":
            json_print(fw.describe())
        else:
            directory = Path(args.directory)
            directory.mkdir(parents=True, exist_ok=False)
            for n, segment in enumerate(fw.segments):
                write_new(directory / f"segment-{n}-pid-{segment.pid:04x}.bin", segment.payload)
            write_new(directory / "manifest.json", (json.dumps(fw.describe(), indent=2) + "\n").encode())
            print(directory)
        return 0
    if command == "info":
        selected = select_device(enumerate_devices(), args.path, args.device)
        with HidTransport(selected, args.trace) as io:
            proto = Protocol(io)
            pid = identify(proto, selected["product_id"])
            models = info_models(pid, selected)
            model = models[0] if len(models) == 1 else None
            result = {"device": selected, "protocol_pid": f"0x{pid:04x}",
                      "model_candidates": [m.slug for m in models],
                      "model": model.slug if model else None,
                      "model_name": model.name if model else None,
                      "firmware_type": model.firmware_type if model else None,
                      "model_basis": "protocol PID and USB product name" if pid == 0x301B and model else "protocol PID",
                      "mode_description": mode_description(selected["mode"]), "version": None}
            try:
                version = read_version(proto, selected["product_id"], pid)
                if version:
                    result.update(version=f"{version[0] / 100:.2f}", version_raw=version[0], beta=version[1])
            except (ProtocolError, OSError) as exc:
                result["version_error"] = str(exc)
            if args.json:
                json_print(result)
            else:
                print(f"interface : {selected['path']} ({VID:04x}:{selected['product_id']:04x})")
                print(f"usb pid   : 0x{selected['product_id']:04x}")
                print(f"device pid: 0x{pid:04x}  ({result['model_name'] or 'ambiguous/unknown model'})")
                types = ", ".join(str(m.firmware_type) for m in models) or "unknown"
                print(f"fw type   : {types}")
                if result["version"] is not None:
                    beta = f" beta {result['beta']}" if result.get("beta") else ""
                    print(f"firmware  : {result['version']} (raw {result['version_raw']}){beta}")
                else:
                    print(f"firmware  : unavailable ({result.get('version_error', 'not reported by this bootloader')})")
                print(f"mode      : {result['mode_description']}")
                if model:
                    print(f"connection: {connection_guidance(model)}")
                if not model and models:
                    print("models    : " + ", ".join(m.slug for m in models) + "; choose the catalog matching your device")
        return 0
    api = OfficialAPI()
    if command == "catalog":
        print("Fetching catalog...", file=sys.stderr, flush=True)
        products = api.products(args.platform)
        if args.receivers:
            codes = list(dict.fromkeys(product["product_code"] for product in products))
            details = {}
            print(f"Fetching receiver details: 0/{len(codes)}", file=sys.stderr, flush=True)
            # Independent per-worker clients avoid sharing mutable urllib state.
            # Bound concurrency and deduplicate product families; keep API order.
            with ThreadPoolExecutor(max_workers=4) as pool:
                pending = {pool.submit(OfficialAPI().product, code): code for code in codes}
                for future in as_completed(pending):
                    code = pending[future]
                    details[code] = future.result().get("receives", [])
                    print(f"Receiver details: {len(details)}/{len(codes)}", file=sys.stderr, flush=True)
            for product in products:
                product["receivers"] = details[product["product_code"]]
        json_print(products)
        return 0
    model = MODELS[args.model]
    if command == "guide":
        print(connection_guidance(model))
        print("app = normal USB firmware; boot = bootloader/recovery. flash/update detect the connected mode.")
        detail = api.product(model.product_code)
        softtype = {"windows": 6, "macos": 7, "linux": 8}[args.platform]
        boot = args.boot or any(x.get("softtype") == softtype and x.get("soft_isboot") for x in detail.get("softtypes", []))
        print(f"Official {args.platform} instructions: {'boot/recovery' if boot else 'normal USB/app'} connection.")
        if "receiver" in model.slug:
            print("Turn the controller off, then connect the receiver directly to USB. Update each separately.")
        else:
            steps = [s for s in detail.get("connects", []) if bool(s.get("connect_isboot")) == bool(boot)]
            if not steps:
                print("The API supplied no instructions for this connection mode. Check the model's manual.")
            for i, step in enumerate(steps, 1):
                print(f"{i}. {step.get('connect_title', '')}\n   Image: {step.get('connect_img', '')}")
        return 0
    if command == "versions":
        json_print(api.versions(model, args.beta))
        return 0
    if command in ("download", "update"):
        if command == "update" and model.route == "unsupported":
            raise Error("This model has no verified flash route")
        row = choose_version(api.versions(model, args.beta), args.fw_version, args.key)
        output = download_path(model, row, args.output)
        args.output = str(output)
        overwrite = confirm_download_overwrite(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        data = api.download(row)
        fw = Firmware.parse(data)
        if model.route != "unsupported":
            fw.validate(model)
        save_download(output, data, overwrite)
        print(f"Saved {args.output}; SHA-256 {fw.sha256}", file=sys.stderr)
        if command == "download":
            return 0
        return flash_cli(args, model, fw)
    fw = Firmware.read(args.file)
    if command == "plan":
        json_print(flash_plan(model, fw, args.mode))
        return 0
    return flash_cli(args, model, fw)


def main(argv=None) -> int:
    configure_stdio()
    try:
        return run(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (Error, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

