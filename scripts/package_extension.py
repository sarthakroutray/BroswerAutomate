#!/usr/bin/env python3
"""Zip the Chrome extension for attaching to a GitHub Release.

Reads the version from extension/manifest.json and writes
dist/browser-extension-<version>.zip containing the extension files
at the zip root (so it can be unzipped and loaded via
chrome://extensions → Load unpacked).

Stdlib only — no dependencies.
"""

import json
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT_DIR = ROOT / "extension"
DIST_DIR = ROOT / "dist"

SKIP_NAMES = {".DS_Store", "Thumbs.db"}


def main() -> None:
    manifest = json.loads((EXT_DIR / "manifest.json").read_text())
    version = manifest["version"]
    DIST_DIR.mkdir(exist_ok=True)
    out = DIST_DIR / f"browser-extension-{version}.zip"
    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(EXT_DIR.rglob("*")):
            if path.is_dir() or path.name in SKIP_NAMES:
                continue
            z.write(path, path.relative_to(EXT_DIR))

    print(f"Wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
