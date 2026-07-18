#!/usr/bin/env python3
"""site_video.py — animate a rebuild MOCKUP (or any URL) into a scrolling phone video.

The broken-site reel's whole problem was "reeks of stock": padding a clip with
Pexels/AI filler when the ONE thing that's uniquely theirs is the rebuilt site we
already made for them. This module renders that rebuild in a headless mobile browser,
smooth-scrolls top->bottom, and composites the capture inside a clean phone device
frame so it reads as "their new site, on a phone." It also does the before->after:
their CURRENT live (broken) site -> a clean wipe -> the animated rebuild.

    from core.site_video import mockup_scroll_clip, before_after_clip
    hero = mockup_scroll_clip("products_internal/landing/mockups/acme.com.html",
                              "hero.mp4", viewport="mobile", device_frame=True)
    reveal = before_after_clip("acme.com", ".../acme.com.html", "reveal.mp4")

$0 (headless Chromium + ffmpeg, no paid backends), fail-soft (every entry point
returns None instead of raising). The silent segments this produces are the HERO
content make_clip's `rebuild_reveal` style wraps with captions + music.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("site_video")

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
CANVAS_W, CANVAS_H = 1080, 1920

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# portrait viewports that map cleanly to the 1080x1920 frame. dsf=2 keeps text crisp.
VIEWPORTS = {
    "mobile": {"width": 412, "height": 892, "dsf": 2, "is_mobile": True},
    "tall":   {"width": 400, "height": 940, "dsf": 2, "is_mobile": True},
}

_STITCH_MAX_WARMUP = 40  # runaway guard on infinite-scroll pages


def _have_ffmpeg() -> bool:
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))


def _probe_duration(path: str) -> float:
    try:
        import json
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _to_target_url(html_path_or_url: str) -> str:
    """A local .html path -> file:// URI; a bare domain -> https://; a URL passes through."""
    s = str(html_path_or_url).strip()
    low = s.lower()
    if low.startswith(("http://", "https://", "file://")):
        return s
    p = Path(s)
    if p.exists():
        return p.resolve().as_uri()
    return "https://" + s.lstrip("/")


def _smoothstep(t: float) -> float:
    """Ease-in/ease-out so the scroll starts and stops like a camera move, not a jerk."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


# ── geometry: a phone device frame sized from the viewport aspect ─────────────────
def _geom(vp: dict) -> dict:
    aspect = vp["width"] / vp["height"]
    top_band = 120           # clear room above the phone for a "before/after" label
    bh = 1720                # phone body height
    by = top_band
    bezel_v, bezel_h = 64, 42
    sh = bh - 2 * bezel_v
    sw = int(round(sh * aspect))
    sx = (CANVAS_W - sw) // 2
    sy = by + bezel_v
    bx = sx - bezel_h
    bw = sw + 2 * bezel_h
    return {"bx": bx, "by": by, "bw": bw, "bh": bh,
            "sx": sx, "sy": sy, "sw": sw, "sh": sh, "label_y": 40}


def _phone_frame_assets(bg_png: Path, frame_png: Path, geom: dict) -> None:
    """Two static 1080x1920 PNGs: a neutral dark gradient background, and a phone-frame
    overlay whose SCREEN area is transparent (content shows through) and whose bezel is
    opaque (masks the content's square corners into rounded ones). Brand-neutral on
    purpose — no palette leak, and the site inside is unmistakably the star."""
    from PIL import Image, ImageDraw, ImageChops

    # background: soft vertical dark gradient + gentle vignette (premium, neutral)
    bg = Image.new("RGB", (CANVAS_W, CANVAS_H))
    top, bot = (18, 19, 24), (9, 10, 13)
    for y in range(CANVAS_H):
        t = y / CANVAS_H
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        ImageDraw.Draw(bg).line([(0, y), (CANVAS_W, y)], fill=(r, g, b))
    bg.save(bg_png, "PNG")

    # phone frame overlay
    fr = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(fr)
    bx, by, bw, bh = geom["bx"], geom["by"], geom["bw"], geom["bh"]
    body = [bx, by, bx + bw, by + bh]
    d.rounded_rectangle([bx - 6, by - 6, bx + bw + 6, by + bh + 6], radius=104,
                        fill=(30, 31, 36, 255))                     # outer rail
    d.rounded_rectangle(body, radius=94, fill=(15, 16, 19, 255))     # body
    d.rounded_rectangle(body, radius=94, outline=(70, 72, 80, 255), width=2)
    # speaker pill + camera dot in the top bezel (opaque region — never covers content)
    cx = bx + bw // 2
    d.rounded_rectangle([cx - 46, by + 26, cx + 46, by + 36], radius=5, fill=(46, 47, 54, 255))
    d.ellipse([cx + 66, by + 24, cx + 80, by + 38], fill=(40, 41, 48, 255))

    # cut the screen: rounded-rect region -> alpha 0 so the scrolling capture shows through
    sx, sy, sw, sh = geom["sx"], geom["sy"], geom["sw"], geom["sh"]
    mask = Image.new("L", (CANVAS_W, CANVAS_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([sx, sy, sx + sw, sy + sh], radius=54, fill=255)
    alpha = fr.getchannel("A")
    fr.putalpha(ImageChops.subtract(alpha, mask))
    fr.save(frame_png, "PNG")


def _hex(c: str, default: tuple) -> tuple:
    try:
        h = str(c).lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except Exception:
        return default


def _font(size: int):
    from PIL import ImageFont
    for f in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/Supplemental/Futura.ttc",
              "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            continue
    from PIL import ImageFont
    return ImageFont.load_default()


def _label_png(text: str, out: Path, accent: str | None = None) -> tuple[int, int]:
    """A rounded 'pill' label (e.g. 'your site today') centered horizontally, with a
    brand-accent dot + underline so it reads on-brand over any content. Full-frame-wide
    (x=0 overlay); returns (pill_w, pill_h)."""
    from PIL import Image, ImageDraw
    acc = _hex(accent, (255, 209, 102)) if accent else (255, 209, 102)
    font = _font(44)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tw = int(probe.textlength(text, font=font))
    dot = 18
    padx, pady = 38, 20
    w, h = tw + 2 * padx + dot + 14, 48 + 2 * pady
    img = Image.new("RGBA", (CANVAS_W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (CANVAS_W - w) // 2
    d.rounded_rectangle([x0, 0, x0 + w, h], radius=h // 2, fill=(16, 17, 21, 235))
    d.rounded_rectangle([x0, 0, x0 + w, h], radius=h // 2, outline=acc + (255,), width=2)
    cy = h // 2
    d.ellipse([x0 + padx - 4, cy - dot // 2, x0 + padx - 4 + dot, cy + dot // 2],
              fill=acc + (255,))
    d.text((x0 + padx + dot + 12, pady - 4), text, font=font, fill=(255, 255, 255, 255))
    img.save(out, "PNG")
    return w, h


def _endcard_png(name: str, cta: str, primary: str, accent: str, out: Path) -> None:
    """A branded outro still — the prospect's real palette + name + CTA. Kenburns pushes
    in on it so the card has motion. Brand-colored (their palette), not neutral: the whole
    point is that this ad is unmistakably a custom build for THEIR business."""
    from PIL import Image, ImageDraw
    p = _hex(primary, (37, 26, 24))
    a = _hex(accent, (127, 63, 42))
    # very dark brand primaries (e.g. deep browns/navys) go muddy — lift toward a richer
    # mid-tone so the endcard reads premium, not black. Keeps the brand hue.
    lum = 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]
    lift = 1.0 if lum >= 90 else (90 / max(lum, 18))
    p = tuple(min(255, int(c * lift)) for c in p)
    img = Image.new("RGB", (CANVAS_W, CANVAS_H))
    d = ImageDraw.Draw(img)
    # brand-tinted vertical gradient (top a touch warmer/brighter -> depth)
    top = tuple(min(255, int(x * 0.9 + 34)) for x in p)
    bot = tuple(int(x * 0.32) for x in p)
    for y in range(CANVAS_H):
        t = y / CANVAS_H
        d.line([(0, y), (CANVAS_W, y)],
               fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    cx = CANVAS_W // 2
    # accent kicker line
    d.rounded_rectangle([cx - 70, 720, cx + 70, 732], radius=6, fill=a + (255,))
    nf = _font(88)
    # wrap name to <= 2 lines
    words = (name or "your business").split()
    lines, cur = [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if d.textlength(t, font=nf) <= CANVAS_W - 160:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    lines = lines[:2]
    y = 800
    for ln in lines:
        w = d.textlength(ln, font=nf)
        d.text((cx - w / 2, y), ln, font=nf, fill=(255, 255, 255))
        y += 108
    sf = _font(46)
    sub = "your new site is ready"
    w = d.textlength(sub, font=sf)
    d.text((cx - w / 2, y + 24), sub, font=sf, fill=(230, 225, 222))
    # CTA pill in the accent color
    cf = _font(52)
    cta = (cta or "book now").strip()
    ctw = d.textlength(cta, font=cf)
    pw, ph = ctw + 96, 108
    px0, py0 = cx - pw / 2, y + 130
    d.rounded_rectangle([px0, py0, px0 + pw, py0 + ph], radius=ph // 2, fill=a + (255,))
    ink = (255, 255, 255) if sum(a) < 380 else (20, 18, 17)
    d.text((cx - ctw / 2, py0 + 24), cta, font=cf, fill=ink)
    img.save(out, "PNG")


# ── headless scroll capture ───────────────────────────────────────────────────────
def _render_scroll_frames(target_url: str, frames_dir: Path, vp: dict,
                          seconds: float, fps: int) -> int:
    """Smooth-scroll the page top->bottom and save one PNG per frame. A warm-up scroll
    first mounts lazy images / reveal animations. Returns the frame count (0 on failure)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        log.warning("site_video: playwright unavailable: %s", e)
        return 0
    frames_dir.mkdir(parents=True, exist_ok=True)
    n = max(2, int(round(seconds * fps)))
    vph = vp["height"]
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True,
                                  args=["--no-sandbox", "--mute-audio", "--hide-scrollbars"])
            ctx = b.new_context(
                viewport={"width": vp["width"], "height": vp["height"]},
                device_scale_factor=vp.get("dsf", 2),
                is_mobile=vp.get("is_mobile", True),
                user_agent=MOBILE_UA,
                ignore_https_errors=True,
            )
            pg = ctx.new_page()
            try:
                pg.goto(target_url, wait_until="networkidle", timeout=25000)
            except Exception:
                try:
                    pg.goto(target_url, wait_until="domcontentloaded", timeout=25000)
                except Exception as e:
                    log.warning("site_video: goto failed for %s: %s", target_url, e)
                    b.close()
                    return 0
            try:
                pg.add_style_tag(content="::-webkit-scrollbar{width:0;height:0;display:none}")
            except Exception:
                pass

            def _doc_h() -> int:
                try:
                    return int(pg.evaluate(
                        "() => Math.max(document.documentElement.scrollHeight,"
                        " document.body ? document.body.scrollHeight : 0)"))
                except Exception:
                    return vph

            # warm-up pass so lazy content + scroll-triggered animation actually render
            total = _doc_h()
            y = 0
            steps = 0
            while y < total and steps < _STITCH_MAX_WARMUP:
                try:
                    pg.evaluate("(s) => window.scrollTo(0, s)", y)
                except Exception:
                    break
                pg.wait_for_timeout(220)
                y += vph
                steps += 1
            try:
                pg.evaluate("() => window.scrollTo(0, 0)")
            except Exception:
                pass
            pg.wait_for_timeout(500)

            total = _doc_h()
            max_scroll = max(0, total - vph)
            for i in range(n):
                frac = i / (n - 1) if n > 1 else 0.0
                yy = int(max_scroll * _smoothstep(frac))
                try:
                    pg.evaluate("(s) => window.scrollTo(0, s)", yy)
                except Exception:
                    pass
                pg.wait_for_timeout(30)
                try:
                    pg.screenshot(path=str(frames_dir / f"f{i:04d}.png"), full_page=False)
                except Exception:
                    b.close()
                    return 0
            b.close()
    except Exception as e:
        log.warning("site_video: scroll capture failed: %s", e)
        return 0
    got = len(list(frames_dir.glob("f*.png")))
    return got if got >= 2 else 0


# cohesive color grades: 'before' reads dated/tired (dull, soft, dim), 'after' reads
# premium (vibrant, crisp, bright) — the contrast makes the bespoke transformation undeniable.
_GRADES = {
    "before": "eq=saturation=0.34:contrast=0.9:brightness=-0.045:gamma=0.96,"
              "gblur=sigma=0.5,vignette=PI/4.4",
    "after":  "eq=saturation=1.17:contrast=1.07:brightness=0.02:gamma=1.02,"
              "unsharp=5:5:0.55:5:5:0.0",
    None:     "eq=saturation=1.05:contrast=1.02",
}


def _assemble_framed_segment(frames_dir: Path, out: Path, fps: int, geom: dict,
                             bg_png: Path, frame_png: Path, label_png: Path | None,
                             label_wh: tuple[int, int] | None, seconds: float,
                             grade: str | None = None) -> bool:
    """Composite: bg  <-  color-graded scroll capture in the screen box  <-  phone frame
    <-  kinetic slide-in label."""
    grade_f = _GRADES.get(grade, _GRADES[None])
    inputs = ["-framerate", str(fps), "-i", str(frames_dir / "f%04d.png"),
              "-loop", "1", "-i", str(bg_png),
              "-loop", "1", "-i", str(frame_png)]
    fc = [
        f"[0:v]scale={geom['sw']}:{geom['sh']}:flags=lanczos,{grade_f},setsar=1[content]",
        f"[1:v][content]overlay={geom['sx']}:{geom['sy']}[a]",
        f"[a][2:v]overlay=0:0[b]",
    ]
    last = "b"
    if label_png and label_png.exists():
        ly = geom["label_y"]
        inputs += ["-loop", "1", "-i", str(label_png)]
        fc.append("[3:v]format=rgba,fade=t=in:st=0:d=0.35:alpha=1[lab]")
        # slide-in from ~26px below its rest position, easing over 0.35s
        fc.append(f"[{last}][lab]overlay=0:'{ly}+26-26*min(1,t/0.35)'[v]")
        last = "v"
    else:
        fc.append(f"[{last}]null[v]")
        last = "v"
    cmd = [FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc),
           "-map", "[v]", "-t", f"{seconds:.3f}", "-r", str(fps),
           "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not out.exists():
        log.warning("site_video: framed assemble failed: %s", (r.stderr or "")[-500:])
        return False
    return True


def _assemble_plain_segment(frames_dir: Path, out: Path, fps: int, vp: dict,
                            label_png: Path | None, geom: dict, seconds: float,
                            grade: str | None = None) -> bool:
    """No device frame: fit the color-graded capture to full height, pad on a neutral bg."""
    grade_f = _GRADES.get(grade, _GRADES[None])
    aspect = vp["width"] / vp["height"]
    cw = int(round(CANVAS_H * aspect))
    padx = max(0, (CANVAS_W - cw) // 2)
    inputs = ["-framerate", str(fps), "-i", str(frames_dir / "f%04d.png")]
    fc = [f"[0:v]scale={cw}:{CANVAS_H}:flags=lanczos,{grade_f},"
          f"pad={CANVAS_W}:{CANVAS_H}:{padx}:0:color=0x0d0d10,setsar=1[v0]"]
    last = "v0"
    if label_png and label_png.exists():
        inputs += ["-loop", "1", "-i", str(label_png)]
        fc.append("[1:v]format=rgba,fade=t=in:st=0:d=0.4:alpha=1[lab]")
        fc.append(f"[{last}][lab]overlay=0:{geom['label_y']}[v]")
        last = "v"
    cmd = [FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc),
           "-map", f"[{last}]", "-t", f"{seconds:.3f}", "-r", str(fps),
           "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not out.exists():
        log.warning("site_video: plain assemble failed: %s", (r.stderr or "")[-500:])
        return False
    return True


def mockup_scroll_clip(html_path_or_url: str, out_mp4: str, viewport: str = "mobile",
                       device_frame: bool = True, seconds: float = 6.0, fps: int = 30,
                       label: str | None = None, work: str | None = None,
                       grade: str | None = None, accent: str | None = None) -> Path | None:
    """Render the rebuild mockup (or any URL) in a headless mobile browser, smooth-scroll
    top->bottom, and assemble a 1080x1920 mp4 — optionally inside a clean phone frame,
    color-graded, with a brand-accent kinetic label.

    html_path_or_url : local .html path, bare domain, or full URL.
    device_frame     : composite the capture inside a neutral phone device frame.
    label            : optional brand-accent slide-in pill ("your new site").
    grade            : 'before' (dull/dated), 'after' (vibrant/premium), or None.
    accent           : brand accent hex for the label.
    Returns the mp4 Path, or None (fail-soft).
    """
    try:
        if not _have_ffmpeg():
            log.warning("site_video: ffmpeg/ffprobe missing")
            return None
        vp = VIEWPORTS.get(viewport) or VIEWPORTS["mobile"]
        wd = Path(work) if work else Path(tempfile.mkdtemp(prefix="sitevid_"))
        wd.mkdir(parents=True, exist_ok=True)
        frames_dir = wd / "frames"
        target = _to_target_url(html_path_or_url)
        n = _render_scroll_frames(target, frames_dir, vp, seconds, fps)
        if n < 2:
            return None
        geom = _geom(vp)
        label_png = None
        label_wh = None
        if label:
            label_png = wd / "label.png"
            label_wh = _label_png(label, label_png, accent=accent)
        out_p = Path(out_mp4)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        if device_frame:
            bg_png, frame_png = wd / "bg.png", wd / "phone.png"
            _phone_frame_assets(bg_png, frame_png, geom)
            ok = _assemble_framed_segment(frames_dir, out_p, fps, geom, bg_png,
                                          frame_png, label_png, label_wh, seconds, grade=grade)
        else:
            ok = _assemble_plain_segment(frames_dir, out_p, fps, vp, label_png, geom,
                                         seconds, grade=grade)
        return out_p if ok and out_p.exists() else None
    except Exception as e:
        log.warning("site_video: mockup_scroll_clip failed: %s", e)
        return None


def _xfade(before_mp4: Path, after_mp4: Path, out: Path, fps: int = 30,
           transition: str = "fadewhite", dur: float = 0.5) -> bool:
    """Energetic reveal from the 'before' segment into the 'after' — a fast flash-to-white
    cut that reads like an edited ad, not a slideshow dissolve."""
    db = _probe_duration(str(before_mp4))
    if db <= 0:
        return False
    offset = max(0.0, db - dur)
    fc = (f"[0:v]scale={CANVAS_W}:{CANVAS_H},setsar=1,fps={fps}[b];"
          f"[1:v]scale={CANVAS_W}:{CANVAS_H},setsar=1,fps={fps}[a];"
          f"[b][a]xfade=transition={transition}:duration={dur}:offset={offset:.3f},"
          f"format=yuv420p[v]")
    cmd = [FFMPEG, "-y", "-i", str(before_mp4), "-i", str(after_mp4),
           "-filter_complex", fc, "-map", "[v]", "-r", str(fps),
           "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not out.exists():
        log.warning("site_video: xfade failed: %s", (r.stderr or "")[-500:])
        return False
    return True


def before_after_clip(live_url: str, mockup_path: str, out_mp4: str,
                      viewport: str = "mobile", before_seconds: float = 2.6,
                      after_seconds: float = 6.4, fps: int = 30,
                      work: str | None = None, accent: str | None = None) -> Path | None:
    """Their CURRENT live site ('your site today', graded dull/dated) -> an energetic
    flash cut -> the animated rebuild scroll ('your new site', graded vibrant/premium).
    The dull->vibrant contrast makes the bespoke transformation undeniable. Fail-soft: if
    the live 'before' can't be captured (broken-site prospects routinely won't render —
    that's the pitch), fall back to the rebuild reveal alone. Returns the mp4 Path or None."""
    try:
        wd = Path(work) if work else Path(tempfile.mkdtemp(prefix="sitevid_ba_"))
        wd.mkdir(parents=True, exist_ok=True)
        out_p = Path(out_mp4)
        out_p.parent.mkdir(parents=True, exist_ok=True)

        after = mockup_scroll_clip(mockup_path, str(wd / "after.mp4"), viewport=viewport,
                                   device_frame=True, seconds=after_seconds, fps=fps,
                                   label="your new site", grade="after", accent=accent,
                                   work=str(wd / "after_w"))
        if not after:
            return None  # the rebuild is the whole point — no after, no clip
        before = None
        if live_url:
            before = mockup_scroll_clip(live_url, str(wd / "before.mp4"), viewport=viewport,
                                        device_frame=True, seconds=before_seconds, fps=fps,
                                        label="your site today", grade="before", accent=accent,
                                        work=str(wd / "before_w"))
        if before and _xfade(Path(before), Path(after), out_p, fps=fps):
            return out_p if out_p.exists() else None
        # no usable 'before' -> ship the rebuild reveal alone
        shutil.copy2(after, out_p)
        return out_p if out_p.exists() else None
    except Exception as e:
        log.warning("site_video: before_after_clip failed: %s", e)
        return None


def endcard_clip(name: str, cta: str, primary: str, accent: str, out_mp4: str,
                 seconds: float = 2.2, fps: int = 30, work: str | None = None) -> Path | None:
    """A branded, moving outro: the prospect's real palette + name + CTA, with a slow
    push-in (kenburns) so it has motion. Fail-soft."""
    try:
        from core import kenburns
        wd = Path(work) if work else Path(tempfile.mkdtemp(prefix="sitevid_end_"))
        wd.mkdir(parents=True, exist_ok=True)
        card = wd / "endcard.png"
        _endcard_png(name, cta, primary, accent, card)
        res = kenburns.stills_to_clip([str(card)], None, str(out_mp4),
                                      per_still=seconds, xfade=0.0, zoom_step=0.0012)
        return Path(out_mp4) if res and Path(out_mp4).exists() else None
    except Exception as e:
        log.warning("site_video: endcard_clip failed: %s", e)
        return None


def finish_reel(base_video: str, captions: list, music: str | None, out_mp4: str, *,
                fps: int = 30, grade: bool = True, music_volume: float = 0.55) -> Path | None:
    """Lay KINETIC captions (slide-up + fade), a cohesive global grade, and a music bed
    onto the assembled base timeline. captions: [{png, start, end, slide?}]. Fail-soft."""
    try:
        base = str(base_video)
        if not Path(base).exists():
            return None
        dur = _probe_duration(base)
        if dur <= 0:
            return None
        caps = [c for c in (captions or []) if c.get("png") and Path(c["png"]).exists()]
        inputs = ["-i", base]
        for c in caps:
            inputs += ["-loop", "1", "-i", str(c["png"])]
        music_idx = None
        if music and Path(music).exists():
            music_idx = 1 + len(caps)
            inputs += ["-stream_loop", "-1", "-i", str(music)]

        fc: list[str] = []
        # subtle global grade ties before/after/broll/endcard into one look
        if grade:
            fc.append("[0:v]eq=saturation=1.05:contrast=1.02,vignette=PI/5.2[g]")
            prev = "g"
        else:
            fc.append("[0:v]null[g]")
            prev = "g"
        fade = 0.35
        for i, c in enumerate(caps):
            k = i + 1
            st = max(0.0, float(c["start"]))
            en = min(dur, float(c["end"]))
            out_st = max(st, en - fade)
            slide = int(c.get("slide", 28))
            fc.append(f"[{k}:v]format=rgba,fade=t=in:st={st:.2f}:d={fade}:alpha=1,"
                      f"fade=t=out:st={out_st:.2f}:d={fade}:alpha=1[cap{k}]")
            lbl = f"ov{k}"
            fc.append(
                f"[{prev}][cap{k}]overlay=x=0:"
                f"y='{slide}-{slide}*min(1,(t-{st:.2f})/0.35)':"
                f"enable='between(t,{st:.2f},{en:.2f})'[{lbl}]")
            prev = lbl
        vlabel = prev

        map_args = ["-map", f"[{vlabel}]"]
        if music_idx is not None:
            a_out = max(0.0, dur - 1.2)
            fc.append(f"[{music_idx}:a]volume={music_volume},"
                      f"afade=t=in:st=0:d=0.6,afade=t=out:st={a_out:.2f}:d=1.2[aud]")
            map_args += ["-map", "[aud]"]

        cmd = [FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc), *map_args,
               "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", str(fps)]
        if music_idx is not None:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-t", f"{dur:.3f}", str(out_mp4)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if r.returncode != 0 or not Path(out_mp4).exists():
            log.warning("site_video: finish_reel failed: %s", (r.stderr or "")[-600:])
            return None
        return Path(out_mp4)
    except Exception as e:
        log.warning("site_video: finish_reel failed: %s", e)
        return None


def concat_segments(paths: list, out_mp4: str, fps: int = 30) -> Path | None:
    """Concatenate silent 1080x1920 segments (re-encoded, normalized) into one mp4."""
    try:
        segs = [Path(p) for p in paths if p and Path(p).exists()]
        if not segs:
            return None
        out_p = Path(out_mp4)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        if len(segs) == 1:
            shutil.copy2(segs[0], out_p)
            return out_p
        inputs: list[str] = []
        fc: list[str] = []
        for i, p in enumerate(segs):
            inputs += ["-i", str(p)]
            fc.append(f"[{i}:v]scale={CANVAS_W}:{CANVAS_H},setsar=1,fps={fps},format=yuv420p[v{i}]")
        cc = "".join(f"[v{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[v]"
        cmd = [FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc) + ";" + cc,
               "-map", "[v]", "-r", str(fps), "-c:v", "libx264", "-preset", "medium",
               "-pix_fmt", "yuv420p", str(out_p)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not out_p.exists():
            log.warning("site_video: concat failed: %s", (r.stderr or "")[-500:])
            return None
        return out_p
    except Exception as e:
        log.warning("site_video: concat_segments failed: %s", e)
        return None


# ── Remotion path: frame-perfect, studio-motion bespoke reveal (the primary renderer) ──
# Instead of screen-capturing a scroll, we capture ONE tall image of their real rebuilt
# page + a recognizable fragment of their real CURRENT site, then let Remotion drive every
# motion frame-perfectly: hard before->after cut, camera push-in, parallax, beat-timed
# kinetic type, brand-colored device chrome + endcard. Un-gettable from a generic prompt.
def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (s or "").lower()).strip("-")[:48] or "site"


def _capture_tall_png(target_url: str, out_png: Path, vp: dict, mode: str = "full") -> tuple | None:
    """A full-page (mode='full', warm-scrolled so reveal-on-scroll content is visible) or
    top-fragment (mode='top') PNG at a mobile viewport. Returns (w, h) or None."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True,
                                  args=["--no-sandbox", "--mute-audio", "--hide-scrollbars"])
            ctx = b.new_context(
                viewport={"width": vp["width"], "height": vp["height"]},
                device_scale_factor=vp.get("dsf", 2), is_mobile=vp.get("is_mobile", True),
                user_agent=MOBILE_UA, ignore_https_errors=True)
            pg = ctx.new_page()
            try:
                pg.goto(target_url, wait_until="networkidle", timeout=25000)
            except Exception:
                try:
                    pg.goto(target_url, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    b.close()
                    return None
            try:
                pg.add_style_tag(content="::-webkit-scrollbar{width:0;height:0;display:none}")
            except Exception:
                pass
            if mode == "full":
                try:
                    vph = vp["height"]
                    total = int(pg.evaluate(
                        "()=>Math.max(document.documentElement.scrollHeight,"
                        " document.body?document.body.scrollHeight:0)"))
                    y, steps = 0, 0
                    while y < total and steps < _STITCH_MAX_WARMUP:
                        pg.evaluate("(s)=>window.scrollTo(0,s)", y)
                        pg.wait_for_timeout(150)
                        y += vph
                        steps += 1
                    pg.evaluate("()=>window.scrollTo(0,0)")
                    pg.wait_for_timeout(500)
                    # neutralize position:fixed/sticky so a full-page screenshot doesn't
                    # duplicate the site header/nav down the page (a known capture artifact)
                    pg.evaluate(
                        "()=>{document.querySelectorAll('*').forEach(el=>{"
                        "const p=getComputedStyle(el).position;"
                        "if(p==='fixed'||p==='sticky'){el.style.position='static';}})}")
                    pg.wait_for_timeout(200)
                except Exception:
                    pass
                pg.screenshot(path=str(out_png), full_page=True)
            else:
                pg.wait_for_timeout(1200)
                pg.screenshot(path=str(out_png), full_page=False)
            b.close()
        from PIL import Image
        with Image.open(out_png) as im:
            w, h = im.size
        return (w, h) if Path(out_png).exists() and w > 0 and h > 0 else None
    except Exception as e:
        log.warning("site_video: capture failed (%s): %s", mode, e)
        return None


def _extract_logo_png(live_url: str, out_png: Path, vp: dict) -> bool:
    """Best-effort element screenshot of the prospect's real logo from their live site.
    Fail-soft: any miss returns False and the reel falls back to their wordmark name."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    sels = ["header img", "a[href='/'] img", "img[alt*='logo' i]", "img[class*='logo' i]",
            ".logo img", "#logo img", "img[src*='logo' i]"]
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox", "--mute-audio"])
            ctx = b.new_context(viewport={"width": vp["width"], "height": vp["height"]},
                                device_scale_factor=2, user_agent=MOBILE_UA, ignore_https_errors=True)
            pg = ctx.new_page()
            try:
                pg.goto(live_url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                b.close()
                return False
            pg.wait_for_timeout(1200)
            for sel in sels:
                try:
                    loc = pg.locator(sel).first
                    if loc.count() == 0:
                        continue
                    box = loc.bounding_box()
                    if not box or box["width"] < 40 or box["height"] < 16:
                        continue
                    if box["width"] > vp["width"] * 0.9:  # a hero/banner, not a logo
                        continue
                    loc.screenshot(path=str(out_png))
                    b.close()
                    return Path(out_png).exists()
                except Exception:
                    continue
            b.close()
    except Exception:
        return False
    return False


def _beat_music_bed(work: Path, seconds: float, bpm: int = 100) -> tuple:
    """A $0, public-domain-by-construction bed with an audible beat (a tremolo-gated bass
    pulse at `bpm` under an ambient pad) so Remotion can snap cuts/reveals to the beat."""
    out = Path(work) / "music_beat.m4a"
    beat = bpm / 60.0
    fc = ("sine=f=220:r=44100[a];sine=f=277:r=44100[b];sine=f=330:r=44100[c];"
          "[a][b][c]amix=inputs=3:normalize=1,lowpass=f=1100,volume=0.42[pad];"
          f"sine=f=68:r=44100,tremolo=f={beat:.4f}:d=0.92,volume=0.55[kick];"
          "[pad][kick]amix=inputs=2:normalize=0,"
          f"afade=t=in:st=0:d=1.2,afade=t=out:st={max(0.0, seconds - 1.6):.2f}:d=1.6[m]")
    cmd = [FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc", "-filter_complex", fc,
           "-map", "[m]", "-t", f"{seconds:.2f}", "-c:a", "aac", "-b:a", "160k", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 10_000:
            return str(out), f"generated beat bed {bpm}bpm (public domain by construction)"
    except Exception:
        pass
    return None, ""


def _ensure_remotion_reveal(project: Path) -> bool:
    """Install the RebuildReveal composition + a Root that registers it (idempotent) into
    the shared Remotion project from the in-repo templates."""
    try:
        tpl = ROOT / "core" / "remotion_templates"
        src = Path(project) / "src"
        for name in ("RebuildReveal.tsx", "Root.tsx"):
            t = tpl / name
            if not t.exists():
                return False
            (src / name).write_text(t.read_text())
        return True
    except Exception as e:
        log.warning("site_video: could not install RebuildReveal comp: %s", e)
        return False


def remotion_reveal_clip(business: dict, brand: dict, out_mp4: str, work: str,
                         meta: dict | None = None, project_dir: str | None = None) -> Path | None:
    """Render the bespoke before->after reveal in Remotion (frame-perfect). Captures their
    real rebuild + real current-site fragment + logo, generates a beat bed, and renders.
    Fail-soft: returns None if Remotion isn't available or any step fails."""
    meta = meta if meta is not None else {}
    try:
        from core import remotion_video
        if not remotion_video.configured():
            log.warning("site_video: remotion not configured")
            return None
        project = Path(project_dir or remotion_video.PROJECT)
        if not (project / "package.json").exists():
            return None
        mockup = business.get("mockup_path") or ""
        if not mockup or not Path(mockup).exists():
            return None
        pub = project / "public"
        try:
            pub.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        vp = VIEWPORTS["mobile"]
        wd = Path(work)
        wd.mkdir(parents=True, exist_ok=True)
        slug = _slug(business.get("domain") or business.get("name"))

        after_png = wd / "after.png"
        dims = _capture_tall_png(_to_target_url(mockup), after_png, vp, "full")
        if not dims:
            return None
        aw, ah = dims

        dom = (business.get("domain") or "").strip()
        before_png = wd / "before.png"
        before_ok = bool(dom) and bool(_capture_tall_png(_to_target_url(dom), before_png, vp, "top"))
        # Logo auto-extraction is too unreliable (grabs menu icons / nav fragments) to put
        # on a client-facing endcard — the clean wordmark name reads better, and the BEFORE
        # segment already shows their real logo/header. Kept opt-in via SITE_VIDEO_LOGO=1.
        logo_png = wd / "logo.png"
        logo_ok = False
        import os as _os
        if _os.environ.get("SITE_VIDEO_LOGO") == "1" and dom:
            logo_ok = _extract_logo_png(_to_target_url(dom), logo_png, vp)

        screen_w, screen_h = 726, 1572
        scale = screen_w / aw
        scaled_h = ah * scale
        scroll = max(0.0, scaled_h - screen_h)
        fps = 30
        after_sec = max(5.5, min(10.5, (scroll / 520.0) + 0.5))
        before_f, end_f, after_f = round(0.7 * fps), round(2.8 * fps), round(after_sec * fps)
        dur = before_f + after_f + end_f
        seconds = dur / fps
        music_path, mnote = _beat_music_bed(wd, seconds, 100)

        def _place(src_path, tag, ext):
            n = f"{tag}_{slug}.{ext}"
            shutil.copy2(src_path, pub / n)
            return n

        after_name = _place(after_png, "after", "png")
        before_name = _place(before_png, "before", "png") if before_ok else None
        logo_name = _place(logo_png, "logo", "png") if logo_ok else None
        audio_name = _place(music_path, "music", "m4a") if music_path else None

        if not _ensure_remotion_reveal(project):
            return None

        lines = ["built for phones", "loads in a blink", "easy to book online"]
        props = {"afterImg": after_name, "afterW": aw, "afterH": ah, "beforeImg": before_name,
                 "logo": logo_name, "name": (brand.get("name") or business.get("name") or "your business"),
                 "city": business.get("city") or "", "cta": "book now",
                 "primary": brand.get("primary", "#141413"), "accent": brand.get("accent", "#d97757"),
                 "ink": "#ffffff", "lines": lines, "audio": audio_name, "bpm": 100,
                 "fps": fps, "width": 1080, "height": 1920, "durationInFrames": dur}
        props_path = wd / "reveal_props.json"
        props_path.write_text(json.dumps(props))

        npx = shutil.which("npx")
        if not npx:
            return None
        out_p = Path(out_mp4)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        cmd = [npx, "remotion", "render", "src/index.ts", "RebuildReveal", str(out_p),
               f"--props={props_path}", "--concurrency=2"]
        r = subprocess.run(cmd, cwd=str(project), capture_output=True, text=True, timeout=1500)
        if r.returncode != 0 or not out_p.exists():
            log.warning("site_video: remotion render failed: %s", (r.stderr or r.stdout or "")[-800:])
            return None
        meta.update({"renderer": "remotion", "before_captured": before_ok, "logo_used": logo_ok,
                     "after_dims": [aw, ah], "scroll_px": round(scroll), "duration_s": round(seconds, 1),
                     "music": mnote, "lines": lines, "bpm": 100})
        return out_p
    except Exception as e:
        log.warning("site_video: remotion_reveal_clip failed: %s", e)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import argparse
    ap = argparse.ArgumentParser(description="animate a rebuild mockup into a scrolling phone video")
    ap.add_argument("target", help="mockup .html path, domain, or URL")
    ap.add_argument("out", help="output mp4")
    ap.add_argument("--before", help="live URL/domain for a before->after reveal")
    ap.add_argument("--no-frame", action="store_true", help="skip the phone device frame")
    ap.add_argument("--seconds", type=float, default=6.4)
    args = ap.parse_args()
    if args.before:
        print(before_after_clip(args.before, args.target, args.out, after_seconds=args.seconds))
    else:
        print(mockup_scroll_clip(args.target, args.out, device_frame=not args.no_frame,
                                 seconds=args.seconds, label="your new site"))
