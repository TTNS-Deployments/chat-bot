#!/usr/bin/env python3
"""Build build/webapp.zip, the package deployed to App Service.

The package keeps the repository layout (requirements.txt at the root, Code/backend, Code/frontend,
Data/sample_docs), so the startup command is the same for zip deploys and Git deploys. Dependencies
are installed by App Service during deployment (remote build), so packaging needs nothing but Python.
"""
from __future__ import annotations

import sys
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


def main() -> int:
    out = ROOT / "build" / "webapp.zip"
    out.parent.mkdir(exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files():
            zf.write(p, p.relative_to(ROOT).as_posix())
            count += 1
    print(f"Built {out.relative_to(ROOT)} ({count} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
