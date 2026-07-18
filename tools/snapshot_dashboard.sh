#!/bin/sh
# Resilient dashboard snapshot. Captures the live build-in-public dashboard (:8360) as a
# self-contained static page so hailports.com/live keeps serving the last-good view even
# if the entire stack dies. Atomic write + completeness check so a half/failed fetch never
# replaces a good snapshot (it keeps the last good one instead).
SEO="$HOME/claude-stack/data/hustle/seo_pages/live"
mkdir -p "$SEO"
TMP="$SEO/.index.tmp.$$"
if curl -sS -m 12 "http://127.0.0.1:8360/" -o "$TMP" 2>/dev/null && [ -s "$TMP" ] && grep -qi "</html>" "$TMP"; then
  mv "$TMP" "$SEO/index.html"
else
  rm -f "$TMP"   # fetch failed/torn -> keep the last good snapshot
fi
