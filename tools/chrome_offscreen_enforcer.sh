#!/bin/bash
# Keep automation Chrome windows OFF-SCREEN (hard rule: headless/off-screen default).
# Skips while an INTERACTIVE login/capture is running so it never fights a real login.
if pgrep -fl "login_capture|signup_yelp|gumroad_login|reddit_login|persona3_login|import_social_session|grab_chrome_cookies|youtube_uploader.*--interactive" >/dev/null 2>&1; then
  exit 0
fi
osascript -e 'tell application "System Events"
  repeat with p in (every process whose name contains "Chrome")
    repeat with w in (every window of p)
      try
        if (item 1 of (position of w)) > -5000 then set position of w to {-32000, -32000}
      end try
    end repeat
  end repeat
end tell' >/dev/null 2>&1
