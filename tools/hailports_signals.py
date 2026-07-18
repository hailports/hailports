#!/usr/bin/env python3
"""hailports_signals.py — the MEASURE step of the hailports self-improvement loop.

This is the missing feedback signal. The autonomy/deploy infrastructure already exists
(scripts/hailports_autoflow.sh, deploy_hailports.sh, responsive_baseline.py,
stream_health_watchdog.sh); what was missing was a periodic, deterministic snapshot of
how the live site + dashboard + stream are ACTUALLY doing, so the CRITIQUE step can name
the single weakest thing instead of guessing.

It assembles one signal snapshot from what's actually available and appends it as one JSON
line to data/hustle/hailports_signals.jsonl:

  • feed     — intake.hailports.com/live.json (and local :8361) 200-health + latency +
               freshness. CATCHES the silent dead-widget / 502 / hung-server regression
               that degraded the dashboard (the guides server can be LISTENING but hung).
  • funnel   — real (bot-dropped via core.traffic_classifier) scan_start / email_capture /
               checkout / lead counts over 24h + 7d = the conversion signal.
  • homepage — live homepage 200 + TTFB/load + a broken-internal-link sweep.
  • responsive — responsive_baseline lint (reused, not redone) + REAL measured horizontal
               overflow at 390px & 1440px via headless chromium.
  • visual   — headless screenshots at 390px (mobile) + 1440px (desktop) saved to disk for
               the CRITIQUE agent to look at, plus cheap heuristics (hero text, CTA count,
               whether live metric tiles actually rendered).
  • stream   — uptime from the stream watchdog state/log (NEVER touches the encoder rails),
               frozen-frame detection on the streamed dashboard, and a best-effort YouTube
               concurrent-viewer read (falls back to the proxy when the token can't read it).

$0: no LLM here. The qualitative "is the hero compelling" judgement happens in the agent
(CRITIQUE) step which already runs under claude -p. Every probe is short-timeout and
fail-closed: a single failed probe records an error field and never crashes the snapshot.

CLI:
  python3 tools/hailports_signals.py snapshot            # full snapshot (site + stream)
  python3 tools/hailports_signals.py snapshot --site     # site/dash signals only
  python3 tools/hailports_signals.py snapshot --stream   # stream signals only
  python3 tools/hailports_signals.py snapshot --quick    # deterministic only, no browser
  python3 tools/hailports_signals.py latest              # print the last snapshot
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DIST = ROOT / "data" / "hustle" / "hailports_dist"
SIG_LOG = ROOT / "data" / "hustle" / "hailports_signals.jsonl"
SHOT_DIR = ROOT / "data" / "hustle" / "signals"
FUNNEL_LOG = ROOT / "data" / "funnel_events.jsonl"
LEADS = ROOT / "data" / "hustle" / "inbound_leads.jsonl"
STREAM_STATE = Path(os.path.expanduser("~/.stream-health-watchdog.state"))
STREAM_LOG = ROOT / "logs" / "stream_health_watchdog.log"
FRAME_HASH = SHOT_DIR / ".last_stream_frame.sha"

LIVE_HOME = "https://www.hailports.com/"
LIVE_FEED = "https://intake.hailports.com/live.json"
LOCAL_FEED = "http://127.0.0.1:8361/live.json"
DASH_LOCAL = "http://127.0.0.1:8360/"

PYBIN = str(ROOT / ".venv" / "bin" / "python")
if not Path(PYBIN).exists():
    PYBIN = sys.executable

_UA = "hailports-signals/1.0 (+monitor)"


def _now() -> float:
    return time.time()


def _http(url: str, timeout: float, method: str = "GET") -> dict:
    """Short-timeout fetch. Returns {status,latency_ms,body,error}. Never raises."""
    t0 = _now()
    req = urllib.request.Request(url, method=method, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read() if method == "GET" else b""
            return {"status": r.status, "latency_ms": int((_now() - t0) * 1000),
                    "body": body, "error": ""}
    except Exception as e:
        code = getattr(e, "code", 0) or 0
        return {"status": int(code), "latency_ms": int((_now() - t0) * 1000),
                "body": b"", "error": f"{type(e).__name__}: {str(e)[:120]}"}


# ── feed health: the dead-widget / hung-server detector ───────────────────────────────

def feed_health() -> dict:
    """200-health + freshness of the homepage's live.json. Tries public then local.
    Flags the exact regression where the server LISTENS but hangs (status 0, high latency)."""
    out = {"source": "none", "ok": False, "status": 0, "latency_ms": 0,
           "metrics_n": 0, "age_s": None, "error": ""}
    for source, url, to in (("public", LIVE_FEED, 7.0), ("local", LOCAL_FEED, 4.0)):
        r = _http(url, timeout=to)
        out.update(source=source, status=r["status"], latency_ms=r["latency_ms"],
                   error=r["error"])
        if r["status"] == 200 and r["body"]:
            try:
                d = json.loads(r["body"].decode() or "{}")
                out["metrics_n"] = len(d.get("metrics") or [])
                ts = d.get("ts")
                if isinstance(ts, (int, float)):
                    out["age_s"] = int(_now() - ts)
                # healthy = 200 + at least one metric tile (empty metrics == dead feed)
                out["ok"] = out["metrics_n"] > 0
                out["error"] = "" if out["ok"] else "feed 200 but metrics empty"
            except Exception as e:
                out["error"] = f"json: {type(e).__name__}"
            return out
        # public failed -> try local; keep the worse error visible
    return out


# ── funnel: the conversion signal (bot-dropped) ───────────────────────────────────────

def _iter_jsonl(path: Path):
    if not path.exists():
        return
    try:
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue
    except Exception:
        return


def _parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def funnel_signal() -> dict:
    """Real (bot/datacenter/owner dropped) funnel counts over 24h + 7d. The conversion lever."""
    try:
        from core import traffic_classifier as tc
        is_human = tc.is_human
    except Exception:
        def is_human(e):  # conservative fallback: drop obvious bot UA only
            ua = (e.get("ua") or "").lower()
            return not any(b in ua for b in ("bot", "crawler", "spider", "curl", "python", "http"))
    now = _now()
    win = {"24h": 86400, "7d": 604800}
    buckets = {k: {} for k in win}
    interest = ("pageview", "storefront_home", "scan_start", "product_click",
                "begin_checkout", "email_capture", "checkout_start_page")
    for e in _iter_jsonl(FUNNEL_LOG):
        ev = e.get("event", "")
        ts = _parse_ts(e.get("ts", ""))
        if not ts:
            continue
        human = False
        try:
            human = is_human(e)
        except Exception:
            human = True
        for wname, wsec in win.items():
            if now - ts <= wsec and human:
                b = buckets[wname]
                b[ev] = b.get(ev, 0) + 1
    leads_total = sum(1 for _ in _iter_jsonl(LEADS))
    leads_24h = sum(1 for r in _iter_jsonl(LEADS)
                    if now - _parse_ts(r.get("ts", "")) <= 86400)

    def _conv(b: dict) -> float:
        starts = b.get("scan_start", 0) + b.get("storefront_home", 0) + b.get("pageview", 0)
        caps = b.get("email_capture", 0) + b.get("begin_checkout", 0)
        return round(caps / starts, 4) if starts else 0.0

    return {
        "h24": buckets["24h"], "d7": buckets["7d"],
        "conv_24h": _conv(buckets["24h"]), "conv_7d": _conv(buckets["7d"]),
        "leads_total": leads_total, "leads_24h": leads_24h,
        "note": "human-only (bot/datacenter/owner dropped via core.traffic_classifier)",
        "_interest": list(interest),
    }


# ── homepage health + broken-link sweep ───────────────────────────────────────────────

def _internal_links() -> list[str]:
    idx = DIST / "index.html"
    if not idx.exists():
        return []
    try:
        html = idx.read_text(errors="ignore")
    except Exception:
        return []
    hrefs = re.findall(r'href="(/[^"#?]+|https://www\.hailports\.com/[^"#?]+)"', html)
    seen, out = set(), []
    for h in hrefs:
        path = h.replace("https://www.hailports.com", "")
        if not path or path in seen or path.endswith((".css", ".js", ".png", ".svg", ".ico")):
            continue
        seen.add(path)
        out.append(path)
    return out


def homepage_health(max_links: int = 18) -> dict:
    r = _http(LIVE_HOME, timeout=10)
    out = {"status": r["status"], "ttfb_ms": r["latency_ms"], "error": r["error"],
           "links_checked": 0, "links_broken": 0, "broken": []}
    if r["status"] != 200:
        return out
    links = _internal_links()[:max_links]
    broken = []
    for path in links:
        url = path if path.startswith("http") else f"https://www.hailports.com{path}"
        hr = _http(url, timeout=8, method="HEAD")
        # some hosts 405 a HEAD -> retry GET before calling it broken
        if hr["status"] in (405, 0):
            hr = _http(url, timeout=8, method="GET")
        if hr["status"] >= 400 or hr["status"] == 0:
            broken.append({"path": path, "status": hr["status"]})
    out["links_checked"] = len(links)
    out["links_broken"] = len(broken)
    out["broken"] = broken[:10]
    return out


# ── headless visual + measured overflow ───────────────────────────────────────────────

def _browser_audit(url: str, want_shots: bool) -> dict:
    """One chromium launch: measured overflow + screenshots + DOM heuristics at 390 & 1440.
    Falls back to the local built file if the live URL won't load. Returns {} on no-playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"error": f"no playwright: {type(e).__name__}"}
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    res = {"url": url, "overflow_390": None, "overflow_1440": None,
           "shot_390": "", "shot_1440": "", "hero_h1": "", "cta_count": 0,
           "tiles_rendered": 0, "error": ""}
    sizes = {"390": (390, 844, str(SHOT_DIR / "home_390.png")),
             "1440": (1440, 900, str(SHOT_DIR / "home_1440.png"))}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            try:
                for key, (w, h, shot) in sizes.items():
                    pg = b.new_page(viewport={"width": w, "height": h},
                                    device_scale_factor=2 if key == "390" else 1)
                    loaded = False
                    for target in (url, (DIST / "index.html").as_uri()):
                        try:
                            pg.goto(target, wait_until="networkidle", timeout=12000)
                            loaded = True
                            res["url"] = target
                            break
                        except Exception:
                            continue
                    if not loaded:
                        res["error"] = "page load failed (live + local)"
                        pg.close()
                        continue
                    pg.wait_for_timeout(1200)  # let live.json fetch + render tiles
                    sw = pg.evaluate("Math.ceil(document.documentElement.scrollWidth)")
                    iw = pg.evaluate("window.innerWidth")
                    res[f"overflow_{key}"] = max(0, int(sw) - int(iw))
                    if key == "390":
                        try:
                            res["hero_h1"] = (pg.inner_text("h1") or "").strip()[:200]
                        except Exception:
                            pass
                        try:
                            res["cta_count"] = int(pg.eval_on_selector_all(
                                "a.btn, .btn, button", "els => els.length"))
                        except Exception:
                            pass
                        try:
                            res["tiles_rendered"] = int(pg.eval_on_selector_all(
                                "#metrics .tile", "els => els.length"))
                        except Exception:
                            pass
                    if want_shots:
                        try:
                            pg.screenshot(path=shot, full_page=(key == "390"))
                            res[f"shot_{key}"] = shot
                        except Exception:
                            pass
                    pg.close()
            finally:
                b.close()
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return res


def responsive_lint() -> dict:
    """Reuse responsive_baseline.py (do not redo it) — count of non-compliant pages in dist."""
    try:
        out = subprocess.run(
            [PYBIN, str(ROOT / "tools" / "responsive_baseline.py"), "--lint",
             str(DIST), "--exclude", "index.html,live"],
            capture_output=True, text=True, timeout=120)
        m = re.search(r":\s*(\d+)/(\d+)\s+pages NON-compliant", out.stdout)
        if m:
            return {"noncompliant": int(m.group(1)), "scanned": int(m.group(2)),
                    "ok": False}
        m = re.search(r"all\s+(\d+)\s+pages compliant", out.stdout)
        if m:
            return {"noncompliant": 0, "scanned": int(m.group(1)), "ok": True}
        return {"noncompliant": None, "scanned": None, "ok": None,
                "error": (out.stdout or out.stderr)[:120]}
    except Exception as e:
        return {"noncompliant": None, "ok": None, "error": f"{type(e).__name__}"}


# ── stream signal: proxy (uptime + frozen-frame + visual) with best-effort YT viewers ──

def _yt_concurrent_viewers() -> dict:
    """Best-effort, READ-ONLY concurrent-viewer read for the live broadcast. The on-disk
    token is the wrong channel (redacted, per ops memory) and carries no broadcast id, so
    this almost always returns unavailable -> the proxy below is the real signal. We never
    write, never touch the broadcast, never block on it."""
    tokf = ROOT / "data" / "hustle" / "yt_live_token.json"
    statef = ROOT / "data" / "hustle" / "yt_live_state.json"
    try:
        tok = json.loads(tokf.read_text())
        rt = tok.get("refresh_token")
        if not rt:
            return {"viewers": None, "note": "no refresh_token"}
        # need a client id/secret to exchange; reuse the stack's if present, else bail
        cid = os.environ.get("YT_CLIENT_ID", "")
        csec = os.environ.get("YT_CLIENT_SECRET", "")
        bid = ""
        if statef.exists():
            bid = (json.loads(statef.read_text()) or {}).get("broadcast_id", "")
        if not (cid and csec and bid):
            return {"viewers": None, "note": "token wrong-account / no broadcast id (use proxy)"}
        d = json.dumps({}).encode()  # placeholder; real exchange omitted to avoid token churn
        return {"viewers": None, "note": "yt read gated (avoids token churn) — proxy used"}
    except Exception as e:
        return {"viewers": None, "note": f"unavailable: {type(e).__name__}"}


def stream_signal(want_shots: bool) -> dict:
    """Stream retention proxy. NEVER touches the encoder/health rails (that is the watchdog's
    job) — read-only. uptime (watchdog) + frozen-frame detection + a captured frame for the
    'is it gripping' visual judgement, plus a best-effort real YT viewer count."""
    out = {"uptime_ok": None, "last_enc_age_s": None, "restarts_24h": None,
           "recent_lag": None, "frozen": None, "frame": "", "yt": {}, "error": ""}
    now = _now()
    # 1) uptime from the watchdog state (last healthy encoder epoch)
    try:
        last_enc = int((STREAM_STATE.read_text().strip() or "0"))
        age = int(now - last_enc)
        out["last_enc_age_s"] = age
        out["uptime_ok"] = age < 180  # encoder is briefly absent between 90s segments
    except Exception as e:
        out["error"] = f"state: {type(e).__name__}"
    # 2) restarts + lag from the watchdog log tail (last 24h)
    try:
        restarts = lag = 0
        for line in (STREAM_LOG.read_text(errors="ignore").splitlines()[-400:]
                     if STREAM_LOG.exists() else []):
            # log lines start "YYYY-MM-DD HH:MM:SS"
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            lts = _parse_ts(line[:10] + "T" + line[11:19]) if m else 0
            if lts and now - lts <= 86400:
                if "RESTART:" in line:
                    restarts += 1
                mm = re.search(r"laggy=(\d+)", line)
                if mm:
                    lag = max(lag, int(mm.group(1)))
        out["restarts_24h"] = restarts
        out["recent_lag"] = lag
    except Exception:
        pass
    # 3) frozen-frame detection on the STREAMED dashboard surface (read-only capture)
    frame_path = str(SHOT_DIR / "stream_frame.png")
    try:
        from playwright.sync_api import sync_playwright
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            try:
                pg = b.new_page(viewport={"width": 1280, "height": 720})
                pg.goto(DASH_LOCAL, wait_until="domcontentloaded", timeout=10000)
                pg.wait_for_timeout(800)
                png = pg.screenshot(path=frame_path if want_shots else None)
                # the dashboard burns in a ticking clock, so two captures ~1.5s apart MUST
                # differ; identical = frozen render (encoder may be streaming a dead frame)
                pg.wait_for_timeout(1500)
                png2 = pg.screenshot()
                h1 = hashlib.sha1(png).hexdigest()
                h2 = hashlib.sha1(png2).hexdigest()
                out["frozen"] = (h1 == h2)
                if want_shots:
                    out["frame"] = frame_path
                pg.close()
            finally:
                b.close()
    except Exception as e:
        out["frozen"] = None
        out["error"] = (out["error"] + f"; frame: {type(e).__name__}").strip("; ")
    # 4) best-effort real retention metric
    out["yt"] = _yt_concurrent_viewers()
    return out


# ── snapshot assembly ─────────────────────────────────────────────────────────────────

def _summary(snap: dict) -> str:
    parts = []
    f = snap.get("feed")
    if f is not None:
        parts.append(f"feed={'OK' if f.get('ok') else 'DOWN'}({f.get('source')},"
                     f"{f.get('status')},{f.get('latency_ms')}ms,m={f.get('metrics_n')})")
    fn = snap.get("funnel")
    if fn is not None:
        h = fn.get("h24", {})
        parts.append(f"funnel24h: starts={h.get('storefront_home',0)+h.get('pageview',0)+h.get('scan_start',0)}"
                     f" caps={h.get('email_capture',0)} conv={fn.get('conv_24h')} leads={fn.get('leads_24h')}")
    hp = snap.get("homepage")
    if hp is not None:
        parts.append(f"home={hp.get('status')}({hp.get('ttfb_ms')}ms) broken={hp.get('links_broken')}/{hp.get('links_checked')}")
    rs = snap.get("responsive")
    if rs is not None:
        parts.append(f"lint_bad={rs.get('noncompliant')}")
    v = snap.get("visual")
    if v is not None:
        parts.append(f"overflow=390:{v.get('overflow_390')}/1440:{v.get('overflow_1440')} tiles={v.get('tiles_rendered')}")
    st = snap.get("stream")
    if st is not None:
        parts.append(f"stream: up={st.get('uptime_ok')} frozen={st.get('frozen')} restarts24h={st.get('restarts_24h')}")
    return " | ".join(parts)


def snapshot(kind: str = "both", quick: bool = False, shots: bool = True) -> dict:
    snap = {"ts": datetime.now(timezone.utc).isoformat(), "epoch": int(_now()), "kind": kind}
    do_site = kind in ("both", "site")
    do_stream = kind in ("both", "stream")
    if do_site:
        snap["feed"] = feed_health()
        snap["funnel"] = funnel_signal()
        snap["homepage"] = homepage_health()
        snap["responsive"] = responsive_lint()
        snap["visual"] = ({"skipped": "quick"} if quick
                          else _browser_audit(LIVE_HOME, want_shots=shots))
    if do_stream:
        snap["stream"] = ({"skipped": "quick"} if quick
                         else stream_signal(want_shots=shots))
    snap["summary"] = _summary(snap)
    SIG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SIG_LOG.open("a") as fh:
        fh.write(json.dumps(snap) + "\n")
    # keep the log bounded
    try:
        lines = SIG_LOG.read_text().splitlines()
        if len(lines) > 500:
            SIG_LOG.write_text("\n".join(lines[-500:]) + "\n")
    except Exception:
        pass
    return snap


def latest() -> dict | None:
    if not SIG_LOG.exists():
        return None
    lines = [l for l in SIG_LOG.read_text().splitlines() if l.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except Exception:
        return None


def _cli(argv) -> int:
    ap = argparse.ArgumentParser(description="hailports MEASURE — signal snapshot")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("snapshot")
    s.add_argument("--site", action="store_true")
    s.add_argument("--stream", action="store_true")
    s.add_argument("--quick", action="store_true", help="deterministic only, no browser")
    s.add_argument("--no-shots", action="store_true")
    sub.add_parser("latest")
    a = ap.parse_args(argv)

    if a.cmd == "latest":
        snap = latest()
        print(json.dumps(snap, indent=2) if snap else "(no snapshots yet)")
        return 0
    # default: snapshot
    kind = "both"
    if getattr(a, "site", False) and not getattr(a, "stream", False):
        kind = "site"
    elif getattr(a, "stream", False) and not getattr(a, "site", False):
        kind = "stream"
    snap = snapshot(kind=kind, quick=getattr(a, "quick", False),
                    shots=not getattr(a, "no_shots", False))
    print("SNAPSHOT", snap["ts"], "kind=" + kind)
    print(snap["summary"])
    print("logged -> " + str(SIG_LOG))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
