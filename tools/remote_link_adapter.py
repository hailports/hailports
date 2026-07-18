#!/usr/bin/env python3
"""Viewer-aware + connection-aware display shaper for the headless mini.

Supersedes the always-IPAD `keep_ipad_locked.sh`. Two simple, independent decisions per tick:

  WHICH DEVICE is actively viewing -> picks the shape by ASPECT, native dummy modes, degree 0 -- EXCEPT
                                      the iPhone, which rotates 90deg to PORTRAIT to fill a vertically-
                                      held phone: iPad 1344x1008 (4:3 HiDPI) / MacBook 1440x900 (16:10,
                                      no-notch) / iPhone 540x960 (portrait, BIG crisp UI, low-res HiDPI).
  WHICH SCALING that device uses    -> per-device: iPad + iPhone HiDPI (crisp; the iPhone at LOW res so
                                      the on-phone UI is big), MacBook + relay LoDPI (light, constrained).

WHY fixed, not measured: each device's link is STABLE, so live per-tick RTT/DERP measurement +
hysteresis was pure fragility -- it generated a long tail of false-rebuild bugs (DERP warmup, retx
misparse, severe-debounce races, idle ping-pong). A deterministic per-device map gives the identical
visible result with nothing to flap. Flip any device via env (RLA_IPAD_HIDPI / RLA_IPHONE_HIDPI /
RLA_MAC_HIDPI) if its situation changes.

DEVICE DETECTION is by which SOCKET is streaming frames (per-IP byte-rate): the live device wins by
strict priority (iPad>iPhone>Mac). The work MacBook's permanent idle socket reads ~0 bytes so it never
grabs the shape unless actually viewed. A static screen (no device streaming) HOLDS the last device;
a lone connected device wins even when static; `echo <device> > ~/.remote_view_device` is a manual
override for the ambiguous multi-static-socket case; an absence HOLDS the last-used shape (the default
is "whatever the last remote session used", never a forced snap-back).

SAFETY: never touches the firewall, never touches port 5900, never blocks remote viewing. Exactly one
virtual desktop; on any build failure it drops to a guaranteed dummy LoDPI mode (never blank); all
BetterDisplay rebuilds serialize through one cross-process lock.

  remote_link_adapter.py once      one decide+enforce tick (the launchd entrypoint)
  remote_link_adapter.py --dry     decide + print what it WOULD do, enforce nothing
  remote_link_adapter.py status    show viewers, selection, link quality, scaling state
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import display_autoresizer as dar          # reliable socket + MagicDNS viewer detection (IPC-free)

STATE = Path.home() / "claude-stack/data/runtime/remote_link_adapter.json"
DP = "/opt/homebrew/bin/displayplacer"
TS = "/opt/homebrew/bin/tailscale"

# DUMMY-DIRECT design: the mini's ONLY screen is a physical dummy HDMI plug. We shape it DIRECTLY
# with displayplacer -- no BetterDisplay, no virtual screens, no betterdisplaycli. The dummy
# advertises a full HiDPI ("scaling:on") mode set, so per-device rendering stays crisp. This removes
# the betterdisplaycli-wedge + virtual-screen-accretion failure class that broke the resizer ~6x:
# when BetterDisplay's CLI responder dies (it does, silently, even with the app running), every
# create/discard times out and orphan desktops pile up. displayplacer has no IPC/host-app dependency,
# so it cannot wedge -- it talks straight to WindowServer.
DUMMY = "424D061B-2C5D-41F5-9FBB-ADD34E533603"

# logical per-device selection tags (w, h, degree); the concrete pixel mode is chosen in _dummy_mode()
# from the dummy's REAL advertised modes. Tags match their target modes for readability.
IPAD, IPHONE, MACBOOK = (1024, 768, 0), (960, 540, 90), (1440, 900, 0)   # iPhone degree 90 = portrait

IPAD_RX = re.compile(r"ipad", re.I)
IPHONE_RX = re.compile(r"iphone|ipod|phone", re.I)
MAC_RX = re.compile(r"macbook|mbp|mba|book|redacted|uscp\d", re.I)
# The work MacBook (redacted002) keeps a PERMANENT background screen-share socket to the mini, so a
# bare socket from it is NOT proof of viewing. It must drive the MacBook shape ONLY when it's the
# SOLE viewer AND its session is actually streaming (= Operator is really on it), never just because the
# idle socket exists -- that idle-socket-grabs-the-shape flip is the exact churn Operator was furious at.
# We hand dar.active_viewers() this sentinel as DISPLAY_FALLBACK_VIEWER. dar emits it ONLY when it
# sees an unnameable GLOBAL/public :5900 peer = a genuine Edovia relay session. That lets us tell a
# real relay (-> LoDPI) apart from a transient Tailscale name-resolution blip (a still-connected
# private socket whose name didn't resolve this tick -> active_viewers()==[], which we FREEZE on).
RELAY_SENTINEL = "__relay__"


def _envf(k, d):
    try: return float(os.environ.get(k, d))
    except Exception: return d


# --- activity gate (separate a live session from an idle/paused socket) ------------------------
ACTIVE_KBPS_FLOOR = _envf("RLA_ACTIVE_KBPS", 1.0)   # screen-share rate above this = someone viewing
ABSENT_TICKS = int(_envf("RLA_ABSENT_TICKS", 3))    # idle ticks before we stop holding the last viewer
# GHOST-SOCKET STALENESS: a killed/backgrounded viewer (e.g. iPhone Screens) leaves its :5900 TCP
# socket half-open ESTABLISHED on the mini for MINUTES, so dar.active_viewers() (netstat-only) keeps
# listing it. A viewer whose frames stay flat below the floor CONTINUOUSLY for longer than this is
# treated as dead -> it can no longer be picked as the sole/most-recent viewer (so it can't freeze the
# shape on a phantom device). Set 0 to disable the gate entirely (reverts to pre-2026-07-02 behavior).
STALE_SOCKET_S = _envf("RLA_STALE_SOCKET_S", 90)
# --- FIXED per-device scaling (HiDPI=crisp, LoDPI=1/4 the pixels = light on a constrained link) -----
# Each device's link is STABLE, so scaling is a deterministic per-device choice -- no live RTT/DERP
# measurement, no hysteresis, nothing to flap. iPad + iPhone render crisp HiDPI (the iPhone at a LOW
# res -- 960x540 -- so the on-phone UI is BIG); the work MacBook (~693ms work net) + a relay session
# ride LoDPI (constrained). Flip any of these via env if a device's situation changes.
HIDPI_IPAD = _envf("RLA_IPAD_HIDPI", 0) >= 1
HIDPI_IPHONE = _envf("RLA_IPHONE_HIDPI", 1) >= 1
HIDPI_MAC = _envf("RLA_MAC_HIDPI", 0) >= 1


def scaling_for(viewer: str, relay: bool) -> bool:
    """Return hidpi (True=HiDPI/crisp, False=LoDPI/light) for the selected device. Fixed, not measured."""
    if relay:
        return False                       # relay/Edovia path is always bandwidth-constrained
    if IPHONE_RX.search(viewer):
        return HIDPI_IPHONE
    if MAC_RX.search(viewer):
        return HIDPI_MAC
    if IPAD_RX.search(viewer):
        return HIDPI_IPAD
    return HIDPI_IPAD                      # ipad-default resting state


def _run(cmd, t=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=t)
    except Exception:
        return None


def _load():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")          # atomic: a mid-write kill can't corrupt the live state
    tmp.write_text(json.dumps(d))
    os.replace(tmp, STATE)


# ---------------------------------------------------------------------------------------------
# Tailscale + screen-share measurement
# ---------------------------------------------------------------------------------------------
def _ts_name_to_ip() -> dict:
    r = _run([TS, "status", "--json"], t=8)
    if not (r and r.stdout.strip().startswith("{")):
        return {}
    try:
        d = json.loads(r.stdout)
    except Exception:
        return {}
    m = {}
    for p in list((d.get("Peer") or {}).values()) + [d.get("Self") or {}]:
        name = (p.get("DNSName") or p.get("HostName") or "").split(".")[0].lower()
        v4 = next((ip for ip in (p.get("TailscaleIPs") or []) if ":" not in ip), None)
        if name and v4:
            m[name] = v4
    return m


def _peer_bytes() -> dict:
    """{remote_ip: cumulative bytes_out} for every ESTABLISHED :5900 screen-share socket. PER-SOCKET
    attribution is the robust fix for the work-MacBook flip: its permanent socket reads ~0 bytes when
    it isn't actually being viewed, even while a RELAY viewer streams on a different socket -- so we
    can tell whose stream is live instead of misattributing the aggregate."""
    r = _run(["nettop", "-m", "tcp", "-l", "1", "-n", "-x"], t=8)
    out = {}
    if not r:
        return out
    for ln in r.stdout.splitlines():
        if "5900<->" not in ln or "Established" not in ln:
            continue
        toks = ln.split()
        addr = next((t for t in toks if "5900<->" in t), None)
        if not addr:
            continue
        try:
            remote = addr.split("<->")[1]
            rip = remote.rsplit(":", 1)[0] if ":" in remote else remote.rsplit(".", 1)[0]
            bo = int(toks[toks.index("Established") + 2])
            if rip and rip != "*":
                out[rip] = out.get(rip, 0) + bo
        except (ValueError, IndexError):
            continue
    return out


def _has_public_peer(peer_kbps: dict) -> bool:
    """Any screen-share peer on a GLOBAL/public IP = a genuine Edovia relay viewer. Detected directly
    from the socket IPs so the always-connected work-MacBook (a private tailscale peer) can't mask it."""
    for ip in peer_kbps:
        try:
            a = ipaddress.ip_address(ip)
            if a.is_global and not a.is_private:
                return True
        except ValueError:
            pass
    return False


def _session_active(kbps: float) -> bool:
    """Is the screen actually being viewed RIGHT NOW -- over tailscale OR the Screens Connect relay
    (which hides the peer)? ScreensharingAgent runs in the console session only while the screen is
    being OBSERVED; it disappears the instant the session ends (verified: present while a viewer is
    connected, gone otherwise). That plus a live byte-rate is the routing-independent signal.
    NB: we deliberately do NOT count a bare ESTABLISHED :5900 socket -- the work MacBook holds one
    24/7, which would pin session=True forever and defeat the freeze/idle path."""
    if kbps >= ACTIVE_KBPS_FLOOR:
        return True
    pg = _run(["pgrep", "-f", "ScreensharingAgent"], t=5)
    return bool(pg and pg.stdout.strip())


HINT_FILE = Path.home() / ".remote_view_device"   # optional manual pin for relay sessions
HINT_TTL_S = _envf("RLA_HINT_TTL_S", 7200)


def _device_hint() -> str | None:
    """A fresh manual pin of the viewing device, for when the relay hides the real peer. Write e.g.
    `echo iphone > ~/.remote_view_device`. Older than HINT_TTL_S is ignored."""
    try:
        if time.time() - HINT_FILE.stat().st_mtime > HINT_TTL_S:
            return None
        v = (HINT_FILE.read_text() or "").strip().lower()
        return v or None
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# Viewer selection (activity-gated, strict priority, freeze-on-idle)
# ---------------------------------------------------------------------------------------------
def _rank(nm: str) -> int:
    if IPAD_RX.search(nm): return 0
    if IPHONE_RX.search(nm): return 1
    if MAC_RX.search(nm): return 2
    return 3


def _profile(nm: str):
    if IPHONE_RX.search(nm): return IPHONE    # iPhone: own 16:9 landscape shape -- fills a sideways phone
    if IPAD_RX.search(nm): return IPAD
    if MAC_RX.search(nm): return MACBOOK
    return IPAD


def _hint_is(hint: str, viewer: str) -> bool:
    """True if a manual hint (a device TAG 'iphone'/'macbook'/'ipad', or an exact peer name) refers to
    this connected viewer -- lets a tag-hint match the concrete peer name (e.g. macbook->redacted002)."""
    h, v = str(hint or "").strip().lower(), str(viewer).lower()
    if not h:
        return False
    if h == v:
        return True
    return bool((IPHONE_RX.search(h) and IPHONE_RX.search(v))
                or (IPAD_RX.search(h) and IPAD_RX.search(v))
                or (MAC_RX.search(h) and MAC_RX.search(v)))


BLIP_HOLD_S = _envf("RLA_BLIP_HOLD_S", 6)   # hold the active viewer through a brief name-resolution blip
# iPhone-only auto-select (Operator 2026-07-14): over the userspace-networking split-tunnel EVERY tailnet
# peer is relayed through 127.0.0.1, so the :5900 socket can't name the viewer AND per-peer byte/
# handshake signals all read 0 -- there's no signal to tell iPhone from iPad. Both iOS devices sit
# PERMANENTLY Online (idle background Tailscale apps), so the old "2+ online -> hold current" guard
# never switched. Operator views on iPhone only now, so the idle iPad is excluded from presence auto-
# select (exactly like the always-on MacBook already is) -> iPhone is the sole candidate -> it auto-
# switches on connect. Re-enable the iPad as an auto-viewer with RLA_IPAD_PRESENCE=1. The manual pin
# (`echo ipad > ~/.remote_view_device`) still selects the iPad regardless of this flag.
# DEFAULT FLIP (Operator 2026-07-15): Operator now views on iPAD, so iPad is the auto-select default and the
# idle iPhone is excluded from presence auto-select (mirrors the old iPhone-only rule, inverted). Re-
# enable the iPhone as an auto-viewer with RLA_IPHONE_PRESENCE=1; force the iPad off with RLA_IPAD_PRESENCE=0.
IPAD_PRESENCE = _envf("RLA_IPAD_PRESENCE", 1) >= 1
IPHONE_PRESENCE = _envf("RLA_IPHONE_PRESENCE", 0) >= 1


def _ts_online_viewers() -> list:
    """Online tailscale VIEWING devices as (rank, name) -- CONNECTION-INDEPENDENT device identity.
    Used when the :5900 socket is a loopback userspace-proxy (tailscale relay over the VPN split-
    tunnel) so the peer IP is 127.0.0.1 and the socket can't name the device. Presence, NOT byte-rate:
    the split-tunnel relays every peer through DERP so per-peer byte counters read 0. The MacBook is
    excluded (it views over its OWN named :5900 socket + is online 24/7); the idle iPad is likewise
    excluded unless RLA_IPAD_PRESENCE=1 (see note above) so the always-online iPad can't block the
    iPhone auto-switch."""
    r = _run([TS, "status", "--json"], t=8)
    if not (r and r.stdout.strip().startswith("{")):
        return []
    try:
        d = json.loads(r.stdout)
    except Exception:
        return []
    out = []
    for p in (d.get("Peer") or {}).values():
        if not p.get("Online"):
            continue
        nm = (p.get("DNSName") or p.get("HostName") or "").split(".")[0].lower()
        if IPAD_RX.search(nm):
            if IPAD_PRESENCE:
                out.append((0, nm))
        elif IPHONE_RX.search(nm) and IPHONE_PRESENCE:
            out.append((1, nm))
    return out


def select_viewer(st: dict, peer_kbps: dict, name_ip: dict, session: bool):
    """Pick the device Operator is ACTUALLY on, by RECENCY OF ACTIVITY. Returns (name|None, relay_bool).

    A viewer's frames only move (kbps) while it's being used, so we stamp each viewer's 'last active'
    time whenever it streams (and once when it first connects). Selection, in order:
      hint (connectivity-gated -> a stale hint can never trap a switch, or a relay-hidden peer)
      -> sole connected device
      -> the device STREAMING right now (Operator is driving it -> instant switch)
      -> the current viewer held through a brief name blip (no flap)
      -> the MOST-RECENTLY-ACTIVE connected viewer (the device last touched)
    This ends the old ambiguity: a lingering idle socket / the always-on background work-Mac can never
    out-rank the device in active use. No session -> None (caller freezes)."""
    if not session:
        return None, False
    now = time.time()
    raw = dar.active_viewers()
    named = [v for v in raw if v != RELAY_SENTINEL]
    relay_now = (RELAY_SENTINEL in raw) or _has_public_peer(peer_kbps)

    def streams(v):
        ip = name_ip.get(v.lower())
        return bool(ip) and peer_kbps.get(ip, 0.0) >= ACTIVE_KBPS_FLOOR

    streaming_now = [v for v in named if streams(v)]

    # recency: stamp a viewer 'active now' when its frames move, or the first tick it connects.
    la = dict(st.get("last_active", {}))
    for v in named:
        if v not in la or v in streaming_now:
            la[v] = now
    la = dict(sorted(la.items(), key=lambda kv: kv[1])[-8:])   # bounded; survives a blip w/o re-stamping
    st["last_active"] = la

    # STALENESS GATE (ghost-socket self-expire): la[v] is re-stamped on EVERY tick a viewer streams
    # (kbps>=floor) and the tick it first connects. A real-but-idle session keeps its link warm --
    # macOS screen-share pushes periodic frames even on a static screen (the log shows an "idle" phone
    # bursting 0->1700 kbps every few seconds), so la[v] is refreshed FAR inside the window and never
    # goes stale. Only a socket with NO agent feeding it (a killed/backgrounded viewer -> truly flat ~0
    # for the whole window) ages past STALE_SOCKET_S. A stale viewer is dropped from live_named below,
    # so it can't be chosen as the sole/most-recent viewer -- it can never pin the shape again.
    def stale(v):
        return STALE_SOCKET_S > 0 and (now - la.get(v, now)) > STALE_SOCKET_S

    live_named = [v for v in named if not stale(v)]

    # CONNECTION-INDEPENDENT ID (loopback userspace-proxy case): no :5900 peer can be named because
    # tailscale is proxying the tunnel through 127.0.0.1 (VPN split-tunnel). Fall back to tailscale
    # ONLINE-PRESENCE: a live session + exactly ONE online viewing-device = unambiguously that device,
    # NO pin needed. With the idle iPad excluded (RLA_IPAD_PRESENCE=0) the iPhone is that sole device,
    # so it auto-switches on connect. 2+ still-eligible iOS = genuinely ambiguous over a signal-zeroing
    # relay -> HOLD the current viewer (never flap); the pin below covers a cold start.
    # MANUAL PIN IS TOP AUTHORITY -- checked BEFORE presence/relay auto-select. Over the userspace-
    # networking split-tunnel the MacBook AND iPhone both arrive as anonymous 127.0.0.1 loopback (the
    # MacBook lost its old named-peer identity), so they can't be told apart automatically; a pinned
    # device (`echo macbook|ipad|iphone > ~/.remote_view_device`) must win over the iPhone-presence
    # default. (Was below the presence block -> the pin was silently ignored on a no-named-peer tick.)
    hint = _device_hint()
    if hint:
        match = next((v for v in named if _hint_is(hint, v)), None)
        if match:
            return match, False
        if relay_now or not named:             # relay OR a loopback-terminated tunnel (127.0.0.1:5900
            return hint, True                  # over the VPN split-tunnel) hides the peer -> trust the pin
        # else: hint names a device that isn't here and there's no relay -> ignore it (no trap)

    if not named:
        # TRANSPORT DISCRIMINATOR (no pin set). iPhone views over the tailscale loopback (127.0.0.1 --
        # PRIVATE -> relay_now False) -> pick it via presence below. The iPad comes in as a PUBLIC
        # Screens-Connect relay peer, which dar collapses to RELAY_SENTINEL under this tick's
        # DISPLAY_FALLBACK_VIEWER, so named=[] AND relay_now=True -> iPad 4:3 (ipad-default). NOTE: the
        # MacBook also arrives as 127.0.0.1 loopback (relay_now False), so it is INDISTINGUISHABLE from
        # the iPhone here and needs the pin above -- without a pin it falls through to the iPhone.
        if relay_now:
            return "ipad-default", True             # public relay session (iPad via Screens Connect) -> iPad 4:3
        ios = _ts_online_viewers()
        if len(ios) == 1:
            return ios[0][1], True                 # pure loopback, one online iOS = the iPhone
        if len(ios) >= 2:
            held = st.get("viewer")
            if held and (IPHONE_RX.search(held) or IPAD_RX.search(held)):
                return held, True

    cur = st.get("viewer")
    # hold the active viewer through a brief name-resolution blip -- BEFORE sole/streaming/recency, so a
    # 1-tick drop of the device you're on never hands the screen to a lingering other device. Skipped
    # the instant another device is actually streaming (you've really moved). Bounded by BLIP_HOLD_S.
    if (cur and cur not in named and cur in la and (now - la.get(cur, 0)) < BLIP_HOLD_S
            and not streaming_now):
        return cur, False

    if len(live_named) == 1:                   # sole LIVE device = unambiguously the viewer (a lone
        return live_named[0], False            # ghost socket is stale -> excluded -> falls through

    if streaming_now:                          # a device actively pushing frames = the one being used
        return max(streaming_now, key=lambda v: la.get(v, 0)), False

    connected = {v: la[v] for v in live_named if v in la}
    if connected:                              # nobody streaming -> the most-recently-touched LIVE device
        return max(connected, key=connected.get), False

    if cur and cur in raw and not stale(cur):  # hold the current viewer only while it's genuinely alive
        return cur, False
    if relay_now:
        return "ipad-default", True
    return None, False


# ---------------------------------------------------------------------------------------------
# Enforce
# ---------------------------------------------------------------------------------------------
def _dp_alive() -> bool:
    r = _run([DP, "list"], t=10)
    return bool(r and r.returncode == 0 and r.stdout)


def _dp_lines():
    r = _run([DP, "list"], t=10)
    return r.stdout.splitlines() if (r and r.returncode == 0 and r.stdout) else None


def _display_count() -> int:
    ls = _dp_lines()
    return -1 if ls is None else sum(1 for ln in ls if ln.startswith("Persistent screen id:"))


def _betterdisplay_running() -> bool:
    r = _run(["pgrep", "-f", "BetterDisplay.app/Contents/MacOS/BetterDisplay"], t=5)
    return bool(r and (r.stdout or "").strip())


def _kill_betterdisplay():
    # BetterDisplay has NO role in the dummy-direct design -- the only thing it can do is spawn a
    # virtual screen that violates the single-display invariant. Killing it instantly removes any
    # virtual it created, with NO betterdisplaycli/IPC call (that IPC is exactly what wedges).
    _run(["pkill", "-9", "-f", "BetterDisplay"], t=5)
    time.sleep(1)


# Concrete dummy modes, each VERIFIED present on 424D061B. (w, h, hidpi). Deterministic per device --
# nothing measured, nothing to flap. Override any via env ("1344x1008", "800x600:lodpi") if a device's
# situation changes -- no code edit needed.
def _mode_env(key, default):
    # iPad is HiDPI (crisp) again (2026-06-28): a native 1344x1008 4:3 scaling:on mode exists on the
    # dummy, so crispness needs NO BetterDisplay -> still wedge-safe. env can pick dpi via :hidpi/:lodpi;
    # with no suffix the default's dpi is kept.
    v = (os.environ.get(key) or "").strip()
    m = re.match(r"(\d+)x(\d+)(?::(hi|lo)dpi)?$", v, re.I)
    if not m:
        return default
    suf = (m.group(3) or "").lower()
    hidpi = (suf == "hi") if suf else default[2]
    return (int(m.group(1)), int(m.group(2)), hidpi)


# All NATIVE modes on 424D061B, all LoDPI (scaling:off = 1:1 framebuffer = fewest pixels to encode +
# ship = lightest on the link and the mini). Full-screen by matching the device's ASPECT ratio:
MODE_IPAD    = _mode_env("RLA_MODE_IPAD",    (1344, 1008, True))   # 4:3 HiDPI (CRISP) -- EXISTING native dummy mode, NO BetterDisplay -> wedge-safe. Serves iPad Pro 13" M4 (2752x2064, 4:3) perfectly, same as the old 12.9". For true 2752x2064 crispness you'd add a 1376x1032 mode to the /Library/Displays override (sudo+reboot) -- not worth the <2.5% gain / wedge risk.
MODE_IPHONE  = _mode_env("RLA_MODE_IPHONE",  (800, 600, True))     # ROTATED 4:3, MAX UI: 800x600 is the smallest NATIVE 4:3 HiDPI mode already on the dummy (no custom resolution needed) -> rotated 90 = 600x800 portrait (3:4), tall-square-ish workspace, biggest scaled UI. 640x480 HiDPI gets refused, so 800x600 is the floor. Wedge-safe (existing native mode, no override edit / SwitchResX). Custom slightly-tall-square instead: add 560x480 to the override + RLA_MODE_IPHONE=560x480:hidpi
MODE_MACBOOK = _mode_env("RLA_MODE_MACBOOK", (1680, 1050, True))   # 16:10 HiDPI (CRISP) -- native /Library/Displays override now advertises 1680x1050 (fb 3360x2100) + 1440x900 (fb 2880x1800) scaling:on on the dummy, so the MacBook built-in gets true Retina, no BetterDisplay. Was 1440x900 LoDPI (fuzzy). Lighter alt: RLA_MODE_MACBOOK=1440x900:hidpi
# iPhone knobs (native, via env): RLA_MODE_IPHONE=640x360:hidpi = even BIGGER UI; =1024x576:lodpi = lighter (softer); RLA_IPHONE_DEGREE=0 = landscape (no portrait rotate). iPad 800x600, MacBook 720x450.
# The iPhone shape ROTATES to portrait (degree:90) so it FILLS a vertically-held phone -- the ONE
# exception to never-rotate (iPad/MacBook stay 0). 2-step set-then-rotate in _set_dummy; verify by readback.
IPHONE_DEGREE = int(_envf("RLA_IPHONE_DEGREE", IPHONE[2]))

# Every per-device mode is guaranteed-native now, so the old "custom mode unavailable" fallback is dead.
# Kept as an (empty) safety hook only: a genuinely unsettable mode -> "hold + retry", never blank.
MODE_FALLBACK = {}


def _dummy_mode(profile):
    w, h, _ = profile
    if (w, h) == MACBOOK[:2]:
        return MODE_MACBOOK
    if (w, h) == IPHONE[:2]:
        return MODE_IPHONE                                       # iPhone: low-res HiDPI (big UI), rotated portrait
    return MODE_IPAD                                              # iPad + unknown/nobody -> crisp 4:3 HiDPI resting shape


def _mode_degree(profile) -> int:
    # iPhone rotates to portrait; every other device NEVER rotates (degree 0).
    return IPHONE_DEGREE if (profile[0], profile[1]) == IPHONE[:2] else 0


def _dummy_now():
    """Current dummy (base_w, base_h, hidpi, degree), or None. displayplacer reports a rotated panel
    with its dims SWAPPED, so we swap them back to the un-rotated 'base' mode dims."""
    ls = _dp_lines()
    if ls is None:
        return None
    for ln in ls:
        if ln.startswith("displayplacer ") and DUMMY in ln:
            mr = re.search(r"res:(\d+)x(\d+)", ln)
            if not mr:
                return None
            md = re.search(r"degree:(\d+)", ln)
            deg = int(md.group(1)) if md else 0
            rw, rh = int(mr.group(1)), int(mr.group(2))
            bw, bh = (rh, rw) if deg in (90, 270) else (rw, rh)
            return bw, bh, ("scaling:on" in ln), deg
    return None


def _dp_set(w, h, hidpi, degree):
    _run([DP, f"id:{DUMMY} res:{w}x{h} hz:60 color_depth:8 "
              f"scaling:{'on' if hidpi else 'off'} origin:(0,0) degree:{degree}"], t=12)


def _set_dummy(w, h, hidpi=False, degree=0):
    # NEVER change resolution AND rotation in ONE displayplacer command -- this dummy can't ("a combined
    # change spins the current mode and can't find the res"), which is why the iPhone rotate is split.
    # The REVERSE (un-rotating back to a landscape device) has the same limit, so we decompose EVERY
    # reshape into proven single-axis steps -- the iPhone<->landscape handoff then can't get stuck rotated:
    #   1) if the panel is ROTATED now, un-rotate it IN PLACE at its current mode (rotation-only)
    #   2) set the TARGET mode at degree:0 (both sides deg0 => a pure resize; a scaling change is fine)
    #   3) if portrait is wanted, rotate the TARGET mode in place (rotation-only; bogus rc, verified by
    #      _dummy_ok readback). All modes native -> no BetterDisplay (no wedge).
    cur = _dummy_now()
    if cur and cur[3] in (90, 270):
        _dp_set(cur[0], cur[1], cur[2], 0)
    _dp_set(w, h, hidpi, 0)
    if degree:
        _dp_set(w, h, hidpi, degree)


def _dummy_ok(w, h, hidpi=False, degree=0) -> bool:
    ls = _dp_lines()
    if ls is None:
        return False
    # displayplacer reports a rotated display with dims SWAPPED (960x540 @ deg90 -> res:540x960).
    rw, rh = (h, w) if degree in (90, 270) else (w, h)
    want = (f"res:{rw}x{rh}", f"scaling:{'on' if hidpi else 'off'}", f"degree:{degree}")
    for ln in ls:
        if ln.startswith("displayplacer ") and DUMMY in ln:
            return all(tok in ln for tok in want)
    return False


def enforce(profile, hidpi: bool, dry: bool, st: dict, force: bool = False) -> str:
    # hidpi arg (from scaling_for) is intentionally ignored: each device's dummy mode already encodes
    # the right scaling, and not every dummy res has both variants -- a fixed mode is what "locked
    # down" means here. Degree is 0 for every device EXCEPT the iPhone, which rotates to portrait.
    w, h, sc = _dummy_mode(profile)
    deg = _mode_degree(profile)
    ow, oh = (h, w) if deg in (90, 270) else (w, h)          # on-screen dims after any rotation
    tag = f"{ow}x{oh} deg{deg} {'HiDPI' if sc else 'LoDPI'} dummy-direct"
    # HARD INVARIANT: exactly ONE display. BetterDisplay is the ONLY thing that can add a second, so
    # if it's running OR we ever see >1 display, kill it (instant, no IPC). This bulletproof hammer
    # replaces the old purge/collapse dance that depended on the (wedging) betterdisplaycli.
    if _betterdisplay_running() or _display_count() > 1:
        if dry:
            return f"WOULD kill BetterDisplay + set {tag}"
        _kill_betterdisplay()
    # On a viewer SWITCH (force=True), never trust a single "ok" readback — always re-apply. The
    # transition-moment readback can misjudge "already correct" and strand the display on the old
    # device's shape (the "killed macbook, jumped to iphone, didn't switch" bug). Idempotency still
    # holds for a STEADY viewer (force=False), so fast polling never churns.
    if not force and _dummy_ok(w, h, sc, deg) and _display_count() == 1:
        return f"ok {tag}"
    # a read failure (loaded mini) must NOT trigger a blind reshape -- only act once dp reads clean.
    if not _dp_alive():
        return f"hold {tag} (displayplacer unavailable)"
    if dry:
        return f"WOULD set {tag}"
    _set_dummy(w, h, sc, deg)
    if _dummy_ok(w, h, sc, deg):
        return f"SET {tag}"
    fb = MODE_FALLBACK.get((w, h))
    if fb:
        _set_dummy(*fb)
        if _dummy_ok(*fb):
            return f"SET {fb[0]}x{fb[1]} {'HiDPI' if fb[2] else 'LoDPI'} (fallback; {w}x{h} unavailable)"
    return f"hold {tag} (set didn't verify -- retry next tick)"


def tick(dry=False) -> dict:
    st = _load()
    now = time.time()
    name_ip = _ts_name_to_ip()
    # Make dar emit our RELAY_SENTINEL for a genuine public-relay peer, so select_viewer can tell a
    # real relay from a name-resolution blip. (The device under relay is chosen via the hint / iPad.)
    os.environ["DISPLAY_FALLBACK_VIEWER"] = RELAY_SENTINEL

    # per-socket bytes_out (validated parser) -> per-IP rates; the aggregate is just their sum, so we
    # no longer need the fragile per-process nettop column parse for the activity signal.
    peers = _peer_bytes()
    prev = st.get("metrics", {})
    dt = max(now - float(prev.get("ts") or now), 1.0)
    prev_peers = prev.get("peers", {})
    peer_kbps = {ip: max(b - int(prev_peers.get(ip, b)), 0) * 8 / 1000.0 / dt for ip, b in peers.items()}
    kbps = sum(peer_kbps.values())

    session = _session_active(kbps)
    viewer, relay = select_viewer(st, peer_kbps, name_ip, session)
    # absence countdown: reset when a viewer is selected; advance toward the iPad-HiDPI settle ONLY
    # when the session is genuinely GONE. A live session that just couldn't name its viewer this tick
    # (a dig/tailscale blip) must NOT drift to settle -- that would force a HiDPI rebuild on a still-
    # connected LoDPI viewer. In that case hold absent where it is and freeze the last shape.
    if viewer:
        st["absent"] = 0
    elif not session:
        st["absent"] = int(st.get("absent", 0)) + 1

    if not viewer:
        # DEFAULT TO LAST-USED: nobody actively selected -> hold the last device's shape + its last
        # scaling indefinitely, so a remote session resumes at the dimensions it last used and never
        # snaps back to a default mid-gap. Only a box with NO prior state falls to the iPad 4:3 resting
        # shape. (No more settle-to-iPad: last-used IS the default now.)
        held = st.get("viewer") or "ipad-default"
        if held == RELAY_SENTINEL:
            held = "ipad-default"
        hidpi = bool(st.get("scaling_hidpi", scaling_for(held, relay=False)))
        profile = _profile("ipad" if held == "ipad-default" else held)
        action = enforce(profile, hidpi, dry, st)
        st["metrics"] = {"ts": now, "peers": peers}
        if not dry:
            _save(st)
        return {"viewer": held, "idle": True, "kbps": round(kbps, 1),
                "scaling": "HiDPI" if hidpi else "LoDPI", "action": action}

    # active viewer -> FIXED per-device scaling. No live RTT/DERP measurement, no hysteresis, nothing
    # that can flap: the device's link is stable, so its scaling is a deterministic lookup.
    hidpi = scaling_for(viewer, relay)
    profile = _profile(viewer)
    # Force a reshape only when the target SHAPE differs, not merely the viewer name. A redundant
    # mode-set still blinks the panel, and a blink makes Screens re-fit its view -- silently dropping
    # the client out of "Actual Size" back to scale-to-fit (= the fuzzy-after-device-switch bug).
    # Anti-stranding intent is preserved: differing shapes still force.
    prev = st.get("viewer")
    changed = prev != viewer and (
        not prev or _dummy_mode(_profile(prev)) != _dummy_mode(profile)
        or _mode_degree(_profile(prev)) != _mode_degree(profile)
    )
    action = enforce(profile, hidpi, dry, st, force=changed)
    st["metrics"] = {"ts": now, "peers": peers}
    st["viewer"], st["scaling_hidpi"] = viewer, hidpi
    if not dry:
        _save(st)
    return {
        "viewer": viewer, "relay": relay, "profile": profile,
        "scaling": "HiDPI" if hidpi else "LoDPI", "kbps": round(kbps, 1), "action": action,
    }


def main():
    dry = "--dry" in sys.argv
    cmd = next((a for a in sys.argv[1:] if not a.startswith("-")), "once")
    if cmd == "status":
        out = tick(dry=True)
        for k, v in out.items():
            print(f"  {k:12} {v}")
        return
    out = tick(dry=dry)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
