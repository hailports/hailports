#!/usr/bin/env python3
"""proof_video_generator.py — render short PROOF videos and stage them PUBLISH-READY.

Produces two kinds of short vertical proof clips and makes them publish-ready into the
existing @hailports pipeline WITHOUT ever posting:

  • kind="pipeline" — proof of the fixed content-agent pipeline running (build-in-public
    terminal cards; optionally seeded from a real pipeline log, PII-filtered per line).
  • kind="site"     — proof of the live @hailports site (assembled from LOCAL screen
    captures if --site-assets is given, else a terminal card fallback).

DESIGN / SAFETY CONTRACT
------------------------
1. RENDERING reuses the proven helpers in agents/hailports_short_renderer.py
   (render_short + _encode). We do NOT reinvent any ffmpeg wiring.
2. EVERY produced video is run through tools/video_anonymity_gate.scan_video_package
   BEFORE it is eligible to stage. A hard-severity gate failure is a HARD STOP: the
   artifact is discarded, never staged, never promoted. The gate is never bypassed.
   (Frame text is ALSO pre-filtered through the same gate so no PII lands in a frame.)
3. This tool NEVER posts. It has no import of any poster and calls no post function.
   Staging writes publish-ready renders to a proof staging dir the posters do NOT sweep.
4. Wiring into the live @hailports queue (data/runtime/youtube/hailports/<fp>/) — the
   SAME queue hailports_tiktok_poster / hailports_x_video consume — is gated behind an
   explicit --publish flag (default OFF). Even when promoted, the actual post remains a
   separate, human-armed step in the posters themselves. --publish is refused while
   AUTOFLOW_OFF is present.

Examples
--------
  # default: render + gate + stage publish-ready (NOT wired into the live queue, no post)
  python3 tools/proof_video_generator.py --dry

  # human, later: promote gate-passed staged artifacts into the live @hailports queue
  python3 tools/proof_video_generator.py --publish
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
sys.path.insert(0, str(ROOT))

# ── reuse the proven renderer helpers (do NOT reinvent ffmpeg wiring) ────────────────
from agents.hailports_short_renderer import render_short, _encode, _font, W, H  # noqa: E402

# ── the MANDATORY anonymity gate every published video must pass ─────────────────────
from tools.video_anonymity_gate import scan_video_package  # noqa: E402

# ── the SAME @hailports pipeline the tiktok poster + x_video read (video.mp4 + metadata.json) ──
QUEUE_DIR = ROOT / "data" / "runtime" / "youtube" / "hailports"
# publish-ready staging the posters do NOT sweep; promotion into QUEUE_DIR is --publish-gated.
STAGING_DIR = ROOT / "data" / "runtime" / "youtube" / "hailports_proof"
# global kill-switch (core/autonomous_send.py convention): present => do not wire live.
AUTOFLOW_OFF = ROOT / "data" / "hustle" / "AUTOFLOW_OFF"

FP_FMT = "%Y%m%d_%H%M%S"  # render-dir fingerprint the pipeline sorts + dedupes by

# Local roots to scrub out of any frame-bound text (usernamed paths, home dir).
_LOCAL_ROOTS = sorted({str(ROOT), os.path.expanduser("~")}, key=len, reverse=True)


# ── gate-safe frame text ─────────────────────────────────────────────────────────────
def _strip_roots(text: str) -> str:
    out = text
    for r in _LOCAL_ROOTS:
        out = out.replace(r, "~")
    return out


def _frame_safe(text: str) -> bool:
    """A frame line is safe iff the anonymity gate finds NO hard violation in it.
    Frame text is not scanned by the gate at publish time, so we self-enforce the SAME
    rule here — no PII, secret, employer, identity, or usernamed path ever hits a pixel."""
    return scan_video_package({"description": _strip_roots(text)})["ok"]


def _safe_lines(raw: list[str], limit: int = 6) -> list[str]:
    """Root-strip, then drop any line the gate would flag. Empty list if nothing survives."""
    out: list[str] = []
    for ln in raw:
        s = _strip_roots(ln.strip())
        if s and _frame_safe(s):
            out.append(s[:64])
        if len(out) >= limit:
            break
    return out


# ── proof creative (PII-free by construction) ────────────────────────────────────────
def _pipeline_beats(log_lines: list[str] | None) -> list[dict]:
    """Terminal build-in-public cards proving the fixed content-agent pipeline is running.
    If a real (PII-filtered) log tail is supplied, its surviving lines seed the receipts card."""
    receipts = _safe_lines(log_lines or [], limit=2) or ["draft -> gate -> stage", "green, reversible, logged"]
    return [
        {"secs": 4.5, "label": "pipeline", "lines": ["the content agent", "is running green"],
         "sub": "fixed + reversible — no human in the loop"},
        {"secs": 4.0, "label": "render", "lines": ["draft -> gate ->", "publish-ready"],
         "sub": "every step logged, every step reversible"},
        {"secs": 5.0, "label": "receipts", "lines": [receipts[0], receipts[-1]],
         "sub": "real renders, not screenshots", "cta": "watch it run in public"},
    ]


def _site_beats() -> list[dict]:
    """Fallback terminal cards for the live-site proof when no local captures are supplied."""
    return [
        {"secs": 4.5, "label": "live", "lines": ["the @hailports site", "is live right now"],
         "sub": "on the record, in the open"},
        {"secs": 4.5, "label": "proof", "lines": ["walk the live site,", "not a mockup"],
         "sub": "shipped in public", "cta": "link in bio"},
    ]


def _transcript(beats: list[dict]) -> str:
    """Domain-free transcript from the beat lines (gate-safe; never carries the .com domain)."""
    parts: list[str] = []
    for b in beats:
        parts.extend(b.get("lines", []))
        if b.get("sub"):
            parts.append(b["sub"])
    return " ".join(_strip_roots(p) for p in parts if p)


# ── assemble the live-site clip from LOCAL screen captures (reuse _encode) ────────────
def _letterbox(im, w: int, h: int):
    from PIL import Image
    im = im.convert("RGB")
    scale = min(w / im.width, h / im.height)
    nw, nh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), (5, 8, 13))
    canvas.paste(im, ((w - nw) // 2, (h - nh) // 2))
    return canvas


def _assemble_captures(images: list[Path], out: Path, per_secs: float = 2.5, fps: int = 6) -> str | None:
    """Letterbox each local capture to 1080x1920 and stitch via the renderer's _encode helper
    (same ffmpeg wiring the shorts use). Returns the mp4 path or None on encode failure."""
    from PIL import Image
    frames = tempfile.mkdtemp(prefix="proofvid_f_")
    try:
        idx = 0
        hold = max(1, int(round(per_secs * fps)))
        for p in images:
            base = _letterbox(Image.open(p), W, H)
            for _ in range(hold):
                base.save(f"{frames}/f{idx:04d}.png")
                idx += 1
        if idx == 0:
            return None
        total = idx / float(fps)
        return _encode(frames, total, fps, str(out))
    finally:
        shutil.rmtree(frames, ignore_errors=True)


# ── metadata (gate-safe: domain-free; handle attribution is metadata-only) ───────────
def _build_meta(kind: str, mp4: Path, transcript: str) -> dict:
    titles = {
        "pipeline": "the content agent, running green in public",
        "site": "the live site walkthrough, on the record",
    }
    descs = {
        "pipeline": "a fixed, reversible content pipeline shipping build-in-public proof — @hailports",
        "site": "a walkthrough of the live @hailports site — link in bio",
    }
    return {
        "channel": "hailports",              # the @hailports pipeline (handle is the attribution)
        "handle": "@hailports",              # metadata only — bare handle, never the .com domain
        "kind": "proof",
        "proof_kind": kind,
        "title": titles[kind],
        "description": descs[kind],
        "transcript": transcript,
        "tags": ["buildinpublic", "aiagents", "automation", "proof"],
        "path": str(mp4),
        "source": "proof_video_generator",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ready_for_upload",
        "gated": {"video_anonymity_gate": True},
        "uploaded": False,                   # NEVER auto-posted; first post is a human trigger
        "posted": False,
    }


# ── staging + (flag-gated) promotion into the live queue ─────────────────────────────
def _stage(fp: str, mp4: Path, meta: dict, dest_root: Path) -> Path:
    d = dest_root / fp
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mp4, d / "video.mp4")
    m = {**meta, "path": str(d / "video.mp4")}
    (d / "metadata.json").write_text(json.dumps(m, indent=2))
    return d


def _process(kind: str, *, log_lines: list[str] | None, site_assets: list[Path] | None,
             fps: int, publish: bool, dry: bool) -> dict:
    """Render -> gate -> stage one proof clip. Returns a record. NEVER posts."""
    rec: dict = {"kind": kind, "gate_ok": False, "staged": None, "promoted": False, "posted": False}
    day = int(datetime.now().strftime("%j"))
    work = Path(tempfile.mkdtemp(prefix="proofvid_"))
    try:
        out = work / f"{kind}.mp4"
        # 1) RENDER (reusing the proven renderer/ffmpeg helpers)
        if kind == "site" and site_assets:
            path = _assemble_captures(site_assets, out, fps=fps)
            beats = _site_beats()  # transcript source only
        else:
            beats = _pipeline_beats(log_lines) if kind == "pipeline" else _site_beats()
            path = render_short(day=day, out=str(out), beats=beats, style="terminal", fps=fps)
        if not path or not os.path.exists(path):
            rec["error"] = "render failed"
            return rec
        rec["rendered_bytes"] = os.path.getsize(path)

        # 2) METADATA + MANDATORY ANONYMITY GATE (hard stop on any hard violation)
        meta = _build_meta(kind, Path(path), _transcript(beats))
        gate = scan_video_package(meta)          # <== video_anonymity_gate.py CALL SITE
        rec["gate_ok"] = gate["ok"]
        rec["violations"] = gate["violations"]
        if not gate["ok"]:
            # HARD STOP — discard the artifact; never stage, never promote, never bypass.
            rec["error"] = "ANON GATE FAILED — artifact discarded (hard stop)"
            return rec

        # 3) STAGE publish-ready (posters do NOT sweep the proof staging dir)
        fp = datetime.now().strftime(FP_FMT) + f"_{kind}"
        staged = _stage(fp, Path(path), meta, STAGING_DIR)
        rec["staged"] = str(staged)
        rec["fp"] = fp

        # 3b) defense-in-depth: re-gate the STAGED metadata before it is ever eligible to move
        staged_meta = json.loads((staged / "metadata.json").read_text())
        if not scan_video_package(staged_meta)["ok"]:
            shutil.rmtree(staged, ignore_errors=True)
            rec["staged"] = None
            rec["error"] = "staged artifact failed re-gate — removed (hard stop)"
            return rec

        # 4) PROMOTE into the live @hailports queue — ONLY behind the explicit flag
        if publish and not dry:
            if AUTOFLOW_OFF.exists():
                rec["publish_skipped"] = "AUTOFLOW_OFF present — not wiring into the live queue"
            else:
                live = _stage(fp, staged / "video.mp4", staged_meta, QUEUE_DIR)
                rec["promoted"] = True
                rec["queue_dir"] = str(live)
        return rec
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render + gate + stage @hailports proof videos (never posts).")
    ap.add_argument("--dry", action="store_true",
                    help="Render + gate + stage publish-ready WITHOUT promoting into the live queue.")
    ap.add_argument("--publish", action="store_true",
                    help="Explicit opt-in (default OFF): promote gate-passed staged artifacts into the "
                         "live @hailports queue. NEVER posts; refused while AUTOFLOW_OFF is present.")
    ap.add_argument("--only", choices=["pipeline", "site", "both"], default="both")
    ap.add_argument("--site-assets", default=None,
                    help="Directory of LOCAL screen captures (.png/.jpg) to assemble the live-site clip.")
    ap.add_argument("--pipeline-log", default=None,
                    help="Optional real pipeline log; its tail seeds the receipts card (PII-filtered).")
    ap.add_argument("--fps", type=int, default=6)
    args = ap.parse_args(argv)

    # gather optional local inputs
    log_lines = None
    if args.pipeline_log:
        try:
            log_lines = Path(args.pipeline_log).read_text(errors="replace").splitlines()[-40:]
        except Exception as e:
            print(f"warn: could not read --pipeline-log: {e}", file=sys.stderr)
    site_assets = None
    if args.site_assets:
        d = Path(args.site_assets)
        if d.is_dir():
            site_assets = sorted(p for p in d.iterdir()
                                 if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        if not site_assets:
            print(f"warn: no captures found in {args.site_assets}; site clip falls back to a card",
                  file=sys.stderr)

    kinds = ["pipeline", "site"] if args.only == "both" else [args.only]
    records = [
        _process(k, log_lines=log_lines, site_assets=site_assets,
                 fps=args.fps, publish=args.publish, dry=args.dry)
        for k in kinds
    ]

    report = {
        "dry": args.dry,
        "publish_flag": args.publish,
        "autoflow_off": AUTOFLOW_OFF.exists(),
        "staging_dir": str(STAGING_DIR),
        "queue_dir": str(QUEUE_DIR),
        "posted": False,  # this tool NEVER posts
        "results": records,
    }
    print(json.dumps(report, indent=2))

    # non-zero exit if anything failed the gate or failed to render/stage
    failed = [r for r in records if r.get("error")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
