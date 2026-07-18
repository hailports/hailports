#!/usr/bin/env python3
"""hailports_card — site-themed social-card renderer (Pillow).

Renders @hailports build-in-public cards that match hailports.com EXACTLY: the dark
terminal-dashboard look — bg #05080d, green accent #39d98a, light ink #e6f1ec, muted
#8aa0b0, monospace type, a green `>` prompt mark, mac traffic-light chrome, an
auto-fit/auto-wrap HIGH-CONTRAST headline (never dark-on-dark, never overflowing),
a thin green accent rule, and a muted "build-in-public · autonomous" footer.

This is for hailports' OWN content (the X cards + the matching Short theme). It is NOT
the warm-paper PROSPECT-MOCKUP look in core/design_kit.py — those stay separate.

    from core.hailports_card import render_card
    render_card("i walked away from the whole company. it's still shipping.",
                "out.png", size="16x9")
"""
from __future__ import annotations
import os
import re

# A card is a visual HOOK, not the whole tweet. Hooks are derived to a sane length that fits
# the card comfortably and ALWAYS end cleanly (sentence punctuation or a real "…" at a word
# boundary) — never a mid-word stub like "…automated a report wi".
HOOK_MAX = 90        # primary hook length target (chars)
HOOK_RETRY = (90, 70, 52)   # progressively shorter hooks for the self-correct regen path
_TERMINAL = ".!?…"      # clean sentence/extract endings
_CLOSERS = "\"')]}»”’"        # closing punctuation that may follow a terminal mark


def _norm(text) -> str:
    """Collapse whitespace/newlines to a single clean line."""
    return " ".join(str(text or "").replace("\r", " ").split())


def hook_from_text(text, *, max_chars: int = HOOK_MAX, min_chars: int = 22) -> str:
    """Turn a (possibly long) tweet into a SHORT, COMPLETE card hook.

    Order: (1) if the whole thing already fits, it's the hook; (2) else the first sentence if it
    fits; (3) else a clean word-boundary extract ending in a real "…". NEVER cuts mid-word, never
    returns a jagged stub. Output is the visual hook only — the full tweet still goes in the post
    body."""
    s = _norm(text)
    if not s:
        return ""
    if len(s) <= max_chars:
        return s                                   # already a complete short hook
    m = re.match(r"^(.+?[.!?])(?:\s|$)", s)         # first sentence
    if m:
        first = m.group(1).strip()
        if min_chars <= len(first) <= max_chars:
            return first
    # (3) first sentence too long → end on a COMPLETE CLAUSE within the limit, so the hook reads
    #     finished ("…it runs itself…") instead of a mid-thought stub ("…the board's…").
    window = s[: max_chars + 1]
    sent = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if sent >= min_chars:
        return window[: sent + 1].strip()                 # complete sentence — no ellipsis needed
    clause = max(window.rfind(" — "), window.rfind(" – "), window.rfind(" - "),
                 window.rfind("; "), window.rfind(": "), window.rfind(", "))
    if clause >= min_chars:
        return window[:clause].rstrip(" ,;:-–—") + "…"     # ends on a natural clause break
    # (4) last resort: clean word boundary (never mid-word)
    cut = s[:max_chars]
    sp = cut.rfind(" ")
    if sp >= min_chars:
        cut = cut[:sp]
    cut = cut.rstrip(" ,;:-–—")
    return (cut + "…") if cut else s[:max_chars]


def text_is_clean_hook(rendered, source=None) -> tuple[bool, str]:
    """True if `rendered` reads as a COMPLETE hook (the gate's mid-word/clip check).

    Clean = ends with terminal punctuation or a real "…"; or — when `source` (the full tweet) is
    given — is not a raw prefix cut mid-word / mid-sentence of it. A jagged "…report wi" fails."""
    r = _norm(rendered)
    if not r:
        return False, "hook empty"
    tail = r.rstrip(_CLOSERS)
    end = tail[-1] if tail else ""
    if end in _TERMINAL or r.endswith("..."):
        return True, ""                            # ends cleanly (sentence or explicit "…")
    if source:
        s = _norm(source)
        if s.startswith(r) and len(r) < len(s):
            nxt = s[len(r)]
            if nxt.isalnum():
                return False, f"hook ends mid-word ('…{r[-14:]}')"
            return False, "hook truncated mid-sentence (no terminal '…')"
    return True, ""

# ── exact hailports.com palette (read from tools/hailports_home_template.html :root) ──
BG     = (5, 8, 13)       # --bg     #05080d
BG2    = (7, 11, 18)      # --bg2    #070b12
PANEL  = (10, 18, 27)     # --panel  #0a121b
PANEL2 = (12, 22, 34)     # --panel2 #0c1622
LINE   = (21, 33, 47)     # --line   #15212f  (hairline)
GREEN  = (57, 217, 138)   # --grn    #39d98a  (accent)
CYAN   = (34, 211, 238)   # --cyn    #22d3ee
INK    = (230, 241, 236)  # --ink    #e6f1ec  (headline)
MUTED  = (138, 160, 176)  # --mut    #8aa0b0  (footer)
DIMGREEN = (34, 104, 70)  # inactive accent (dots/rules)
# mac traffic-light dots (site .tl .r/.y/.g)
TL_R, TL_Y, TL_G = (255, 95, 86), (255, 189, 46), (39, 201, 63)

SIZES = {"16x9": (1280, 720), "1x1": (1080, 1080), "9x16": (1080, 1920),
         "4x5": (1080, 1350)}

# site --mono = ui-monospace, SF Mono, Menlo, …  → Menlo is the best mono actually installed
_MONO = "/System/Library/Fonts/Menlo.ttc"
_MONO_FALLBACK = ["/System/Library/Fonts/Monaco.ttf",
                  "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"]
_FCACHE: dict = {}


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    key = (size, bold)
    if key in _FCACHE:
        return _FCACHE[key]
    f = None
    if os.path.exists(_MONO):
        try:
            f = ImageFont.truetype(_MONO, size, index=1 if bold else 0)  # Menlo 0=Reg 1=Bold
        except Exception:
            f = None
    if f is None:
        for p in _MONO_FALLBACK:
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, size); break
                except Exception:
                    pass
    if f is None:
        from PIL import ImageFont as _IF
        f = _IF.load_default()
    _FCACHE[key] = f
    return f


def blend(a, b, t):
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def radial_glow(draw, cx, cy, rmax, color, strength=0.10, step=7):
    """Soft corner glow over an already-#05080d background (concentric discs, no alpha)."""
    r = int(rmax)
    while r > 0:
        f = (1 - r / rmax) * strength
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=blend(BG, color, f))
        r -= step


def wrap_fit(text, max_w, max_h, *, start=72, min_size=30, bold=True, line_h_factor=1.3):
    """Largest mono size whose word-wrapped block fits max_w × max_h. Returns
    (font, lines, line_h). Honors explicit newlines; never lets a line exceed max_w
    (down to min_size); guarantees the block fits the height by trimming if forced."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    paras = [p.strip() for p in str(text).replace("\r", "").split("\n")]

    def wrap_at(f):
        out = []
        for para in paras:
            words = para.split()
            if not words:
                continue
            cur = ""
            for w in words:
                trial = (cur + " " + w).strip()
                if not cur or d.textlength(trial, font=f) <= max_w:
                    cur = trial
                else:
                    out.append(cur); cur = w
            if cur:
                out.append(cur)
        return out or [str(text).strip()]

    size = start
    while size >= min_size:
        f = _font(size, bold=bold)
        lines = wrap_at(f)
        line_h = int(size * line_h_factor)
        too_wide = any(d.textlength(l, font=f) > max_w for l in lines)
        if not too_wide and len(lines) * line_h <= max_h:
            return f, lines, line_h
        size -= 3
    f = _font(min_size, bold=bold)
    lines = wrap_at(f)
    line_h = int(min_size * line_h_factor)
    max_lines = max(1, max_h // line_h)

    def _clamp(s, *, ell):
        """Fit one line to max_w. If anything must be removed, end on a clean '…' at a space
        (never a bare mid-word stub)."""
        if not ell and d.textlength(s, font=f) <= max_w:
            return s
        if d.textlength(s + ("…" if ell else ""), font=f) <= max_w:
            return s + ("…" if ell else "")
        while s and d.textlength(s + "…", font=f) > max_w:   # something is being cut -> ellipsize
            sp = s.rfind(" ")
            s = s[:sp].rstrip() if sp > 0 else s[:-1]
        return (s + "…") if s else "…"

    trimmed = len(lines) > max_lines
    lines = [_clamp(l, ell=False) for l in lines[:max_lines]]
    if trimmed and lines:                          # dropped lines -> mark the clip cleanly
        lines[-1] = _clamp(lines[-1].rstrip().rstrip(_TERMINAL + " "), ell=True)
    globals()["_LAST_CLIPPED"] = trimmed           # signal an image-level clip to render_card_checked
    return f, lines, line_h


def _draw_prompt(d, x, y, label, font):
    """Green `❯` prompt mark + ink wordmark, left-middle anchored. Returns end-x."""
    d.text((x, y), "❯", font=font, fill=GREEN, anchor="lm")
    gap = d.textlength("❯  ", font=font)
    d.text((x + gap, y), label, font=font, fill=INK, anchor="lm")
    return x + gap + d.textlength(label, font=font)


def render_card(headline, out_path, *, size="16x9",
                footer="build-in-public · autonomous", label="hailports",
                live="live") -> str:
    """Render one site-themed card PNG. `headline` auto-fits + auto-wraps in high-contrast
    ink; never goes dark-on-dark, never overflows. Returns out_path."""
    from PIL import Image, ImageDraw
    W, H = SIZES.get(size, SIZES["16x9"])
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # subtle corner glows (cyan top-right, green bottom-left) — the site body background
    big = max(W, H)
    radial_glow(d, int(W * 0.93), int(-H * 0.05), int(big * 0.66), CYAN, strength=0.11)
    radial_glow(d, int(-W * 0.04), int(H * 0.12), int(big * 0.58), GREEN, strength=0.09)

    # ── framed terminal panel (rounded, hairline, panel→bg2 gradient) ──
    pad = int(W * 0.045)
    px0, py0, px1, py1 = pad, pad, W - pad, H - pad
    pw, ph = px1 - px0, py1 - py0
    radius = max(14, int(W * 0.02))
    panel = Image.new("RGB", (pw, ph), PANEL)
    pdraw = ImageDraw.Draw(panel)
    for yy in range(ph):
        pdraw.line([(0, yy), (pw, yy)], fill=blend(PANEL, BG2, yy / max(1, ph)))
    mask = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw - 1, ph - 1], radius=radius, fill=255)
    img.paste(panel, (px0, py0), mask)
    d.rounded_rectangle([px0, py0, px1 - 1, py1 - 1], radius=radius, outline=LINE, width=2)

    inner = int(W * 0.04)
    bx = px0 + inner                      # content left
    bx_r = px1 - inner                    # content right

    # ── header chrome row: traffic lights + green prompt brand · live ──
    hy = py0 + int(H * 0.072)
    dot_r = max(5, int(W * 0.0072))
    step = dot_r * 3
    for k, c in enumerate((TL_R, TL_Y, TL_G)):
        cx = bx + dot_r + k * step
        d.ellipse([cx - dot_r, hy - dot_r, cx + dot_r, hy + dot_r], fill=c)
    f_brand = _font(max(18, int(W * 0.025)), bold=True)
    _draw_prompt(d, bx + 3 * step + dot_r * 3, hy, label, f_brand)
    if live:
        f_live = _font(max(15, int(W * 0.018)), bold=False)
        d.text((bx_r, hy), live, font=f_live, fill=CYAN, anchor="rm")
        lw = d.textlength(live, font=f_live)
        ld_r = max(4, int(W * 0.005))
        ldx = bx_r - lw - ld_r * 3
        d.ellipse([ldx - ld_r, hy - ld_r, ldx + ld_r, hy + ld_r], fill=GREEN)
    header_b = hy + int(H * 0.055)
    d.line([(bx, header_b), (bx_r, header_b)], fill=LINE, width=2)

    # ── footer (muted mono) ──
    f_foot = _font(max(15, int(W * 0.0185)), bold=False)
    foot_y = py1 - int(H * 0.07)
    d.text((bx, foot_y), str(footer), font=f_foot, fill=MUTED, anchor="lm")
    dom = "hailports.com"
    d.text((bx_r, foot_y), dom, font=f_foot, fill=MUTED, anchor="rm")
    d.line([(bx, foot_y - int(H * 0.045)), (bx_r, foot_y - int(H * 0.045))],
           fill=LINE, width=1)

    # ── body: green accent rule + auto-fit/auto-wrap ink headline, vertically centered ──
    body_top = header_b + int(H * 0.04)
    body_bot = foot_y - int(H * 0.06)
    rule_h = max(4, int(W * 0.005))
    rule_w = int(W * 0.07)
    rule_gap = int(H * 0.045)
    head_max_h = (body_bot - body_top) - rule_h - rule_gap
    start = int(W * 0.062)
    f_h, lines, line_h = wrap_fit(headline, bx_r - bx, head_max_h,
                                  start=start, min_size=max(26, int(W * 0.026)), bold=True)
    block_h = rule_h + rule_gap + len(lines) * line_h
    top = body_top + max(0, (body_bot - body_top - block_h) // 2)
    d.rounded_rectangle([bx, top, bx + rule_w, top + rule_h], radius=rule_h // 2, fill=GREEN)
    ty = top + rule_h + rule_gap
    for ln in lines:
        d.text((bx, ty), ln, font=f_h, fill=INK, anchor="la")
        ty += line_h

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    img.save(out_path)
    return out_path


def _lum(p):
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def assess_card(path, text=None, source=None) -> dict:
    """Cheap readability/on-theme audit of a rendered card PNG. Catches the failure modes the
    old gen_image_free palette=4 produced: dark-on-dark (unreadable), empty, off-theme (no
    green accent), or text overflowing to the edge. When `text` (the rendered hook) is given —
    optionally with `source` (the full tweet) — also REJECTS a card whose text ends mid-word or
    is a jagged mid-sentence cut (the 'dying sentence' card). Returns {ok, reasons, metrics}."""
    from PIL import Image
    try:
        im = Image.open(path).convert("RGB")
    except Exception as e:
        return {"ok": False, "reasons": [f"unreadable file: {e}"], "metrics": {}}
    W0, H0 = im.size
    tw = min(360, W0)
    im = im.resize((tw, max(1, int(tw * H0 / W0))))
    w, h = im.size
    px = list(im.getdata())
    n = max(1, len(px))
    corners = [px[2 * w + 2], px[2 * w + (w - 3)], px[(h - 3) * w + 2], px[(h - 3) * w + (w - 3)]]
    bg_lum = sum(_lum(c) for c in corners) / 4.0
    bw, bh = max(2, int(w * 0.03)), max(2, int(h * 0.03))
    ink = green = border_ct = border_bright = 0
    for idx, p in enumerate(px):
        L = _lum(p)
        if L > 150:
            ink += 1
        if p[1] > 120 and p[1] > p[0] * 1.35 and p[1] > p[2] * 1.12:
            green += 1
        x, y = idx % w, idx // w
        if x < bw or x >= w - bw or y < bh or y >= h - bh:
            border_ct += 1
            if L > 150:
                border_bright += 1
    ink_frac = ink / n
    green_frac = green / n
    border_frac = border_bright / max(1, border_ct)
    reasons = []
    if bg_lum >= 70:
        reasons.append(f"background not dark/on-theme (corner lum {bg_lum:.0f})")
    if ink_frac < 0.0025:
        reasons.append(f"headline empty or too faint (bright-ink {ink_frac*100:.2f}%)")
    if ink_frac > 0.55:
        reasons.append(f"card blown-out / not the dark theme (bright {ink_frac*100:.0f}%)")
    if green_frac < 0.00025:
        reasons.append("missing green accent (off-theme)")
    if border_frac > 0.06:
        reasons.append(f"text overflowing to the edge ({border_frac*100:.0f}% bright border)")
    if text is not None:
        clean, why = text_is_clean_hook(text, source=source)
        if not clean:
            reasons.append(why)
    return {"ok": not reasons, "reasons": reasons,
            "metrics": {"bg_lum": round(bg_lum, 1), "ink_frac": round(ink_frac, 4),
                        "green_frac": round(green_frac, 4), "border_frac": round(border_frac, 3)}}


def card_ok(path) -> bool:
    return bool(assess_card(path).get("ok"))


def render_card_checked(headline, out_path, *, size="16x9",
                        footer="build-in-public · autonomous", label="hailports",
                        max_attempts: int = 3) -> str | None:
    """Render + PRE-POST quality gate. The card shows a short COMPLETE hook derived from the
    (possibly long) tweet — NOT a fixed char-count hard-truncate. Renders, audits (incl. the
    mid-word/clip gate); if it fails, regenerates with a SHORTER clean hook; if it STILL fails,
    deletes the file and returns None so the caller posts WITHOUT a broken image. Returns path or
    None. The full tweet stays in the post body — this only shapes the card line."""
    src = str(headline)
    attempts: list[str] = []
    for mx in HOOK_RETRY:                          # progressively shorter, always-complete hooks
        h = hook_from_text(src, max_chars=mx)
        if h and h not in attempts:
            attempts.append(h)
    if not attempts:
        attempts = [_norm(src)[:HOOK_MAX] or src]
    last = None
    for hk in attempts[:max(1, max_attempts)]:
        try:
            p = render_card(hk, out_path, size=size, footer=footer, label=label)
        except Exception as e:
            last = {"reasons": [f"render error: {e}"]}
            continue
        if globals().get("_LAST_CLIPPED"):            # hook rendered but got clipped mid-thought -> shorter hook
            last = {"reasons": ["card text clipped in image — trying a shorter hook"]}
            continue
        a = assess_card(p, text=hk, source=src)
        if a.get("ok"):
            return p
        last = a
    try:
        os.remove(out_path)
    except Exception:
        pass
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--assess":
        import json
        print(json.dumps(assess_card(sys.argv[2]), indent=2))
        raise SystemExit(0)
    txt = sys.argv[1] if len(sys.argv) > 1 else \
        "i walked away from the whole company. it's still shipping without me."
    sz = sys.argv[2] if len(sys.argv) > 2 else "16x9"
    out = sys.argv[3] if len(sys.argv) > 3 else "/tmp/hailports_card_demo.png"
    print(render_card(txt, out, size=sz))
