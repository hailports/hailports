#!/bin/zsh
# llama_reaper: SIGKILL any llama-server that is NOT a descendant of the live
# `ollama serve` process AND is holding real model memory (RSS > 512MB).
# This targets leaked/orphaned model servers that ollama lost track of
# (invisible to `ollama ps`, unreachable by `ollama stop`) while sparing
# healthy managed servers and tiny transient spawns.
# Run on a short interval via launchd.

set -u
RSS_MIN_KB=524288   # 512MB floor: below this it's a transient helper, ignore
LOG="$HOME/.openclaw/workspace/CompanyA-local/digests/llama_reaper.log"

OLLAMA_PID=$(pgrep -f "Contents/Resources/ollama serve" | head -1)

is_managed() {
  # returns 0 if pid's ancestry reaches OLLAMA_PID
  local pid=$1
  [ -n "$OLLAMA_PID" ] || return 1
  while [ "${pid:-0}" -gt 1 ] 2>/dev/null; do
    [ "$pid" = "$OLLAMA_PID" ] && return 0
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    [ -z "$pid" ] && return 1
  done
  return 1
}

# iterate every llama-server process
ps -o pid=,rss= -ax -o command= 2>/dev/null | grep '[l]lama-server' | while read pid rss rest; do
  [ "${rss:-0}" -ge "$RSS_MIN_KB" ] 2>/dev/null || continue
  if ! is_managed "$pid"; then
    kill -9 "$pid" 2>/dev/null \
      && echo "$(date '+%Y-%m-%d %H:%M:%S') REAPED orphan llama-server pid=$pid rss=${rss}KB (ollama_serve=${OLLAMA_PID:-none})" >> "$LOG"
  fi
done
exit 0
