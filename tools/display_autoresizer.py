#!/usr/bin/env python3
"""Dynamic display auto-resizer for a HEADLESS Mac.

The only display that matters is whatever device is remoting in. This detects the
active remote viewer (via Tailscale active peer + established VNC session), maps it
to a target resolution, and sets the Mac's virtual display to the nearest valid
mode via displayplacer. Debounced: only resizes when the viewing DEVICE changes,
so there's no flicker churn.

  display_autoresizer.py once     detect + resize if the device changed
  display_autoresizer.py --dry    detect + show what it WOULD do (no change)
  display_autoresizer.py status   show current display + detected viewer
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

STATE = Path.home() / "claude-stack/data/runtime/display_autoresizer.json"
DP = "/opt/homebrew/bin/displayplacer"
VERIFY_INTERVAL = float(os.environ.get("DISPLAY_VERIFY_INTERVAL", "15"))

# Each viewer profile: (hostname regex, (w, h, want_hidpi, degree), priority). The
# script picks the nearest available display mode matching that aspect/size, then
# rotates the framebuffer by `degree` (0/90/180/270). PRIORITY breaks ties when several
# devices view at once — lower wins, so a real work device (iPad/MacBook, 0) always
# beats a phone (1): a phone never hijacks the display from a laptop/tablet, but it DOES
# drive a resize when it's the only viewer connected. Anything not listed = untouched.
# NOTE (2026-06-23): this module is DETECTION-ONLY. The (w,h,...) target tuples below are vestigial
# -- only the device REGEXES are used now, by active_viewers(), to name who's viewing. Actual display
# shaping is done by remote_link_adapter.py (dummy-direct displayplacer; BetterDisplay removed).
# PRIORITY: lower wins. iPad=0 ALWAYS beats the work MacBook (redacted002, priority 2), which
# holds a permanent background screen-share socket and must only drive when it's the SOLE viewer.
DEVICE_TARGETS = [
    (re.compile(r"ipad", re.I),                                       (1344, 1008, True, 0),  0),  # iPad Pro 13" M4 (4:3): 1344x1008 is the nearest VALID native dummy HiDPI mode (13"/12.9" are both 4:3), always wins
    (re.compile(r"iphone|ipod|android|pixel|galaxy|sm-|phone", re.I), (800,  600,  True, 90), 1),  # phone: 800x600 virtual rotated 90 -> 600x800 portrait
    (re.compile(r"macbook|mac-?book|mbp|mba|book-?air|redacted|uscp\d", re.I), (1680, 1050, True, 0),  2),  # MacBook 16:10 (built-in shape); work redacted002 only when sole viewer
]
DEFAULT_TARGET = (1344, 1008, True, 0)  # nobody / unknown viewer -> iPad shape (Operator's device: iPad Pro 13" M4, 4:3)
RESTORE_GRACE_SECONDS = float(os.environ.get("DISPLAY_RESTORE_GRACE_SECONDS", "2"))
HANDOFF_COOLDOWN_SECONDS = float(os.environ.get("DISPLAY_HANDOFF_COOLDOWN_SECONDS", "20"))
CLIENT_REFIT_COOLDOWN_SECONDS = float(os.environ.get("DISPLAY_CLIENT_REFIT_COOLDOWN_SECONDS", "12"))
READY_CHECK_INTERVAL = float(os.environ.get("DISPLAY_READY_CHECK_INTERVAL", "60"))


# netstat lives ONLY in /usr/sbin, which the launchd PATH omits — a bare "netstat" silently fails
# under the resizer loop, blinding _vnc_peer_ips()/active_viewers() so the shaper falls back to the
# stale last viewer (the "stuck on MacBook while on the phone" flap, 2026-07-02). Resolve it absolute.
NETSTAT = next((p for p in ("/usr/sbin/netstat", "/sbin/netstat") if os.path.exists(p)), "netstat")


def _run(cmd, timeout=8):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def screen_sharing_listener_ok() -> bool:
    code, out, _ = _run([NETSTAT, "-an"], timeout=8)
    if code != 0:
        return False
    for line in out.splitlines():
        if "LISTEN" in line and (".5900 " in line or ".5900\t" in line):
            return True
    return False


def ensure_screen_sharing_ready() -> str | None:
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    if time.time() - float(state.get("ready_checked_at") or 0) < READY_CHECK_INTERVAL:
        return None
    state["ready_checked_at"] = time.time()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    if screen_sharing_listener_ok():
        return None
    _run(["launchctl", "kickstart", "-k", "system/com.apple.screensharing"], timeout=5)
    _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.apple.screensharing.agent"], timeout=5)
    return "self-heal: attempted to restart Screen Sharing listener."


def _tailscale_json():
    # 2026-06-19: migrated to a system `tailscaled` daemon, so the Homebrew CLI talks to it
    # directly (no GUI session / `launchctl asuser` needed). Keep the legacy App Store path
    # last in case it's ever restored.
    import os
    uid = str(os.getuid())
    sys_ts = "/opt/homebrew/bin/tailscale"
    app_ts = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    attempts = [
        [sys_ts, "status", "--json"],                              # system tailscaled (current)
        ["tailscale", "status", "--json"],                          # PATH fallback
        ["launchctl", "asuser", uid, app_ts, "status", "--json"],   # legacy App Store GUI client
        [app_ts, "status", "--json"],
    ]
    errs = []
    for cmd in attempts:
        code, out, err = _run(cmd)
        if code == 0 and out.strip().startswith("{"):
            try:
                return json.loads(out)
            except Exception as e:
                errs.append(f"{cmd[0]}:json {e}")
        else:
            errs.append(f"{cmd[1] if cmd[0]=='launchctl' else cmd[0].split('/')[-1]}: rc={code} {(err or out).strip()[:80]}")
    if "--debug" in sys.argv:
        sys.stderr.write("[tailscale] " + " | ".join(errs) + "\n")
    return {}


SCREEN_SHARE_PORTS = ("5900", "5901", "3283")


def _vnc_peer_ips() -> list[str]:
    """Remote IPs with a LIVE screen-share connection to this Mac (port 5900/5901).
    netstat needs no GUI or daemon, so this is reliable under launchd. This socket IS
    the proof that someone is actively viewing — independent of Tailscale."""
    code, out, _ = _run([NETSTAT, "-an"], timeout=8)
    ips = []
    for line in out.splitlines():
        if "ESTABLISHED" not in line:
            continue
        parts = line.split()
        if len(parts) < 5 or not parts[0].startswith("tcp"):
            continue
        local, remote = parts[3], parts[4]
        # netstat appends the port as ".<port>" on both IPv4 and IPv6 endpoints
        if local.rsplit(".", 1)[-1] not in SCREEN_SHARE_PORTS:
            continue
        rip = remote.rsplit(".", 1)[0]
        if rip and not rip.startswith("127.") and rip != "::1":
            ips.append(rip)
    return ips


def _viewer_ip_map() -> dict[str, str]:
    peers = _vnc_peer_ips()
    if not peers:
        return {}
    ts_map = None
    out = {}
    for ip in peers:
        nm = _name_for_ip(ip)
        if not nm:
            if ts_map is None:
                ts_map = _ts_ip_to_name()
            nm = ts_map.get(ip)
        if nm and any(rx.search(nm) for rx, _, _ in DEVICE_TARGETS):
            out[nm] = ip
    return out


def _ts_ip_to_name() -> dict:
    """Map every Tailscale IP (v4 + v6) -> short device name. Best-effort: when a peer
    is actively connected over Tailscale the daemon is up, so this resolves; we only
    ask once a socket already proved a viewer exists."""
    d = _tailscale_json()
    m = {}
    for p in list((d.get("Peer") or {}).values()) + [d.get("Self") or {}]:
        name = (p.get("DNSName") or p.get("HostName") or "").split(".")[0]
        for ip in (p.get("TailscaleIPs") or []):
            if name:
                m[ip] = name
    return m


def _name_for_ip(ip: str) -> str | None:
    """Tailscale IP -> short device name via MagicDNS reverse lookup. A plain DNS query
    works under launchd; the App Store Tailscale CLI does NOT (it's a GUI shim that
    can't start headless). Ask the Tailscale resolver directly, then the default one."""
    for server in (["@10.0.0.1"], []):
        code, out, _ = _run(["dig", "+short", "+time=2", "+tries=1", *server, "-x", ip], timeout=5)
        first = (out.strip().splitlines() or [""])[0].rstrip(".")
        if first and not first[0].isdigit():   # a hostname, not an IP/empty
            return first.split(".")[0]
    return None


def active_viewers() -> list[str]:
    """The iPad, MacBook or phone currently viewing this Mac — the only devices we resize for.
    A live screen-share socket is the proof. Over TAILSCALE we name the peer via MagicDNS and
    match it to an exact device profile. Over SCREENS CONNECT (Edovia relay) the peer is a
    public relay IP we can't name, so we fall back to the configured default device
    (DISPLAY_FALLBACK_VIEWER, the iPad). Both remote paths drive a resize; LAN peers don't."""
    peers = _vnc_peer_ips()
    if not peers:
        return []
    ts_map = None
    def name_of(ip):
        nonlocal ts_map
        nm = _name_for_ip(ip)
        if nm:
            return nm
        if ts_map is None:                 # fallback: CLI map (works from a shell)
            ts_map = _ts_ip_to_name()
        return ts_map.get(ip)

    # A listed device (iPad / MacBook / phone) named via Tailscale drives an exact resize.
    candidates = set()
    unknown_remote = False
    for ip in peers:
        nm = name_of(ip)
        if nm and any(rx.search(nm) for rx, _, _ in DEVICE_TARGETS):
            candidates.add(nm)
            continue
        # Unnamed peer: if it's a PUBLIC ip (not LAN 192.168.x, not Tailscale 100.64/10), the
        # viewer came in over Screens Connect's relay — remember it for the fallback below.
        try:
            a = ipaddress.ip_address(ip)
            if a.is_global and not a.is_private:
                unknown_remote = True
        except ValueError:
            pass
    if not candidates and unknown_remote:
        # Live Screens-Connect session we can't name -> assume the default device (the iPad) so
        # the display still resizes. LAN / unknown-private peers stay untouched (no fallback).
        candidates.add(os.environ.get("DISPLAY_FALLBACK_VIEWER", "ipad-screensconnect"))
    return sorted(candidates)


def active_viewer() -> str | None:
    candidates = active_viewers()
    if not candidates:
        return None
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    last = state.get("viewer")
    handoff_to = state.get("handoff_to")
    if handoff_to in candidates and time.time() - float(state.get("handoff_at") or 0) < HANDOFF_COOLDOWN_SECONDS:
        return handoff_to
    previous = set(state.get("active_viewers") or [])
    new = sorted(set(candidates) - previous, key=_priority)
    if new:
        return new[0]
    best = min(_priority(nm) for nm in candidates)
    tier = sorted({nm for nm in candidates if _priority(nm) == best})
    return last if last in tier else tier[0]


REQUEST_TTL_SECONDS = float(os.environ.get("DISPLAY_REQUEST_TTL", "25"))
_MACBOOK_RX = re.compile(r"macbook|mac-?book|mbp|mba|book-?air|redacted|uscp\d", re.I)
_CUR_REQSIG = None  # signature of the per-monitor request honored this cycle (persisted to state)


def pending_request():
    """A fresh per-monitor target pushed by the MacBook-side helper (mb_display_helper).
    File: <runtime>/display_request.json {w,h,hidpi,degree}. Returns the tuple or None.
    Stale (> TTL) / missing / malformed = None, so the daemon falls back to the static
    device profile whenever the helper isn't actively running."""
    f = STATE.parent / "display_request.json"
    try:
        if time.time() - f.stat().st_mtime > REQUEST_TTL_SECONDS:
            return None
        d = json.loads(f.read_text())
        return (int(d["w"]), int(d["h"]), bool(d.get("hidpi", True)), int(d.get("degree", 0)))
    except Exception:
        return None


def target_for(host: str):
    # Dynamic per-monitor follow is OFF by default. Tracking where the Screens window/cursor
    # is reshaped the mini at the wrong moments -- roaming the cursor onto another monitor
    # broke the aspect ratio. The mini now holds ONE fixed shape per viewing DEVICE; nothing
    # repositions it live. Re-enable the live follow only by setting DISPLAY_DYNAMIC_FOLLOW=1.
    if os.environ.get("DISPLAY_DYNAMIC_FOLLOW") == "1" and host and _MACBOOK_RX.search(host):
        req = pending_request()
        if req:
            return req
    for rx, tgt, _ in DEVICE_TARGETS:
        if rx.search(host):
            return tgt
    return DEFAULT_TARGET


def _priority(host: str) -> int:
    for rx, _, pri in DEVICE_TARGETS:
        if rx.search(host):
            return pri
    return 99


def client_refit(owner: str, dry=False) -> str | None:
    """Disabled: this was forcing the viewer Screens 5 window back to 1920x1080."""
    return None

def display_info(target_w=None, target_h=None, want_hidpi=None):
    """Return (persistent_id, current_w, current_h, modes[list of (w,h,scaling)])."""
    code, out, _ = _run([DP, "list"], timeout=10)
    screens = []
    current = None
    for line in out.splitlines():
        if line.startswith("Persistent screen id:"):
            current = {"pid": line.split(":", 1)[1].strip(), "type": "", "cw": None, "ch": None, "modes": [], "main": False, "rot": 0}
            screens.append(current)
            continue
        if current is None:
            continue
        if line.startswith("Type:"):
            current["type"] = line.split(":", 1)[1].strip()
            continue
        rm = re.match(r"Rotation: (\d+)", line.strip())
        if rm:
            current["rot"] = int(rm.group(1))
            continue
        m = re.match(r"Resolution: (\d+)x(\d+)", line.strip())
        if m and current["cw"] is None:
            current["cw"], current["ch"] = int(m.group(1)), int(m.group(2))
            continue
        if " - main display" in line:
            current["main"] = True
            continue
        mm = re.search(r"res:(\d+)x(\d+) hz:(\d+) color_depth:\d+( scaling:on)?", line)
        if mm:
            current["modes"].append((int(mm.group(1)), int(mm.group(2)), bool(mm.group(4))))
    if not screens:
        return None, None, None, []
    # displayplacer enumerates modes in the CURRENT orientation: a rotated screen
    # lists portrait dims (e.g. 600x800). Targets/pick_mode always reason in
    # landscape, so swap rotated modes back to landscape — otherwise pick_mode can't
    # find the intended mode and the daemon re-resizes (flaps) every verify cycle.
    for s in screens:
        if s["rot"] in (90, 270):
            s["modes"] = [(h, w, sc) for (w, h, sc) in s["modes"]]
    # Mirroring needs a resolution every screen supports — offer only the
    # intersection when several screens will be mirrored together.
    if len(screens) > 1:
        common = set(screens[0]["modes"])
        for s in screens[1:]:
            common &= set(s["modes"])
        if common:
            for s in screens:
                s["modes"] = [m for m in s["modes"] if m in common]
    if target_w and target_h:
        exact = []
        for s in screens:
            for w, h, scaling in s["modes"]:
                if w == target_w and h == target_h and (want_hidpi is None or scaling == want_hidpi):
                    exact.append(s)
                    break
        preferred = next((s for s in exact if "built in" in str(s.get("type", "")).lower()), None)
        preferred = preferred or next((s for s in exact if s.get("main")), None) or (exact[0] if exact else None)
        if preferred:
            return preferred["pid"], preferred["cw"], preferred["ch"], preferred["modes"]
    preferred = next((s for s in screens if "built in" in str(s.get("type", "")).lower()), None)
    preferred = preferred or next((s for s in screens if s.get("main")), None) or screens[0]
    return preferred["pid"], preferred["cw"], preferred["ch"], preferred["modes"]


def displayplacer_command() -> list[str]:
    code, out, _ = _run([DP, "list"], timeout=10)
    if code != 0:
        return []
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("displayplacer "):
            try:
                return shlex.split(line)
            except Exception:
                return []
    return []


def _screen_arg_id(arg: str) -> str | None:
    m = re.search(r"(?:^|\s)id:([^\s]+)", arg)
    return m.group(1) if m else None


def _rewrite_screen_arg(arg: str, *, enabled: bool | None = None, w: int | None = None, h: int | None = None, scaling: bool | None = None) -> str:
    out = arg
    if w and h:
        out = re.sub(r"res:\d+x\d+", f"res:{w}x{h}", out)
    if enabled is not None:
        if re.search(r"enabled:(true|false)", out):
            out = re.sub(r"enabled:(true|false)", f"enabled:{str(enabled).lower()}", out)
        else:
            out += f" enabled:{str(enabled).lower()}"
    if scaling is not None:
        if re.search(r"scaling:(on|off)", out):
            out = re.sub(r"scaling:(on|off)", f"scaling:{'on' if scaling else 'off'}", out)
        else:
            out += f" scaling:{'on' if scaling else 'off'}"
    return out


def single_display_command(pid: str, w: int, h: int, scaling: bool, degree: int = 0) -> list[str]:
    """One logical screen for the remote viewer WITHOUT disconnecting anything:
    MIRROR all other screens onto the target. displayplacer enabled:false fully
    disconnects built-in/DisplayLink panels (software can never bring them back —
    replug/reboot only), so mirroring is the only safe 'single screen' mode.
    Restore = replay the saved unmirrored arrangement."""
    res = f"res:{w}x{h} scaling:{'on' if scaling else 'off'} origin:(0,0) degree:{degree}"
    args = displayplacer_command()
    seen, others = set(), []
    for arg in args[1:]:
        i = _screen_arg_id(arg)
        if i and i != pid and i not in seen and "+" not in i:
            seen.add(i)
            others.append(i)
    if not others:
        return [DP, f"id:{pid} {res}"]
    return [DP, f"id:{'+'.join([pid] + others)} {res}"]


def pick_mode(tw, th, want_hidpi, modes):
    """Nearest mode to target: match aspect ratio, prefer HiDPI(scaling), closest area."""
    if not modes:
        return None
    target_ar = tw / th
    def score(m):
        w, h, scaling = m
        ar = w / h
        ar_pen = abs(ar - target_ar) * 5.0           # aspect match dominates
        area_pen = abs((w * h) - (tw * th)) / 1_000_000.0
        hidpi_pen = 0.0 if scaling == want_hidpi else 0.4
        return ar_pen + area_pen + hidpi_pen
    return min(modes, key=score)


def apply(pid, w, h, scaling, degree=0):
    # Always set the LANDSCAPE mode first (degree:0). displayplacer can't switch
    # resolution and rotate in one command on this dummy — a combined rotate just
    # spins the *current* mode and reports "could not find". So: set the mode clean,
    # then rotate it in a second call. The rotate call returns a bogus non-zero exit
    # even when it works, so for degree!=0 we verify by reading the resolution back
    # (it should come back with width/height swapped).
    code, out, err = _run(single_display_command(pid, w, h, scaling), timeout=10)
    if code != 0 or not degree:
        return code, out, err
    _run(single_display_command(pid, w, h, scaling, degree=degree), timeout=10)
    _, cw, ch, _ = display_info()
    want = (h, w) if degree in (90, 270) else (w, h)
    if (cw, ch) == want:
        return 0, "", ""
    return 1, "", f"rotation to degree {degree} did not take (got {cw}x{ch}, want {want[0]}x{want[1]})"


def _screen_count(cmd) -> int:
    return sum(1 for a in (cmd or [])[1:] if _screen_arg_id(a))


def _save_state(viewer, w, h, scaling, restore_cmd=None):
    try:
        previous = json.loads(STATE.read_text())
    except Exception:
        previous = {}
    restore_cmd = restore_cmd or previous.get("restore_cmd") or displayplacer_command()
    if restore_cmd:
        restore_cmd = [DP] + list(restore_cmd[1:])
    # Baseline = best-known FULL arrangement. `displayplacer list` omits disabled
    # displays, so a capture taken after a failed restore only sees one screen;
    # never let such a capture shrink the baseline.
    baseline = previous.get("baseline_cmd") or []
    if _screen_count(restore_cmd) > _screen_count(baseline):
        baseline = restore_cmd
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "viewer": viewer,
        "w": w,
        "h": h,
        "scaling": scaling,
        "verified_at": time.time(),
        "session_active": True,
        "last_seen": time.time(),
        "restore_cmd": restore_cmd,
        "baseline_cmd": baseline,
        "active_viewers": active_viewers(),
        "req_sig": _CUR_REQSIG,
    }))


def handoff_disconnect_old_sessions(new_owner: str, dry=False) -> str | None:
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    previous_owner = state.get("viewer")
    previous_viewers = set(state.get("active_viewers") or [])
    current_viewers = set(active_viewers())
    if not previous_owner or previous_owner == new_owner:
        return None
    if new_owner not in current_viewers:
        return None
    if previous_owner not in previous_viewers and len(current_viewers) < 2:
        return None
    last_handoff = float(state.get("handoff_at") or 0)
    if state.get("handoff_to") == new_owner and time.time() - last_handoff < HANDOFF_COOLDOWN_SECONDS:
        return None
    state["handoff_to"] = new_owner
    state["handoff_at"] = time.time()
    state["viewer"] = new_owner
    state["active_viewers"] = sorted(current_viewers)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    if dry:
        return f"DRY RUN would hand off from {previous_owner} to {new_owner} and restart Screen Sharing."
    code, _, _ = _run(["killall", "screensharingd"], timeout=5)
    if code != 0:
        _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.apple.screensharing"], timeout=5)
    return f"handoff: disconnected old remote session owner={previous_owner}; new owner={new_owner} should reconnect."


def restore_display_if_needed(dry=False) -> str:
    # This mini is ALWAYS a single headless desktop: one dummy-plug display, never
    # any external monitors. There is no multi-screen arrangement to put back, and
    # snapping the resolution back on disconnect is exactly what made Screens refresh
    # on every iPad app-switch. So on disconnect we leave the display precisely where
    # the viewer left it -> the next reconnect finds the same mode and is seamless.
    # Just clear the session bookkeeping; never run displayplacer here, never mirror,
    # never restore a baseline.
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        return "idle: no remote viewer; leaving display as-is."
    if not state.get("session_active"):
        return "idle: no remote viewer; leaving display as-is."
    last_seen = float(state.get("last_seen") or 0)
    if time.time() - last_seen < RESTORE_GRACE_SECONDS:
        return "ending: remote viewer disconnected; settling before clearing session."
    if dry:
        return "DRY RUN would clear session (single-desktop mini: display left as-is)."
    state["session_active"] = False
    state["viewer"] = None
    state["restored_at"] = time.time()
    state.pop("restore_cmd", None)
    STATE.write_text(json.dumps(state))
    return "ended: viewer disconnected; single-desktop mini, display left at viewer resolution."


def maximize_remote_viewer_window():
    """Disabled: this was forcing local/remote screen-sharing windows to fixed bounds."""
    return None

def decide_and_apply(dry=False, force=False) -> str:
    """One detect->resize cycle, returning a short status string. Cheap on the common
    paths: no viewer = just a netstat; a steady same-device session = netstat + name
    lookup only. The heavy displayplacer mode-enumeration runs ONLY when the viewing
    device changes, so the watch loop can poll fast without real cost."""
    global _CUR_REQSIG
    _req = pending_request()
    _CUR_REQSIG = repr(_req) if _req else None
    ready_msg = ensure_screen_sharing_ready()
    viewer = active_viewer()
    if not viewer:
        return ready_msg or restore_display_if_needed(dry=dry)
    try:
        last = json.loads(STATE.read_text())
    except Exception:
        last = {}
    if last.get("viewer") == viewer and not force:
        last["last_seen"] = time.time()
        last["session_active"] = True
        last["active_viewers"] = active_viewers()
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(last))
        age = time.time() - float(last.get("verified_at") or 0)
        if age < VERIFY_INTERVAL and _CUR_REQSIG == last.get("req_sig"):
            refit = client_refit(viewer, dry=dry)
            if refit:
                return refit
            return f"steady: viewer={viewer} (no change)"
        tw, th, hi, deg = target_for(viewer)
        pid, cw, ch, modes = display_info(tw, th, hi)
        if not pid:
            return "error: could not read display via displayplacer."
        m = pick_mode(tw, th, hi, modes)
        if not m:
            return "error: no suitable mode found."
        w, h, scaling = m
        dw, dh = (h, w) if deg in (90, 270) else (w, h)  # on-screen dims after rotation
        if (cw, ch) == (dw, dh):
            handoff = handoff_disconnect_old_sessions(viewer, dry=dry)
            if handoff:
                return handoff
            restore_cmd = last.get("restore_cmd") or displayplacer_command()
            # Mirror only when the CURRENT arrangement is still multi-screen;
            # an already-mirrored set reports one logical screen and is left alone.
            if _screen_count(displayplacer_command()) > 1:
                if dry:
                    return f"DRY RUN would mirror other screens: viewer={viewer} mode {w}x{h} scaling={scaling} degree={deg}"
                code, _, err = apply(pid, w, h, scaling, degree=deg)
                if code != 0:
                    return f"resize FAILED: {err.strip()[:200]}"
                _save_state(viewer, w, h, scaling, restore_cmd=restore_cmd)
                refit = client_refit(viewer, dry=dry)
                if refit:
                    return refit
                return f"mirrored other screens onto main: viewer={viewer} mode {w}x{h} scaling={scaling} degree={deg}"
            _save_state(viewer, w, h, scaling)
            refit = client_refit(viewer, dry=dry)
            if refit:
                return refit
            return f"verified: viewer={viewer} mode {w}x{h} scaling={scaling} degree={deg}"
        if dry:
            return f"DRY RUN would repair drift: viewer={viewer} target={tw}x{th} -> mode {w}x{h} scaling={scaling} degree={deg} (current {cw}x{ch})"
        restore_cmd = last.get("restore_cmd") or displayplacer_command()
        code, _, err = apply(pid, w, h, scaling, degree=deg)
        if code != 0:
            return f"resize FAILED: {err.strip()[:200]}"
        _save_state(viewer, w, h, scaling, restore_cmd=restore_cmd)
        refit = client_refit(viewer, dry=dry)
        if refit:
            return refit
        return f"repaired drift: viewer={viewer} target={tw}x{th} -> mode {w}x{h} scaling={scaling} (was {cw}x{ch})"

    tw, th, hi, deg = target_for(viewer)
    pid, cw, ch, modes = display_info(tw, th, hi)
    if not pid:
        return "error: could not read display via displayplacer."
    m = pick_mode(tw, th, hi, modes)
    if not m:
        return "error: no suitable mode found."
    w, h, scaling = m
    dw, dh = (h, w) if deg in (90, 270) else (w, h)  # on-screen dims after rotation
    line = f"viewer={viewer} target={tw}x{th} → mode {w}x{h} scaling={scaling} degree={deg} (current {cw}x{ch})"
    if (cw, ch) == (dw, dh):
        handoff = handoff_disconnect_old_sessions(viewer, dry=dry)
        if handoff:
            return handoff
        restore_cmd = last.get("restore_cmd") or displayplacer_command()
        if _screen_count(displayplacer_command()) > 1:
            if dry:
                return f"DRY RUN would mirror other screens: {line}"
            maximize_remote_viewer_window()
            code, _, err = apply(pid, w, h, scaling, degree=deg)
            if code != 0:
                return f"resize FAILED: {err.strip()[:200]}"
            _save_state(viewer, w, h, scaling, restore_cmd=restore_cmd)
            refit = client_refit(viewer, dry=dry)
            if refit:
                return refit
            return f"mirrored other screens onto main: {line}"
        _save_state(viewer, w, h, scaling)
        refit = client_refit(viewer, dry=dry)
        if refit:
            return refit
        return f"already correct: {line}"
    if dry:
        return f"DRY RUN would set: {line}"
    maximize_remote_viewer_window()
    restore_cmd = last.get("restore_cmd") or displayplacer_command()
    code, _, err = apply(pid, w, h, scaling, degree=deg)
    if code != 0:
        return f"resize FAILED: {err.strip()[:200]}"
    _save_state(viewer, w, h, scaling, restore_cmd=restore_cmd)
    handoff = handoff_disconnect_old_sessions(viewer, dry=dry)
    if handoff:
        return handoff
    refit = client_refit(viewer, dry=dry)
    return refit or f"resized: {line}"


def main():
    # DETECTION-ONLY. This module exposes active_viewers(), imported by remote_link_adapter.py
    # (the single display owner, com.claude-stack.ipad-display-lock). The BetterDisplay virtual-screen
    # driver was removed 2026-06-23 (BetterDisplay uninstalled); the legacy displayplacer resizer above
    # is retained only for active_viewer()/_priority(). No display is ever shaped from here.
    cmd = next((a for a in sys.argv[1:] if not a.startswith("-")), "viewers")
    if cmd == "request":
        # back-compat no-op: the dummy-direct adapter ignores display_request.json.
        print("request ignored (display owner: remote_link_adapter.py)")
        return
    print(json.dumps(active_viewers()))


if __name__ == "__main__":
    main()
