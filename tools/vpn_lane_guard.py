#!/usr/bin/env python3
"""vpn_lane_guard.py — keep the VPN out of the work lane, forever.

Root cause of the Salesforce freeze (2026-07-08): the Mullvad VPN ran as a
Tailscale *exit node* on the MAIN daemon, which grabs the default route and
pushes ALL outbound traffic — including Salesforce logins — through Mullvad's
Dallas datacenter IP. Salesforce saw a datacenter IP + geo jump and froze the
accounts.

The rule now: the MAIN tailscaled must NEVER carry an exit node. Whole-Mac VPN
is banned. Hustle anonymity goes through the per-profile SOCKS lane instead
(see vpn_socks_setup.sh). This guard enforces that:

  guard   (watchdog, launchd/60s) — if the main daemon has an exit node OR
          egress is a Mullvad IP, CLEAR it and page Operator. Self-healing.
  status  — human-readable lane state + verdict.
  work    — preflight for any Salesforce/OWA/work access. Exit 0 iff egress is
          verified residential; else auto-heal, re-check, exit 1 if still bad.
  heal    — clear the main-daemon exit node now.

Override (deliberate full-tunnel, rare): touch ~/.vpn-fulltunnel.allow — the
guard will leave the exit node alone while that file exists.
"""
from __future__ import annotations
import json
import os
import socket
import subprocess
import sys

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, STACK)

ALLOW_FULLTUNNEL = os.path.expanduser("~/.vpn-fulltunnel.allow")
TAILSCALE = "/opt/homebrew/bin/tailscale"

# Hustle anonymity SOCKS lane (separate userspace tailscaled — never the main route).
VPN_SOCKS_LABEL = "com.claude-stack.vpn-socks"
SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 1055


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """(rc, stdout). stderr is dropped on purpose: tailscale prints a
    version-skew Warning to stderr that would corrupt JSON parsing if merged."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "")
    except Exception as e:  # noqa: BLE001
        return 1, ""


def main_exit_node() -> str:
    """Exit node engaged on the MAIN tailscaled ('' = none = good).

    NOTE: with this tailscaled build, setting an exit node populates
    ExitNodeID but leaves ExitNodeIP EMPTY — so we must key on the ID (or
    either field), else the guard misses the exact freeze condition.
    """
    rc, out = _run([TAILSCALE, "debug", "prefs"])
    if rc != 0:
        return ""
    try:
        p = json.loads(out)
        return (p.get("ExitNodeID") or p.get("ExitNodeIP") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def mullvad_egress() -> tuple[bool | None, str]:
    """(is_mullvad_ip, ip) via am.i.mullvad.net. None = couldn't determine."""
    rc, out = _run(["curl", "-s", "--max-time", "6", "https://am.i.mullvad.net/json"])
    if rc != 0 or not out.strip():
        rc2, ip = _run(["curl", "-s", "--max-time", "6", "https://api.ipify.org"])
        return None, (ip.strip() if rc2 == 0 else "?")
    try:
        d = json.loads(out)
        return bool(d.get("mullvad_exit_ip")), (d.get("ip") or "?")
    except Exception:  # noqa: BLE001
        return None, "?"


def clear_exit_node() -> bool:
    rc, _ = _run([TAILSCALE, "set", "--exit-node="], timeout=20)
    return rc == 0


def verdict() -> dict:
    exit_ip = main_exit_node()
    is_mv, ip = mullvad_egress()
    allowed = os.path.exists(ALLOW_FULLTUNNEL)
    # SAFE for the work lane iff no main-daemon exit node AND egress isn't Mullvad.
    danger = bool(exit_ip) or (is_mv is True)
    return {
        "exit_node": exit_ip,
        "egress_ip": ip,
        "is_mullvad": is_mv,
        "fulltunnel_allowed": allowed,
        "safe": not danger,
        "danger": danger,
    }


def _page(subject: str, body: str, healed: bool) -> None:
    try:
        from core import alert_gateway
        alert_gateway.page_critical("vpn_lane_guard", subject, body, healed=healed)
    except Exception:  # noqa: BLE001
        print(f"[vpn_lane_guard] {subject}\n{body}", file=sys.stderr)


def cmd_status() -> int:
    v = verdict()
    mv = {True: "YES (masked)", False: "no (residential)", None: "unknown"}[v["is_mullvad"]]
    print("VPN lane guard")
    print(f"  main-daemon exit node : {v['exit_node'] or '(none)'}")
    print(f"  egress IP             : {v['egress_ip']}")
    print(f"  Mullvad egress        : {mv}")
    if v["fulltunnel_allowed"]:
        print("  override              : ~/.vpn-fulltunnel.allow present (guard paused)")
    print(f"  verdict               : {'DANGER — work traffic would be frozen' if v['danger'] else 'SAFE for Salesforce/work'}")
    return 1 if v["danger"] else 0


def cmd_heal() -> int:
    if main_exit_node():
        ok = clear_exit_node()
        print("cleared main-daemon exit node" if ok else "FAILED to clear exit node")
        return 0 if ok else 1
    print("no main-daemon exit node set — nothing to heal")
    return 0


def cmd_work() -> int:
    """Preflight before touching Salesforce/OWA/anything work. 0 = go."""
    v = verdict()
    if not v["danger"]:
        print(f"OK residential ({v['egress_ip']}) — safe to open Salesforce/work")
        return 0
    if v["fulltunnel_allowed"]:
        print("REFUSING: ~/.vpn-fulltunnel.allow is set — whole-Mac VPN is on. "
              "Remove it (rm ~/.vpn-fulltunnel.allow) before any work access.", file=sys.stderr)
        return 1
    print("DANGER: VPN is on the main route — auto-healing before work access...", file=sys.stderr)
    clear_exit_node()
    v = verdict()
    if v["danger"]:
        print(f"STILL UNSAFE (egress {v['egress_ip']}). Do NOT open Salesforce.", file=sys.stderr)
        return 1
    print(f"healed — now residential ({v['egress_ip']}). Safe to proceed.")
    return 0


def _socks_daemon_alive() -> bool:
    """The vpn-socks userspace tailscaled (SOCKS lane) process is running."""
    rc, _ = _run(["pgrep", "-f", "socks5-server=localhost:1055"])
    return rc == 0


def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _socks_curl_ok() -> bool:
    """A real request survives the SOCKS proxy end-to-end."""
    rc, _ = _run(["curl", "-s", "-o", "/dev/null", "--socks5", f"{SOCKS_HOST}:{SOCKS_PORT}",
                  "--max-time", "6", "https://api.ipify.org"], timeout=10)
    return rc == 0


def _guard_socks_health() -> int:
    """SOCKS lane serving-health (F3), independent of the exit-node logic below.

    If the vpn-socks tailscaled is alive but the proxy isn't actually serving —
    127.0.0.1:1055 not LISTEN, or a curl through it fails — kickstart the vpn-socks
    job and page. Returns 0 = healthy / lane intentionally down; 1 = broke + heal
    attempted. Never touches the main-daemon exit node."""
    if not _socks_daemon_alive():
        return 0  # SOCKS lane not up right now — nothing to serve, not our failure
    if _port_listening(SOCKS_HOST, SOCKS_PORT) and _socks_curl_ok():
        return 0
    uid = os.getuid()
    kicked = _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{VPN_SOCKS_LABEL}"],
                  timeout=20)[0] == 0
    action = (f"kickstarted {VPN_SOCKS_LABEL}" if kicked
              else f"FAILED to kickstart — do it by hand: "
                   f"launchctl kickstart -k gui/{uid}/{VPN_SOCKS_LABEL}")
    _page(
        "SOCKS lane down — tailscaled alive but not serving 127.0.0.1:1055",
        f"The vpn-socks tailscaled is running but the SOCKS proxy on "
        f"{SOCKS_HOST}:{SOCKS_PORT} is not serving (port not LISTEN or a request through "
        f"it failed). Hustle anonymity traffic has no egress lane.\nAction: {action}",
        healed=kicked,
    )
    return 0 if kicked else 1


def cmd_guard() -> int:
    """Watchdog tick: ban whole-Mac exit node, self-heal, page once; also verify the
    SOCKS lane is actually serving (independent of the exit-node logic below)."""
    socks_rc = _guard_socks_health()
    if os.path.exists(ALLOW_FULLTUNNEL):
        return socks_rc
    exit_ip = main_exit_node()
    is_mv, ip = mullvad_egress()
    if not exit_ip and is_mv is not True:
        return socks_rc
    healed = clear_exit_node()
    _page(
        "whole-Mac VPN caught on the work route — auto-killed",
        f"The main tailscaled had an exit node ({exit_ip or 'egress was Mullvad'}), which "
        f"routes Salesforce/work traffic through a datacenter IP and freezes accounts.\n"
        f"egress was: {ip}\n"
        f"Action: {'cleared exit node (route back to residential)' if healed else 'FAILED to clear — do it by hand: tailscale set --exit-node='}\n"
        f"Hustle anonymity belongs on the SOCKS lane (socks5://127.0.0.1:1055), not the main route.",
        healed=healed,
    )
    return (0 if healed else 1) or socks_rc


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    return {
        "status": cmd_status,
        "work": cmd_work,
        "check": cmd_work,
        "heal": cmd_heal,
        "guard": cmd_guard,
    }.get(cmd, cmd_status)()


if __name__ == "__main__":
    raise SystemExit(main())
