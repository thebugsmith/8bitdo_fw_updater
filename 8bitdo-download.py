#!/usr/bin/env python3
"""
8bitdo-download.py - fetch official 8BitDo firmware files from their public API.

The updater web app (https://web.8bitdo.com) talks to

    POST https://support.8bitdo.com/restapi/link/v1/client/firmware/select
         {"firmware_type": <int>, "beta": 0}
      -> {"data": [{file_version, fileURL, fileSize, md5, readme_en, ...}, ...]}

and firmware_type is a per-product number (see products.json). This script
exposes that API plus a catalogue scan.

Usage:
  ./8bitdo-download.py --list-products          every known product + type
  ./8bitdo-download.py --type 75                versions for one product
  ./8bitdo-download.py --type 75 --latest -o .  download newest build
  ./8bitdo-download.py --all -o fw/             mirror the whole catalogue
  ./8bitdo-download.py --scan 1-300             probe firmware_type range
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request

API = "https://support.8bitdo.com/restapi/link/v1/client/firmware/select"
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (X11; Linux x86_64) 8bitdo-download"


def post(payload, timeout=30):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json;charset=utf-8", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    if body.get("code") != 200:
        raise RuntimeError(body.get("message") or body)
    return body.get("data") or []


def versions(ftype, beta=0):
    return post({"firmware_type": ftype, "beta": beta})


def find_data(name):
    """products.json may sit next to the script or in a dt/ subdir."""
    up = os.path.dirname(HERE)
    for d in (HERE, os.path.join(HERE, "dt"), up, os.path.join(up, "dt")):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(HERE, name)


def load_products():
    with open(find_data("products.json")) as f:
        return json.load(f)


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
    return dest


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def safe_name(s):
    return "".join(c if c.isalnum() or c in " ._-" else "_" for c in s).strip()


def grab(entry, outdir, force=False):
    os.makedirs(outdir, exist_ok=True)
    name = f'{safe_name(entry["fileName"])}_v{entry["file_version"]}_{entry["firmware_type"]}.dat'
    dest = os.path.join(outdir, name)
    if os.path.exists(dest) and not force and md5(dest) == entry["md5"].lower():
        print(f"  = {name} (already present)")
        return dest
    download(entry["fileURL"], dest)
    got = md5(dest)
    size_ok = os.path.getsize(dest) == entry["fileSize"]
    # the API's md5 field is unreliable (identical across different builds), so
    # treat it as a hint only and fail on size instead.
    md5_ok = got == (entry.get("md5") or "").lower()
    print(f'  {"+" if size_ok else "!"} {name}  {entry["fileSize"]}B  '
          f'size {"ok" if size_ok else "MISMATCH"}  md5 {got[:12]}'
          + ("" if md5_ok else " (server md5 differs: " +
             (entry.get("md5") or "")[:12] + ")"))
    if not size_ok:
        raise SystemExit("download size mismatch")
    return dest


def main(argv):
    ap = argparse.ArgumentParser(description="8BitDo firmware downloader")
    ap.add_argument("--type", type=int, action="append", help="firmware_type (repeatable)")
    ap.add_argument("--list-products", action="store_true")
    ap.add_argument("--scan", help="range of firmware_type to probe, e.g. 1-300")
    ap.add_argument("--latest", action="store_true", help="download newest build only")
    ap.add_argument("--all", action="store_true", help="download every build")
    ap.add_argument("--beta", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("-o", "--output", default="fw")
    ap.add_argument("-j", "--json", action="store_true", help="raw JSON")
    args = ap.parse_args(argv)

    products = load_products()
    by_type = {}
    for p in products:
        by_type.setdefault(p.get("type"), []).append(p)

    if args.list_products:
        print(f'{"type":>5}  {"pid":>7}  product')
        for p in sorted(products, key=lambda x: (x.get("type") or 0)):
            print(f'{p.get("type"):>5}  {p["Pid"]:>7}  '
                  f'{p.get("EnName") or p.get("Name") or "?"}')
        return

    types = list(args.type or [])
    if args.scan:
        a, b = args.scan.split("-")
        types = list(range(int(a), int(b) + 1))

    if not types:
        ap.error("need --type, --scan, or --list-products")

    for t in types:
        try:
            entries = versions(t, args.beta)
        except Exception as e:  # noqa: BLE001
            print(f"[{t}] error: {e}", file=sys.stderr)
            continue
        if not entries:
            continue
        names = ", ".join(p.get("EnName") or p.get("Name") or "?"
                          for p in by_type.get(t, [])) or "?"
        print(f"[{t}] {names}  ({len(entries)} build(s))")
        if args.json:
            print(json.dumps(entries, indent=1, ensure_ascii=False))
            continue
        entries.sort(key=lambda e: e["created_at"], reverse=True)
        for e in entries[:1] if args.latest else entries:
            print(f'  {e["created_at"]}  v{e["file_version"]:<6} {e["fileSize"]:>8}B  '
                  f'{e["fileURL"].rsplit("/", 1)[-1]}')
            if (args.latest or args.all) and not args.json:
                grab(e, args.output, args.force)
                print(f'    {e.get("readme_en", "").strip()[:160]}')


if __name__ == "__main__":
    main(sys.argv[1:])
