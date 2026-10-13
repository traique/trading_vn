"""Build the Botkeep upload: only what runs, within 250 files and 4 MB.

    python scripts/build_botkeep_bundle.py

Writes ``dist/botkeep/`` (the folder to deploy) and
``dist/tradingagents-vn-botkeep.zip`` (the same, zipped for upload), then fails
if either limit is crossed. Tests, docs, images, the CLI and the reference
repos are left out; dependencies are installed by Botkeep from
``requirements.txt``.
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
OUT = DIST / "botkeep"
ZIP = DIST / "tradingagents-vn-botkeep.zip"
MAX_FILES = 250
MAX_BYTES = 4 * 1024 * 1024

PACKAGES = ("tradingagents", "vnbot")
EXTRA = {
    "main_botkeep.py": "main_botkeep.py",
    "deploy/botkeep/requirements.txt": "requirements.txt",
    "deploy/botkeep/.env.example": ".env.example",
    "deploy/botkeep/README.md": "README.md",
    "LICENSE": "LICENSE",
}
SKIP_DIRS = {"__pycache__"}
KEEP_SUFFIXES = {".py", ".svg"}


def collect() -> list[tuple[Path, str]]:
    files = []
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*")):
            if path.is_file() and path.suffix in KEEP_SUFFIXES and not SKIP_DIRS & set(path.parts):
                files.append((path, path.relative_to(ROOT).as_posix()))
    for source, target in EXTRA.items():
        files.append((ROOT / source, target))
    return files


def main() -> int:
    files = collect()
    if OUT.exists():
        shutil.rmtree(OUT)
    for source, target in files:
        dest = OUT / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as archive:
        for _source, target in files:
            archive.write(OUT / target, target)

    raw = sum((OUT / t).stat().st_size for _, t in files)
    zipped = ZIP.stat().st_size
    print(f"files: {len(files)} (limit {MAX_FILES})")
    print(f"size: {raw / 1024:.0f} KB unpacked, {zipped / 1024:.0f} KB zipped (limit {MAX_BYTES // 1024} KB)")
    print(f"folder: {OUT}\nzip:    {ZIP}")
    problems = []
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files > {MAX_FILES}")
    if max(raw, zipped) > MAX_BYTES:
        problems.append(f"{max(raw, zipped)} bytes > {MAX_BYTES}")
    if problems:
        print("OVER THE BOTKEEP LIMIT: " + "; ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
