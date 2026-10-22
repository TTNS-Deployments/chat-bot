#!/usr/bin/env python3
"""Build build/webapp.zip, the package deployed to App Service.

    python3 Deployment/scripts/package.py            # code only; App Service installs dependencies
    python3 Deployment/scripts/package.py --vendor   # code + dependencies in .packages (run-from-package)

The package keeps the repository layout (requirements.txt at the root, Code/backend, Code/frontend,
Data/sample_docs), so the startup command is the same for every deployment method.

--vendor is for deployments without a remote build, such as Azure Cloud Shell, where the App Service
deployment (Kudu) endpoints cannot be reached. It downloads Linux x86-64 wheels for Python 3.12 into
.packages; the app setting PYTHONPATH=/home/site/wwwroot/.packages makes them importable.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INCLUDE = ["requirements.txt", "Code/backend/requirements.txt", "Code/backend/chatbot", "Code/frontend", "Data/sample_docs"]
SKIP_PARTS = {"__pycache__", ".pytest_cache", "tests"}


def files():
    for item in INCLUDE:
        path = ROOT / item
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for p in candidates:
            if not SKIP_PARTS.intersection(p.relative_to(ROOT).parts) and p.suffix != ".pyc":
                yield p


PIP_PLATFORM_FLAGS = [
    "--only-binary=:all:", "--implementation", "cp", "--python-version", "3.12",
    "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_17_x86_64", "--platform", "manylinux_2_28_x86_64",
]


def vendor(target: Path) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    *PIP_PLATFORM_FLAGS, "--target", str(target), "-r", str(ROOT / "Code" / "backend" / "requirements.txt")],
                   check=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    with_packages = "--vendor" in args
    out = ROOT / "build" / "webapp.zip"
    out.parent.mkdir(exist_ok=True)
    count = 0
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files():
            zf.write(p, p.relative_to(ROOT).as_posix())
            count += 1
        if with_packages:
            packages = Path(tmp) / ".packages"
            print("Downloading dependencies for Linux / Python 3.12 into .packages ...")
            vendor(packages)
            for p in sorted(packages.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    zf.write(p, (Path(".packages") / p.relative_to(packages)).as_posix())
                    count += 1
    size = out.stat().st_size / 1_048_576
    print(f"Built {out.relative_to(ROOT)} ({count} files, {size:.1f} MB{', with dependencies' if with_packages else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
