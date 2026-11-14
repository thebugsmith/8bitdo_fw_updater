#!/usr/bin/env python3
"""Create a project-local environment on Windows, macOS or Linux."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import venv


def interpreter_path(root: Path, host: str | None = None) -> Path:
    host = sys.platform if host is None else host
    return root / ".venv" / ("Scripts/python.exe" if host == "win32" else "bin/python")


def install(root: Path) -> Path:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required; Python 3.12 is recommended")
    if sys.platform not in ("win32", "darwin") and not sys.platform.startswith("linux"):
        raise RuntimeError("Use native Windows, macOS or Linux Python")
    python = interpreter_path(root)
    environment = root / ".venv"
    if environment.exists():
        if not python.is_file():
            raise RuntimeError("The existing .venv does not match this OS. Move it aside, then run install.py again")
    else:
        print(f"Creating environment: {environment}", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    # Verify the reused environment rather than assuming it matches the launcher.
    subprocess.run([str(python), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"], check=True)
    requirements = root / "requirements.txt"
    subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:",
                    "-r", str(requirements)], check=True)
    return python


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        python = install(Path(__file__).resolve().parent)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        print("A matching binary wheel is required. Try Python 3.12 with an OS-native architecture "
              "(Windows x64, or Intel/Apple Silicon macOS). No system driver was changed.", file=sys.stderr)
        return 1
    print(f"Ready. Python: {python}")
    print("Next: eightbitdo.cmd doctor" if sys.platform == "win32" else "Next: sh ./eightbitdo doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
