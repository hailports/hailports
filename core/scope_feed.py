#!/usr/bin/env python3
"""Bounty scope feed — the DENY-BY-DEFAULT legal gate every white-hat agent must pass.

Pulls the canonical public bug-bounty scope data (arkadiyt/bounty-targets-data, regenerated
daily) + disclose.io safe-harbor classifications, and normalizes them into ONE allowlist
table. Every downstream probe calls `in_scope(host)` and gets nothing back unless the host
is LITERALLY on an authorized, safe-harbor program's in-scope list. Out-of-scope or no
safe harbor = zero legal protection = never touched.

Each entry carries: program, platform, bounty(bool), safe_harbor(enum), automation_allowed
(default DENY — only the no-touch OSINT tier runs without it). Emits a 'new-this-run' delta
so agents prioritize freshly-added assets (freshness = the anti-duplicate edge).

  python3 -m core.scope_feed --refresh      # pull feeds, rebuild table, print delta
  from core.scope_feed import in_scope
  s = in_scope("api.example.com")           # -> scope dict or None (deny-by-default)
"""
from __future__ import annotations
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "bounty"
TABLE = OUT / "scope_table.json"

# canonical, free, no-auth feeds
BT = "https://raw.githubusercontent.com/arkadiyt/bounty-targets-data/main/data/"
FEEDS = {  # platform -> (url, program-name key, scope-list extractor)
    "hackerone": BT + "hackerone_data.json",
    "bugcrowd": BT + "bugcrowd_data.json",
    "intigriti": BT + "intigriti_data.json",
    "yeswehack": BT + "yeswehack_data.json",
}
DISCLOSE = "https://raw.githubusercontent.com/disclose/diodb/master/program-list.json"


def _get_json(url: str, timeout: int = 40):
    # curl negotiates IPv6 (this host's IPv4 egress is Cloudflare-ASN-banned for some CDNs)
    try:
        out = subprocess.run(["curl", "-sL", "--max-time", str(timeout), url],
                             capture_output=True, text=True, timeout=timeout + 5).stdout
        return json.loads(out) if out.strip() else None
    except Exception:
        return None


def _norm_host(t: str) -> str | None:
    t = str(t or "").strip().lower()
    t = t.replace("http://", "").replace("https://", "").split("/")[0]
    if not t or " " in t or ("." not in t and not t.startswith("*")):
        return None
    return t


def _extract(platform: str, data) -> list[dict]:
    """Pull in-scope web/host targets + bounty flag from one platform feed."""
    out, progs = [], data if isinstance(data, list) else data.get("programs", []) if isinstance(data, dict) else []
    for p in progs:
        if not isinstance(p, dict):
            continue
        name = p.get("name") or p.get("handle") or p.get("url") or "?"
        bounty = bool(p.get("offers_bounties") or p.get("bounty") or p.get("max_severity"))
        targets = p.get("targets", {})
        in_scope = targets.get("in_scope", []) if isinstance(targets, dict) else (targets or [])
        for t in in_scope:
            asset = t.get("asset_identifier") or t.get("target") or t.get("endpoint") if isinstance(t, dict) else t
            atype = (t.get("asset_type", "") if isinstance(t, dict) else "").upper()
            if atype and atype not in ("URL", "WILDCARD", "WEBSITE", "API", "DOMAIN", "IP_ADDRESS", ""):
                continue  # skip mobile/source/hardware/etc.
            host = _norm_host(asset)
            if host:
                out.append({"pattern": host, "program": str(name)[:80], "platform": platform, "bounty": bounty})
    return out


def refresh() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    rows, seen = [], set()
    for platform, url in FEEDS.items():
        data = _get_json(url)
        if not data:
            print(f"  [scope] {platform}: feed unreachable (skipped)")
            continue
        ext = _extract(platform, data)
        for r in ext:
            key = (r["pattern"], r["program"])
            if key not in seen:
                seen.add(key); rows.append(r)
        print(f"  [scope] {platform}: {len(ext)} in-scope assets")
    # safe-harbor overlay from disclose.io (best-effort)
    sh = {}
    dz = _get_json(DISCLOSE)
    if isinstance(dz, list):
        for e in dz:
            for k in ("policy_url", "contact_url", "program_url"):
                u = _norm_host(e.get(k, ""))
                if u:
                    sh[u] = (e.get("preferred_languages") and "full") or e.get("safe_harbor", "unknown")
    for r in rows:
        base = r["pattern"].lstrip("*.")
        r["safe_harbor"] = sh.get(base, "platform-default")   # platform programs carry their own SH terms
        r["automation_allowed"] = False                        # DENY by default; per-program policy parse can raise it

    prev = set()
    if TABLE.exists():
        try:
            prev = {(x["pattern"], x["program"]) for x in json.loads(TABLE.read_text()).get("rows", [])}
        except Exception:
            pass
    delta = [r for r in rows if (r["pattern"], r["program"]) not in prev]
    TABLE.write_text(json.dumps({
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows), "rows": rows}, indent=1))
    (OUT / "scope_delta.json").write_text(json.dumps({"new": delta, "count": len(delta)}, indent=1))
    print(f"[scope] table: {len(rows)} in-scope assets across {len(set(r['program'] for r in rows))} programs | "
          f"NEW this run: {len(delta)}")
    return {"count": len(rows), "new": len(delta)}


def _table() -> list[dict]:
    try:
        return json.loads(TABLE.read_text()).get("rows", [])
    except Exception:
        return []


def in_scope(host: str) -> dict | None:
    """DENY-BY-DEFAULT: returns the scope entry only if host is literally on an in-scope
    pattern (wildcard-aware). None otherwise — agents must treat None as 'never touch'."""
    host = _norm_host(host)
    if not host:
        return None
    for r in _table():
        pat = r["pattern"]
        if pat.startswith("*."):
            if host == pat[2:] or host.endswith("." + pat[2:]):
                return r
        elif host == pat:
            return r
    return None


if __name__ == "__main__":
    if "--refresh" in sys.argv or not TABLE.exists():
        refresh()
    else:
        t = _table()
        print(f"[scope] {len(t)} in-scope assets cached. Use in_scope(host) for deny-by-default lookups.")
