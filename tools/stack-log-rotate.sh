#!/bin/bash
# stack-log-rotate.sh — perf-neutral log rotation for the claude-stack.
# Compresses large logs to the external T7, then truncates the live file IN PLACE
# (`: > file`) so the inode is preserved and any running process keeps logging
# without interruption. If T7 is not mounted, falls back to a tail-trim so the
# internal disk can never fill. Safe to run repeatedly.
set -u

EXT_ROOT="/Volumes/External/logs/rotated-archive"
THRESH_MB=10
KEEP_DAYS=30
KEEP_BYTES=2000000
LOGDIRS=(
  "$HOME/Library/Logs/claude-stack"
  "$HOME/claude-stack/data/logs"
  "$HOME/Library/Logs/openclaw"
)
# data/ root holds append-only .jsonl (metrics.jsonl et al) outside data/logs
FLATDIRS=(
  "$HOME/claude-stack/data"
)

have_t7=0
if [ -d /Volumes/External ] && touch /Volumes/External/.rwtest 2>/dev/null; then
  rm -f /Volumes/External/.rwtest
  mkdir -p "$EXT_ROOT" 2>/dev/null && have_t7=1
fi

ts() { date +%Y%m%d-%H%M%S; }

trim_in_place() {
  # Bound by BYTES, not lines: a fat-lined .jsonl (metrics.jsonl carries a top_procs
  # blob per record) blows past any sane line cap. tail -n +2 drops the leading
  # partial line so .jsonl stays parseable. Preserves inode; live writers unaffected.
  tail -c "$KEEP_BYTES" "$1" 2>/dev/null | tail -n +2 > "$1.rotate.tmp" \
    && cat "$1.rotate.tmp" > "$1" && rm -f "$1.rotate.tmp"
}

rotate_file() {
  f="$1"
  [ -f "$f" ] || return 0
  if [ "$have_t7" = 1 ] && gzip -c "$f" > "$EXT_ROOT/$(basename "$f").$(ts).gz" 2>/dev/null; then
    case "$f" in
      # .jsonl consumers read a recent window; keep it rather than zeroing
      *.jsonl) trim_in_place "$f" ;;
      *)       : > "$f" ;;
    esac
  else
    trim_in_place "$f"
  fi
}

for d in "${LOGDIRS[@]}"; do
  [ -d "$d" ] || continue
  while IFS= read -r f; do rotate_file "$f"; done < <(
    find "$d" -type f \( -name '*.log' -o -name '*.jsonl' \) -size +${THRESH_MB}M 2>/dev/null)
done

for d in "${FLATDIRS[@]}"; do
  [ -d "$d" ] || continue
  while IFS= read -r f; do rotate_file "$f"; done < <(
    find "$d" -maxdepth 1 -type f \( -name '*.log' -o -name '*.jsonl' \) -size +${THRESH_MB}M 2>/dev/null)
done

# prune old archives on T7
if [ "$have_t7" = 1 ]; then
  find "$EXT_ROOT" -type f -name '*.gz' -mtime +${KEEP_DAYS} -delete 2>/dev/null
fi
exit 0
