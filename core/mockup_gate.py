"""Visual send-gate for the rebuild mockups — no ugly page can ever become a proof link.

`_validate()` in site_generator is static-HTML only; it PASSED the empty-void mockup that
went out. This gate actually RENDERS the page (mobile, Playwright) and fails it on the real
failure modes a human sees: a large blank/void band, leftover builder stubs / meta-copy, and
horizontal overflow. A mockup must clear this before it can be published or linked in a send.

  from core.mockup_gate import gate
  r = gate("products_internal/landing/mockups/dowlingelectric.com.html")
  if not r["ok"]: ...   # r["reasons"] explains why

  python3 -m core.mockup_gate <file.html> [...]   # CLI: PASS/FAIL each
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

_STUB_PATTERNS = [
    r"add your (phone|number|email|hours|logo|photo)",
    r"\bthis rebuild\b", r"\w+ pages for\b", r"clean layout, easy contact",
    r"\blorem ipsum\b", r"\byour business name\b",
    r"\[(your|add|insert|placeholder|business name|company name|logo|photo)[a-z ]*\]",
]
_STUB_RE = re.compile("|".join(_STUB_PATTERNS), re.I)

VOID_FRACTION = 0.20   # a blank band taller than 20% of the page = a void
BLANK_STDDEV = 3.5     # per-strip grayscale stddev below this = "nothing rendered here"
STRIP_PX = 16


def _stub_scan(html: str) -> list[str]:
    hits = sorted({m.group(0).strip().lower() for m in _STUB_RE.finditer(html)})
    return [f"stub/meta text: {h!r}" for h in hits[:5]]


def _render_mobile(path: Path):
    """Full-page mobile screenshot + page/viewport widths. Returns (PIL.Image, overflow_px)."""
    from playwright.sync_api import sync_playwright
    from PIL import Image
    shot = Path(tempfile.mkdtemp(prefix="gate_")) / "m.png"
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 390, "height": 844})
        pg.goto("file://" + str(path.resolve()), wait_until="networkidle", timeout=20000)
        scroll_w = pg.evaluate("() => document.documentElement.scrollWidth")
        client_w = pg.evaluate("() => document.documentElement.clientWidth")
        pg.screenshot(path=str(shot), full_page=True)
        b.close()
    return Image.open(shot).convert("L"), max(0, int(scroll_w) - int(client_w))


def _void_scan(img) -> list[str]:
    """Find the tallest run of near-uniform horizontal strips (a blank void)."""
    w, h = img.size
    px = img.load()
    step = max(6, w // 40)
    run = best = best_at = 0
    at = 0
    for y0 in range(0, h - STRIP_PX, STRIP_PX):
        vals = [px[x, y] for y in range(y0, y0 + STRIP_PX, 4) for x in range(0, w, step)]
        n = len(vals) or 1
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        if var ** 0.5 < BLANK_STDDEV:
            if run == 0:
                at = y0
            run += STRIP_PX
            if run > best:
                best, best_at = run, at
        else:
            run = 0
    if best > VOID_FRACTION * h:
        return [f"blank void ~{best}px ({best / h:.0%} of page) at y={best_at}"]
    return []


def gate(mockup: str | Path) -> dict:
    """Render + inspect a mockup. Returns {ok, reasons, page_h}. Fail-CLOSED: a render
    failure is NOT a pass (we can't prove it's fine => it doesn't ship)."""
    path = Path(mockup)
    reasons: list[str] = []
    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"ok": False, "reasons": [f"unreadable: {e}"], "page_h": 0}
    reasons += _stub_scan(html)
    try:
        img, overflow = _render_mobile(path)
    except Exception as e:
        reasons.append(f"render failed ({type(e).__name__}) — cannot verify")
        return {"ok": False, "reasons": reasons, "page_h": 0}
    if overflow > 4:
        reasons.append(f"horizontal overflow {overflow}px (breaks mobile)")
    reasons += _void_scan(img)
    return {"ok": not reasons, "reasons": reasons, "page_h": img.size[1]}


if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:
        r = gate(f)
        tag = "PASS" if r["ok"] else "FAIL"
        print(f"[{tag}] {f}  (h={r['page_h']}px)")
        for why in r["reasons"]:
            print(f"    - {why}")
