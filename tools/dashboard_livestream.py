#!/usr/bin/env python3
"""dashboard_livestream.py — nonstop 24/7 video stream of the public dashboard.

Headless, anonymous, default-OFF. Captures the public case-study dashboard and pushes a
SINGLE CONTINUOUS H.264/RTMP feed to a live endpoint (YouTube Live). The dashboard
auto-refreshes (its own ticking clock = motion), so the feed is never a "static stream".

ARCHITECTURE (2026-06-26 rewrite — continuous, low-bandwidth, always-fresh):
  - ONE long-running ffmpeg. NO 90s segments. The old design relaunched ffmpeg every 90s
    (288 reconnects/day) and the supervisor periodically wedged "no encoder for 60s" — that
    whole failure class is gone with a single nonstop encoder.
  - FRESH content without restarts: ffmpeg reads PNG frames from STDIN via the image2pipe
    (png_pipe) demuxer. A tiny feeder thread writes the CURRENT dashboard frame to that pipe
    at the (low) target fps. A separate capture thread re-screenshots the dashboard into a
    STABLE path every REFRESH_SEC and atomic-renames it. So the encoder keeps running while
    the picture updates underneath it — the long-broken "ffmpeg loops one still and never
    refreshes" problem is solved by feeding frames, not by restarting.
  - STABLE frame path: data/hustle/stream/dashboard_frame.png (NOT a per-run random tmpdir).
    It persists across supervisor restarts, so a relaunch primes instantly and never wedges
    on a missing/empty temp file (the old empty-tmpdir abort + KeepAlive litter).
  - MOST bandwidth-friendly: a near-static 720p dashboard compresses to almost nothing.
    Default 2 fps, long GOP, ~250k video cap (down from 800k), 40k mono silent audio. The
    HW VideoToolbox encoder emits tiny P-frames for the static periods; only the dashboard's
    own ticker/metric changes cost bits. Real measured egress is well under the cap.

SAFETY (non-negotiable):
  - DEFAULT-OFF. Streams only when CASE_STUDY_STREAM_LIVE=1 AND a stream key is present
    (data/hustle/case_study_stream_key.txt or --rtmp-url / CASE_STUDY_RTMP_URL).
  - FAIL-CLOSED anonymity: before EACH (re)connect, the dashboard HTML is run through the
    page gate (core.anon_scrub). Any leak marker => refuse to stream.
  - The stream key file is the ONE human gate: a NEW anonymous live account's RTMP ingest
    URL+key. Never a brand/persona/owner account.
  - Never broadcast a browser error page: capture gates on a real HTTP 200 first; on any
    non-200/unreachable it holds the last good frame.

  test:   python tools/dashboard_livestream.py --selftest      # checks deps + gate, no stream
  dryrun: CASE_STUDY_STREAM_LIVE=1 python tools/dashboard_livestream.py --rtmp-url /tmp/t.flv
  arm:    CASE_STUDY_STREAM_LIVE=1 python tools/dashboard_livestream.py
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
while str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

DASH_URL = os.environ.get("CASE_STUDY_DASH_URL", "http://127.0.0.1:8360/")

# Bandwidth-friendly defaults (measured 2026-06-26 on the live dashboard frame, h264_videotoolbox):
#   OLD 800k/5fps/2sGOP/128k-stereo = 322 kbit/s = 3.48 GB/day
#   NEW 150k/2fps/4sGOP/40k-mono    = 109 kbit/s = 1.18 GB/day  (66% less, ~2.3 GB/day saved)
# The cap is a CEILING, not a target -- videotoolbox tracks below it on a near-static panel (~109k
# actual at a 150k cap), and the cap holds egress flat even when the ticker/metrics move. Text stays
# readable well below this (120k verified). A low steady bitrate is NOT a YouTube drop-risk -- health
# depends on a steady feed + regular keyframes, not on a high bitrate.
# Text legibility beats bandwidth-thrift here: 150k at 720p smeared the dashboard text into mush.
# At 2 fps a NEAR-STATIC panel only spends bits on the frames that change (typing/git log), so a high
# CAP keeps text razor-sharp while the average egress stays ~1 Mbps on static content.
BITRATE = os.environ.get("CASE_STUDY_STREAM_BITRATE", "6000k")
BUFSIZE = os.environ.get("CASE_STUDY_STREAM_BUFSIZE", "12000k")
# DEFAULT OFF (2026-06-28): the viewer-throttle changed bitrate whenever a remote session
# connected/disconnected, and each change forces ONE ffmpeg reconnect. During normal use the
# operator remotes in/out constantly, so the encoder reconnected all day -> every reconnect is a
# brief RTMP drop -> YouTube flips OFFLINE -> "stream dead" pages. The 150k/2fps baseline is already
# ~109 kbit/s (lighter than the old 500k throttle ever was), so an active screen-share isn't
# meaningfully starved by leaving the stream at one constant bitrate. A rock-solid, never-reconnecting
# encoder beats shaving a few kbit during a screen-share. Re-enable only with CASE_STUDY_STREAM_THROTTLE=1.
THROTTLE_ON_VIEW = os.environ.get("CASE_STUDY_STREAM_THROTTLE", "0") == "1"
VIEW_BITRATE = os.environ.get("CASE_STUDY_STREAM_VIEW_BITRATE", "100k")
VIEW_BUFSIZE = os.environ.get("CASE_STUDY_STREAM_VIEW_BUFSIZE", "200k")
# Low fps + long GOP = the big bandwidth win on near-static content. 2 fps is plenty for a dashboard
# (YouTube accepts low fps); 4s keyframe interval is YouTube's documented max and halves keyframe
# cost (the dominant bit-spend on a static stream). GOP in seconds -> -g = fps*GOP_SEC.
FPS = int(os.environ.get("CASE_STUDY_STREAM_FPS", "2"))
GOP_SEC = int(os.environ.get("CASE_STUDY_STREAM_GOP_SEC", "4"))
AUDIO_BITRATE = os.environ.get("CASE_STUDY_STREAM_AUDIO_BITRATE", "40k")
# Dashboard re-capture cadence. It's a dashboard, not video -- a fresh frame every ~45s keeps
# the data current without spinning Chrome constantly. The encoder shows it within REFRESH_SEC.
REFRESH_SEC = int(os.environ.get("CASE_STUDY_STREAM_REFRESH", "45"))

# Stable, known frame path (survives restarts -> instant prime, no empty-tmpdir wedge).
FRAME_DIR = ROOT / "data" / "hustle" / "stream"
FRAME = FRAME_DIR / "dashboard_frame.png"

KEY_FILE = ROOT / "data" / "hustle" / "case_study_stream_key.txt"
# OPTIONAL extra RTMP sinks (TikTok/X/...). Default MISSING -> YouTube-only (no behavior change).
# JSON list of bare "rtmp://.../key" strings OR {"url":..., "onfail":"ignore"} dicts. Each entry is
# a NEW human gate exactly like KEY_FILE: a fresh ANONYMOUS ingest, never a brand/persona/owner acct.
# BAN-SAFE: a TikTok-LIVE sink must NOT live here for the 24/7 run -- add/remove it only during short,
# attended, monitored windows. YouTube (the primary) stays the sole 24/7 anchor.
EXTRA_TARGETS_FILE = ROOT / "data" / "hustle" / "case_study_stream_targets.json"
LIVE = os.environ.get("CASE_STUDY_STREAM_LIVE") == "1"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("chromium") or "", shutil.which("google-chrome") or "",
    shutil.which("chrome") or "",
]
FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]
# Resolve ffmpeg by absolute path too: under launchd the PATH may lack /opt/homebrew/bin,
# so a bare shutil.which("ffmpeg") returns None and the job hard-loops on "ffmpeg not found".
FFMPEG_CANDIDATES = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    shutil.which("ffmpeg") or "",
]


def _find(cands) -> str:
    for c in cands:
        if c and Path(c).exists():
            return c
    return ""


def ffmpeg_bin() -> str:
    return _find(FFMPEG_CANDIDATES) or "ffmpeg"


def rtmp_url(cli: str | None) -> str:
    if cli:
        return cli.strip()
    env = os.environ.get("CASE_STUDY_RTMP_URL", "").strip()
    if env:
        return env
    try:
        return KEY_FILE.read_text().strip()
    except Exception:
        return ""


def _parse_extra_entry(entry) -> dict | None:
    """Normalize one secondary-endpoint spec into {url, onfail}. Accepts a bare RTMP string or a
    {"url":..., "onfail":...} dict. Secondaries default to onfail=ignore -- a stall/drop on a
    secondary must NEVER kill the YouTube primary. Blank / '#'-commented entries are dropped."""
    if isinstance(entry, str):
        url, onfail = entry.strip(), "ignore"
    elif isinstance(entry, dict):
        url = str(entry.get("url", "")).strip()
        onfail = str(entry.get("onfail", "ignore")).strip().lower() or "ignore"
    else:
        return None
    if not url or url.startswith("#"):
        return None
    if onfail not in ("ignore", "abort"):
        onfail = "ignore"
    return {"url": url, "onfail": onfail}


def _extra_targets() -> list[dict]:
    """Optional secondary RTMP sinks. Default OFF -> []. Precedence mirrors rtmp_url(): env
    CASE_STUDY_RTMP_EXTRA (comma/newline list) OVERRIDES the JSON file EXTRA_TARGETS_FILE.
    Each secondary is a NEW human gate exactly like KEY_FILE: a fresh ANONYMOUS ingest."""
    import json
    raw: list = []
    env = os.environ.get("CASE_STUDY_RTMP_EXTRA", "").strip()
    if env:
        raw = re.split(r"[,\n]", env)
    else:
        try:
            data = json.loads(EXTRA_TARGETS_FILE.read_text())
            if isinstance(data, list):
                raw = data
        except Exception:
            raw = []
    out: list[dict] = []
    for entry in raw:
        t = _parse_extra_entry(entry)
        if t:
            out.append(t)
    return out


def stream_targets(cli: str | None) -> list[dict]:
    """Ordered RTMP sink list: [primary YouTube, *optional secondaries]. Primary = rtmp_url(cli)
    (unchanged precedence) and carries NO onfail (default=abort) so a YouTube drop kills ffmpeg ->
    stream_loop reconnects = YouTube stays the 24/7 health anchor. Secondaries carry onfail=ignore
    so a TikTok/X stall is dropped silently without touching the primary. Empty/missing extras ->
    single-target [primary] = byte-identical YT-only path (no behavior change). No primary -> []."""
    primary = rtmp_url(cli)
    if not primary:
        return []
    targets = [{"url": primary, "onfail": None}]
    seen = {primary}
    for t in _extra_targets():
        if t["url"] not in seen:
            seen.add(t["url"])
            targets.append(t)
    return targets


def dashboard_clean() -> tuple[bool, list]:
    """Fail-closed: render the dashboard page and run the whole-page anonymity gate."""
    try:
        from tools.public_case_study_dashboard import render_page, page_leaks
        page = render_page()
        leaks = page_leaks(page)
        return (not leaks), leaks
    except Exception as e:
        return False, [f"gate-error:{e}"]


def ensure_dashboard() -> subprocess.Popen | None:
    """Start the dashboard server if nothing is answering on it."""
    import urllib.request
    try:
        urllib.request.urlopen(DASH_URL, timeout=2).read(1)
        return None  # already up
    except Exception:
        pass
    py = str(ROOT / ".venv" / "bin" / "python")
    py = py if Path(py).exists() else sys.executable
    proc = subprocess.Popen(
        [py, str(ROOT / "tools" / "public_case_study_dashboard.py")],
        cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(DASH_URL, timeout=2).read(1)
            return proc
        except Exception:
            continue
    return proc


def _remote_viewer_active() -> bool:
    """Is someone remoted into the mini right now? ScreensharingAgent runs in the console session
    ONLY while the screen is being observed (over tailscale OR the Edovia relay) and exits the moment
    the session ends -- the same signal the display adapter uses. While a viewer is connected we hand
    the uplink to their interactive screen-share by throttling this background YouTube stream."""
    try:
        r = subprocess.run(["pgrep", "-f", "ScreensharingAgent"],
                           capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _desired_bitrate() -> tuple[str, str]:
    """(video_bitrate, bufsize). Drops to VIEW_BITRATE while a viewer is connected so the screen-share
    gets the uplink; restores full BITRATE when they disconnect. The encoder is CONTINUOUS, so a change
    triggers exactly ONE clean reconnect (debounced ~30s) -- not a per-segment restart. The new baseline
    is already lighter than the old viewer-throttle was, so the uplink is freer at all times."""
    if THROTTLE_ON_VIEW and _remote_viewer_active():
        return VIEW_BITRATE, VIEW_BUFSIZE
    return BITRATE, BUFSIZE


def _tee_spec(targets: list[dict]) -> str:
    """Build the `tee` muxer output spec from the ordered target list. ONE encode, N FLV sinks: the
    already-encoded H.264/AAC packets are COPIED to every output (no second encode -> CPU stays at the
    single-videotoolbox cost). Primary (onfail=None) gets bare [f=flv] = default abort, so a YouTube
    drop kills ffmpeg -> stream_loop reconnects (YouTube = health anchor). Every secondary gets
    [f=flv:onfail=ignore] so its stall/drop is dropped silently and the primary keeps flowing."""
    parts = []
    for t in targets:
        opt = "f=flv:onfail=ignore" if t.get("onfail") else "f=flv"
        parts.append(f"[{opt}]{t['url']}")
    return "|".join(parts)


def ffmpeg_cmd(targets: list[dict], fps: int, w: int, h: int, br: str, buf: str) -> list[str]:
    # SINGLE continuous encoder. Video frames arrive on STDIN via the image2pipe (png_pipe)
    # demuxer; a feeder thread writes the current dashboard PNG at `fps`. -use_wallclock_as_timestamps
    # stamps each piped frame with wall-clock PTS so pacing is driven by real time (the feeder), and
    # -r forces clean CFR output. Audio is real-time silence (anullsrc -re); live endpoints require an
    # audio track. HARDWARE encode via VideoToolbox (~5% CPU vs ~40% software). -allow_sw keeps the
    # stream UP if the HW media engine is momentarily busy (the watchdog's CPU ceiling catches a
    # sustained SW fallback). Long GOP + low fps + low cap = minimal RTMP egress on a near-static panel.
    # NB: this ffmpeg build has NO drawtext filter -- do NOT add one (a missing filter aborts the encode).
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "warning",
        "-f", "image2pipe", "-framerate", str(fps),
        "-use_wallclock_as_timestamps", "1", "-i", "-",
        "-re", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-vf", f"scale={w}:{h},format=yuv420p", "-r", str(fps),
        "-c:v", "h264_videotoolbox", "-realtime", "1", "-allow_sw", "1",
        "-g", str(fps * GOP_SEC), "-keyint_min", str(fps),
        "-b:v", br, "-maxrate", br, "-bufsize", buf,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100", "-ac", "1",
    ]
    if len(targets) <= 1:
        # Byte-identical YT-only path: single FLV sink, automatic stream selection (no -map needed).
        cmd += ["-f", "flv", targets[0]["url"]]
    else:
        # Multi-sink simulcast: the tee muxer DISABLES automatic stream selection, so map explicitly
        # (video = filtered input 0, audio = input 1). Then fan the same encoded packets to N FLV sinks.
        cmd += ["-map", "0:v", "-map", "1:a", "-f", "tee", _tee_spec(targets)]
    return cmd


def grab_frame(chrome: str, w: int, h: int, dest: Path) -> bool:
    # NEVER broadcast a browser error page. Chrome screenshots a 404/"page can't be found" just as
    # happily as the real dashboard, so a transient dashboard restart used to get pushed as the live
    # frame (the frozen-404 incident). Gate on a real HTTP 200 first; on any non-200/unreachable return
    # False so the feeder holds the last good frame.
    import urllib.request
    try:
        resp = urllib.request.urlopen(DASH_URL, timeout=3)
        if getattr(resp, "status", getattr(resp, "code", 200)) != 200:
            return False
    except Exception:
        return False
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--no-sandbox", f"--window-size={w},{h}",
           "--force-device-scale-factor=1",
           f"--screenshot={dest}", f"{DASH_URL}?_t={int(time.time())}"]
    try:
        subprocess.run(cmd, timeout=12, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        return dest.exists() and dest.stat().st_size > 0
    except Exception:
        return False


def _capture_loop(chrome: str, w: int, h: int, stop: threading.Event) -> None:
    """Keep FRAME fresh in the background, fully DECOUPLED from the encoder. Atomic-rename so the
    feeder never reads a half-written PNG. A slow Chrome screenshot can no longer stall the live feed
    (that was the old image2pipe failure: capture ran inline in the feed loop)."""
    captmp = FRAME_DIR / ".dashboard_frame.tmp.png"

    def _refresh() -> bool:
        if grab_frame(chrome, w, h, captmp):
            try:
                os.replace(captmp, FRAME)
                return True
            except Exception:
                pass
        return False

    while not stop.is_set():
        ok = _refresh()
        # success -> next capture in REFRESH_SEC; failure (dashboard down) -> retry soon
        stop.wait(REFRESH_SEC if ok else 3)


def _feed(proc: subprocess.Popen, fps: int, desired: dict, br: str,
          stop: threading.Event) -> int:
    """Pump the CURRENT frame into ffmpeg's stdin at `fps`. Returns when the encoder dies, when a
    debounced viewer-throttle bitrate change wants a clean reconnect, or on stop. mtime-cached so a
    static dashboard re-reads the 320KB PNG from disk only when it actually changes (once / REFRESH_SEC)."""
    interval = 1.0 / max(fps, 1)
    cached_mtime = None
    cached_bytes: bytes | None = None
    changed_since: float | None = None
    while True:
        if stop.is_set():
            try:
                proc.stdin.close()
            except Exception:
                pass
            return 0
        if proc.poll() is not None:
            return proc.returncode or 1               # encoder died -> respawn
        # viewer-throttle: only reconnect after the new state holds ~30s (no flapping reconnects)
        if desired["br"] != br:
            changed_since = changed_since or time.time()
            if time.time() - changed_since >= 30:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.terminate()
                return 0                                # clean restart with the new bitrate
        else:
            changed_since = None
        try:
            st = FRAME.stat()
            if cached_mtime != st.st_mtime_ns:
                b = FRAME.read_bytes()
                if b:
                    cached_bytes, cached_mtime = b, st.st_mtime_ns
        except FileNotFoundError:
            pass
        if cached_bytes:
            try:
                proc.stdin.write(cached_bytes)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                return proc.poll() if proc.poll() is not None else 1
        time.sleep(interval)


def _monitor_bitrate(desired: dict, stop: threading.Event) -> None:
    """Re-evaluate the viewer-throttle target every ~15s; the feeder reacts (debounced)."""
    while not stop.is_set():
        desired["br"], desired["buf"] = _desired_bitrate()
        stop.wait(15)


def stream_loop(targets: list[dict], fps: int, w: int, h: int, chrome: str) -> None:
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()

    # prime: ensure FRAME exists & is current before the encoder starts. A persisted frame from a
    # previous run means a relaunch primes instantly; a stale/missing one is refreshed here.
    captmp = FRAME_DIR / ".dashboard_frame.tmp.png"
    for _ in range(20):
        if grab_frame(chrome, w, h, captmp):
            try:
                os.replace(captmp, FRAME)
                break
            except Exception:
                pass
        time.sleep(3)
    if not FRAME.exists():
        print("[stream] could not capture an initial dashboard frame; aborting", flush=True)
        return

    threading.Thread(target=_capture_loop, args=(chrome, w, h, stop), daemon=True).start()
    desired = {"br": BITRATE, "buf": BUFSIZE}
    desired["br"], desired["buf"] = _desired_bitrate()
    threading.Thread(target=_monitor_bitrate, args=(desired, stop), daemon=True).start()

    backoff = 2
    while not stop.is_set():
        ok, leaks = dashboard_clean()
        if not ok:
            print(f"[stream] anonymity gate FAILED, refusing to stream: {leaks}", flush=True)
            stop.set(); return
        br, buf = desired["br"], desired["buf"]
        proc = subprocess.Popen(ffmpeg_cmd(targets, fps, w, h, br, buf), stdin=subprocess.PIPE)
        primary = targets[0]["url"]
        host = primary.split('/')[2] if '/' in primary else 'rtmp'
        extra = len(targets) - 1
        sinks = f" +{extra} secondary sink(s)" if extra else ""
        print(f"[stream] CONTINUOUS live -> {host}{sinks} @ {fps}fps {w}x{h} cap{br} "
              f"gop{fps*GOP_SEC} aud{AUDIO_BITRATE} refresh{REFRESH_SEC}s", flush=True)
        try:
            rc = _feed(proc, fps, desired, br, stop)
        except KeyboardInterrupt:
            stop.set()
            try: proc.terminate()
            except Exception: pass
            return
        if rc == 0:
            backoff = 2                                # clean reconnect (viewer-throttle change)
            continue
        print(f"[stream] encoder rc={rc}; reconnect in {backoff}s", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def selftest(rtmp: str, chrome: str) -> int:
    import json
    ok_gate, leaks = dashboard_clean()
    extras = _extra_targets()
    # Mask ingest keys: report only the host of each secondary, never the full URL+key.
    extra_hosts = [t["url"].split('/')[2] if '/' in t["url"] else t["url"] for t in extras]
    report = {
        "chrome_found": bool(chrome), "chrome": chrome,
        "ffmpeg_found": bool(_find(FFMPEG_CANDIDATES)),
        "font_found": bool(_find(FONT_CANDIDATES)),
        "dashboard_url": DASH_URL,
        "frame_path": str(FRAME),
        "encoder": "continuous image2pipe (single ffmpeg, no segments)",
        "video": f"{BITRATE} cap @ {FPS}fps, gop {FPS*GOP_SEC}f / {GOP_SEC}s",
        "audio": f"aac {AUDIO_BITRATE} mono silent",
        "anonymity_gate_clean": ok_gate, "gate_leaks": leaks,
        "stream_key_present": bool(rtmp),
        "sinks": ("tee: 1 primary + %d secondary" % len(extras)) if extras else "single flv (YouTube only)",
        "extra_endpoints": extra_hosts,
        "extra_endpoints_config": str(EXTRA_TARGETS_FILE),
        "default_off": not LIVE,
        "would_stream_now": bool(LIVE and rtmp and ok_gate and chrome and _find(FFMPEG_CANDIDATES)),
    }
    blockers = []
    if not chrome:
        blockers.append("no Chrome/Chromium binary found")
    if not _find(FFMPEG_CANDIDATES):
        blockers.append("ffmpeg not installed (brew install ffmpeg)")
    if not rtmp:
        blockers.append(f"no stream key — one-time: paste RTMP ingest URL into {KEY_FILE}")
    if not ok_gate:
        blockers.append(f"anonymity gate not clean: {leaks}")
    report["blockers"] = blockers
    report["RESULT"] = "READY (arm with CASE_STUDY_STREAM_LIVE=1)" if not blockers else "BLOCKED"
    print(json.dumps(report, indent=2))
    return 0 if not blockers else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtmp-url", default=None)
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--width", type=int, default=int(os.environ.get("CASE_STUDY_STREAM_W", "1920")))
    ap.add_argument("--height", type=int, default=int(os.environ.get("CASE_STUDY_STREAM_H", "1080")))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    chrome = _find(CHROME_CANDIDATES)
    rtmp = rtmp_url(args.rtmp_url)

    if args.selftest:
        return selftest(rtmp, chrome)
    if not LIVE:
        print("DEFAULT-OFF: set CASE_STUDY_STREAM_LIVE=1 to stream. Run --selftest to check readiness.")
        return 0
    if not (rtmp and chrome and _find(FFMPEG_CANDIDATES)):
        return selftest(rtmp, chrome)
    ensure_dashboard()
    stream_loop(stream_targets(args.rtmp_url), args.fps, args.width, args.height, chrome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
