#!/usr/bin/env python3
"""CDP foreground-intrusion warden.

HARD RULE (owner): no automation may ever jack the foreground. Every automation
Chrome runs on a remote-debugging port; the owner's own Chrome does NOT. That is
the discriminator: this warden only ever touches debug-port Chromes, never the
human's browser.

Each tick, for every automation CDP port:
  1. Close any dead-session login popup (accounts.google.com / oauth / signin /
     about:blank popups) -- these are the windows that escape the -32000 offscreen
     launch position, because the auth flow spawns a NEW window at default coords.
  2. Force every window of that profile back offscreen via Browser.setWindowBounds
     (belt-and-suspenders for any non-login window that opens on-screen).
  3. When a login popup is closed, drop a reauth flag + log so the dead session is
     surfaced (and the source automation can skip instead of re-popping it).

Gate: if data/runtime/CDP_LOGIN_IN_PROGRESS exists, do nothing -- a human is doing
a deliberate one-time login (the only sanctioned headful path) and we must not fight it.
"""
import base64, json, os, socket, struct, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "data" / "runtime"
HUSTLE = ROOT / "data" / "hustle"
REAUTH_DIR = HUSTLE / "cdp_reauth_needed"
LOGIN_LOCK = RUNTIME / "CDP_LOGIN_IN_PROGRESS"
LOG = HUSTLE / "cdp_foreground_warden.out"
# Breadcrumb of the last app the OWNER was actually in (never an automation Chrome), so when an
# automation window steals the foreground between ticks we can hand control straight back to it.
FRONT_STATE = RUNTIME / "warden_last_good_frontmost.txt"
PARK_STAMP = RUNTIME / "warden_last_park.txt"  # throttles the expensive System Events re-park
PARK_SAFETY_S = 300  # slow safety re-park cadence when nothing escaped (vs every 12s)

PORT_RANGE = range(18800, 18831)
OFFSCREEN = {"left": -32000, "top": -32000, "width": 1920, "height": 1080, "windowState": "normal"}  # legacy CDP bounds (kept for reference); final positioning is now done by _park_bottom_left() via System Events, the only API that can place a too-tall window at the bottom edge instead of force-topping it.
NUB_PX = 22  # how many px of a parked window peek into the bottom-left corner (barely visible nub)
# A page whose URL contains any of these is a dead-session auth intrusion, not real work.
LOGIN_MARKERS = (
    "accounts.google.com", "accounts.youtube.com", "/signin", "/ServiceLogin",
    "/o/oauth2", "/v3/signin", "/AccountChooser", "/CheckConnection",
)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def _http(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _ws_cmd(ws_url: str, method: str, params: dict, _id: int):
    """Minimal one-shot CDP websocket command (pure stdlib, no deps)."""
    # ws://127.0.0.1:PORT/devtools/browser/<id>
    assert ws_url.startswith("ws://")
    hostport, _, path = ws_url[len("ws://"):].partition("/")
    host, _, port = hostport.partition(":")
    port = int(port or 80)
    path = "/" + path
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    s = socket.create_connection((host, port), timeout=2.0)
    try:
        s.sendall(req.encode())
        s.settimeout(2.0)
        # drain handshake response headers
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(1024)
            if not chunk:
                return None
            buf += chunk
        payload = json.dumps({"id": _id, "method": method, "params": params}).encode()
        # client->server frame: FIN+text, masked
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        s.sendall(bytes(header) + masked)
        # read one server frame (best-effort; we mostly fire-and-forget)
        try:
            hdr = s.recv(2)
            if len(hdr) < 2:
                return True
            ln = hdr[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", s.recv(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", s.recv(8))[0]
            data = b""
            while len(data) < ln:
                c = s.recv(ln - len(data))
                if not c:
                    break
                data += c
            return json.loads(data.decode("utf-8", "replace")) if data else True
        except Exception:
            return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def offscreen_all_windows(port: int, browser_ws: str, targets: list) -> None:
    seen = set()
    _id = 1000
    for t in targets:
        if t.get("type") != "page":
            continue
        tid = t.get("id")
        if not tid:
            continue
        _id += 1
        res = _ws_cmd(browser_ws, "Browser.getWindowForTarget", {"targetId": tid}, _id)
        if not isinstance(res, dict):
            continue
        win = (res.get("result") or {}).get("windowId")
        if not win or win in seen:
            continue
        seen.add(win)
        _id += 1
        _ws_cmd(browser_ws, "Browser.setWindowBounds", {"windowId": win, "bounds": OFFSCREEN}, _id)


def sweep_port(port: int) -> dict:
    out = {"port": port, "closed": 0, "offscreened": False, "login_hit": False}
    ver = _http(f"http://127.0.0.1:{port}/json/version")
    if not ver:
        return out
    try:
        browser_ws = json.loads(ver).get("webSocketDebuggerUrl", "")
    except Exception:
        browser_ws = ""
    lst = _http(f"http://127.0.0.1:{port}/json/list")
    if not lst:
        return out
    try:
        targets = json.loads(lst)
    except Exception:
        return out
    for t in targets:
        url = (t.get("url") or "")
        is_login = any(m in url for m in LOGIN_MARKERS)
        # a bare about:blank PAGE that lingers is almost always an auth popup shell
        is_blank_popup = t.get("type") == "page" and url == "about:blank"
        if is_login or is_blank_popup:
            tid = t.get("id")
            if tid and _http(f"http://127.0.0.1:{port}/json/close/{tid}") is not None:
                out["closed"] += 1
                if is_login:
                    out["login_hit"] = True
    # Positioning is handled once per cycle by _park_bottom_left() (System Events, bottom-left corner)
    # rather than CDP setWindowBounds, which can only force a too-tall window to the top-left.
    return out


def _automation_pids() -> set:
    """PIDs of the debug-port Chrome instances ONLY — never the human's own browser, which does
    not listen on a CDP port. This is what keeps _park_bottom_left from ever moving Operator's Chrome."""
    pids = set()
    for port in PORT_RANGE:
        try:
            r = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=5)
            pids.update(int(x) for x in r.stdout.split() if x.strip().isdigit())
        except Exception:
            pass
    return pids


def _park_bottom_left(pids: set | None = None) -> None:
    """Park automation Chrome windows as a barely-visible nub in the TRUE bottom-left corner.
    Compute a MODERATE offset from each window's own width + the screen height
    ({NUB_PX - width, screenH - NUB_PX}) instead of the old {-32000, 32000} extreme. At that
    extreme macOS's off-screen clamp lands a *full-height* window as a tall LEFT-EDGE sliver
    (visible, mid-screen -- the exact bug Operator reported), NOT the intended small corner nub.
    A moderate offset is honored verbatim by the WindowServer (verified: requested == got),
    leaving only ~NUB_PX of the window peeking into the bottom-left corner. Works for too-tall
    windows too (they just tuck to the same corner). Filtered to automation PIDs so the owner's
    own browser is never touched (the warden's hard rule)."""
    if pids is None:
        pids = _automation_pids()
    if not pids:
        return
    conds = " or ".join(f"unix id is {p}" for p in pids)
    script = (
        'tell application "Finder" to set _scr to bounds of window of desktop\n'
        'set _scrW to item 3 of _scr\n'
        'set _scrH to item 4 of _scr\n'
        f'set _nub to {NUB_PX}\n'
        'tell application "System Events"\n'
        f'  repeat with proc in (every process whose ({conds}))\n'
        '    repeat with w in (every window of proc)\n'
        '      try\n'
        '        set _sz to size of w\n'
        '        set _cw to item 1 of _sz\n'
        '        set _ch to item 2 of _sz\n'
        '        if _cw > (_scrW - 2) then set _cw to (_scrW - 2)\n'
        '        if _ch > (_scrH - 2) then set _ch to (_scrH - 2)\n'
        '        set size of w to {_cw, _ch}\n'
        '        set position of w to {(_nub - _cw), (_scrH - _nub)}\n'
        '      end try\n'
        '    end repeat\n'
        '  end repeat\n'
        'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
    except Exception:
        pass


def _frontmost() -> tuple[int, str]:
    """(unix pid, app name) of the frontmost process, or (0, '') if it can't be read."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get {unix id, name} of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=4)
        parts = [x.strip() for x in (r.stdout or "").strip().split(",")]
        return int(parts[0]), ", ".join(parts[1:]).strip()
    except Exception:
        return 0, ""


def _is_automation_chrome(pid: int) -> bool:
    """True if `pid`'s command line carries an automation marker (a CDP debug port or a
    cdp-profile user-data-dir). This closes the LAUNCH RACE: a freshly-spawned headful
    automation Chrome grabs the foreground ~1-2s BEFORE its debug port starts listening, so
    `_automation_pids()` (a port-listener sweep) can't see it yet -- but its argv already proves
    it's ours. The owner's own daily Chrome never carries `--remote-debugging-port` or a
    `chrome-cdp-profile` dir, so it can never match and is never yanked."""
    if not pid:
        return False
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return False
    return ("--remote-debugging-port=" in cmd) or ("chrome-cdp-profile" in cmd) or (".chrome-cdp" in cmd)


def return_focus_if_stolen(pids: set) -> None:
    """If an AUTOMATION Chrome (a debug-port instance -- never the owner's own browser, which
    has no CDP port) has grabbed the foreground, hand control straight back to the last app the
    owner was really in. When the owner's OWN app (incl. their own non-debug Chrome) is front,
    just record it as the return target and do nothing. This is what makes an offscreen chrome's
    auth/newtab popup stop holding the cursor -- the launch-time fix can't touch a window that a
    still-running chrome spawns internally."""
    pid, name = _frontmost()
    if not name:
        return
    is_auto = name == "Google Chrome" and (pid in pids or _is_automation_chrome(pid))
    if not is_auto:
        if name != "Google Chrome":  # a real owner app is front -> remember where to return to
            try:
                FRONT_STATE.write_text(name)
            except Exception:
                pass
        return
    prior = ""
    try:
        prior = FRONT_STATE.read_text().strip()
    except Exception:
        pass
    if prior and prior != "Google Chrome":
        try:
            subprocess.run(["osascript", "-e", f'tell application "{prior}" to activate'],
                           capture_output=True, timeout=4)
            log(f"focus-return: automation Chrome (pid {pid}) held the foreground -> reactivated {prior!r}")
        except Exception:
            pass


# Persistent-daemon cadences. The old model was a 12s launchd StartInterval one-shot, so a
# headful window that opened right after a tick held Operator's keyboard+cursor for UP TO 12s
# before focus was handed back. launchd throttles quick-exit jobs to ~10s min relaunch, so a
# one-shot can't go faster. As a persistent daemon we split the work: the CHEAP focus-return
# check runs every FAST_INTERVAL_S (stolen focus now comes back in ~1s), the expensive 31-port
# CDP sweep only every FULL_INTERVAL_S. The System Events re-park stays throttled exactly as
# before (only on escape, or the PARK_SAFETY_S safety cadence) so CPU never spikes.
FAST_INTERVAL_S = 1.0
FULL_INTERVAL_S = 12.0


def _park_and_return(total_closed: int) -> None:
    """Shared tail: throttled re-park + focus hand-back. total_closed>0 forces an escape re-park."""
    pids = _automation_pids()
    # The expensive System Events re-park (enumerates every window of every automation Chrome
    # and sets positions, ~12s) was firing EVERY run (12s) — re-parking already-parked windows
    # forever, pegging System Events ~95% CPU indefinitely = interactive lag. CDP setWindowBounds
    # (in sweep_port) already keeps windows offscreen; the AppleScript re-park only needs to run
    # when a window actually escaped this cycle (an intrusion was swept, or an automation Chrome
    # is frontmost), plus a slow safety re-park at most every PARK_SAFETY_S.
    front_pid, front_name = _frontmost()
    escaped = bool(total_closed) or (front_name == "Google Chrome"
                                     and (front_pid in pids or _is_automation_chrome(front_pid)))
    now = time.time()
    try:
        last_park = float(PARK_STAMP.read_text().strip()) if PARK_STAMP.exists() else 0.0
    except Exception:
        last_park = 0.0
    if escaped or (now - last_park > PARK_SAFETY_S):
        _park_bottom_left(pids)
        try:
            PARK_STAMP.write_text(str(now))
        except Exception:
            pass
    return_focus_if_stolen(pids)


def full_pass() -> int:
    """One full cycle: 31-port intrusion sweep + throttled re-park + focus hand-back."""
    if LOGIN_LOCK.exists():
        log("CDP_LOGIN_IN_PROGRESS present -> standing down (deliberate human login)")
        return 0
    REAUTH_DIR.mkdir(parents=True, exist_ok=True)
    total_closed = 0
    for port in PORT_RANGE:
        r = sweep_port(port)
        if r["closed"] or r["login_hit"]:
            total_closed += r["closed"]
            log(f"port {port}: closed {r['closed']} intrusion page(s)"
                f"{' [LOGIN/reauth]' if r['login_hit'] else ''}"
                f"{' offscreened' if r['offscreened'] else ''}")
            if r["login_hit"]:
                try:
                    (REAUTH_DIR / f"{port}.flag").write_text(
                        f"{time.strftime('%Y-%m-%dT%H:%M:%S')} dead session -> one-time re-login needed\n")
                except Exception:
                    pass
    if total_closed:
        log(f"swept {total_closed} foreground intrusion(s)")
    _park_and_return(total_closed)
    return 0


def fast_pass() -> None:
    """Cheap cycle (no port sweep): hand focus back the instant an automation Chrome is front."""
    if LOGIN_LOCK.exists():
        return
    _park_and_return(0)


def run_forever() -> int:
    """Persistent loop: fast focus-return every FAST_INTERVAL_S, full sweep every FULL_INTERVAL_S."""
    last_full = 0.0
    while True:
        try:
            now = time.time()
            if now - last_full >= FULL_INTERVAL_S:
                full_pass()
                last_full = now
            else:
                fast_pass()
        except Exception as e:  # never let one bad cycle kill the warden
            log(f"cycle error (continuing): {e!r}")
        time.sleep(FAST_INTERVAL_S)


def main() -> int:
    # `--once` = legacy single full pass (manual invocation / any caller expecting one-shot).
    if "--once" in sys.argv:
        return full_pass()
    return run_forever()


if __name__ == "__main__":
    sys.exit(main())
