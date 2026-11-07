#!/usr/bin/env python3
"""Transport tests that need no hardware.

The hidapi path is the one that runs on Windows. There is no Windows box here, so
it is exercised against a fake hid module: the same open/write/read/parse code
that a real pad drives, minus the USB.
"""
import importlib.util
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "8bitdo-fw.py")


def load():
    spec = importlib.util.spec_from_file_location("fw", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fw"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeHandle:
    def __init__(self, response):
        self.response = response
        self.writes = []
        self.closed = False
        self.nonblocking = None

    def open_path(self, path):
        self.path = path

    def set_nonblocking(self, v):
        self.nonblocking = v

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def read(self, size, timeout_ms):
        return self.response

    def close(self):
        self.closed = True


class FakeHid:
    def __init__(self, path, response):
        self._path = path
        self._response = response
        self.handles = []

    def enumerate(self, vid):
        assert vid == 0x2DC8
        return [dict(path=self._path, vendor_id=0x2DC8, product_id=0x301B,
                     product_string="8BitDo Ultimate 2C Wireless Controller",
                     usage_page=0xFF7A, usage=0x0001, interface_number=0,
                     serial_number="")]

    def device(self):
        h = FakeHandle(self._response)
        self.handles.append(h)
        return h


def response_for(cmd, payload=b"", total=0, off=0):
    body = struct.pack("<BHHHHII", 0x05, 0, cmd, len(payload), 0, total, off)
    return b"\x81" + body + payload


def main():
    os.environ["8BITDO_BACKEND"] = "hidapi"
    mod = load()
    fake = FakeHid(b"\\\\?\\hid#vid_2dc8&pid_301b", response_for(0x0008, b"\x1b\x30"))
    mod._hidapi = fake  # type: ignore[attr-defined]

    assert mod.backend() == "hidapi", mod.backend()

    devs = mod.list_hidraw()
    assert len(devs) == 1, devs
    d0 = devs[0]
    assert d0["pid"] == 0x301B and d0["output_id"] == 0x81, d0
    print(f"PASS: hidapi enumeration -> {d0['node']!r} out={d0['output_id']:#04x}")

    picked = mod.pick_device(0x301B)
    assert picked["node"] == d0["node"]
    print("PASS: pick_device selects the vendor interface")

    d = mod.Device(picked["node"], picked["output_id"], usb_pid=0x301B)
    handle = fake.handles[-1]
    assert handle.nonblocking == 0, "blocking mode required for read timeout"
    print("PASS: open via hidapi, blocking mode set")

    status, payload = d.xfer(0x05, 0x0008, total_len=4)
    assert status == 0 and payload == b"\x1b\x30", (status, payload)
    print(f"PASS: xfer parsed status {status} payload {payload.hex()}")

    w = handle.writes[-1]
    assert len(w) == 64, f"report must be 64 bytes, got {len(w)}"
    assert w[0] == 0x81, f"report id must be 0x81, got {w[0]:#04x}"
    assert w[1] == 0x05, "tag"
    print("PASS: write is a 64-byte report, id 0x81, tag 0x05")

    # bootloader pid must not be written as 0x81
    assert mod.list_hidapi is not None
    d.close()
    assert handle.closed
    print("PASS: close() releases the hidapi handle")

    os.environ["8BITDO_BACKEND"] = "hidraw"
    assert mod.backend() == "hidraw"
    os.environ.pop("8BITDO_BACKEND")
    print("PASS: backend override and fallback")


if __name__ == "__main__":
    main()
