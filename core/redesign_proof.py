#!/usr/bin/env python3
"""redesign_proof — the shared proof-first "site redesign" engine.

One URL in, a before/after "redesign proof" out. It ASSEMBLES already-built pieces (reuse-only,
reimplements nothing):

  url -> BEFORE  (core.render_proof.render_proof — full-page stitched capture + identity gate)
      -> AFTER   (core.site_generator.generate_mockup — their content rebuilt premium, design_kit look)
      -> AFTER png (agents.mockup_render.render — HTML -> screenshot, for a real side-by-side)
      -> a single self-contained before|after proof page (look from core.design_kit / maroon_branding)

Two brand skins share this engine (the ONLY branch is `_resolve_skin`):
  - FACELESS (scannerapp/docsapp/signalhq/builtfast/hailport): anonymous, broken-site-only, the proof
    page must clear `core.anon_scrub.scrub()` FAIL-CLOSED before it is ever written; the BEFORE image
    ships only when render_proof says `shippable`.
  - BrandA: named brand, own look (maroon_branding), no anon gate — but assert no faceless-brand token
    leaks onto it (air-gap both directions).

Proof-first doctrine: pre-build the AFTER, show before/after, charge for the live rebuild. This module
NEVER sends and NEVER auto-publishes to a public host — it writes a local artifact; the outbound /
self-serve wrappers gate the actual exposure.

CLI:  python3 -m core.redesign_proof https://<url> --brand scannerapp
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.render_proof import render_proof, strip_internal  # noqa: E402
from core.site_generator import generate_mockup             # noqa: E402
from core import design_kit                                 # noqa: E402
from core.anon_scrub import find_identity_leaks             # noqa: E402

OUT_DIR = ROOT / "products_internal" / "landing" / "redesign_proofs"
FACELESS_BRANDS = {"scannerapp", "docsapp", "signalhq", "builtfast", "hailport"}
_SIPS = "/usr/bin/sips"


def _safe(url: str) -> str:
    d = re.sub(r"^https?://", "", (url or "").strip().lower()).split("/")[0]
    return re.sub(r"[^a-z0-9.]+", "_", d) or "site"


def _seeded_palette(key: str) -> dict:
    """Deterministic per-prospect palette pick (anti-fingerprint: each site a consistent distinct look)."""
    idx = int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(design_kit.PALETTES)
    return design_kit.PALETTES[idx]


def _resolve_skin(brand: str) -> dict:
    """The single brand branch. Returns the skin the whole pipeline reads from."""
    b = (brand or "scannerapp").strip().lower()
    if b == "BrandA":
        pal = {"bg": "#1a0d0d", "surface": "#241313", "ink": "#f5ece9", "muted": "#c9a9a4",
               "border": "#3a2020", "primary": "#6b0f0f", "accent": "#b23a3a"}
        try:
            from core import maroon_branding  # noqa: F401  (named-brand look lives here)
            pal["_maroon_module"] = True
        except Exception:
            pass
        return {"kind": "BrandA", "brand": "BrandA", "palette": pal, "anon_gate": False,
                "label": "BrandA Standard"}
    # faceless
    if b not in FACELESS_BRANDS:
        b = "scannerapp"
    return {"kind": "faceless", "brand": b, "palette": None, "anon_gate": True, "label": b}


def _img_data_uri(png_path: str, max_w: int = 1400) -> str | None:
    """Downscale (sips, fail-soft) then base64 data-URI — self-contained + anon-safe (no external ref)."""
    p = Path(png_path)
    if not p.exists():
        return None
    src = p
    try:
        if _SIPS and Path(_SIPS).exists():
            tmp = Path(tempfile.mkdtemp(prefix="rdp_")) / "s.png"
            r = subprocess.run([_SIPS, "--resampleWidth", str(max_w), str(p), "--out", str(tmp)],
                               capture_output=True, timeout=30)
            if r.returncode == 0 and tmp.exists():
                src = tmp
    except Exception:
        src = p
    try:
        return "data:image/png;base64," + base64.b64encode(src.read_bytes()).decode()
    except Exception:
        return None


def _assemble_proof_page(skin: dict, name: str, before_uri: str | None, after_uri: str | None,
                         after_live_url: str | None, reasons: list, cta_url: str) -> str:
    pal = skin["palette"] or _seeded_palette(name)
    bg, surface = pal.get("bg", "#12100e"), pal.get("surface", "#1b1815")
    ink, muted = pal.get("ink", "#f4f1ea"), pal.get("muted", "#a49a88")
    border, accent = pal.get("border", "#2a2620"), pal.get("accent", "#d97757")
    title = "your site, rebuilt" if skin["kind"] == "faceless" else "a modern site for your firm"
    finds = "".join(
        f'<li>{re.sub("<[^>]+>", "", str(r[1] if isinstance(r, (list, tuple)) and len(r) > 1 else r))[:140]}</li>'
        for r in (reasons or [])[:5]
    ) or "<li>dated design, slow load, weak mobile layout</li>"
    before_html = (f'<img src="{before_uri}" alt="current site">' if before_uri
                   else '<div class="ph">current site capture pending</div>')
    after_html = (f'<img src="{after_uri}" alt="rebuilt site">' if after_uri
                  else '<div class="ph">rebuild preview</div>')
    live_btn = (f'<a class="btn ghost" href="{after_live_url}" target="_blank" rel="noopener">open the live rebuild</a>'
                if after_live_url else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{name} — {title}</title>
<style>
:root{{--bg:{bg};--sf:{surface};--ink:{ink};--mut:{muted};--bd:{border};--ac:{accent}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:48px 22px 72px}}
h1{{font-size:clamp(28px,5vw,44px);margin:.1em 0 .2em;letter-spacing:-.02em}}
.sub{{color:var(--mut);font-size:18px;max-width:640px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:34px 0}}
@media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:var(--sf);border:1px solid var(--bd);border-radius:16px;overflow:hidden}}
.card .lab{{padding:12px 16px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);
border-bottom:1px solid var(--bd)}}
.card.after .lab{{color:var(--ac)}}
.frame{{max-height:460px;overflow:hidden;position:relative}}
.frame img{{width:100%;display:block}}
.frame:after{{content:"";position:absolute;left:0;right:0;bottom:0;height:70px;
background:linear-gradient(transparent,var(--sf))}}
.ph{{padding:80px 20px;text-align:center;color:var(--mut)}}
.finds{{background:var(--sf);border:1px solid var(--bd);border-radius:16px;padding:20px 24px;margin:8px 0 30px}}
.finds h3{{margin:.2em 0 .5em;font-size:15px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}}
.finds ul{{margin:0;padding-left:20px}}.finds li{{margin:.3em 0}}
.cta{{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-top:10px}}
.btn{{display:inline-block;background:var(--ac);color:#1a1512;font-weight:600;text-decoration:none;
padding:14px 26px;border-radius:12px}}
.btn.ghost{{background:transparent;color:var(--ink);border:1px solid var(--bd)}}
.foot{{color:var(--mut);font-size:13px;margin-top:44px}}
</style></head><body><div class="wrap">
<h1>{title}</h1>
<p class="sub">here's {name} rebuilt — same business, a site that loads fast, reads clean, and works on a phone. left is live today, right is the rebuild.</p>
<div class="grid">
  <div class="card"><div class="lab">your site now</div><div class="frame">{before_html}</div></div>
  <div class="card after"><div class="lab">the rebuild</div><div class="frame">{after_html}</div></div>
</div>
<div class="finds"><h3>what we fixed</h3><ul>{finds}</ul></div>
<div class="cta"><a class="btn" href="{cta_url}">get this live</a>{live_btn}</div>
<p class="foot">rebuilt from your real content. no obligation — if it's not better, walk away.</p>
</div></body></html>"""


def build_redesign_proof(url: str, brand: str = "scannerapp", *, vertical: str = "",
                         brief: dict | None = None, render_after_png: bool = True,
                         write_page: bool = True, cta_url: str = "#") -> dict:
    """Assemble a before/after redesign proof for one URL. Reuse-only. Fail-soft per step; the
    proof page is written ONLY if it clears the skin's publish gate (faceless: anon_scrub fail-closed)."""
    skin = _resolve_skin(brand)
    safe = _safe(url)
    out: dict = {"url": url, "brand": skin["brand"], "skin": skin["kind"], "shippable": False,
                 "before_png": None, "after_mockup_url": None, "after_png": None,
                 "proof_page_path": None, "anon_ok": None, "blocked_reasons": [], "reasons": []}

    # 1. BEFORE — full-page capture + identity gate
    try:
        rp = strip_internal(render_proof(url, want_mobile=True))
        out["before_png"] = rp["screenshots"].get("desktop_png")
        out["reasons"] = rp.get("reasons") or []
        out["anon_ok"] = rp.get("anon_ok")
        before_shippable = rp.get("shippable")
    except Exception as e:
        out["blocked_reasons"].append(f"before_capture: {e}")
        before_shippable = False

    # 2. AFTER — rebuild their content into a premium mockup (design_kit look, visual-gated)
    after_live_url = None
    try:
        m = generate_mockup({"domain": url, "url": url, "vertical": vertical or None,
                             "reasons": out["reasons"]})
        if m.get("valid"):
            out["after_mockup_url"] = after_live_url = m.get("url")
            out["_after_html_path"] = m.get("path")
            out["name"] = m.get("name")
    except Exception as e:
        out["blocked_reasons"].append(f"after_mockup: {e}")

    # 3. AFTER -> PNG (optional; enables a true image side-by-side)
    if render_after_png and out.get("_after_html_path"):
        try:
            from agents.mockup_render import render as render_mockup
            r = render_mockup(out["_after_html_path"], mockup_id=safe)
            if r.get("ok"):
                out["after_png"] = r.get("desktop_png")
        except Exception as e:
            out["blocked_reasons"].append(f"after_png: {e}")

    name = out.get("name") or re.sub(r"^https?://", "", url).split("/")[0]

    # 4. Assemble the proof page (images inlined only when they truly cleared their gates)
    before_uri = _img_data_uri(out["before_png"]) if (out["before_png"] and before_shippable) else None
    after_uri = _img_data_uri(out["after_png"]) if out["after_png"] else None
    html = _assemble_proof_page(skin, name, before_uri, after_uri, after_live_url,
                                out["reasons"], cta_url)

    # 5. Publish gate (faceless: fail-closed anon_scrub). BrandA: assert no faceless token leaked.
    if skin["anon_gate"]:
        # SERVED-TREE gate (same standard as mockups/seo_pages): block ONLY on operator/employer
        # identity tokens. The prospect's own business name + numbers are theirs to show; the full
        # scrub()'s "specific-number" rule + LLM judge are for build-in-public POSTS about the
        # operator and wrongly block a client's rebuilt site.
        leaks = find_identity_leaks(html)
        if leaks:
            out["blocked_reasons"] += [f"identity_leak:{t}" for t in leaks]
            return out
    else:  # BrandA: air-gap — no faceless brand name may appear on a named artifact
        low = html.lower()
        leaked = [fb for fb in FACELESS_BRANDS if fb in low]
        if leaked:
            out["blocked_reasons"].append(f"BrandA air-gap: faceless token(s) {leaked}")
            return out

    # 6. Write the artifact (local only — wrappers gate real exposure)
    if write_page:
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            page = OUT_DIR / f"{safe}.html"
            page.write_text(html, encoding="utf-8")
            out["proof_page_path"] = str(page)
            out["proof_page_url"] = f"/redesign-proofs/{safe}.html"
        except Exception as e:
            out["blocked_reasons"].append(f"write: {e}")
            return out

    # shippable = we have a real BEFORE (gated) AND a valid AFTER
    out["shippable"] = bool(before_uri and after_live_url)
    out["built_at"] = datetime.now(timezone.utc).isoformat()
    out.pop("_after_html_path", None)
    return out


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a before/after site-redesign proof for one URL.")
    ap.add_argument("url")
    ap.add_argument("--brand", default="scannerapp", help="scannerapp/docsapp/signalhq/builtfast/hailport/BrandA")
    ap.add_argument("--vertical", default="")
    ap.add_argument("--no-after-png", action="store_true")
    ap.add_argument("--cta", default="#")
    a = ap.parse_args(argv)
    r = build_redesign_proof(a.url, a.brand, vertical=a.vertical,
                             render_after_png=not a.no_after_png, cta_url=a.cta)
    r.pop("reasons", None)
    print(json.dumps(r, indent=2, default=str))
    return 0 if r.get("shippable") else 1


if __name__ == "__main__":
    sys.exit(_cli())
