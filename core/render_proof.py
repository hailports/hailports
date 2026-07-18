#!/home/user/claude-stack/.venv/bin/python
"""Real-browser render-proof probe — the runtime/visual complement to core.web_failure_probe.

web_failure_probe.probe() is urllib-only: it never executes JS, so a site that throws on
load, ships a blank React mount, 404s its own CSS/JS bundle, has dead images, overflows on
mobile, or breaks only in Safari reads as HTTP-200-healthy. This module opens a REAL browser
(Playwright: Chromium for the desktop+mobile proof screenshots, WebKit for Safari-engine
parity) and captures ONLY runtime facts the owner can verify:

  - JavaScript console errors / uncaught page exceptions on load
  - failed / 4xx-5xx network requests for subresources (CSS, JS, fonts, XHR/fetch)
  - dead <img> resources (broken images)
  - blank render (body has no visible content — failed JS mount)
  - horizontal overflow at a phone-width (390px) viewport
  - Safari/WebKit-only breakage (renders in Chrome, broken in WebKit)

Design contract (mirrors the stack):
  * TRUTH-BY-CONSTRUCTION — every finding comes from a real capture event; nothing invented.
    No browser / no capture => no findings (never a fabricated reason).
  * FAIL-SAFE — playwright missing, browser launch failure, timeout, SSRF-blocked host, or any
    exception => a DEGRADED result (available=False, severity="ok", reasons=[]) that a caller
    loop can attach and move past. It NEVER raises into the broken-site loop.
  * NON-DUPLICATING — it does NOT re-derive probe()'s static signals (HTTP status, TLS, HTTPS
    presence, viewport-meta presence, parked/scaffold text, staleness). It ADDS runtime signals.
  * {severity, reasons} shape matches web_failure_probe.probe() so findings merge straight into
    the severity-ranked broken-site queue (union reasons, max severity) with no new plumbing.

  python3 -m core.render_proof example.com --json
  python3 -m core.render_proof --file data/hustle/biz_domains.txt --broken-only
  python3 -m core.render_proof --selftest
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the vetted SSRF guard so a real browser can never be bounced into an internal host.
try:
    from core.web_failure_probe import _host_blocked  # type: ignore
except Exception:  # pragma: no cover - standalone fallback, semantically identical
    import ipaddress
    import socket

    def _host_blocked(host: str) -> bool:
        if not host:
            return True
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return True
        return False

SHOTS = ROOT / "data" / "hustle" / "render_proof_shots"
LOG = ROOT / "data" / "hustle" / "render_proof.log"

DESKTOP_VIEWPORT = (1440, 900)
MOBILE_VIEWPORT = (390, 844)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
GOTO_TIMEOUT_MS = 20000
MIN_PNG_BYTES = 1024
BLANK_TEXT_THRESHOLD = 20  # visible innerText chars below which we call a render "blank"
_SUBRESOURCE_TYPES = ("script", "stylesheet", "font", "fetch", "xhr")

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "ok": 9}

_FFMPEG = shutil.which("ffmpeg")
_STITCH_MAX_SEGMENTS = 40  # runaway guard on infinite-scroll pages


def _stitch_full_page(page, out_path: Path, dsf: int) -> bool:
    """Capture the WHOLE page as stitched viewport screenshots instead of one page.screenshot(
    full_page=True), which comes back blank/torn on Framer/WebGL/lazy-loaded pages. A warm-up scroll
    first mounts lazy images + scroll-triggered animations; per-viewport shots are then cropped to
    device px (a 150px overlap buffer absorbs the scroll-bottom gap) and ffmpeg vstacks them. Returns
    True only on a valid stitched PNG; ANY failure returns False so the caller falls back to the
    one-shot screenshot (zero regression). Ported from the vetted MengTo stitch skill."""
    if not _FFMPEG:
        return False
    vp = page.viewport_size or {}
    vh = int(vp.get("height") or 900)
    step = max(1, vh - 150)  # overlap buffer so the scroll-bottom segment never leaves a gap
    tmp = Path(tempfile.mkdtemp(prefix="rp_stitch_"))
    try:
        def _doc_h() -> int:
            return int(page.evaluate(
                "() => Math.max(document.documentElement.scrollHeight,"
                " document.body ? document.body.scrollHeight : 0)"))
        # warm-up: scroll through once so lazy content + scroll animations actually render
        total = _doc_h()
        y = 0
        while y < total and y < vh * _STITCH_MAX_SEGMENTS:
            page.evaluate("(s) => window.scrollTo(0, s)", y)
            page.wait_for_timeout(300)
            y += vh
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(600)
        # capture pass: viewport screenshots down the page, cropped to their intended device height
        total = _doc_h()
        segs = []
        y = 0
        while y < total and len(segs) < _STITCH_MAX_SEGMENTS:
            page.evaluate("(s) => window.scrollTo(0, s)", y)
            page.wait_for_timeout(350)
            raw = tmp / f"{len(segs):03d}_raw.png"
            page.screenshot(path=str(raw), full_page=False)
            crop_h = int(min(step, total - y) * dsf)  # device px
            seg = tmp / f"{len(segs):03d}.png"
            r = subprocess.run([_FFMPEG, "-y", "-i", str(raw), "-vf", f"crop=iw:{crop_h}:0:0", str(seg)],
                               capture_output=True, timeout=30)
            if r.returncode != 0 or not seg.exists():
                return False
            segs.append(seg)
            y += step
        if not segs:
            return False
        inputs = []
        for seg in segs:
            inputs += ["-i", str(seg)]
        fc = "".join(f"[{i}:v]" for i in range(len(segs))) + f"vstack=inputs={len(segs)}"
        r = subprocess.run([_FFMPEG, "-y", *inputs, "-filter_complex", fc, str(out_path)],
                           capture_output=True, timeout=90)
        return r.returncode == 0 and out_path.exists() and out_path.stat().st_size > MIN_PNG_BYTES
    except Exception:
        return False
    finally:
        try:
            for f in tmp.glob("*"):
                f.unlink()
            tmp.rmdir()
        except Exception:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0].split("?")[0]
    return d.strip().strip(".")


def _safe(domain: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "_", _norm_domain(domain)) or "site"


def _to_target(url_or_domain: str) -> tuple[str, str, str]:
    """Return (url, domain, scheme). Bare domains become https://; file:// passes through
    (self-test only). Never raises."""
    s = str(url_or_domain).strip()
    if s.lower().startswith("file://"):
        return s, "", "file"
    if s.lower().startswith(("http://", "https://")):
        dom = _norm_domain(s)
        return s, dom, s.split("://", 1)[0].lower()
    dom = _norm_domain(s)
    return f"https://{dom}", dom, "https"


def _log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{_now_iso()} {line}\n")
    except Exception as exc:  # pragma: no cover
        print(f"[render_proof] LOG write failed: {exc}", file=sys.stderr)


def _valid_png(path: Path) -> bool:
    """Verify-from-source: exists, >1KB, decodes. A screenshot we cannot verify is NOT proof."""
    try:
        if not path.exists() or path.stat().st_size <= MIN_PNG_BYTES:
            return False
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def _blank_diag(url: str, domain: str, error: str) -> dict:
    """A degraded (no real capture) findings dict — fail-safe shape. severity=ok / reasons=[]
    so it never poisons the broken-site queue with an invented signal."""
    return {
        "ok": False,
        "available": False,
        "url": url,
        "domain": domain,
        "severity": "ok",
        "reasons": [],
        "screenshots": {"desktop_png": None, "mobile_png": None, "webkit_png": None},
        "engines": {},
        "anon_ok": None,
        "anon_reasons": [],
        "shippable": False,
        "captured_at": _now_iso(),
        "error": error,
    }


def _new_engine_diag() -> dict:
    return {
        "ok": False,
        "console_errors": [],
        "page_errors": [],
        "failed_requests": [],
        "dead_images": [],
        "img_total": 0,
        "img_failed": 0,
        "blank": None,
        "mobile_overflow": None,
        "render_ms": 0,
        "error": None,
        "desktop_png": None,
        "mobile_png": None,
        "_dom_sample": "",  # internal (anon gate only); wire MUST drop keys starting with "_"
    }


def _wire_handlers(page, diag: dict) -> None:
    seen_dead = set()
    seen_failed = set()

    def _on_console(msg):
        try:
            if msg.type == "error":
                diag["console_errors"].append(str(msg.text)[:300])
        except Exception:
            pass

    def _on_pageerror(exc):
        try:
            diag["page_errors"].append(str(exc)[:300])
        except Exception:
            pass

    def _on_requestfailed(req):
        try:
            rt = req.resource_type
            if rt == "image":
                diag["img_failed"] += 1
                if req.url not in seen_dead:
                    seen_dead.add(req.url)
                    diag["dead_images"].append(req.url)
            elif rt in _SUBRESOURCE_TYPES:
                key = (rt, req.url)
                if key not in seen_failed:
                    seen_failed.add(key)
                    fail = ""
                    try:
                        fail = (req.failure or {}).get("errorText", "") if isinstance(req.failure, dict) else str(req.failure or "")
                    except Exception:
                        fail = ""
                    diag["failed_requests"].append(
                        {"url": req.url[:300], "resource_type": rt, "failure": fail[:120] or "request failed"}
                    )
        except Exception:
            pass

    def _on_response(resp):
        try:
            rt = resp.request.resource_type
            status = resp.status
            if rt == "image":
                diag["img_total"] += 1
                if not (200 <= status < 300) and resp.url not in seen_dead:
                    diag["img_failed"] += 1
                    seen_dead.add(resp.url)
                    diag["dead_images"].append(resp.url)
            elif rt in _SUBRESOURCE_TYPES and status >= 400:
                key = (rt, resp.url)
                if key not in seen_failed:
                    seen_failed.add(key)
                    diag["failed_requests"].append(
                        {"url": resp.url[:300], "resource_type": rt, "failure": f"HTTP {status}"}
                    )
        except Exception:
            pass

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    page.on("requestfailed", _on_requestfailed)
    page.on("response", _on_response)


def _goto(page, url: str, timeout_ms: int) -> None:
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    except Exception:
        # networkidle stalls on long-poll/analytics pings — fall back so we still capture a real
        # render instead of failing closed on a benign stall (same idiom as mockup_render).
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)


def _measure_render(page) -> tuple[int, bool, str]:
    """(visible_text_len, mobile_overflow, dom_sample) — from the LIVE rendered DOM. Best-effort."""
    text_len, overflow, sample = 0, False, ""
    try:
        text_len = int(page.evaluate(
            "() => (document.body ? (document.body.innerText||'').trim().length : 0)"
        ))
    except Exception:
        pass
    try:
        overflow = bool(page.evaluate(
            "() => document.documentElement.scrollWidth > (window.innerWidth + 4)"
        ))
    except Exception:
        overflow = False
    try:
        sample = str(page.evaluate(
            "() => (document.body ? (document.body.innerText||'') : '').slice(0,4000)"
        ))
        sample += "\n" + str(page.content())[:8000]
    except Exception:
        sample = ""
    return text_len, overflow, sample


def _engine_capture(p, engine_name: str, url: str, *, want_mobile: bool,
                    timeout_ms: int, shots_dir: Path, safe: str) -> dict:
    """Launch one engine, render desktop (+ mobile for the proof), capture runtime diagnostics
    and full-page screenshots. Returns an engine diag dict. Raises only on hard launch failure
    (the caller catches and degrades that engine)."""
    diag = _new_engine_diag()
    t0 = time.time()
    engine = getattr(p, engine_name)
    browser = engine.launch(headless=True)
    try:
        # --- desktop pass (the primary proof screenshot) ---
        ctx = browser.new_context(
            viewport={"width": DESKTOP_VIEWPORT[0], "height": DESKTOP_VIEWPORT[1]},
            device_scale_factor=2,
            ignore_https_errors=True,  # TLS validity is probe()'s job; render must not fail on it
        )
        try:
            page = ctx.new_page()
            _wire_handlers(page, diag)
            _goto(page, url, timeout_ms)
            text_len, _, sample = _measure_render(page)
            diag["_dom_sample"] = sample
            diag["blank"] = text_len < BLANK_TEXT_THRESHOLD and diag["img_total"] == 0
            dpath = shots_dir / f"{safe}__{engine_name}_desktop.png"
            try:
                if not _stitch_full_page(page, dpath, 2):   # device_scale_factor=2 on this ctx
                    page.screenshot(path=str(dpath), full_page=True)
                if _valid_png(dpath):
                    diag["desktop_png"] = str(dpath)
            except Exception as exc:
                diag["error"] = f"desktop_screenshot: {type(exc).__name__}: {exc}"
        finally:
            ctx.close()

        # --- mobile pass (proof screenshot + overflow measurement) ---
        if want_mobile:
            mctx = browser.new_context(
                viewport={"width": MOBILE_VIEWPORT[0], "height": MOBILE_VIEWPORT[1]},
                device_scale_factor=3,
                is_mobile=True,
                user_agent=MOBILE_UA,
                ignore_https_errors=True,
            )
            try:
                mpage = mctx.new_page()
                _wire_handlers(mpage, diag)
                _goto(mpage, url, timeout_ms)
                _, overflow, _ = _measure_render(mpage)
                diag["mobile_overflow"] = overflow
                mpath = shots_dir / f"{safe}__{engine_name}_mobile.png"
                try:
                    if not _stitch_full_page(mpage, mpath, 3):   # device_scale_factor=3 on this ctx
                        mpage.screenshot(path=str(mpath), full_page=True)
                    if _valid_png(mpath):
                        diag["mobile_png"] = str(mpath)
                except Exception:
                    pass
            finally:
                mctx.close()

        diag["ok"] = True
    finally:
        try:
            browser.close()
        except Exception:
            pass
    diag["render_ms"] = int((time.time() - t0) * 1000)
    return diag


def _derive_reasons(engines: dict) -> list[tuple[str, str]]:
    """Build (severity, reason) tuples from REAL capture events only. Chromium is canonical for
    the desktop signals; WebKit adds the Safari-only parity signal. Every string is a fact the
    owner can reproduce in a browser."""
    out: list[tuple[str, str]] = []
    cr = engines.get("chromium") or {}
    wk = engines.get("webkit") or {}

    if cr.get("ok"):
        if cr.get("page_errors"):
            first = cr["page_errors"][0]
            out.append(("high",
                        f"the homepage throws a JavaScript error on load (uncaught: {first[:120]}) "
                        f"— parts of the site silently don't work"))
        elif cr.get("console_errors"):
            n = len(cr["console_errors"])
            out.append(("medium",
                        f"the browser logs {n} JavaScript console error{'s' if n != 1 else ''} on load "
                        f"(e.g. {cr['console_errors'][0][:120]})"))
        fr = cr.get("failed_requests") or []
        if fr:
            ex = fr[0]
            out.append(("high",
                        f"{len(fr)} required file{'s' if len(fr) != 1 else ''} fail to load "
                        f"(e.g. {ex.get('resource_type')} {ex.get('url')} -> {ex.get('failure')}) "
                        f"— the page is missing styles/scripts"))
        if cr.get("img_failed"):
            out.append(("medium",
                        f"{cr['img_failed']} image{'s' if cr['img_failed'] != 1 else ''} on the "
                        f"homepage are broken (return an error / don't load)"))
        if cr.get("blank"):
            out.append(("high",
                        "the homepage renders blank in a real browser (no visible content) "
                        "— likely a broken build or a script that fails before the page draws"))
        if cr.get("mobile_overflow"):
            out.append(("medium",
                        "the homepage overflows sideways on a phone-width (390px) screen — mobile "
                        "visitors have to pinch and scroll horizontally to read it"))

    # Safari/WebKit-only breakage: only assert when Chromium proved the page is otherwise fine.
    if cr.get("ok") and not cr.get("blank") and not cr.get("page_errors") and wk.get("ok"):
        wk_broke = bool(wk.get("blank")) or bool(wk.get("page_errors"))
        if wk_broke:
            detail = "renders blank" if wk.get("blank") else f"throws ({(wk['page_errors'][0] if wk.get('page_errors') else '')[:100]})"
            out.append(("high",
                        f"the site {detail} in Safari/WebKit but loads in Chrome — Safari and "
                        f"iPhone users can't use it"))
    return out


def _anon_check(domain: str, screenshots: dict, engines: dict) -> tuple[bool | None, list[str]]:
    """A render-proof screenshot may be SHOWN to a prospect only if it clears the operator/employer
    identity gate. The PNG is opaque, so run anon_scrub over the SOURCE the render came from: the
    domain string, the screenshot filenames, and the captured DOM. Uses find_identity_leaks()
    (IDENTITY_TOKENS — scoped + FP-free), NOT find_leaks(), because a prospect's own industry words
    (irrigation/dealer/plumber) legitimately appear and must pass. Fail-CLOSED: if the gate is
    unavailable we return (False, ...) so nothing ships unverified.

    Returns (anon_ok, reasons). anon_ok=None only when there was no capture to check."""
    try:
        from core.anon_scrub import find_identity_leaks  # type: ignore
    except Exception as exc:
        return False, [f"anon_scrub unavailable (fail-closed): {exc}"]

    blobs: list[str] = [domain or ""]
    for v in screenshots.values():
        if v:
            blobs.append(Path(v).name)
    for eng in engines.values():
        s = (eng or {}).get("_dom_sample") or ""
        if s:
            blobs.append(s)
    if not any(b.strip() for b in blobs):
        return None, []
    leaks = sorted(set(find_identity_leaks("\n".join(blobs))))
    return (len(leaks) == 0), [f"identity token leaked: {t}" for t in leaks]


def render_proof(url_or_domain: str, *, engines: tuple[str, ...] = ("chromium", "webkit"),
                 want_mobile: bool = True, timeout_ms: int = GOTO_TIMEOUT_MS,
                 shots_dir: Path | None = None, run_anon_gate: bool = True) -> dict:
    """Capture a real-browser render-proof for one URL/domain.

    Chromium produces the desktop + mobile proof screenshots and the primary runtime findings;
    WebKit is a Safari-engine parity pass (screenshots kept for evidence, used to flag Safari-only
    breakage). Returns the findings dict (see module docstring / schema). FAIL-SAFE: any failure
    path returns a degraded dict (available=False, severity='ok', reasons=[]); it never raises.

    NOTE: the returned engine sub-dicts contain a leading-underscore key `_dom_sample` used only by
    the anon gate. A caller persisting this record MUST drop keys starting with '_' (they are large
    and carry the prospect's raw DOM). Use `strip_internal(findings)` before writing to a queue."""
    url, domain, scheme = _to_target(url_or_domain)

    # SSRF guard — apply to real network schemes; allow file:// (self-test / local render only).
    if scheme in ("http", "https") and _host_blocked(domain):
        return _blank_diag(url, domain, "blocked-non-public-host")

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as exc:
        _log(f"RENDER domain={domain} available=False err=playwright-import: {exc}")
        return _blank_diag(url, domain, f"playwright unavailable: {exc}")

    shots = shots_dir or SHOTS
    try:
        shots.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    safe = _safe(domain) if domain else "selftest"

    from playwright.sync_api import sync_playwright
    eng_results: dict = {}
    try:
        with sync_playwright() as p:
            for name in engines:
                # Chromium owns the proof screenshots (desktop+mobile); WebKit is a desktop
                # parity pass, so skip its mobile capture to save a launch.
                want_mob = want_mobile and (name == "chromium")
                try:
                    eng_results[name] = _engine_capture(
                        p, name, url, want_mobile=want_mob,
                        timeout_ms=timeout_ms, shots_dir=shots, safe=safe,
                    )
                except Exception as exc:
                    d = _new_engine_diag()
                    d["error"] = f"{type(exc).__name__}: {exc}"
                    d["page_errors"].append("engine_launch_exception: "
                                            + traceback.format_exc().strip().splitlines()[-1])
                    eng_results[name] = d
    except Exception as exc:
        _log(f"RENDER domain={domain} available=False err=playwright-driver: {exc}")
        return _blank_diag(url, domain, f"playwright driver error: {exc}")

    available = any(e.get("ok") for e in eng_results.values())
    if not available:
        out = _blank_diag(url, domain, "no engine produced a render")
        out["engines"] = eng_results
        _log(f"RENDER domain={domain} available=False (no engine rendered)")
        return out

    reason_pairs = _derive_reasons(eng_results)
    reasons = [txt for _, txt in reason_pairs]
    severity = "ok"
    for sev, _ in reason_pairs:
        if SEVERITY_RANK.get(sev, 9) < SEVERITY_RANK.get(severity, 9):
            severity = sev

    cr = eng_results.get("chromium") or {}
    screenshots = {
        "desktop_png": cr.get("desktop_png"),
        "mobile_png": cr.get("mobile_png"),
        "webkit_png": (eng_results.get("webkit") or {}).get("desktop_png"),
    }

    anon_ok, anon_reasons = (None, [])
    if run_anon_gate:
        anon_ok, anon_reasons = _anon_check(domain, screenshots, eng_results)

    findings = {
        "ok": True,
        "available": True,
        "url": url,
        "domain": domain,
        "severity": severity,
        "reasons": reasons,
        "screenshots": screenshots,
        "engines": eng_results,
        "anon_ok": anon_ok,
        "anon_reasons": anon_reasons,
        # A screenshot may be surfaced to a prospect only if we truly captured one AND it cleared
        # the identity gate. reasons/console/network can inform the angle regardless (they carry no
        # operator identity), but the IMAGE ships only when shippable is True.
        "shippable": bool(screenshots.get("desktop_png")) and anon_ok is True,
        "captured_at": _now_iso(),
        "error": None,
    }
    _log(f"RENDER domain={domain} available=True severity={severity} "
         f"reasons={len(reasons)} shippable={findings['shippable']} anon_ok={anon_ok}")
    return findings


def strip_internal(findings: dict) -> dict:
    """Return a copy safe to persist to a queue: drops the large internal `_dom_sample` (raw DOM)
    from each engine sub-dict. Call this before writing render-proof findings to a prospect rec."""
    out = dict(findings)
    engs = out.get("engines") or {}
    out["engines"] = {
        k: {kk: vv for kk, vv in (v or {}).items() if not kk.startswith("_")}
        for k, v in engs.items()
    }
    return out


def render_proof_batch(domains, *, want_mobile: bool = True,
                       timeout_ms: int = GOTO_TIMEOUT_MS) -> list[dict]:
    """Serial fan-out over render_proof(). Each URL is fully isolated: one failing/slow domain
    degrades to its own _blank_diag and the loop continues. Kept serial (a real browser per URL is
    heavy) — a caller wanting throughput should bound concurrency itself, mindful of runaway_guard
    (a webkit/chromium render sustaining >85% of a core for >180s is a SIGKILL candidate)."""
    results = []
    for d in domains:
        d = (d or "").strip()
        if not d or d.startswith("#") or d.startswith("--"):
            continue
        try:
            results.append(render_proof(d, want_mobile=want_mobile, timeout_ms=timeout_ms))
        except Exception as exc:  # belt-and-suspenders; render_proof already fail-safes
            url, domain, _ = _to_target(d)
            results.append(_blank_diag(url, domain, f"batch: {type(exc).__name__}: {exc}"))
    return results


def _selftest() -> int:
    """Truth-by-construction proof: render a local page engineered with a KNOWN console error, a
    KNOWN uncaught exception, and a KNOWN dead image, then assert render_proof CAPTURED exactly
    those real events (never invented). Also asserts fail-safe on an SSRF-blocked host."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="render_proof_selftest_"))
    html = tmp / "boom.html"
    html.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><title>selftest</title></head>"
        "<body style='font-family:sans-serif;padding:40px'>"
        "<h1>render-proof self-test</h1><p>this page intentionally breaks.</p>"
        # dead image: .invalid never resolves -> requestfailed -> dead_images
        "<img src='https://nonexistent-selftest-host.invalid/missing.png' alt='x'>"
        "<script>console.error('SELFTEST_CONSOLE_BOOM');"
        "setTimeout(function(){throw new Error('SELFTEST_PAGEERROR_BOOM')},0);</script>"
        "</body></html>",
        encoding="utf-8",
    )
    f = render_proof(html.resolve().as_uri(), engines=("chromium",),
                     want_mobile=False, run_anon_gate=False)
    cr = (f.get("engines") or {}).get("chromium") or {}
    checks = {
        "available": f.get("available") is True,
        "console_error_captured": any("SELFTEST_CONSOLE_BOOM" in e for e in cr.get("console_errors", [])),
        "page_error_captured": any("SELFTEST_PAGEERROR_BOOM" in e for e in cr.get("page_errors", [])),
        "dead_image_captured": cr.get("img_failed", 0) >= 1,
        "desktop_png_valid": bool(f.get("screenshots", {}).get("desktop_png")),
        "reasons_nonempty": len(f.get("reasons", [])) >= 1,
    }
    # fail-safe: an internal host must degrade, never raise or fabricate a finding.
    blocked = render_proof("http://127.0.0.1", engines=("chromium",), want_mobile=False)
    checks["ssrf_degrades_safely"] = (
        blocked.get("available") is False
        and blocked.get("severity") == "ok"
        and blocked.get("reasons") == []
        and blocked.get("error") == "blocked-non-public-host"
    )
    ok = all(checks.values())
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print("RENDER-PROOF SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m core.render_proof",
                                 description="Real-browser render-proof probe (Chromium + WebKit).")
    ap.add_argument("targets", nargs="*", help="domains or URLs")
    ap.add_argument("--file", help="file with one domain/URL per line")
    ap.add_argument("--json", action="store_true", help="emit full findings JSON")
    ap.add_argument("--broken-only", action="store_true", help="only print targets with findings")
    ap.add_argument("--no-mobile", action="store_true", help="skip the mobile viewport pass")
    ap.add_argument("--chromium-only", action="store_true", help="skip the WebKit parity pass")
    ap.add_argument("--timeout", type=int, default=GOTO_TIMEOUT_MS, help="goto timeout (ms)")
    ap.add_argument("--selftest", action="store_true", help="run the truth-by-construction self-test")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if args.selftest:
        return _selftest()

    targets = list(args.targets)
    if args.file:
        targets += [l for l in Path(args.file).read_text(encoding="utf-8").splitlines()]
    if not targets:
        ap.print_usage()
        return 1

    engines = ("chromium",) if args.chromium_only else ("chromium", "webkit")
    out = []
    for t in targets:
        t = (t or "").strip()
        if not t or t.startswith("#"):
            continue
        f = render_proof(t, engines=engines, want_mobile=not args.no_mobile, timeout_ms=args.timeout)
        if args.broken_only and not f.get("reasons"):
            continue
        out.append(strip_internal(f))

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for f in out:
            tag = f["severity"] if f.get("available") else "degraded"
            body = "; ".join(f.get("reasons", [])) or ("no runtime issues" if f.get("available") else f.get("error", ""))
            print(f"[{tag:>8}] {f.get('domain') or f.get('url'):<32} {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
