#!/bin/bash
# One-shot: arm RLA_MODE_IPAD=1600x1200:hidpi, but only once no VNC viewer is
# connected (never rebuild the display under a live viewer). Verifies the mode
# actually landed and self-reverts if the dummy refuses the 3200x2400 backing.
# Deletes itself from launchd once done. Safe to re-run.
set -u
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PLIST="$HOME/Library/LaunchAgents/com.claude-stack.ipad-display-lock.plist"
JOB="gui/501/com.claude-stack.ipad-display-lock"
LOG="$HOME/.display-profile-router/ipad-1600-arm.log"
BAK="$(ls -t "$PLIST".bak-ipad1600-* 2>/dev/null | head -1)"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

# root-owned screensharingd sockets are invisible to unprivileged lsof -- use netstat
viewer_connected() {
  netstat -an 2>/dev/null \
    | awk '$1 ~ /^tcp/ && $4 ~ /\.5900$/ && $6 == "ESTABLISHED" {found=1} END{exit !found}'
}

looks_like() {
  system_profiler SPDisplaysDataType 2>/dev/null \
    | awk -F'[ :x]+' '/UI Looks like/ {print $5"x"$6; exit}'
}

clear_idle=0
while true; do
  if viewer_connected; then
    clear_idle=0
  else
    clear_idle=$((clear_idle + 1))
  fi

  # two consecutive clear samples -> the session is really gone, not mid-reconnect
  if [ "$clear_idle" -ge 2 ]; then
    log "no viewer for 2 samples -- arming 1600x1200:hidpi"
    launchctl kickstart -k "$JOB" >/dev/null 2>&1
    sleep 12

    got="$(looks_like)"
    # Accept ANY real HiDPI mode the dummy grants (1600x1200 ideal, 1376x1032 the
    # common fallback) — anything beats reverting to the degraded 800x600 lock, which
    # was the actual bug: a good 1376x1032 got thrown away for insisting on 1600x1200.
    if [ -n "$got" ] && [ "$got" != "800x600" ]; then
      log "OK armed: UI looks like $got"
      launchctl bootout gui/501/com.claude-stack.ipad-1600-arm >/dev/null 2>&1
      rm -f "$HOME/Library/LaunchAgents/com.claude-stack.ipad-1600-arm.plist"
      exit 0
    fi

    log "FAIL: expected 1600x1200, got '$got' -- reverting"
    if [ -n "$BAK" ] && [ -f "$BAK" ]; then
      cp "$BAK" "$PLIST" && launchctl kickstart -k "$JOB" >/dev/null 2>&1
      log "reverted to $BAK"
    fi
    launchctl bootout gui/501/com.claude-stack.ipad-1600-arm >/dev/null 2>&1
    rm -f "$HOME/Library/LaunchAgents/com.claude-stack.ipad-1600-arm.plist"
    exit 1
  fi
  sleep 15
done
