#!/usr/bin/env python3
"""brand_email.py — per-brand HTML email skin (house-of-competing-brands).

ONE theme token object per brand is the single source of truth for BOTH the
storefront (products/self_serve/app.py STOREFRONT_BRANDS[*]["theme"]) AND the
brand's outbound email. _email_skin(theme) maps the identical tokens into an
inline-CSS HTML email skin, so a brand's email always matches its site and no
two brands share a palette — the brand-consistency guarantee and the
anti-fingerprint invariant in one object.

Compliance authority stays with agents/core/canspam.py: it produces the
deliverability-safe PLAIN-TEXT part (body + the SHARED postal-address footer +
List-Unsubscribe). This module only adds the themed multipart/alternative HTML
skin built from the SAME postal address (canspam.postal_address via
canspam.build_footer) — the HTML footer can never carry a different address.

Identity protection (HARD): pseudonymous signers only, never Operator/Operator,
never a shared persona across brands, no "our other brands" tell.

Run `python3 -m core.brand_email --selftest` for unit checks.
"""
from __future__ import annotations

import html as _html
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from agents.core import canspam  # type: ignore
except Exception:  # pragma: no cover - canspam should always be importable
    canspam = None  # type: ignore

_SYSTEM_FONT = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"

# ── Brand registry ────────────────────────────────────────────────────────────
# theme tokens mirror products/self_serve/app.py STOREFRONT_BRANDS[*]["theme"]
# (plus the two anonymous self-serve lanes, scannerapp/docsapp). Keep them in
# lockstep with the storefront — same object, two surfaces. persona_name is a
# pseudonymous signer, unique per brand, never Operator/Operator.
BRANDS: dict[str, dict] = {
    "builtfast": {
        "key": "builtfast", "brand_name": "Built Fast", "host": "redacted.com",
        "from_email": "user@example.com", "from_name": "Built Fast",
        "persona_name": "Theo", "kicker": "// done-for-you systems, shipped",
        "theme": {"bg": "#0b0f17", "accent": "#22d3ee", "accent2": "#a3e635", "text": "#e8f0f7", "muted": "#8b9bb0", "card": "#131a26", "border": "#243044"},
    },
    "signalhq": {
        "key": "signalhq", "brand_name": "BuyerSignal HQ", "host": "redacted.com",
        "from_email": "user@example.com", "from_name": "BuyerSignal HQ",
        "persona_name": "Mara", "kicker": "reach the hand already raised",
        "theme": {"bg": "#071a2e", "accent": "#2ee6a6", "accent2": "#4d9fff", "text": "#e6eef7", "muted": "#8aa0bd", "card": "#0d243d", "border": "#1c3a5c"},
    },
    "hailport": {
        "key": "hailport", "brand_name": "Hailport Research", "host": "hailports.com",
        "from_email": "user@example.com", "from_name": "Hailport Research",
        "persona_name": "J. Calder", "kicker": "we publish, you decide",
        "theme": {"bg": "#1a2230", "accent": "#d98a3d", "accent2": "#d98a3d", "text": "#e7e2d6", "muted": "#9aa3ad", "card": "#f4ede0", "cardink": "#20262e", "border": "#2c3644"},
    },
    "promptsite": {
        "key": "promptsite", "brand_name": "PromptSite", "host": "promptsite.app",
        "from_email": "user@example.com", "from_name": "PromptSite",
        "persona_name": "Robin", "kicker": "your site and socials, always on",
        "theme": {"bg": "#fdfcff", "accent": "#6d28d9", "accent2": "#db2777", "text": "#1f1633", "muted": "#6b6280", "card": "#ffffff", "border": "#ece6f5"},
    },
    "opsapp": {
        "key": "opsapp", "brand_name": "opsapp", "host": "opsapp.app",
        "from_email": "user@example.com", "from_name": "opsapp",
        "persona_name": "Sam", "kicker": "the back-office work, handled",
        "theme": {"bg": "#11161c", "accent": "#14b8a6", "accent2": "#64748b", "text": "#eef2f4", "muted": "#93a1ad", "card": "#171d25", "border": "#283039"},
    },
    "frontcounter": {
        "key": "frontcounter", "brand_name": "Front Counter", "host": "opsapp.us",
        "from_email": "user@example.com", "from_name": "Front Counter",
        "persona_name": "Dale", "kicker": "run your shop, we'll handle the online part",
        "theme": {"bg": "#faf6ec", "accent": "#2f6b4f", "accent2": "#c4683a", "text": "#2a2620", "muted": "#6b6355", "card": "#fffdf8", "border": "#ddd0b8"},
    },
    "scannerapp": {
        "key": "scannerapp", "brand_name": "scannerapp", "host": "scannerapp.dev",
        "from_email": "user@example.com", "from_name": "scannerapp Team",
        "persona_name": "scannerapp Team", "kicker": "scan -> score -> fix",
        "theme": {"bg": "#0a1622", "accent": "#38bdf8", "accent2": "#34d399", "text": "#e2eef7", "muted": "#8aa0bd", "card": "#0e2034", "border": "#1c3a55"},
    },
    "docsapp": {
        "key": "docsapp", "brand_name": "docsapp", "host": "docsapp.dev",
        "from_email": "user@example.com", "from_name": "docsapp Team",
        "persona_name": "docsapp Team", "kicker": "clean it up before AI amplifies it",
        "theme": {"bg": "#0e1726", "accent": "#f59e0b", "accent2": "#60a5fa", "text": "#e8eef7", "muted": "#8b97ad", "card": "#131d2e", "border": "#243349"},
    },
}

# host + brand_name -> key, so resolve_brand accepts key / domain / display name.
_ALIASES: dict[str, str] = {}
for _k, _b in BRANDS.items():
    _ALIASES[_k] = _k
    _ALIASES[_b["brand_name"].lower()] = _k
    _ALIASES[_b["host"].lower()] = _k
    _ALIASES["www." + _b["host"].lower()] = _k
# legacy sender-pool strings (opsapp vs opsapp) -> resolve, don't return None.
_ALIASES.update({"opsdeck": "opsapp", "opsapp.app": "opsapp", "opsdeck team": "opsapp"})


def resolve_brand(brand: str | dict | None) -> dict | None:
    """Look up a brand record by key, host, or display name (case-insensitive).
    Pass-through if given a dict that already carries a 'theme'."""
    if isinstance(brand, dict):
        return brand if "theme" in brand else BRANDS.get(_ALIASES.get(str(brand.get("key", "")).lower(), ""))
    key = _ALIASES.get(str(brand or "").strip().lower())
    return BRANDS.get(key) if key else None


# ── Theme -> email skin (pure) ────────────────────────────────────────────────

def _lum(hex_color: str) -> float:
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return 0.0
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return 0.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _email_skin(theme: dict) -> dict:
    """Map the SAME storefront theme tokens to an inline-CSS email skin.

    Tokens consumed (identical to the site layouts): bg, accent, accent2(opt),
    text, muted, card, border, cardink(opt light-card ink), btntext(opt, default
    bg), font(opt, default system stack). Derived keys (band/link/hairline/...)
    are computed from the source so site and email cannot drift.
    """
    bg = theme.get("bg", "#0b0f17")
    accent = theme.get("accent", "#22d3ee")
    accent2 = theme.get("accent2", accent)
    text = theme.get("text", "#e8f0f7")
    muted = theme.get("muted", "#8b9bb0")
    card = theme.get("card", "#131a26")
    border = theme.get("border", "#243044")
    # editorial light cards (e.g. Hailport) carry their own ink for body text.
    ink = theme.get("cardink", text)
    # button text defaults to bg, exactly like the storefront CTA (color:bg).
    btntext = theme.get("btntext", bg)
    # wordmark sits ON the accent band; contrast against the accent, not bg.
    band_ink = btntext if "btntext" in theme else ("#0b0f17" if _lum(accent) > 0.6 else "#ffffff")
    return {
        "font": theme.get("font", _SYSTEM_FONT),
        "page_bg": bg,
        "card_bg": card,
        "ink": ink,
        "muted": muted,
        "band": accent,
        "band_ink": band_ink,
        "link": accent2,
        "hairline": border,
        "btn_grad": f"linear-gradient(135deg,{accent},{accent2})",
        "btn_text": btntext,
    }


# ── HTML rendering ────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"(https?://[^\s<>\")]+)")


def _linkify(escaped_text: str, link_color: str) -> str:
    return _URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" style="color:{link_color};text-decoration:underline">{m.group(1)}</a>',
        escaped_text,
    )


def _paragraphs(body: str, sk: dict) -> str:
    blocks = re.split(r"\n\s*\n", (body or "").strip())
    out = []
    for blk in blocks:
        if not blk.strip():
            continue
        esc = _linkify(_html.escape(blk.strip()), sk["link"]).replace("\n", "<br>")
        out.append(
            f'<p style="margin:0 0 16px;color:{sk["ink"]};font-size:15px;'
            f'line-height:1.6">{esc}</p>'
        )
    return "".join(out)


def render_html(brand: dict, *, subject: str, body: str, footer_text: str) -> str:
    """Wrap a plain-text body in the brand's themed HTML email skin.

    `footer_text` MUST come from canspam.build_footer so the HTML footer carries
    the identical SHARED postal address + unsubscribe wording as the text part.
    """
    sk = _email_skin(brand.get("theme", {}))
    name = brand.get("brand_name", "")
    kicker = brand.get("kicker", "")
    kicker_html = (
        f'<div style="font-size:12px;letter-spacing:.4px;opacity:.85;'
        f'margin-top:4px;color:{sk["band_ink"]}">{_html.escape(kicker)}</div>'
        if kicker else ""
    )
    footer_html = _linkify(_html.escape(footer_text.strip()), sk["link"]).replace("\n", "<br>")
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_html.escape(subject)}</title></head>"
        f'<body style="margin:0;padding:0;background:{sk["page_bg"]};'
        f'font-family:{sk["font"]}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{sk["page_bg"]}"><tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:560px;background:{sk["card_bg"]};border:1px solid {sk["hairline"]};'
        'border-radius:12px;overflow:hidden">'
        # header band (accent) with brand wordmark
        f'<tr><td style="background:{sk["band"]};padding:18px 28px">'
        f'<div style="font-size:18px;font-weight:800;letter-spacing:-.3px;'
        f'color:{sk["band_ink"]}">{_html.escape(name)}</div>{kicker_html}</td></tr>'
        # body
        f'<tr><td style="padding:26px 28px 8px">{_paragraphs(body, sk)}</td></tr>'
        # hairline + footer (shared postal address, themed-muted)
        f'<tr><td style="padding:0 28px"><hr style="border:0;border-top:1px solid '
        f'{sk["hairline"]};margin:8px 0"></td></tr>'
        f'<tr><td style="padding:8px 28px 26px;color:{sk["muted"]};font-size:12px;'
        f'line-height:1.6">{footer_html}</td></tr>'
        "</table></td></tr></table></body></html>"
    )


def render_email(
    brand: str | dict,
    *,
    subject: str,
    body: str,
    to_email: str,
    reason: str | None = None,
    unsubscribe_url: str | None = None,
) -> dict:
    """Build a CAN-SPAM-compliant, brand-themed multipart email.

    Returns {ok, problems, from, to, subject, text, html, headers, brand}.
    `text`/`headers`/`ok`/`problems` come straight from canspam.render (the
    compliance authority); `html` is the themed multipart/alternative skin built
    from the SAME postal-address footer. Callers MUST refuse to send when ok is
    False. The plain-text part remains the deliverability-safe fallback.
    """
    rec = resolve_brand(brand)
    if rec is None:
        return {"ok": False, "problems": ["unknown_brand"], "text": "", "html": "",
                "headers": {}, "from": "", "to": to_email, "subject": subject, "brand": str(brand)}
    if canspam is None:
        return {"ok": False, "problems": ["canspam_unavailable"], "text": "", "html": "",
                "headers": {}, "from": "", "to": to_email, "subject": subject, "brand": rec["key"]}

    rendered = canspam.render(
        subject=subject, body=body,
        from_email=rec["from_email"], from_name=rec["from_name"],
        brand=rec["brand_name"], to_email=to_email,
        unsubscribe_url=unsubscribe_url, reason=reason,
    )
    footer_text, _ = canspam.build_footer(
        rec["from_email"], rec["brand_name"],
        unsubscribe_url=unsubscribe_url, reason=reason,
    )
    html = render_html(rec, subject=rendered["subject"], body=body, footer_text=footer_text)
    return {
        "ok": rendered["ok"],
        "problems": rendered["problems"],
        "from": rendered["from"],
        "to": rendered["to"],
        "subject": rendered["subject"],
        "text": rendered["body"],   # plain-text part (footer already appended)
        "html": html,               # themed multipart/alternative part
        "headers": rendered["headers"],
        "brand": rec["key"],
    }


# ── Self-test ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    os.environ.setdefault("COLD_OUTREACH_POSTAL_ADDRESS", "TEST ADDR, 1 Test St, Testville, CA 90000")
    fails: list[str] = []

    if canspam is None:
        print("BRAND_EMAIL SELFTEST FAILED: canspam not importable")
        return 1
    addr = canspam.postal_address() or ""

    seen_bands: dict[str, str] = {}
    seen_personas: dict[str, str] = {}
    for key, rec in BRANDS.items():
        r = render_email(
            key, subject="quick idea for your team",
            body="Hi there,\n\nSaw your post — here's the one fix that matters.\n\nTake a look: https://example.com/x\n\n— " + rec["brand_name"],
            to_email="user@example.com",
            reason=f"{rec['brand_name']} sent you this as a one-time business message.",
        )
        if not r["ok"]:
            fails.append(f"{key}: render not ok: {r['problems']}")
        if not addr or addr not in r["text"] or addr not in r["html"]:
            fails.append(f"{key}: shared postal address missing from text or html")
        if rec["brand_name"] not in r["html"]:
            fails.append(f"{key}: wordmark missing from html")
        sk = _email_skin(rec["theme"])
        if sk["band"].lower() not in r["html"].lower():
            fails.append(f"{key}: accent band color missing from html")
        seen_bands.setdefault(sk["band"].lower(), key)
        if sk["band"].lower() in seen_bands and seen_bands[sk["band"].lower()] != key:
            fails.append(f"{key}: palette collides with {seen_bands[sk['band'].lower()]}")
        if rec["persona_name"] in seen_personas and seen_personas[rec["persona_name"]] != key:
            fails.append(f"{key}: persona '{rec['persona_name']}' shared with another brand")
        seen_personas[rec["persona_name"]] = key
        blob = (str(rec) + r["html"]).lower()
        if "Operator" in blob or "Operator" in blob:
            fails.append(f"{key}: identity leak (Operator/Operator)")

    # consumer recipient still blocked through the themed path
    bad = render_email("signalhq", subject="hi", body="hi", to_email="user@example.com")
    if bad["ok"]:
        fails.append("themed render allowed a consumer recipient")

    if fails:
        print("BRAND_EMAIL SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"BRAND_EMAIL SELFTEST PASSED ({len(BRANDS)} brands, palettes+personas distinct, postal address in every email)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
