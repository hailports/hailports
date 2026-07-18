#!/bin/bash
# HiDPI display-override sentinel.
#
# Guards the native /Library/Displays scale-resolutions override that gives the
# dummy (HDP-V104) its 16:10 Retina modes (see memory
# project_native_hidpi_override_works_on_m4_2026-07-03). Any cleaner, macOS
# update, or the SwitchResX uninstaller can delete /Library/Displays — this
# restores it byte-for-byte from the canonical critical-mirror backup.
#
# FILE-ONLY + wedge-safe: it never runs displayplacer / never reshapes the live
# screen (that would fight remote_link_adapter and violate the "no live rebuild
# while connected" rule). The restored file just makes the NEXT display-detect
# (reboot / hotplug / wake) advertise the HiDPI modes again.
#
# Runs as root (system LaunchDaemon com.claude-stack.hidpi-override-sentinel).
set -uo pipefail

OVR="/Library/Displays/Contents/Resources/Overrides/DisplayVendorID-843/DisplayProductID-104"
BAK="/home/user/.claude-stack-critical-mirror/display_hidpi_override/DisplayVendorID-843__DisplayProductID-104.plist"
LOG="/home/user/Library/Logs/claude-stack/hidpi-override-sentinel.log"
HB="/home/user/Library/Logs/claude-stack/.hidpi-sentinel.heartbeat"

log(){ printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

# Backup is the single source of truth. If IT is gone we can't heal — shout once.
if [ ! -f "$BAK" ]; then log "FATAL: canonical backup missing at $BAK — cannot guard override"; exit 1; fi
BAKSHA=$(shasum -a256 "$BAK" | awk '{print $1}')

: > "$HB" 2>/dev/null || true   # heartbeat: prove the sentinel ran this tick

if [ -f "$OVR" ]; then NOW=$(shasum -a256 "$OVR" | awk '{print $1}'); else NOW=""; fi

if [ "$NOW" = "$BAKSHA" ]; then
  exit 0   # intact — stay silent to keep the log near-empty
fi

# Missing or drifted -> restore from canonical backup.
mkdir -p "$(dirname "$OVR")"
if cp "$BAK" "$OVR" && chown root:wheel "$OVR" && chmod 644 "$OVR"; then
  if [ -z "$NOW" ]; then log "RESTORED override (was MISSING) from backup $BAKSHA"; else log "RESTORED override (drifted was=$NOW) from backup $BAKSHA"; fi
else
  log "ERROR: restore failed (was=${NOW:-MISSING})"; exit 1
fi

# bounded log
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1000000 ]; then tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; fi
exit 0
