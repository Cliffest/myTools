#!/usr/bin/env bash
# rsync_git.sh — Incremental git project sync (remote ←→ local)
#
# Purpose : Sync a git project between remote and local, honoring
#           the source's .gitignore (skips outputs, model weights, etc.).
# Why     : For frequent in-day syncs that aren't worth a commit/push.
# SSH     : Uses ~/.ssh/config host aliases (no password / key flags here).
# Speed   : Light, fast, safe-by-default (dry-run preview before apply).

set -euo pipefail

# ── Usage ─────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $(basename "$0") <SRC> <DST> [-y] [--git]

  SRC, DST   Either a local path or 'sshhost:path' (uses ~/.ssh/config).
             A trailing slash on SRC syncs CONTENTS of the directory.
  -y         Skip confirmation (for unattended/frequent runs).
  --git      Include the .git/ directory (default: excluded).

Examples:
  $(basename "$0") remote:/remote/git/dir/ /local/git/dir/
  $(basename "$0") ./ remote:~/dir/ -y
  $(basename "$0") ./ remote:~/dir/ -y --git
EOF
  exit "${1:-0}"
}

# ── Args ──────────────────────────────────────────────────────────────
ASSUME_YES=0
INCLUDE_GIT=0
POS=()
for arg in "$@"; do
  case "$arg" in
    -y)        ASSUME_YES=1 ;;
    --git)     INCLUDE_GIT=1 ;;
    -h|--help) usage 0 ;;
    -*)        echo "Unknown option: $arg" >&2; usage 1 ;;
    *)         POS+=("$arg") ;;
  esac
done

[[ ${#POS[@]} -eq 2 ]] || usage 1
SRC="${POS[0]}"
DST="${POS[1]}"

# ── Locate .gitignore for the filter ──────────────────────────────────
# rsync's `:- .gitignore` reads .gitignore files relative to the transfer
# root on the SOURCE side. For a remote source rsync handles this on the
# remote shell automatically. For a local source we just need the file
# present at the root, which is the normal case for a git project.
RSYNC_OPTS=(
  -avzh
  --partial
  --delete
  --filter=':- .gitignore'
)
[[ "${INCLUDE_GIT}" -eq 0 ]] && RSYNC_OPTS+=(--exclude='.git/')

# ── Step 1: Dry-run preview ───────────────────────────────────────────
echo "=== DRY RUN: ${SRC}  →  ${DST} ==="
rsync "${RSYNC_OPTS[@]}" -n --stats "${SRC}" "${DST}"

# ── Step 2: Confirm and execute ───────────────────────────────────────
if [[ "${ASSUME_YES}" -eq 0 ]]; then
  read -rp $'\nProceed with actual sync? [y/N]: ' confirm
  [[ "${confirm}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

echo "=== SYNCING ==="
rsync "${RSYNC_OPTS[@]}" --progress "${SRC}" "${DST}"
echo "=== DONE: $(date '+%Y-%m-%d %H:%M:%S') ==="
