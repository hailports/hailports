#!/usr/bin/env python3
"""Focus-safe, corner-parked Chrome launch for macOS automation.

Two bugs this fixes, both from launching a rendered (headful) automation window:

1. FOCUS THEFT. Executing the Chrome .app binary directly -- or even `open -gjn`
   -- still makes the freshly-created window become key/front, so it grabs the
   owner's keyboard + cursor the instant it opens. `-g` alone does NOT stop it
   (Chrome activates itself on window creation; verified empirically). The only
   reliable macOS answer is to HAND FOCUS BACK: capture whoever was frontmost,
   launch, and the moment the debug port answers (window now exists) reactivate
   that app. Control returns in ~1-2s and stays returned.

2. THE MID-SCREEN BLANK-WINDOW FLASH. `--window-position=-32000,-32000` is meant
   to hide the window, but on a single display macOS CLAMPS that extreme back
   on-screen as a tall left-edge sliver (the "blank tab pops then disappears"
   report). A MODERATE offset -- the bottom-left NUB, {NUB_PX - width, screenH -
   NUB_PX} -- is honored verbatim by the WindowServer (same finding the
   foreground-warden's _park_bottom_left relies on), so the window opens straight
   to a barely-visible corner nub instead of flashing mid-screen. We rewrite any
   -32000 window-position in the command to that nub before launching.

Headless (`--headless=new`) renders no window and never foregrounds, so it does
NOT need this -- keep headless launches on a plain Popen. This is only for the
handful of flows that MUST render a real window (YouTube Studio's <input type=file>
won't mount headless; some sites' bot-detection trips on the headless fingerprint).

Fire-and-forget: `open` returns before the debug port is up, so callers still poll
the port for readiness exactly as they did with the old direct-Popen path.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class ForegroundBusy(RuntimeError):
    """Raised INSTEAD of launching an offscreen-headful Chrome while the owner is actively
    using the machine. Per the header, Chrome self-activates on window creation and we can
    hold the foreground for up to ~16s while handing focus back — that jacked Operator's screen
    mid-work (2026-07-09). Callers must defer the lane and retry on the next cycle."""


def user_idle_seconds() -> float:
    """Seconds since the last human keyboard/mouse input. Returns -1.0 if unknowable."""
    try:
        # NOTE: no `-d 1` — HIDIdleTime lives deeper than depth 1 and gets filtered out,
        # which silently returned -1 (= "active" forever, permanently blocking the lane).
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                             capture_output=True, text=True, timeout=8).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if m:
            return int(m.group(1)) / 1e9  # ioreg reports nanoseconds
    except Exception:
        pass
    return -1.0

NUB_PX = 22  # px of the parked window left peeking into the bottom-left corner (matches the warden)
_DEFAULT_SCREEN_H = 1080  # fallback if the desktop bounds can't be read
_DEFAULT_SCREEN_W = 1440  # fallback if the desktop bounds can't be read
# deliberate one-time human login lock — never fight it by handing focus away (path derived, not hardcoded)
_LOGIN_LOCK = Path(__file__).resolve().parent.parent / "data" / "runtime" / "CDP_LOGIN_IN_PROGRESS"


def _venv_python() -> str:
    """Path to the repo venv python (cdp_minimize needs playwright, which lives there).
    Falls back to the current interpreter if the venv is missing."""
    import sys
    cand = Path(__file__).resolve().parent.parent / "venv" / "bin" / "python3"
    return str(cand) if cand.exists() else sys.executable


def app_bundle(chrome_bin: str) -> str:
    """'.../Google Chrome.app/Contents/MacOS/Google Chrome' -> '.../Google Chrome.app'."""
    p = Path(chrome_bin)
    for parent in p.parents:
        if parent.suffix == ".app":
            return str(parent)
    return "/Applications/Google Chrome.app"


def _frontmost_app() -> str:
    """Name of the app the owner is currently in (so we can hand focus back to it)."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to name of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=4)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _screen_wh() -> tuple[int, int]:
    """(width, height) of the desktop in logical points. Finder returns bounds as
    'left, top, right, bottom' -> width=right, height=bottom. Falls back if unreadable."""
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=4)
        parts = [int(x.strip()) for x in (r.stdout or "").strip().split(",")]
        return parts[2], parts[3]
    except Exception:
        return _DEFAULT_SCREEN_W, _DEFAULT_SCREEN_H


def _screen_height() -> int:
    return _screen_wh()[1]


def _win_dims(cmd: list[str]) -> tuple[int, int]:
    for a in cmd:
        if a.startswith("--window-size="):
            try:
                w, h = a.split("=", 1)[1].split(",")[:2]
                return int(w), int(h)
            except Exception:
                break
    return 1920, 1080


def _park_to_nub(cmd: list[str]) -> list[str]:
    """Force EVERY headful launch straight into the bottom-left nub
    {NUB_PX - width, screenH - NUB_PX}, overriding whatever --window-position the
    caller passed (or injecting one if absent). Owner mandate: an autonomous headful
    Chrome must ALWAYS tuck as far into the lower-left corner as possible and NEVER
    open mid-screen or hold focus. This is unconditional on purpose — a forgotten or
    stale --window-position must not be able to leak a window onto the screen. The
    only legitimate on-screen headful path is a gated interactive human login, which
    never routes through launch_headful_offscreen()/this helper."""
    sw, sh = _screen_wh()
    dw, dh = _win_dims(cmd)
    # A window LARGER than the display can't hang off the edge as a nub — macOS clamps
    # the oversized window fully back on-screen, landing the whole 1920x1080 on top of
    # everything (the "grabs the whole screen" bug on sub-1080p displays). Shrink the
    # window to fit the screen FIRST; only then is the off-left nub offset honored.
    w = min(dw, sw - 2)
    h = min(dh, sh - 2)
    posx = NUB_PX - w
    posy = sh - NUB_PX
    pos = f"--window-position={posx},{posy}"
    size = f"--window-size={w},{h}"
    out, seen_pos, seen_size = [], False, False
    for a in cmd:
        if a.startswith("--window-position="):
            out.append(pos); seen_pos = True
        elif a.startswith("--window-size="):
            out.append(size); seen_size = True
        else:
            out.append(a)
    inject = ([] if seen_pos else [pos]) + ([] if seen_size else [size])
    if inject:  # inject right after the binary so they're always applied
        out = [out[0], *inject, *out[1:]]
    return out


def launch_headful_offscreen(chrome_cmd: list[str], port: int | None = None,
                             restore_focus: bool = True) -> None:
    """Launch a NEW headful Chrome from a `[CHROME_BIN, *flags]` command, parked at the
    bottom-left nub, without holding the foreground.

    port: the remote-debugging port (parsed from the command if omitted); used to detect
    when the window exists so focus can be handed back. Pass it when you have it."""
    if not chrome_cmd:
        raise ValueError("chrome_cmd is empty")

    # FOREGROUND RULE (HARD): an automation must NEVER jack the screen. A headful Chrome
    # always steals focus on window creation (see header) and we hold it for up to ~16s
    # handing it back. So only launch when the owner is genuinely away. FAIL CLOSED:
    # unknown idle (-1) counts as "active", so a jack can never happen by accident.
    gate = float(os.environ.get("HEADFUL_IDLE_GATE_S", "180"))
    if gate > 0:
        idle = user_idle_seconds()
        if idle < gate:
            raise ForegroundBusy(
                f"owner active (HID idle {idle:.0f}s < {gate:.0f}s) — deferring offscreen-headful Chrome")

    cmd = _park_to_nub(chrome_cmd)
    bundle = app_bundle(cmd[0])
    if port is None:
        for a in cmd:
            if a.startswith("--remote-debugging-port="):
                try:
                    port = int(a.split("=", 1)[1])
                except Exception:
                    pass
                break

    prior = _frontmost_app() if restore_focus else ""
    subprocess.Popen(
        ["open", "-gjn", "-a", bundle, "--args", *cmd[1:]],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Hand focus back AND MINIMIZE, the moment the window actually exists (port up). The nub
    # only limits a *stable* window to a 22px peek; the instant Chrome activates it for OAuth
    # it comes fully forward AND macOS un-parks it. The only reliable hide is a CDP minimize
    # (Browser.setWindowBounds windowState=minimized) — a minimized window cannot be clamped
    # or peek. We fire it repeatedly for the first ~14s to swallow the launch paint AND the
    # immediate OAuth/consent re-raise; the persistent window-hider watcher covers the long tail.
    # Detached so it survives the (often short-lived) caller exiting. Never reactivate Chrome
    # onto itself, and never fight a deliberate one-time human login.
    if port:
        guard = str(_LOGIN_LOCK)
        py = _venv_python()
        minimizer = str(Path(__file__).resolve().parent.parent / "tools" / "cdp_minimize.py")
        reactivate = ""
        if restore_focus and prior and prior != "Google Chrome":
            prior_esc = prior.replace('"', '\\"')
            reactivate = f'osascript -e \'tell application "{prior_esc}" to activate\' >/dev/null 2>&1 || true; '
        watcher = (
            f'[ -e "{guard}" ] && exit 0; '
            f'for i in $(seq 1 40); do '
            f'curl -s --max-time 2 "http://127.0.0.1:{port}/json/version" >/dev/null 2>&1 && break; '
            f'sleep 0.4; done; '
            f'{reactivate}'
            # re-minimize on a tight loop: catches the launch window + any OAuth-triggered re-raise.
            f'for j in $(seq 1 7); do [ -e "{guard}" ] && break; '
            f'"{py}" "{minimizer}" {port} >/dev/null 2>&1 || true; sleep 2; done'
        )
        try:
            subprocess.Popen(["bash", "-c", watcher], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ============================================================================
# NO-FOREGROUND-STEAL LAUNCH POLICY  (owner mandate: autonomous Chrome must
# NEVER flash the macOS foreground or warp the cursor). Every autonomous
# Playwright launch MUST go through resolve_launch()/safe_launch(); raw
# chromium.launch(headless=False, ...) outside this module is banned and
# enforced by tools/check_no_headful_steal.py.
# ============================================================================
def resolve_launch(headless: bool = True, args=None, *, stealth: bool = True,
                   allow_env: str = "OFFSCREEN_ALLOW_HEADFUL"):
    """Return (headless, args) with the no-foreground-steal policy applied.

    Pure + sync, so it works for both sync and async call sites:

        hl, a = resolve_launch(headless=False, args=[...])
        browser = await pw.chromium.launch(headless=hl, args=a)

    Policy:
      * default headless;
      * a caller asking for headful FAIL-CLOSES to headless unless the owner
        explicitly opts in (OFFSCREEN_ALLOW_HEADFUL=1) AND is not at the
        keyboard right now;
      * any headful that survives is forced fully off-screen + non-occluded.
    """
    import os
    out = list(args or [])

    def _add(*flags):
        for f in flags:
            if f not in out:
                out.append(f)

    if stealth:
        _add("--disable-blink-features=AutomationControlled")

    if not headless:
        allow = os.environ.get(allow_env, "0").strip().lower() in {"1", "true", "yes", "on"}
        if not allow:
            headless = True  # autonomous headful is never permitted
        else:
            try:
                from core.user_active import is_active
                if is_active():
                    headless = True  # human is at the keyboard: don't fight them
            except Exception:
                pass

    if not headless:
        _add("--window-position=-32000,-32000", "--window-size=10,10",
             "--disable-features=CalculateNativeWinOcclusion")
    return headless, out


def safe_launch(p, *, headless: bool = True, args=None, stealth: bool = True, **kw):
    """The ONLY sanctioned SYNC Playwright chromium launcher for autonomous
    code. Applies resolve_launch() then launches. Returns a Browser."""
    headless, args = resolve_launch(headless, args, stealth=stealth)
    return p.chromium.launch(headless=headless, args=args, **kw)


def require_interactive_headful(what: str = "this login"):
    """Gate for the interactive human-login capture tools: they legitimately
    open a real, focusable window — but ONLY when a human is actually driving.
    Refuse if we're not attached to a TTY and HEADFUL_LOGIN isn't set, so a
    daemon / launchd job can never pop one of these windows into the owner's
    face. Raises SystemExit when the gate is closed."""
    import os
    import sys
    if os.environ.get("HEADFUL_LOGIN", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            return
    except Exception:
        pass
    raise SystemExit(
        f"refusing to open a headful window for {what}: no TTY and HEADFUL_LOGIN unset. "
        "Run this by hand (set HEADFUL_LOGIN=1 if launching from a GUI)."
    )
