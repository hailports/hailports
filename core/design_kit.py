#!/usr/bin/env python3
"""design_kit.py — the ONE source of truth for the premium "Claude/Anthropic" look.

Every renderer in the stack (the broken-site mockups in core.site_generator, the
build-in-public Shorts in agents.hailports_short_renderer, social cards, etc.) pulls
its design language from here instead of hardcoding its own palette, type, spacing and
motion. Change the look in one place, the whole stack moves with it.

What lives here (pure data + pure functions, ZERO import side effects):
  • PALETTE   — the warm "paper + ink + one craft accent" system, lifted from the
                Anthropic/Claude language: warm paper, near-black ink, mid-gray,
                hairline, the clay/"book cloth" coral, kraft, plus the secondary
                blue/green. Named constants + a WARM_ACCENTS anti-fingerprint pool +
                the full PALETTES / FONT_PAIRS token tables the web mockups consume.
  • TYPE      — an OPEN/installed font resolver that EVOKES the Claude pairing without
                ever touching the proprietary Copernicus/Styrene files: an editorial
                serif display (Source-Serif/Tiempos/Lora register -> Charter / Iowan
                Old Style fallbacks) over a clean grotesque/humanist sans
                (Inter/Helvetica-Neue register -> Helvetica Neue / Avenir Next).
                Path resolver + cached Pillow loader + an auto-shrink fitter.
  • SPACE     — 8px base scale, section padding, radii.
  • DRAW      — reusable Pillow helpers: rounded panel, soft card, accent rule,
                monogram badge, color blend (for fades), type-fit.
  • MOTION    — for video: pacing, fade/slide easing, text-reveal + color treatment.

Nothing here imports Pillow at module load; PIL is imported lazily inside the draw
helpers, so `import core.design_kit` is cheap and safe everywhere (web + headless).
"""
from __future__ import annotations

import os
from functools import lru_cache

__all__ = [
    # palette
    "PAPER", "SURFACE", "PANEL", "INK", "MID_GRAY", "MUTED", "HAIRLINE",
    "CLAY", "CLAY_DEEP", "KRAFT", "BLUE", "GREEN", "WARM_ACCENTS",
    "PALETTES", "FONT_PAIRS", "SANS_STACK",
    "rgb", "luminance", "contrast", "best_on", "palette_ok", "blend",
    "to_hex", "hsv", "is_vivid", "saturate", "darken", "dominant_colors",
    # type
    "FONT_CANDIDATES", "resolve_font", "load_font", "fit_font", "text_size",
    # space
    "BASE", "SPACE", "RADII", "RADIUS", "SECTION_PAD_Y", "MAXW", "TYPE_SCALE",
    # draw
    "hex_to_rgb", "rounded_panel", "soft_card", "accent_rule", "monogram_badge",
    # motion
    "VIDEO", "MOTION", "ease_in_out", "ease_out", "scene_alpha", "slide_offset",
    # pattern corpus (technique reference; look still comes from the tokens above)
    "PATTERN_CORPUS", "list_patterns", "load_pattern",
]

# ════════════════════════════════════════════════════════════════════════════════
# PALETTE — warm paper + ink + one craft accent (pilfered Claude/Anthropic language)
# ════════════════════════════════════════════════════════════════════════════════
PAPER = "#faf9f5"      # warm cream page background
SURFACE = "#ffffff"    # raised card / surface
PANEL = "#f3f0e9"      # soft tinted panel / ibadge ground
INK = "#141413"        # near-black headline/body ink
MID_GRAY = "#b0aea5"   # warm mid-gray (hairline-adjacent, quiet UI)
MUTED = "#5d594f"      # warm muted body-secondary
HAIRLINE = "#e8e6dc"   # 1px warm border / divider

# craft accents
CLAY = "#d97757"       # the "book cloth" coral — the signature accent
CLAY_DEEP = "#c15f3c"  # deeper clay for primary/pressed/contrast text
KRAFT = "#d4a27f"      # warm kraft/tan secondary
BLUE = "#6a9bcc"       # secondary slate-blue
GREEN = "#788c5d"      # secondary olive-green

# anti-fingerprint pool: every prospect/brand gets a DISTINCT accent at the SAME
# quality bar. (name, accent, deeper-primary) — all sit on the same warm neutrals.
WARM_ACCENTS = [
    ("clay",       CLAY,      CLAY_DEEP),
    ("kraft",      KRAFT,     "#b07a4f"),
    ("terracotta", "#cc6a45", "#a4452a"),
    ("olive",      GREEN,     "#4f6240"),
    ("slate",      BLUE,      "#2f5f86"),
    ("amber",      "#c8893a", "#9a6420"),
    ("teal",       "#4f9a93", "#2f6f6a"),
    ("plum",       "#9a6f9d", "#6b4a6e"),
    ("rose",       "#c46e78", "#9a3f4c"),
]

# ── full token tables the WEB mockups (core.site_generator) consume ──────────────
# Shared paper/ink/gray/hairline neutrals; ONLY the brand accent varies (anti-
# fingerprint). All light/editorial. ink/bg/surface contrast re-checked at runtime.
PALETTES = [
    {"name": "claude",     "bg": "#faf9f5", "surface": "#ffffff", "panel": "#f3f0e9", "ink": "#141413", "muted": "#5d594f", "border": "#e8e4d9", "primary": "#b4502f", "accent": "#d97757"},
    {"name": "terracotta", "bg": "#fbf8f4", "surface": "#ffffff", "panel": "#f4efe8", "ink": "#1a1613", "muted": "#5e554c", "border": "#eae3d8", "primary": "#a4452a", "accent": "#cc6a45"},
    {"name": "sage",       "bg": "#f7f8f4", "surface": "#ffffff", "panel": "#eef1e8", "ink": "#181a14", "muted": "#555e4c", "border": "#e4e7da", "primary": "#4f6240", "accent": "#788c5d"},
    {"name": "ocean",      "bg": "#f6f8fa", "surface": "#ffffff", "panel": "#eaf0f3", "ink": "#14191d", "muted": "#4e5a62", "border": "#e0e6ea", "primary": "#2f5f86", "accent": "#5b91bf"},
    {"name": "forest",     "bg": "#f5f8f5", "surface": "#ffffff", "panel": "#e9f0ea", "ink": "#131a15", "muted": "#4e5b51", "border": "#e0e7e0", "primary": "#2f5d43", "accent": "#4f8f6b"},
    {"name": "plum",       "bg": "#f9f6f9", "surface": "#ffffff", "panel": "#f1ebf1", "ink": "#1a141a", "muted": "#5a4f58", "border": "#e8e0e6", "primary": "#6b4a6e", "accent": "#9a6f9d"},
    {"name": "navy",       "bg": "#f8f7f3", "surface": "#ffffff", "panel": "#eeece4", "ink": "#15171c", "muted": "#535963", "border": "#e3e1d6", "primary": "#2b3a55", "accent": "#cf6f49"},
    {"name": "amber",      "bg": "#faf8f3", "surface": "#ffffff", "panel": "#f3eee2", "ink": "#1a1611", "muted": "#5e554a", "border": "#eae3d2", "primary": "#9a6420", "accent": "#c8893a"},
    {"name": "teal",       "bg": "#f4f8f7", "surface": "#ffffff", "panel": "#e7f0ee", "ink": "#121a18", "muted": "#4c5b58", "border": "#dde8e5", "primary": "#2f6f6a", "accent": "#4f9a93"},
    {"name": "rose",       "bg": "#fbf6f6", "surface": "#ffffff", "panel": "#f4eaea", "ink": "#1c1416", "muted": "#5d4f54", "border": "#ece0e0", "primary": "#9a3f4c", "accent": "#c46e78"},
]

# CSS font stacks for the WEB mockups — system fonts only (offline, zero remote links).
# Editorial SERIF display over a clean grotesque/humanist SANS, a couple all-sans pairs
# for variation. `sans` flags the all-sans pairs. (Browsers; the video path resolves to
# real font files via resolve_font below.)
SANS_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
FONT_PAIRS = [
    {"name": "editorial",  "sans": False, "head": "'Iowan Old Style','Palatino Linotype',Palatino,Georgia,'Times New Roman',serif", "body": SANS_STACK},
    {"name": "charter",    "sans": False, "head": "Charter,'Bitstream Charter','Sitka Text',Cambria,Georgia,serif", "body": SANS_STACK},
    {"name": "palatino",   "sans": False, "head": "'Palatino Linotype',Palatino,'Book Antiqua',Georgia,serif", "body": SANS_STACK},
    {"name": "baskerville","sans": False, "head": "Baskerville,'Hoefler Text','Iowan Old Style',Georgia,serif", "body": "'Avenir Next',Avenir,'Segoe UI',Roboto,sans-serif"},
    {"name": "georgia",    "sans": False, "head": "Georgia,Cambria,'Times New Roman',serif", "body": SANS_STACK},
    {"name": "optima",     "sans": True,  "head": "Optima,Candara,'Gill Sans','Segoe UI',sans-serif", "body": "Candara,'Segoe UI',Roboto,sans-serif"},
    {"name": "grotesque",  "sans": True,  "head": "'Helvetica Neue',-apple-system,'Segoe UI',Arial,sans-serif", "body": SANS_STACK},
    {"name": "humanist",   "sans": True,  "head": "'Avenir Next',Avenir,'Segoe UI',Roboto,sans-serif", "body": "'Avenir Next',Avenir,'Segoe UI',Roboto,sans-serif"},
]


# ── color helpers (WCAG-ish; shared by every renderer) ───────────────────────────
def rgb(hexc: str) -> tuple[int, int, int]:
    h = str(hexc).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


hex_to_rgb = rgb  # alias for the Pillow side


def luminance(hexc: str) -> float:
    def lin(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(hexc)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def best_on(bg: str) -> str:
    """Readable ink color over any background."""
    return "#0b0f14" if contrast("#0b0f14", bg) >= contrast("#ffffff", bg) else "#ffffff"


def palette_ok(p: dict) -> bool:
    return contrast(p["ink"], p["bg"]) >= 4.5 and contrast(p["ink"], p["surface"]) >= 4.5


def blend(fg, bg, t: float):
    """Linear-blend two colors (hex str or rgb tuple) by t in 0..1 (t=0 -> bg, 1 -> fg).

    The cheap way to fade text in/out on an opaque background without RGBA compositing:
    draw the glyphs in blend(fg, bg, alpha). Returns an (r,g,b) tuple.
    """
    fr, fgc, fb = fg if isinstance(fg, (tuple, list)) else rgb(fg)
    br, bgc, bb = bg if isinstance(bg, (tuple, list)) else rgb(bg)
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (round(br + (fr - br) * t), round(bgc + (fgc - bgc) * t), round(bb + (fb - bb) * t))


def to_hex(c) -> str:
    """(r,g,b) tuple or hex-string -> normalized '#rrggbb'."""
    r, g, b = c if isinstance(c, (tuple, list)) else rgb(c)
    return "#%02x%02x%02x" % (max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b))))


def hsv(hexc) -> tuple[float, float, float]:
    """(hue 0..1, sat 0..1, val 0..1) for a color (hex or rgb tuple)."""
    import colorsys
    r, g, b = hexc if isinstance(hexc, (tuple, list)) else rgb(hexc)
    return colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)


def is_vivid(hexc, *, min_sat: float = 0.28, min_val: float = 0.16, max_val: float = 0.95) -> bool:
    """A real BRAND color, not page chrome: saturated enough to read as a hue, and
    neither near-white nor near-black. Drops the gray/cream/ink neutrals a site is
    mostly made of so only the actual brand cues survive."""
    _, s, v = hsv(hexc)
    return s >= min_sat and min_val <= v <= max_val


def saturate(hexc, *, sat: float = 0.62, val: float | None = None) -> str:
    """Push a color toward a cleaner, more saturated version (brand-skin polish)."""
    import colorsys
    h, s, v = hsv(hexc)
    s = max(s, sat)
    if val is not None:
        v = val
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return to_hex((r * 255, g * 255, b * 255))


def darken(hexc, amount: float = 0.22) -> str:
    """Multiply value down by `amount` (for a readable primary / band ground)."""
    import colorsys
    h, s, v = hsv(hexc)
    r, g, b = colorsys.hsv_to_rgb(h, s, max(0.0, v * (1 - amount)))
    return to_hex((r * 255, g * 255, b * 255))


def dominant_colors(source, *, n: int = 6, quantize: int = 8) -> list[str]:
    """Vivid brand colors from an image (PIL Image, raw bytes, or a data: URI),
    most-frequent first, de-duplicated by hue. PIL.Image.quantize over a downscaled
    copy; neutrals (gray/white/black page chrome) are dropped via is_vivid. Fail-soft:
    returns [] on any decode/PIL error."""
    try:
        from PIL import Image
        import io as _io
        import base64 as _b64
        if isinstance(source, str):                       # data: URI
            if "base64," in source:
                source = _b64.b64decode(source.split("base64,", 1)[1])
            else:
                return []
        if isinstance(source, (bytes, bytearray)):
            im = Image.open(_io.BytesIO(bytes(source)))
        else:
            im = source
        im = im.convert("RGB")
        im.thumbnail((220, 220))
        try:
            q = im.quantize(colors=max(4, quantize), method=Image.Quantize.FASTOCTREE)
        except Exception:
            q = im.quantize(colors=max(4, quantize))
        pal = q.getpalette() or []
        counts = q.getcolors() or []                      # [(count, palette_index)]
        scored: list[tuple[int, str]] = []
        for cnt, idx in counts:
            base = idx * 3
            if base + 2 >= len(pal):
                continue
            hexc = to_hex((pal[base], pal[base + 1], pal[base + 2]))
            if is_vivid(hexc, min_sat=0.34):   # photos average out muddy; demand a real hue
                scored.append((cnt, hexc))
        scored.sort(key=lambda x: -x[0])
        out: list[str] = []
        seen_hues: list[float] = []
        for _, hexc in scored:
            h = hsv(hexc)[0]
            if any(min(abs(h - sh), 1 - abs(h - sh)) < 0.06 for sh in seen_hues):
                continue                                  # near-duplicate hue
            seen_hues.append(h)
            out.append(hexc)
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


# ════════════════════════════════════════════════════════════════════════════════
# TYPE — font resolver: OPEN/installed fonts that EVOKE the Claude pairing.
# Never references proprietary Copernicus/Styrene. Each role+weight is a list of
# (path, ttc-index); the resolver returns the first file that exists. Open-source
# faces (Source Serif, Tiempos-alike, Lora, Inter, IBM Plex) are tried FIRST so a
# drop-in install upgrades the look; high-quality macOS fallbacks guarantee output.
# ════════════════════════════════════════════════════════════════════════════════
_HOME = os.path.expanduser("~")
_FONT_DIRS = [
    os.path.join(_HOME, "Library/Fonts"),
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
]


def _find(*names: str) -> list[str]:
    """Resolve bare font filenames against the standard macOS font dirs."""
    out = []
    for n in names:
        if os.path.isabs(n):
            out.append(n)
            continue
        for d in _FONT_DIRS:
            out.append(os.path.join(d, n))
    return out


# role -> weight -> ordered [(path, index)] candidates (open-source first, then system)
FONT_CANDIDATES: dict[str, dict[str, list[tuple[str, int]]]] = {
    # editorial serif display (the "crafted/human" Copernicus-evoking headline)
    "serif": {
        "regular": (
            [(p, 0) for p in _find("SourceSerif4-Regular.ttf", "SourceSerifPro-Regular.ttf",
                                    "Lora-Regular.ttf", "Tiempos-Text-Regular.ttf",
                                    "PTSerif-Regular.ttf", "IBMPlexSerif-Regular.ttf",
                                    "NewsreaderText-Regular.ttf")]
            + [("/System/Library/Fonts/Supplemental/Charter.ttc", 0),
               ("/System/Library/Fonts/Supplemental/Iowan Old Style.ttc", 0),
               ("/System/Library/Fonts/NewYork.ttf", 0),
               ("/System/Library/Fonts/Supplemental/Georgia.ttf", 0),
               ("/System/Library/Fonts/Times.ttc", 0)]
        ),
        "bold": (
            [(p, 0) for p in _find("SourceSerif4-Semibold.ttf", "SourceSerif4-Bold.ttf",
                                    "SourceSerifPro-Semibold.ttf", "Lora-SemiBold.ttf",
                                    "Lora-Bold.ttf", "Tiempos-Headline-Semibold.ttf",
                                    "PTSerif-Bold.ttf", "IBMPlexSerif-SemiBold.ttf")]
            + [("/System/Library/Fonts/Supplemental/Charter.ttc", 3),
               ("/System/Library/Fonts/Supplemental/Iowan Old Style.ttc", 1),
               ("/System/Library/Fonts/Supplemental/Georgia Bold.ttf", 0),
               ("/System/Library/Fonts/Times.ttc", 1)]
        ),
    },
    # clean grotesque/humanist sans (Inter / Helvetica-Neue register) for body + labels
    "sans": {
        "regular": (
            [(p, 0) for p in _find("Inter-Regular.ttf", "Inter-Regular.otf",
                                    "InterDisplay-Regular.ttf", "HelveticaNeue-Regular.otf",
                                    "IBMPlexSans-Regular.ttf", "WorkSans-Regular.ttf")]
            + [("/System/Library/Fonts/HelveticaNeue.ttc", 0),
               ("/System/Library/Fonts/Avenir Next.ttc", 7),
               ("/System/Library/Fonts/Helvetica.ttc", 0),
               ("/System/Library/Fonts/Supplemental/Arial.ttf", 0)]
        ),
        "medium": (
            [(p, 0) for p in _find("Inter-Medium.ttf", "Inter-Medium.otf",
                                    "IBMPlexSans-Medium.ttf", "WorkSans-Medium.ttf")]
            + [("/System/Library/Fonts/HelveticaNeue.ttc", 10),  # Medium
               ("/System/Library/Fonts/Avenir Next.ttc", 5),     # Medium
               ("/System/Library/Fonts/HelveticaNeue.ttc", 0)]
        ),
        "bold": (
            [(p, 0) for p in _find("Inter-SemiBold.ttf", "Inter-Bold.ttf", "Inter-Bold.otf",
                                    "IBMPlexSans-SemiBold.ttf", "WorkSans-SemiBold.ttf")]
            + [("/System/Library/Fonts/HelveticaNeue.ttc", 1),
               ("/System/Library/Fonts/Avenir Next.ttc", 0),
               ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0)]
        ),
    },
    # monospace (for any code/terminal accents)
    "mono": {
        "regular": (
            [(p, 0) for p in _find("JetBrainsMono-Regular.ttf", "IBMPlexMono-Regular.ttf",
                                    "FiraCode-Regular.ttf", "SFMono-Regular.otf")]
            + [("/System/Library/Fonts/Menlo.ttc", 0),
               ("/System/Library/Fonts/SFNSMono.ttf", 0),
               ("/System/Library/Fonts/Monaco.ttf", 0)]
        ),
        "bold": (
            [(p, 0) for p in _find("JetBrainsMono-Bold.ttf", "IBMPlexMono-SemiBold.ttf")]
            + [("/System/Library/Fonts/Menlo.ttc", 1),
               ("/System/Library/Fonts/Menlo.ttc", 0)]
        ),
    },
}
# weight fallbacks within a role
_WEIGHT_FALLBACK = {"medium": ("medium", "regular", "bold"),
                    "bold": ("bold", "medium", "regular"),
                    "regular": ("regular", "medium", "bold")}


@lru_cache(maxsize=64)
def resolve_font(role: str = "serif", weight: str = "regular") -> tuple[str, int]:
    """Return (path, ttc_index) for a role/weight, preferring open-source faces.

    role: 'serif' (display) | 'sans' (body) | 'mono'.  weight: 'regular'|'medium'|'bold'.
    Always returns a path that exists (final system fallback is guaranteed present).
    """
    table = FONT_CANDIDATES.get(role) or FONT_CANDIDATES["sans"]
    for w in _WEIGHT_FALLBACK.get(weight, (weight, "regular")):
        for path, idx in table.get(w, []):
            if os.path.exists(path):
                return path, idx
    # absolute last resort
    return "/System/Library/Fonts/HelveticaNeue.ttc", 0


@lru_cache(maxsize=256)
def load_font(role: str = "serif", size: int = 64, weight: str = "regular"):
    """Cached Pillow ImageFont for a role/weight at a pixel size. Lazy PIL import."""
    from PIL import ImageFont
    path, idx = resolve_font(role, weight)
    try:
        return ImageFont.truetype(path, int(size), index=idx)
    except Exception:
        try:
            return ImageFont.truetype(path, int(size))
        except Exception:
            return ImageFont.load_default()


def text_size(text: str, font) -> tuple[int, int]:
    """(w, h) of a single line in a font — no Draw instance required."""
    try:
        l, t, r, b = font.getbbox(text)
        return r - l, b - t
    except Exception:
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        return r - l, b - t


def fit_font(text: str, role: str, max_width: int, *, start: int = 120,
             min_size: int = 28, weight: str = "regular", step: int = 4):
    """Auto-shrink: largest role font (from `start` down to `min_size`) where `text`
    fits within `max_width`. Returns a Pillow font."""
    size = int(start)
    last = load_font(role, size, weight)
    while size > min_size:
        f = load_font(role, size, weight)
        w, _ = text_size(text, f)
        if w <= max_width:
            return f
        last = f
        size -= step
    return load_font(role, max(min_size, size), weight)


# ════════════════════════════════════════════════════════════════════════════════
# SPACE — 8px base scale, radii, section rhythm
# ════════════════════════════════════════════════════════════════════════════════
BASE = 8
SPACE = {"xs": 8, "sm": 16, "md": 24, "lg": 40, "xl": 64, "2xl": 96}
RADII = [10, 14, 18, 22]
RADIUS = 16                       # default soft radius
SECTION_PAD_Y = (64, 96)          # web section vertical padding range
MAXW = 1280                       # max content width

# editorial type scale (web px) — hero ~72 / H1 48 / H2 36 / body 16-18 @1.6,
# headline tracking -0.02em. Video sizes live in VIDEO below.
TYPE_SCALE = {
    "hero": 72, "h1": 48, "h2": 36, "h3": 24, "body": 17, "small": 14,
    "line_height": 1.6, "head_tracking": -0.02, "lead": 20,
}


# ════════════════════════════════════════════════════════════════════════════════
# DRAW — reusable Pillow helpers (lazy PIL). All take colors as hex or rgb tuples.
# ════════════════════════════════════════════════════════════════════════════════
def _c(color):
    return color if isinstance(color, (tuple, list)) else rgb(color)


def rounded_panel(draw, box, radius: int = RADIUS, *, fill=None, outline=None, width: int = 1):
    """A soft rounded rectangle (the base of every card/panel/badge)."""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius,
                           fill=_c(fill) if fill is not None else None,
                           outline=_c(outline) if outline is not None else None,
                           width=width)


def soft_card(img, box, radius: int = RADIUS, *, fill=SURFACE, bg=PAPER,
              outline=HAIRLINE, elevation: int = 18):
    """A surface card with a subtle warm drop-shadow on `img` (an RGB Image).

    The shadow is a soft, low-opacity warm-ink blur (never hard black) for the
    "barely raised" premium feel. Draws in place; returns img.
    """
    from PIL import Image, ImageDraw, ImageFilter
    x0, y0, x1, y1 = [int(v) for v in box]
    if elevation > 0:
        pad = elevation * 2
        sh = Image.new("RGBA", (img.width, img.height), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        sd.rounded_rectangle([x0, y0 + elevation // 2, x1, y1 + elevation // 2],
                             radius=radius, fill=(20, 20, 19, 46))
        sh = sh.filter(ImageFilter.GaussianBlur(elevation))
        base = img.convert("RGBA")
        base.alpha_composite(sh)
        img.paste(base.convert("RGB"), (0, 0))
    d = ImageDraw.Draw(img)
    rounded_panel(d, (x0, y0, x1, y1), radius, fill=fill,
                  outline=outline, width=1)
    return img


def accent_rule(draw, x: int, y: int, length: int = 48, *, color=CLAY,
                thickness: int = 4, radius: int = 2):
    """The short clay accent rule that sits under an eyebrow / divides sections."""
    draw.rounded_rectangle([x, y, x + length, y + thickness], radius=radius, fill=_c(color))


def monogram_badge(draw, box, text: str, font, *, fill=PANEL, text_fill=CLAY,
                   radius: int = 14, outline=None):
    """A small rounded monogram/label badge (e.g. a brand initial or 'day N')."""
    x0, y0, x1, y1 = box
    rounded_panel(draw, box, radius, fill=fill,
                  outline=outline, width=1 if outline else 0)
    w, h = text_size(text, font)
    try:
        asc, _ = font.getmetrics()
    except Exception:
        asc = h
    cx = x0 + (x1 - x0 - w) / 2
    # vertically center using bbox top offset
    l, t, r, b = (font.getbbox(text) if hasattr(font, "getbbox") else (0, 0, w, h))
    cy = y0 + (y1 - y0 - (b - t)) / 2 - t
    draw.text((cx - l, cy), text, font=font, fill=_c(text_fill))


# ════════════════════════════════════════════════════════════════════════════════
# MOTION — video pacing + easing + reveal/color treatment.
# Premium register: nothing snaps. Content fades + eases up into place over the
# first beat of a scene and crossfades out at the end; one quiet clay progress cue
# rides the whole piece. No bounce, no spin, no hard cuts on text.
# ════════════════════════════════════════════════════════════════════════════════
VIDEO = {
    "w": 1080, "h": 1920, "fps": 6, "encode_fps": 30,
    # video pixel type scale (much larger than web for phone legibility)
    "eyebrow": 44, "hero": 108, "hero_min": 56, "sub": 52, "cta": 50, "badge": 40,
    "line_height": 132, "margin_x": 96,
    "bg": PAPER, "ink": INK, "muted": MUTED, "accent": CLAY, "accent_deep": CLAY_DEEP,
    "hairline": HAIRLINE,
}
MOTION = {
    # per-scene timing as a fraction of the scene's duration
    "fade_in": 0.22,        # content eases in over first 22% of the scene
    "fade_out": 0.16,       # and crossfades out over the last 16%
    "slide_dist": 46,       # px the content rises into place
    "min_scene_secs": 3.5,  # below this a scene reads as a flash
    "max_scene_secs": 6.0,
    "reveal": "fade+rise",  # text reveal style (vs the terminal style's per-char type)
    "color_treatment": "warm-paper, ink headline, one clay accent + clay progress",
}


def ease_out(x: float) -> float:
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return 1 - (1 - x) * (1 - x)


def ease_in_out(x: float) -> float:
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return 2 * x * x if x < 0.5 else 1 - (-2 * x + 2) ** 2 / 2


def scene_alpha(elapsed: float, dur: float, *, fade_in: float | None = None,
                fade_out: float | None = None) -> float:
    """Opacity 0..1 for a scene's content given seconds `elapsed` into a `dur`-second
    scene — eased fade-in at the head, crossfade-out at the tail, full in the middle."""
    if dur <= 0:
        return 1.0
    fi = (MOTION["fade_in"] if fade_in is None else fade_in) * dur
    fo = (MOTION["fade_out"] if fade_out is None else fade_out) * dur
    if elapsed < fi and fi > 0:
        return ease_out(elapsed / fi)
    if elapsed > dur - fo and fo > 0:
        return ease_out(max(0.0, (dur - elapsed) / fo))
    return 1.0


def slide_offset(elapsed: float, dur: float, *, dist: int | None = None,
                 fade_in: float | None = None) -> float:
    """Vertical px offset (positive = below resting position) that eases to 0 as a
    scene's content settles. Pair with scene_alpha for a 'fade + rise' reveal."""
    d = MOTION["slide_dist"] if dist is None else dist
    fi = (MOTION["fade_in"] if fade_in is None else fade_in) * max(dur, 1e-6)
    if elapsed >= fi or fi <= 0:
        return 0.0
    return d * (1 - ease_out(elapsed / fi))


# ════════════════════════════════════════════════════════════════════════════════
# PATTERNS — external design-pattern corpus (technique reference only).
# 60 brand-neutral web-design technique specs live at PATTERN_CORPUS (MIT, vetted).
# They supply STRUCTURE/technique for a section or page; the LOOK (palette, type,
# spacing, motion) still comes from the tokens above — never override a design_kit
# palette with a spec's example colors. Pure path/read helpers, zero import-time I/O.
# ════════════════════════════════════════════════════════════════════════════════
PATTERN_CORPUS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "hustle", "design_pattern_corpus",
)


# Single-flag kill switch for the whole design-skills integration. Present => the
# corpus is severed from every renderer instantly (list is empty, loads are None),
# without removing any file. Also honored by the daily-design-harvest loop.
_KILL_FLAG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "hustle", "DESIGN_SKILLS_OFF",
)


def _patterns_disabled() -> bool:
    return os.path.exists(_KILL_FLAG)


def list_patterns() -> list[str]:
    """Available pattern spec names (no .md), sorted. Empty if corpus absent or
    the DESIGN_SKILLS_OFF kill switch is present."""
    if _patterns_disabled():
        return []
    try:
        return sorted(
            f[:-3] for f in os.listdir(PATTERN_CORPUS)
            if f.endswith(".md") and f not in ("INDEX.md", "PROVENANCE.md")
        )
    except OSError:
        return []


def load_pattern(name: str) -> str | None:
    """Return a pattern spec's markdown text, or None if it doesn't exist or the
    DESIGN_SKILLS_OFF kill switch is present.

    `name` may be with or without the .md suffix. Path-traversal safe: only a bare
    corpus filename is honored (basename-stripped), never an arbitrary path.
    """
    if _patterns_disabled():
        return None
    stem = os.path.basename(str(name))
    if stem.endswith(".md"):
        stem = stem[:-3]
    path = os.path.join(PATTERN_CORPUS, stem + ".md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None
