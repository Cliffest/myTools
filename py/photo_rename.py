"""
Rename photos/videos to "YYYY-MM-DD HHMMSS.ext" based on capture time.

- Images (JPG/HEIC): EXIF DateTimeOriginal -> specify timezone with --taken-UTC
- Videos (MOV/MP4): QuickTime/MP4 creation_time (UTC) -> converted to target UTC offset
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


IMAGE_EXTS = {".JPG", ".JPEG", ".HEIC"}
VIDEO_EXTS = {".MOV", ".MP4"}

NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{6}\.[A-Za-z0-9]+$")


def iter_files(root: Path, recursive: bool) -> Iterable[Path]:
    globber = root.rglob if recursive else root.glob
    for p in globber("*"):
        if not p.is_file():
            continue
        ext = p.suffix.upper()
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
                    return dt  # EXIF is naive datetime (no timezone info)
    except Exception:
        pass
    return None


def run_ffprobe(path: Path) -> list[str]:
    # Query both container- and stream-level tags + Apple QuickTime tag
    cmd = [
        "ffprobe", "-v", "quiet", "-hide_banner",  # Silence harmless QuickTime warning
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


def get_capture_time(path: Path, *, prefer_mtime: bool = False, taken_utc_offset: int = 0, target_utc_offset: int = 0) -> Tuple[Optional[datetime], str]:
    """
    Returns (datetime, source)
    - For images: EXIF naive datetime -> interpret as taken_utc_offset timezone -> convert to target_utc_offset
    - For videos: creation_time (UTC) -> convert to target_utc_offset
    - If prefer_mtime: use file mtime (local) -> convert to target_utc_offset
    """
    ext = path.suffix.upper()
    taken_tz = timezone(timedelta(hours=taken_utc_offset))
    target_tz = timezone(timedelta(hours=target_utc_offset))
    
    # 1) primary metadata
    if ext in IMAGE_EXTS:
        dt = get_image_time_exif(path)
        if dt:
            # EXIF datetime is naive - interpret it as being in the taken_utc_offset timezone
            dt_aware = dt.replace(tzinfo=taken_tz)
            return dt_aware.astimezone(target_tz), "exif"
    elif ext in VIDEO_EXTS:
        dt = get_video_time_ffprobe(path)
        if dt:
            # Video datetime is typically UTC, convert to target UTC offset
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(target_tz), "ffprobe"

    # 2) fallback: mtime
    if prefer_mtime:
        try:
            ts = path.stat().st_mtime
            # mtime is in local timezone, convert to target UTC offset
            local_dt = datetime.fromtimestamp(ts)
            # Get system local timezone
            local_tz = datetime.now().astimezone().tzinfo
            local_dt_aware = local_dt.replace(tzinfo=local_tz)
            return local_dt_aware.astimezone(target_tz), "mtime"
        except Exception:
            pass

    return None, "none"


# ---------- Renaming ----------

def fmt_target_name(dt: datetime, ext: str) -> str:
    # Strip timezone info for filename (we want just the clock time in target timezone)
    dt_naive = dt.replace(tzinfo=None)
    return dt_naive.strftime("%Y-%m-%d %H%M%S") + ext.upper()


def is_already_named(p: Path) -> bool:
    return NAME_RE.match(p.name) is not None


def resolve_collision(dst_dir: Path, base_dt: datetime, ext: str) -> Tuple[datetime, Path]:
    dt = base_dt
    while True:
        candidate = dst_dir / fmt_target_name(dt, ext)
        if not candidate.exists():
            return dt, candidate
        dt = dt + timedelta(seconds=1)


def rename_one(p: Path, *, use_mtime_fallback: bool, taken_utc_offset: int, target_utc_offset: int) -> Tuple[bool, str, Optional[Path]]:
    dt, src = get_capture_time(p, prefer_mtime=use_mtime_fallback, taken_utc_offset=taken_utc_offset, target_utc_offset=target_utc_offset)
    if dt is None:
        return False, f"[SKIP] No datetime for {p}", None

    dst_dir = p.parent
    dt, dst_path = resolve_collision(dst_dir, dt, p.suffix)
    
    # Format UTC offsets for display
    ext = p.suffix.upper()
    if ext in IMAGE_EXTS:
        taken_str = f"UTC{taken_utc_offset:+d}" if taken_utc_offset != 0 else "UTC"
        target_str = f"UTC{target_utc_offset:+d}" if target_utc_offset != 0 else "UTC"
        conversion_info = f"{src}@{taken_str}->{target_str}"
    else:
        target_str = f"UTC{target_utc_offset:+d}" if target_utc_offset != 0 else "UTC"
        conversion_info = f"{src}->{target_str}"
    
    return True, f"[OK]  {p.name} ({conversion_info}) -> {dst_path.name}", dst_path


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
    ap.add_argument("--taken-UTC", type=int, default=8, metavar="OFFSET",
                    help="UTC offset for EXIF photo timestamps (e.g., --taken-UTC 8 if photos taken in UTC+8). Default: 8 (UTC)")
    ap.add_argument("--target-UTC", type=int, default=8, metavar="OFFSET", 
                    help="Target UTC offset for output filenames (e.g., --target-UTC 8 for UTC+8 times). Default: 8 (UTC)")
    args = ap.parse_args()

    root = args.directory.expanduser().resolve()
    if not root.exists():
        print(f"[ERROR] Directory not found: {root}")
        raise SystemExit(2)

    # Validate UTC offsets
    for offset, name in [(args.taken_UTC, "taken-UTC"), (args.target_UTC, "target-UTC")]:
        if not -12 <= offset <= 14:
            print(f"[ERROR] {name} offset must be between -12 and +14, got: {offset}")
            raise SystemExit(2)

    files = list(iter_files(root, args.recursive))
    if args.videos_only:
        files = [p for p in files if p.suffix.upper() in VIDEO_EXTS]
    if args.images_only:
        files = [p for p in files if p.suffix.upper() in IMAGE_EXTS]

    if not files:
        print("[INFO] No media files found.")
        return

    taken_str = f"UTC{args.taken_UTC:+d}" if args.taken_UTC != 0 else "UTC"
    target_str = f"UTC{args.target_UTC:+d}" if args.target_UTC != 0 else "UTC"
    print(f"[INFO] Processing {len(files)} files")
    print(f"[INFO] Photo EXIF timezone: {taken_str}")
    print(f"[INFO] Target output timezone: {target_str}")

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
            taken_utc_offset=args.taken_UTC,
            target_utc_offset=args.target_UTC,
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
            print(f"[ERROR] Rename failed for {p}: {e}")
            failed += 1

    print(f"\nSummary: renamed={renamed}, skipped={skipped}, failed={failed}")


if __name__ == "__main__":
    main()