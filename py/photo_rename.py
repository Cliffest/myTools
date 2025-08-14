#!/usr/bin/env python3
"""
Rename photos/videos to "YYYY-MM-DD HHMMSS.ext" based on capture time.

- Images (JPG/HEIC): EXIF DateTimeOriginal -> local time (naive)
- Videos (MOV/MP4): QuickTime/MP4 creation_time (UTC) -> converted to local
- Optional fallback: file mtime
- If a name collides, increment by +1 second until unique

Requirements:
  pip install "exifread<3"   # for JPG/HEIC EXIF
  # ffprobe must be available on PATH (part of ffmpeg)
"""

from __future__ import annotations
import argparse
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Iterable, Tuple

try:
    import exifread  # type: ignore
except ImportError:  # allow running --videos-only without exifread
    exifread = None  # noqa: N816


IMAGE_EXTS = {".jpg", ".jpeg", ".heic"}
VIDEO_EXTS = {".mov", ".mp4"}

NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{6}\.[A-Za-z0-9]+$")


def iter_files(root: Path, recursive: bool) -> Iterable[Path]:
    globber = root.rglob if recursive else root.glob
    for p in globber("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in IMAGE_EXTS | VIDEO_EXTS:
            yield p


# ---------- Time extraction ----------

def parse_exif_datetime_string(s: str) -> Optional[datetime]:
    # EXIF is typically "YYYY:MM:DD HH:MM:SS"
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None


def get_image_time_exif(path: Path) -> Optional[datetime]:
    if exifread is None:
        return None
    try:
        with path.open("rb") as f:
            tags = exifread.process_file(f, details=False)
        for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
            if key in tags:
                s = tags[key].printable
                dt = parse_exif_datetime_string(s)
                if dt:
                    return dt  # EXIF is usually local wall time (naive)
    except Exception:
        pass
    return None


def run_ffprobe(path: Path) -> list[str]:
    # Query both container- and stream-level tags + Apple QuickTime tag
    cmd = [
        "ffprobe", "-v", #"error",
        "quiet", "-hide_banner",  # Silence harmless QuickTime warning
        "-show_entries",
        "format_tags=creation_time:stream_tags=creation_time:format_tags=com.apple.quicktime.creationdate",
        "-of", "default=nw=1:nk=1",
        str(path),
    ]
    out = subprocess.check_output(cmd, text=True, errors="ignore")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def try_parse_iso_z(s: str) -> Optional[datetime]:
    # Accept ...Z or with timezone offset
    s2 = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)  # assume UTC if naked
        return dt
    except ValueError:
        return None


def try_parse_quicktime_local(s: str) -> Optional[datetime]:
    # e.g., "2021-07-15 12:35:11" (no tz)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)  # naive
        except ValueError:
            pass
    # e.g., "2021-07-15 12:35:11 +0800"
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y/%m/%d %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt)  # aware
        except ValueError:
            pass
    # e.g., "UTC 2021-07-15 12:35:11"
    if s.upper().startswith("UTC "):
        try:
            return datetime.strptime(s[4:], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def get_video_time_ffprobe(path: Path) -> Optional[datetime]:
    try:
        lines = run_ffprobe(path)
    except Exception:
        return None

    for s in lines:
        # Try ISO first (e.g., 2021-07-15T12:35:11.000000Z)
        dt = try_parse_iso_z(s)
        if dt:
            return dt
        # Try QuickTime variants
        dt = try_parse_quicktime_local(s)
        if dt:
            return dt
    return None


def get_capture_time(path: Path, *, prefer_mtime: bool = False) -> Tuple[Optional[datetime], str]:
    """
    Returns (datetime, source)
    - For images: EXIF local naive
    - For videos: creation_time (UTC -> convert to local later)
    - If prefer_mtime: use file mtime if metadata missing
    """
    ext = path.suffix.lower()
    # 1) primary metadata
    if ext in IMAGE_EXTS:
        dt = get_image_time_exif(path)
        if dt:
            return dt, "exif"
    elif ext in VIDEO_EXTS:
        dt = get_video_time_ffprobe(path)
        if dt:
            return dt, "ffprobe"

    # 2) fallback: mtime
    if prefer_mtime:
        try:
            ts = path.stat().st_mtime
            return datetime.fromtimestamp(ts), "mtime"
        except Exception:
            pass

    return None, "none"


# ---------- Renaming ----------

def fmt_target_name(dt: datetime, ext: str) -> str:
    return dt.strftime("%Y-%m-%d %H%M%S") + ext.lower()


def is_already_named(p: Path) -> bool:
    return NAME_RE.match(p.name) is not None


def resolve_collision(dst_dir: Path, base_dt: datetime, ext: str) -> Tuple[datetime, Path]:
    dt = base_dt
    while True:
        candidate = dst_dir / fmt_target_name(dt, ext)
        if not candidate.exists():
            return dt, candidate
        dt = dt + timedelta(seconds=1)


def rename_one(p: Path, *, use_mtime_fallback: bool, to_local_tz: bool) -> Tuple[bool, str, Optional[Path]]:
    dt, src = get_capture_time(p, prefer_mtime=use_mtime_fallback)
    if dt is None:
        return False, f"[SKIP] No datetime for {p}", None

    # If dt is timezone-aware (videos often UTC), convert to local wall time for naming
    if dt.tzinfo is not None:
        if to_local_tz:
            dt = dt.astimezone().replace(tzinfo=None)
        else:
            # strip tz to keep UTC clock time as-is (rarely desired)
            dt = dt.replace(tzinfo=None)

    dst_dir = p.parent
    dt, dst_path = resolve_collision(dst_dir, dt, p.suffix)
    return True, f"[OK]  {p.name} ({src}) -> {dst_path.name}", dst_path


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Rename photos/videos to 'YYYY-MM-DD HHMMSS.ext' by capture time.")
    ap.add_argument("directory", type=Path, help="Root directory to process")
    ap.add_argument("-r", "--recursive", action="store_true", help="Recurse into subdirectories")
    ap.add_argument("-n", "--dry-run", action="store_true", help="Show what would happen without renaming")
    ap.add_argument("--use-mtime-fallback", action="store_true", help="If metadata missing, use file mtime")
    ap.add_argument("--videos-only", action="store_true", help="Process only videos (MOV/MP4)")
    ap.add_argument("--images-only", action="store_true", help="Process only images (JPG/HEIC)")
    ap.add_argument("--skip-already-named", action="store_true", help="Skip files already in target name pattern")
    ap.add_argument("--keep-tree", action="store_true", help="Keep files in place (default).")
    ap.add_argument("--to-local-tz", action="store_true",
                    help="Convert timezone-aware video timestamps (usually UTC) to local time before naming")
    args = ap.parse_args()

    root = args.directory.expanduser().resolve()
    if not root.exists():
        print(f"[ERR] Directory not found: {root}")
        raise SystemExit(2)

    files = list(iter_files(root, args.recursive))
    if args.videos_only:
        files = [p for p in files if p.suffix.lower() in VIDEO_EXTS]
    if args.images_only:
        files = [p for p in files if p.suffix.lower() in IMAGE_EXTS]

    if not files:
        print("[INFO] No media files found.")
        return

    renamed = 0
    skipped = 0
    failed = 0

    for p in sorted(files):
        if args.skip_already_named and is_already_named(p):
            print(f"[SKIP] Already named: {p.name}")
            skipped += 1
            continue

        ok, msg, dst = rename_one(
            p,
            use_mtime_fallback=args.use_mtime_fallback,
            to_local_tz=args.to_local_tz,
        )
        print(msg)
        if not ok:
            failed += 1
            continue

        if args.dry_run:
            skipped += 1
            continue

        try:
            p.rename(dst)  # in-place rename
            renamed += 1
        except Exception as e:
            print(f"[ERR] Rename failed for {p}: {e}")
            failed += 1

    print(f"\nSummary: renamed={renamed}, skipped={skipped}, failed={failed}")


if __name__ == "__main__":
    main()