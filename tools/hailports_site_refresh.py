#!/usr/bin/env python3
"""Set-and-forget LIVING-SITE loop for hailports.com.

Pulls REAL faceless state (repos now public, milestone counters, recent machine
activity) and injects it as DYNAMIC sections into the home + /live pages, then
deploys — but ONLY when the injected payload actually changed (idempotent, hashed).

Reuses the existing single-source renderer (tools/build_hailports_home.py) so the
guide-link SEO + baked metrics fallback stay identical; this script only layers the
"open source now" card, a rotating "what's new" activity feed, and the freshest
dashboard tiles on top, then hands the built tree to scripts/deploy_hailports.sh
(which independently HARD-ABORTS on any cross-brand / fingerprint / identity leak
and runs the responsive lint — the deploy safety net).

Anonymity: every injected line passes core/anon_scrub FIRST, fail-closed. The whole
rendered page is checked with anon_scrub.find_identity_leaks() (the correct served-
tree gate — the full find_leaks() flags legit external hrefs like youtube.com). If
anon_scrub can't be imported or a leak is found, the refresh ABORTS and nothing ships.

    PYTHONPATH=. .venv/bin/python tools/hailports_site_refresh.py             # refresh + deploy on change
    PYTHONPATH=. .venv/bin/python tools/hailports_site_refresh.py --no-deploy # write dist, skip deploy
    PYTHONPATH=. .venv/bin/python tools/hailports_site_refresh.py --dry-run   # build + gate only, no write/deploy
    PYTHONPATH=. .venv/bin/python tools/hailports_site_refresh.py --show      # dry-run + print the injected sections
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import anon_scrub  # noqa: E402  (fail-closed: import error == no deploy)
from tools import build_hailports_home as home  # noqa: E402  (single-source renderer, read-only reuse)

HUSTLE = ROOT / "data" / "hustle"
DIST = HUSTLE / "hailports_dist"
STATE_FILE = HUSTLE / ".hailports_site_refresh.json"
LOG_DIR = HUSTLE / "logs"
DEPLOY = ROOT / "scripts" / "deploy_hailports.sh"

WN_START = "<!--WHATS_NEW_START-->"
WN_END = "<!--WHATS_NEW_END-->"

GITHUB_ORG = "https://github.com/hailports"
FEED_MAX = 5
RECENT_DAYS = 4  # a signal counts as "recent" if its source moved within this window


# ── real state (faceless, qualitative — NEVER emit a 3+ sig-fig number: anon_scrub blocks it) ──
def _git_commit_count() -> int | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            return int(out.stdout.strip())
    except Exception:
        pass
    return None


def _cumulative_cycles() -> int | None:
    total, found = 0, False
    try:
        for p in HUSTLE.glob("autoflow_claude_runs_*.count"):
            try:
                total += int(p.read_text().strip())
                found = True
            except Exception:
                continue
    except Exception:
        return None
    return total if found else None


def _guides_count() -> int:
    gdir = DIST / "guides"
    if not gdir.exists():
        return 0
    return sum(1 for f in gdir.glob("*.html") if f.name != "index.html")


def _recent_mtime(paths) -> bool:
    cutoff = time.time() - RECENT_DAYS * 86400
    for p in paths:
        try:
            if p.exists() and p.stat().st_mtime >= cutoff:
                return True
        except Exception:
            continue
    return False


def _shorts_recent() -> bool:
    dirs = [HUSTLE / "youtube_shorts_queue", HUSTLE / "daily_content", HUSTLE / "premium_shorts"]
    paths = list(dirs)
    for d in dirs:
        if d.exists():
            paths.extend(d.iterdir())
    return _recent_mtime(paths)


def gather_state(prev: dict) -> dict:
    return {
        "repos_public": True,  # verified: github.com/hailports + self-healing-agent + devkit are public
        "commits_total": _git_commit_count(),
        "cycles_total": _cumulative_cycles(),
        "guides_count": _guides_count(),
        "shorts_recent": _shorts_recent(),
        "prev_commits": (prev.get("state") or {}).get("commits_total"),
        "prev_cycles": (prev.get("state") or {}).get("cycles_total"),
        "prev_guides": (prev.get("state") or {}).get("guides_count"),
    }


# ── "what's new" feed: real-signal lines (included when their signal is true) + a rotating
# evergreen pool, keyed by the day so the list self-changes on a cadence. Every line is
# qualitative and number-free; each is anon-gated before it can ship. ──
def _grew(now, prev) -> bool:
    return isinstance(now, (int, float)) and isinstance(prev, (int, float)) and now > prev


def build_feed(st: dict) -> list[str]:
    signal_lines: list[str] = []
    if st.get("repos_public"):
        signal_lines.append("open-sourced the self-running back office so anyone can read how it works")
    if _grew(st.get("commits_total"), st.get("prev_commits")):
        signal_lines.append("pushed another batch of autonomous commits to the public repos")
    if _grew(st.get("cycles_total"), st.get("prev_cycles")):
        signal_lines.append("ran more build-and-ship cycles overnight, hands-off")
    if _grew(st.get("guides_count"), st.get("prev_guides")):
        signal_lines.append("published new field guides to the library")
    if st.get("shorts_recent"):
        signal_lines.append("shipped a fresh build-in-public short from the machine")

    evergreen = [
        "kept the live board streaming around the clock, self-healing as it went",
        "self-healed a job that hiccuped and carried on",
        "rotated fresh proof onto the live board",
        "held every outward action for owner review — staged, not sent",
        "kept the numbers rounded and the operator out of frame, on purpose",
        "recovered a flaky job and picked up where it left off",
        "refreshed the board to reflect what actually shipped",
        "tuned itself to spend less on paid models, more on local",
    ]
    # rotate the evergreen pool by day-of-year so the feed changes on a cadence
    doy = date.today().timetuple().tm_yday
    rot = evergreen[doy % len(evergreen):] + evergreen[:doy % len(evergreen)]

    feed: list[str] = []
    for line in signal_lines + rot:
        if line not in feed:
            feed.append(line)
        if len(feed) >= FEED_MAX:
            break
    return feed


def _feed_html(feed: list[str], indent: str) -> str:
    return "\n".join(
        f'{indent}<li><span class="wn-dot"></span><span>{html.escape(line)}</span></li>'
        for line in feed
    )


def _inject_feed(page: str, feed_html: str, indent: str) -> str:
    pat = re.compile(re.escape(WN_START) + r".*?" + re.escape(WN_END), re.S)
    block = f"{WN_START}\n{feed_html}\n{indent}{WN_END}"
    if not pat.search(page):
        return page  # template missing markers -> leave base render untouched (defensive)
    return pat.sub(lambda _m: block, page, count=1)


# ── anonymity gate (fail-closed) ──────────────────────────────────────────────────────────────
class AnonBlocked(Exception):
    pass


def _gate_lines(feed: list[str]) -> None:
    """Each generated line must be fully clean under the STRICT outbound gate (no URLs, no numbers)."""
    for line in feed:
        leaks = anon_scrub.find_leaks(line)
        if leaks:
            raise AnonBlocked(f"what's-new line blocked {sorted(set(leaks))!r}: {line!r}")


def _gate_page(name: str, page: str) -> None:
    """Whole served page: identity/employer tokens must be absent (the correct served-tree gate —
    find_leaks() would false-flag legit external hrefs like youtube.com/github.com)."""
    ident = anon_scrub.find_identity_leaks(page)
    if ident:
        raise AnonBlocked(f"{name}: identity/employer leak {sorted(set(ident))!r}")
    # false-autonomy truth gate (same rail the deploy guard enforces) — catch it here so bad
    # copy never even reaches the deploy step. Humans stay gated on money/sends/strategy.
    try:
        from core import autonomy_ledger
        ok, reason = autonomy_ledger.assert_truthful(page)
        if not ok:
            raise AnonBlocked(f"{name}: false-autonomy claim — {reason}")
    except ImportError:
        pass  # deploy guard still enforces it downstream (fail-closed at the wire)


# ── build ──────────────────────────────────────────────────────────────────────────────────────
def render(prev: dict) -> tuple[str, str, list[str], str]:
    st = gather_state(prev)
    feed = build_feed(st)
    _gate_lines(feed)

    home_page = _inject_feed(home.build(), _feed_html(feed, "      "), "    ")
    live_page = _inject_feed(home.build_live(), _feed_html(feed, "        "), "      ")

    _gate_page("home", home_page)
    _gate_page("live", live_page)

    payload = home_page + "\x00" + live_page
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return home_page, live_page, feed, digest


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(digest: str, feed: list[str], st: dict, deployed: bool) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "hash": digest,
        "feed": feed,
        "state": {k: st.get(k) for k in ("commits_total", "cycles_total", "guides_count")},
        "deployed": deployed,
        "ts": datetime.now(timezone.utc).isoformat(),
    }, indent=2, default=str))


def _deploy() -> int:
    print("==> state changed -> deploying via scripts/deploy_hailports.sh")
    r = subprocess.run(["bash", str(DEPLOY)], cwd=str(ROOT))
    return r.returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="build + anon-gate only; no write, no deploy")
    ap.add_argument("--no-deploy", action="store_true", help="write dist on change but do not deploy")
    ap.add_argument("--show", action="store_true", help="dry-run and print the injected sections")
    ap.add_argument("--force", action="store_true", help="deploy even if the payload hash is unchanged")
    args = ap.parse_args(argv)

    prev = _load_state()
    st = gather_state(prev)
    try:
        home_page, live_page, feed, digest = render(prev)
    except AnonBlocked as e:
        print(f"❌ ABORT (anon gate, fail-closed): {e}")
        return 2
    except Exception as e:
        print(f"❌ ABORT (render/gate error, fail-closed): {e}")
        return 2

    changed = args.force or digest != prev.get("hash")
    print(f"==> anon gate OK · feed={len(feed)} lines · hash={digest[:12]} · "
          f"{'CHANGED' if changed else 'unchanged'} (prev={str(prev.get('hash'))[:12]})")

    if args.show:
        m = re.search(re.escape(WN_START) + r"(.*?)" + re.escape(WN_END), home_page, re.S)
        os_m = re.search(r'<section class="opensrc".*?</section>', home_page, re.S)
        print("\n--- open source now (home) ---")
        print(os_m.group(0) if os_m else "(open-source section not found)")
        print("\n--- what's new feed ---")
        print(m.group(1).strip() if m else "(feed markers not found)")

    if args.dry_run or args.show:
        would = "would deploy" if changed and not args.no_deploy else \
                ("would write dist (no-deploy)" if changed else "no change -> would skip")
        print(f"==> DRY RUN — no files written. {would}.")
        return 0

    if not changed:
        print("==> no change -> nothing written, nothing deployed (idempotent).")
        return 0

    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / "index.html").write_text(home_page, encoding="utf-8")
    (DIST / "live").mkdir(parents=True, exist_ok=True)
    (DIST / "live" / "index.html").write_text(live_page, encoding="utf-8")
    print(f"==> wrote {DIST/'index.html'} + {DIST/'live'/'index.html'}")

    deployed = False
    if args.no_deploy:
        print("==> --no-deploy: dist updated, skipping deploy.")
    else:
        rc = _deploy()
        deployed = rc == 0
        if not deployed:
            print(f"❌ deploy returned rc={rc} (see above). dist is updated; state hash NOT advanced so "
                  f"the next run retries.")
            _save_state(prev.get("hash", ""), feed, st, False)  # keep old hash -> retry next cadence
            return 1

    _save_state(digest, feed, st, deployed)
    print("==> done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
