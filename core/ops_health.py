#!/usr/bin/env python3
"""Ops health — SILENT operational self-heal for every lane/bot Operator depends on.

Sibling to clone_health.py, but with a different job and a different temperament:

  clone_health : "is every capability WIRED?" — reports a board, pages when degraded.
  ops_health   : "is every lane actually PRODUCING?" — and it FIXES instead of telling.

THE MANDATE (Operator, emphatic): silent self-correction. This module does NOT alert, report,
or text on success or on a fault it healed. No "green" pushes. It writes a private internal
ledger (data/learning/ops_health.jsonl) and moves on. The ONLY thing that ever reaches Operator
is an irreducible owner-action — a session that needs his MFA to re-login, a PAT he must mint —
and even that is batched through alert_gateway to near-zero, never a stream.

THE TRAP THIS CLOSES (the TikTok lesson): a launchd job exits 0 while producing NOTHING.
The tiktok poster loops, hits "no file input on upload page (hydration timeout)", logs HELD,
and exits clean. `launchctl list` shows exit 0. The video never posted. So every lane here is
graded on its REAL ARTIFACT — the post log's last terminal result, the state file's last
post ts, live search results, the index mtime — NEVER on the launchd exit code.

Per failing lane, the heal path is grounded, mirroring never_twice's disk-verify discipline:
  1. never_twice.seen(signature)
       known_accepted -> leave it, ledger it, no alert.
       known_fix      -> apply the crystallized fix, then RE-VERIFY the LANE outcome (not the
                         heal command's exit) — held only if the artifact now reads producing.
       novel          -> generic heal (kickstart the lane's launchd job), re-verify the lane,
                         and record_novel() as crystallization fuel — silently.
  2. Any autonomous mutation is gated through decision_modeling.score() ("would Operator approve").
  3. Only a lane that stays stalled AND carries an irreducible owner_action_hint (a re-login
     needing MFA / a token) is escalated — batched, deduped, one line.

run(heal=False) is read-only and touches no live service (the smoke path): it classifies every
lane and records the DRY plan it would run. run(heal=True) actually heals + re-verifies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
import sys
from pathlib import Path

# The staged launchd plist runs this as a direct script; without this bootstrap the direct-run
# sys.path[0] is core/, not the repo root, so `from core import ...` -> ModuleNotFoundError and the
# heal daemon crash-loops silently (defeating its whole mandate). Works for both -m and direct run.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR

DATA = BASE_DIR / "data"
LEARN = DATA / "learning"
HUSTLE = DATA / "hustle"
LEDGER = LEARN / "ops_health.jsonl"
STATE = DATA / "runtime" / "ops_health.json"
STAGED_PLIST = BASE_DIR / "deploy" / "launchagents" / "com.claude-stack.ops-health.plist"
for _d in (LEARN, STATE.parent):
    _d.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────── session self-heal (auto re-auth) config
# Stalled lanes whose residual fault is a dead login map here to the reauth_social platform that
# fixes them. A death event fires ONE auto-reauth per platform per cooldown (never a 600s loop).
#   lane name -> (reauth_social friendly cli name, canonical totp platform, default account handle)
SESSION_REAUTH_LANES: dict[str, tuple[str, str, str]] = {
    "reddit_warmer":    ("reddit", "reddit", "hailportss"),
    "tiktok_poster":    ("tiktok", "tiktok", "hailportshq"),
    "tiktok_engage":    ("tiktok", "tiktok", "hailportshq"),
    "youtube_uploader": ("youtube", "youtube", "redacted"),
}
REAUTH_STATE = Path(os.environ.get("OPS_HEALTH_REAUTH_STATE",
                                   str(DATA / "runtime" / "session_reauth.json")))
REAUTH_COOLDOWN_S = int(os.environ.get("OPS_HEALTH_REAUTH_COOLDOWN_S", str(3 * 3600)))


def _reddit_dead_account(default: str) -> str:
    """The reddit account currently flagged flag_reason=='session' (the one to re-auth+land)."""
    d = _load_json(HUSTLE / "reddit_warmer_state.json") or {}
    for name, rec in (d.get("accounts") or {}).items():
        if str((rec or {}).get("flag_reason", "")).lower() == "session":
            return name
    return default


def _reauth_seed_present(totp_platform: str, account: str) -> bool:
    try:
        from core import totp_store
        return totp_store.has_seed(totp_platform, account)
    except Exception:
        return False


def _launch_reauth(friendly: str, account: str) -> None:
    """Fire-and-forget the reauth flow (detached). Seed present -> it runs headless zero-touch;
    no seed -> it opens the visible login window on the mini. --force because the shallow check
    can lie; --land-account so the fresh cookies reach the right per-account pool file."""
    py = BASE_DIR / ".venv" / "bin" / "python"
    cmd = [str(py), str(BASE_DIR / "tools" / "reauth_social.py"), friendly,
           "--auto", "--force", "--land-account", account]
    try:
        subprocess.Popen(cmd, cwd=str(BASE_DIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def _route_reauth_alert(friendly: str, account: str) -> bool:
    try:
        from core import alert_gateway
        alert_gateway.route(
            "warn", "ops-health-reauth",
            f"{friendly} session died — login window is open on the mini, just log in",
            f"account: {account}\nno 2FA seed enrolled — enroll one for silent zero-touch next time:\n"
            f"  tools/totp_enroll.py <platform> {account}",
            issue_key=f"session-reauth-{friendly}")
        return True
    except Exception:
        return False


def _auto_reauth(lane_name: str, *, heal: bool, alert: bool) -> dict | None:
    """Attempt an auto re-auth for a session-dead lane. DEDUPED (one per platform per cooldown).

    dry (heal=False): plan only — touches nothing, stamps nothing, never alerts (smoke path).
    heal=True:
      * seed enrolled  -> launch HEADLESS zero-touch reauth, SILENT (no alert, self-healed).
      * no seed        -> launch the VISIBLE login window + route ONE deduped gateway warn.
    OPS_HEALTH_REAUTH_NO_LAUNCH=1 exercises the full decision/dedup/alert path without a browser.
    """
    m = SESSION_REAUTH_LANES.get(lane_name)
    if not m:
        return None
    friendly, totp_platform, account = m
    if totp_platform == "reddit":
        account = _reddit_dead_account(account)
    seed = _reauth_seed_present(totp_platform, account)
    out = {"lane": lane_name, "platform": friendly, "account": account,
           "seed": seed, "action": None, "alerted": False}

    if not heal:  # dry / smoke — plan only
        out["action"] = "plan_headless_auto" if seed else "plan_visible_window_plus_alert"
        return out

    now = time.time()
    st = _load_json(REAUTH_STATE) or {}
    last = 0.0
    try:
        last = float((st.get(friendly) or {}).get("at", 0) or 0)
    except Exception:
        last = 0.0
    if now - last < REAUTH_COOLDOWN_S:
        out["action"] = "cooldown_skip"
        out["cooldown_remaining_s"] = int(REAUTH_COOLDOWN_S - (now - last))
        return out

    # stamp BEFORE launching so a crash mid-login can't re-fire windows/texts every cycle
    st[friendly] = {"at": now, "account": account, "seed": bool(seed)}
    try:
        REAUTH_STATE.parent.mkdir(parents=True, exist_ok=True)
        REAUTH_STATE.write_text(json.dumps(st, indent=1))
    except Exception:
        pass

    no_launch = os.environ.get("OPS_HEALTH_REAUTH_NO_LAUNCH") == "1"
    if seed:
        out["action"] = "headless_auto"          # silent, zero-touch
        if not no_launch:
            _launch_reauth(friendly, account)
    else:
        out["action"] = "visible_window_plus_alert"
        if not no_launch:
            _launch_reauth(friendly, account)
        if alert:
            out["alerted"] = _route_reauth_alert(friendly, account)
    return out

# lane statuses — graded on real output, never on exit code
PRODUCING = "producing"   # real artifact produced in-window — healthy
IDLE_OK = "idle_ok"       # loaded, nothing to produce this cycle — honest, NOT a fault
STALLED = "stalled"       # should be producing, isn't — THE fault we heal
ABSENT = "absent"         # no job / no artifact — informational, never healed/alerted
HEALTHY_SET = {PRODUCING, IDLE_OK, ABSENT}


# ─────────────────────────────────────────────────────── low-level probes
def _sh(cmd: list[str], timeout: int = 12) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _launchd_loaded(label: str) -> bool:
    """loaded? — used ONLY to know whether a heal target exists, never as a health signal."""
    r = _sh(["launchctl", "list"])
    if not r:
        return False
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == label:
            return True
    return False


def _age_h(path: Path) -> float:
    """hours since mtime; +inf if absent."""
    try:
        return (time.time() - path.stat().st_mtime) / 3600.0
    except Exception:
        return float("inf")


def _iso_age_h(ts: str) -> float:
    """hours since an ISO-8601 timestamp; +inf on parse failure."""
    if not ts:
        return float("inf")
    try:
        s = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return float("inf")


def _tail(path: Path, n: int = 120) -> list[str]:
    try:
        return path.read_text(errors="ignore").splitlines()[-n:]
    except Exception:
        return []


_LOGIN_MARKERS = (
    "login", "log in", "logged out", "authwall", "checkpoint", "captcha",
    "session expired", "re-auth", "reauth", "2fa", "mfa", "verify your identity",
    "unauthorized", "401", "403", "token expired", "credentials", "sign in",
)


def _needs_owner(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _LOGIN_MARKERS)


def _load_json(path: Path):
    """Read + parse a JSON state file. None on missing/broken — callers map None -> ABSENT."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _daily_newest_age_h(daily: dict) -> float:
    """Hours since UTC-midnight of the newest YYYY-MM-DD key in a {date: count/obj} map.
    +inf if the map is empty or has no date keys."""
    if not isinstance(daily, dict) or not daily:
        return float("inf")
    dates = [str(k) for k in daily.keys() if _DATE_RE.match(str(k))]
    if not dates:
        return float("inf")
    try:
        dt = datetime.fromisoformat(max(dates) + "T00:00:00+00:00")
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return float("inf")


def _epoch_age_h(secs) -> float:
    """Hours since an epoch-seconds timestamp; +inf on bad input."""
    try:
        return (time.time() - float(secs)) / 3600.0
    except Exception:
        return float("inf")


# ─────────────────────────────────────────────────────── lane model
@dataclass
class Lane:
    name: str
    check: "callable"          # () -> LaneResult
    heal_label: str | None = None   # launchd job to kickstart as the generic (novel) heal
    # what to escalate if the lane stays stalled after every silent heal is exhausted. Only a
    # lane whose residual fault is genuinely an owner-only action carries one; everything else
    # stays silent forever (ledger only).
    owner_action_hint: str | None = None


@dataclass
class LaneResult:
    status: str
    detail: str
    signature: str = ""        # human blob fed to never_twice for stalled lanes
    field_extra: dict = field(default_factory=dict)


def _ok(status, detail, signature=""):
    return LaneResult(status, detail, signature)


# ─────────────────────────────────────────────────────── OUTCOME checks
# Each grades the lane's REAL artifact. A stalled result carries a signature describing the
# fault so never_twice can match a known fix/acceptance.

def _last_result_line(log_lines: list[str]) -> dict | None:
    """Newest `result: {json}` line from a poster log, parsed. None if none found."""
    for ln in reversed(log_lines):
        m = re.search(r"result:\s*(\{.*\})\s*$", ln)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                return None
    return None


def _c_tiktok() -> LaneResult:
    """OUTCOME: did a video actually publish? The poster exits 0 while HELD on hydration
    timeout, so we IGNORE the exit code and read (a) the state file's last real post ts and
    (b) the newest terminal result in the post log."""
    state = HUSTLE / "hailports_tiktok" / "state.json"
    log = HUSTLE / "logs" / "hailports_tiktok.log"
    window_h = 30.0  # poster runs several times/day; a healthy lane posts within ~a day

    last_post_age = float("inf")
    try:
        s = json.loads(state.read_text())
        posted = s.get("posted") or []
        if posted:
            last_post_age = min(
                _iso_age_h(str(posted[-1].get("ts", ""))),
                _iso_age_h(str(datetime.fromtimestamp(
                    s.get("last_post_ts", 0), timezone.utc).isoformat()))
                if s.get("last_post_ts") else float("inf"),
            )
    except Exception:
        pass

    if last_post_age <= window_h:
        return _ok(PRODUCING, f"tiktok posted {last_post_age:.1f}h ago")

    res = _last_result_line(_tail(log))
    if res is not None and res.get("state") == "posted" and res.get("ok"):
        return _ok(PRODUCING, "tiktok log: last terminal result = posted")

    reason = (res or {}).get("reason", "no recent post; no terminal result in log")
    # THE TRAP: exit 0 but the newest real result is a failure (hydration timeout / HELD).
    sig = f"tiktok poster not publishing — held: {reason}"
    return _ok(STALLED, f"tiktok NOT posting (last post {last_post_age if last_post_age!=float('inf') else '>window'}h); "
                        f"newest result: {reason}", sig)


def _c_youtube() -> LaneResult:
    """OUTCOME: did a short actually upload? Read the uploader's result ledger, not exit code."""
    up = DATA / "runtime" / "youtube" / "studio_uploads.jsonl"
    queue = DATA / "runtime" / "youtube" / "upload_queue.jsonl"
    window_h = 48.0
    lines = _tail(up, 40)
    last_ok_age = float("inf")
    last_reason = "no upload rows"
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        last_reason = r.get("reason", r.get("state", ""))
        if r.get("ok") and r.get("state") in ("posted", "uploaded", "published"):
            last_ok_age = _age_h(up)  # proxy: file mtime as the freshness of the last write
            break
        break  # newest row decides
    if last_ok_age <= window_h:
        return _ok(PRODUCING, f"youtube last upload ok ({last_ok_age:.1f}h)")
    queued = queue.exists() and queue.stat().st_size > 0
    if not queued:
        return _ok(IDLE_OK, "youtube: empty upload queue, nothing to publish")
    sig = f"youtube uploader not publishing — {last_reason}"
    return _ok(STALLED, f"youtube queue non-empty but not uploading; newest: {last_reason}", sig)


def _c_linkedin() -> LaneResult:
    """OUTCOME: last run's real result. 'nothing publishable' is IDLE_OK (honest — an empty
    queue is not a fault, the reaction_grader lesson). A run that errored, or a stale runs
    ledger, is the fault."""
    runs = HUSTLE / "hailports_linkedin_runs.jsonl"
    window_h = 26.0
    if _age_h(runs) > window_h * 3:
        return _ok(STALLED, f"linkedin runs ledger stale ({_age_h(runs):.1f}h) — lane not running",
                   "linkedin lane not running — runs ledger stale")
    last = None
    for ln in reversed(_tail(runs, 10)):
        try:
            last = json.loads(ln)
            break
        except Exception:
            continue
    if not last:
        return _ok(ABSENT, "linkedin: no parseable runs yet")
    if last.get("posted"):
        return _ok(PRODUCING, f"linkedin posted {len(last['posted'])} in last run")
    skipped = last.get("skipped") or []
    if any("nothing publishable" in str(sk.get("reason", "")) for sk in skipped):
        # ANTI-MASK (the reaction_grader lesson, inverted): an empty queue is IDLE_OK only if the
        # lane actually PRODUCED recently. A generator that leaves the queue empty forever is a
        # fault wearing idle's costume. Grade on the company page's real post state, not the run.
        # hailports_li_page owns the single owner-escalation, so this branch stays heal-only.
        stt = _load_json(HUSTLE / "hailports_linkedin_state.json") or {}
        published = int(stt.get("posts_published") or 0)
        recent = stt.get("recent_posts") or []
        _last = recent[-1] if recent else None
        _last_ts = _last.get("ts", "") if isinstance(_last, dict) else (_last if isinstance(_last, str) else "")
        last_real = _iso_age_h(str(_last_ts)) if _last_ts else float("inf")
        if published > 0 and last_real <= 48.0:
            return _ok(IDLE_OK, "linkedin: nothing publishable (empty queue; real post within 48h)")
        return _ok(STALLED, "linkedin content-starved — queue empty >48h, generator not filling it",
                   "linkedin content-starved — queue empty >48h, generator not filling it")
    if last.get("error") or last.get("blocked"):
        sig = f"linkedin run error/blocked: {last.get('error') or last.get('blocked')}"
        return _ok(STALLED, sig, sig)
    return _ok(IDLE_OK, "linkedin ran, no post this cycle")


def _c_searxng() -> LaneResult:
    """OUTCOME: does the local SearXNG actually RETURN results? (Not 'is the port open'.)"""
    url = ("http://127.0.0.1:8890/search?q=coffee+shop&format=json"
           "&engines=bing,mojeek,qwant,duckduckgo")
    try:
        with urllib.request.urlopen(url, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        n = len(data.get("results", []))
    except Exception as e:
        return _ok(STALLED, f"searxng unreachable: {e}",
                   "searxng docker down — unreachable, no results (colima)")
    if n > 0:
        return _ok(PRODUCING, f"searxng returned {n} results")
    return _ok(STALLED, "searxng dry — 0 results",
               "searxng dry 0 results — docker/colima container down")


def _c_colima() -> LaneResult:
    """OUTCOME: is the docker VM up? `colima status` -> 'colima is running'."""
    r = _sh(["colima", "status"], timeout=20)
    blob = ((r.stdout if r else "") + (r.stderr if r else "")).lower()
    if r and "running" in blob:
        return _ok(PRODUCING, "colima running")
    return _ok(STALLED, "colima not running",
               "colima docker down not running — restart the colima VM")


def _c_email_triage() -> LaneResult:
    """OUTCOME: is the triage state advancing? Freshness of the seen-ids ledger."""
    st = DATA / "triage_state.json"
    window_h = 12.0
    if not st.exists():
        return _ok(ABSENT, "no triage_state.json")
    age = _age_h(st)
    if age <= window_h:
        return _ok(PRODUCING, f"email triage state fresh ({age:.1f}h)")
    loaded = _launchd_loaded("com.claude-stack.email-triage") or \
        _launchd_loaded("com.Operator.outlook-triage")
    sig = f"email triage stale ({age:.1f}h) — not sweeping"
    return _ok(STALLED if loaded else ABSENT, sig, sig)


def _c_work_rag() -> LaneResult:
    """OUTCOME: is the work-lane RAG index fresh enough to answer? mtime of the sqlite."""
    idx = LEARN / "work_rag.sqlite"
    window_h = 30.0
    if not idx.exists():
        return _ok(ABSENT, "work_rag.sqlite not built")
    age = _age_h(idx)
    if age <= window_h:
        return _ok(PRODUCING, f"work RAG index fresh ({age:.1f}h)")
    return _ok(STALLED, f"work RAG index stale ({age:.1f}h) — reindex not running",
               "work rag index stale — rag.py index not refreshing")


def _c_content_factory() -> LaneResult:
    """OUTCOME: is the content factory emitting? Freshness of its render output tree."""
    samples = DATA / "runtime" / "content_edge_samples"
    window_h = 48.0
    if not samples.exists():
        return _ok(ABSENT, "no content_edge_samples output")
    age = _age_h(samples)
    if age <= window_h:
        return _ok(PRODUCING, f"content factory output fresh ({age:.1f}h)")
    return _ok(STALLED, f"content factory output stale ({age:.1f}h)",
               "content factory not producing — daily_content_factory stale")


# ─────────────────────────────────────────────────────── OUTCOME checks (social/growth lanes)
def _c_linkedin_own_post() -> LaneResult:
    """OUTCOME: did Operator's own LinkedIn lane post? Newest post_log[-1].ts."""
    d = _load_json(HUSTLE / "linkedin_lane_state.json")
    if d is None:
        return _ok(ABSENT, "no linkedin_lane_state.json")
    log = d.get("post_log") or []
    if not log:
        return _ok(STALLED, "linkedin own-post log empty — never posted",
                   "linkedin own personal post lane empty — no post_log entries")
    age = _iso_age_h(str(log[-1].get("ts", "")))
    if age <= 30.0:
        return _ok(PRODUCING, f"linkedin own post {age:.1f}h ago")
    return _ok(STALLED, f"linkedin own post stale ({age:.1f}h > 30h) — not posting",
               "linkedin own personal post lane stalled — last post_log entry >30h")


def _c_hailports_li_page() -> LaneResult:
    """OUTCOME: has the @hailports COMPANY page ever posted? recent_posts[-1].ts + posts_published.
    Armed+running with 0 posts ever is the trap — reads STALLED, not idle."""
    st = HUSTLE / "hailports_linkedin_state.json"
    runs = HUSTLE / "hailports_linkedin_runs.jsonl"
    window_h = 48.0
    d = _load_json(st)
    if d is None:
        return _ok(ABSENT, "no hailports_linkedin_state.json")
    published = int(d.get("posts_published") or 0)
    recent = d.get("recent_posts") or []
    # recent_posts entries are usually {"ts": ...} dicts but some rows are bare ISO strings —
    # `.get` on a str raised AttributeError and the check fell to a fake STALLED (instrument lying).
    _last = recent[-1] if recent else None
    if isinstance(_last, dict):
        last_age = _iso_age_h(str(_last.get("ts", "")))
    elif isinstance(_last, str):
        last_age = _iso_age_h(_last)
    else:
        last_age = float("inf")
    if published > 0 and last_age <= window_h:
        return _ok(PRODUCING, f"hailports LI page posted {last_age:.1f}h ago ({published} total)")
    running = _age_h(runs) <= window_h
    sig = "hailports LinkedIn company page armed+running but 0 posts ever — no draft approved"
    return _ok(STALLED,
               f"hailports LI page NOT posting (published={published}, "
               f"{'running' if running else 'runs ledger stale'}); no post within {window_h:.0f}h",
               sig)


def _c_linkedin_deck() -> LaneResult:
    """OUTCOME: did a gate-passing carousel deck actually post? Parse the log for `posted: true`."""
    log = BASE_DIR / "logs" / "linkedin_deck.log"
    window_h = 30.0
    if not log.exists():
        return _ok(ABSENT, "no linkedin_deck.log")
    age = _age_h(log)
    txt = "\n".join(_tail(log, 400))
    posted_true = re.search(r'"posted"\s*:\s*true', txt) is not None
    if posted_true and age <= window_h:
        return _ok(PRODUCING, f"linkedin-deck carousel posted (log {age:.1f}h)")
    if not posted_true:
        return _ok(STALLED, "linkedin-deck: no gate-passing posted carousel in log — deck queue unfed",
                   "linkedin-deck carousels no gate-passing draft ever — deck queue empty/unfed")
    return _ok(STALLED, f"linkedin-deck log stale ({age:.1f}h > 30h) — not producing carousels",
               "linkedin-deck stale — carousel deck not running")


def _c_tiktok_engage() -> LaneResult:
    """OUTCOME: a posted tiktok COMMENT. The governor is the comment ledger; the state file's
    mtime only proves the process ran (it touches seen/ on aborted no-browser runs) — never that
    a comment landed. So grade on the governor's tiktok:comment event, state mtime as last resort."""
    state = HUSTLE / "hailports_tiktok_engage_state.json"
    gov = HUSTLE / "social_governor_state.json"
    window_h = 48.0
    if not state.exists() and not gov.exists():
        return _ok(ABSENT, "no tiktok engage state/governor")
    comment_age = float("inf")
    g = _load_json(gov)
    if g:
        ev = (g.get("events") or {}).get("tiktok:comment") or []
        if ev:
            comment_age = _epoch_age_h(ev[-1])
    if comment_age == float("inf"):
        comment_age = _age_h(state)
    if comment_age <= window_h:
        return _ok(PRODUCING, f"tiktok engage commented {comment_age:.1f}h ago")
    sig = "tiktok engage not commenting — no CDP browser @18802 (profile contention), aborting"
    return _ok(STALLED, f"tiktok engage NOT commenting (last comment {comment_age:.1f}h > {window_h:.0f}h)", sig)


def _c_tiktok_lite() -> LaneResult:
    """OUTCOME: newest sent_<YYYY-MM-DD> bucket in the tt-lite commenter state."""
    d = _load_json(HUSTLE / "hailports_tt_lite_state.json")
    if d is None:
        return _ok(ABSENT, "no hailports_tt_lite_state.json")
    daily = {str(k)[5:]: v for k, v in d.items() if str(k).startswith("sent_")}
    age = _daily_newest_age_h(daily)
    if age <= 30.0:
        return _ok(PRODUCING, f"tt-lite commented (newest sent bucket {age:.1f}h)")
    return _ok(STALLED, f"tt-lite stale (newest sent bucket {age:.1f}h > 30h)",
               "tt-lite commenter not sending — newest sent_<date> bucket stale")


def _c_hailports_x_video() -> LaneResult:
    """OUTCOME: did a short post to @hailports on X? last_post_ts (epoch) and/or daily newest date."""
    d = _load_json(HUSTLE / "hailports_x_video_state.json")
    if d is None:
        return _ok(ABSENT, "no hailports_x_video_state.json")
    ts_age = _epoch_age_h(d["last_post_ts"]) if d.get("last_post_ts") else float("inf")
    daily_age = _daily_newest_age_h(d.get("daily") or {})
    age = min(ts_age, daily_age)
    if age <= 30.0:
        return _ok(PRODUCING, f"hailports X-video posted {age:.1f}h ago")
    return _ok(STALLED, f"hailports X-video NOT posting (last {age:.1f}h > 30h) — no un-posted shorts",
               "hailports x-video no un-posted shorts in queue — shorts pipeline unfed")


def _c_x_engage() -> LaneResult:
    """OUTCOME: @hailports X device-engage verified. Newest JSON line with ok+verified+ts."""
    out = Path(os.path.expanduser("~/.openclaw/workspace/android-logs/persona1-x-engage.out"))
    window_h = 14.0
    if not out.exists():
        return _ok(ABSENT, "no persona1-x-engage.out")
    for ln in reversed(_tail(out, 200)):
        m = re.search(r"(\{.*\})", ln)
        if not m:
            continue
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if "ok" not in d and "ts" not in d:
            continue
        age = _iso_age_h(str(d.get("ts", "")))
        if d.get("ok") and d.get("verified") and age <= window_h:
            return _ok(PRODUCING, f"@hailports X engage verified {age:.1f}h ago")
        return _ok(STALLED, f"X engage not verified/fresh (age {age:.1f}h, ok={d.get('ok')})",
                   "hailports X engage device session flaky (adb/composer) — re-check")
    return _ok(STALLED, "X engage: no result line in log",
               "hailports X engage no verified result line — device session flaky")


def _c_x_intel() -> LaneResult:
    """OUTCOME: harvester freshness (a MINER, not a poster). Raw jsonl mtime."""
    f = HUSTLE / "x_intel_raw.jsonl"
    if not f.exists():
        return _ok(ABSENT, "no x_intel_raw.jsonl")
    age = _age_h(f)
    if age <= 30.0:
        return _ok(PRODUCING, f"x-intel harvest fresh ({age:.1f}h)")
    return _ok(STALLED, f"x-intel harvest stale ({age:.1f}h > 30h) — miner not harvesting",
               "x-intel miner not harvesting — raw jsonl stale")


def _c_youtube_engage() -> LaneResult:
    """OUTCOME: newest day in the youtube-engage daily map (likes/comments)."""
    d = _load_json(HUSTLE / "hailports_youtube_engage_state.json")
    if d is None:
        return _ok(ABSENT, "no hailports_youtube_engage_state.json")
    age = _daily_newest_age_h(d.get("daily") or {})
    if age <= 30.0:
        return _ok(PRODUCING, f"youtube engage active (newest day {age:.1f}h)")
    return _ok(STALLED, f"youtube engage stale (newest day {age:.1f}h > 30h)",
               "youtube engage not liking/commenting — daily bucket stale")


def _c_reddit_warmer() -> LaneResult:
    """OUTCOME: newest post across every account's daily map. A session-dead account (dead login)
    is a silent posting failure (not_logged_in) and takes priority — that's an owner re-login."""
    d = _load_json(HUSTLE / "reddit_warmer_state.json")
    if d is None:
        return _ok(ABSENT, "no reddit_warmer_state.json")
    accts = d.get("accounts") or {}
    session_dead = sorted(n for n, a in accts.items()
                          if str((a or {}).get("flag_reason", "")).lower() == "session")
    if session_dead:
        names = ", ".join(session_dead)
        return _ok(STALLED,
                   f"reddit warmer: {len(session_dead)} account(s) session-dead ({names}) — posting silently fails",
                   f"reddit warmer session-dead accounts: {names} — not_logged_in")
    newest = float("inf")
    for a in accts.values():
        newest = min(newest, _daily_newest_age_h((a or {}).get("daily") or {}))
    if newest <= 30.0:
        return _ok(PRODUCING, f"reddit warmer active (newest post {newest:.1f}h)")
    return _ok(STALLED, f"reddit warmer stale (newest post {newest:.1f}h > 30h)",
               "reddit warmer not posting — daily buckets stale")


def _c_redditgrow() -> LaneResult:
    """OUTCOME: board freshness (drafts by design — PRODUCING if the board is fresh)."""
    f = HUSTLE / "REDDITGROW_BOARD.md"
    if not f.exists():
        return _ok(ABSENT, "no REDDITGROW_BOARD.md")
    age = _age_h(f)
    if age <= 4.0:
        return _ok(PRODUCING, f"redditgrow board fresh ({age:.1f}h) — drafts by design")
    return _ok(STALLED, f"redditgrow board stale ({age:.1f}h > 4h) — generator not refreshing",
               "redditgrow board stale — draft generator not running")


def _c_social_orchestrator() -> LaneResult:
    """OUTCOME: heartbeat ts freshness (the scheduler must tick every cycle)."""
    d = _load_json(HUSTLE / "social_orchestrator_heartbeat.json")
    if d is None:
        return _ok(ABSENT, "no social_orchestrator_heartbeat.json")
    age = _iso_age_h(str(d.get("ts", "")))
    if age <= 1.0:
        return _ok(PRODUCING, f"social orchestrator heartbeat {age:.1f}h ago")
    return _ok(STALLED, f"social orchestrator heartbeat stale ({age:.1f}h > 1h)",
               "social orchestrator heartbeat stale — scheduler not ticking")


def _c_social_crosspost() -> LaneResult:
    """OUTCOME: @redacted crosspost. All-blocked (or no log at all) = posters/sessions never
    built — an owner build/disable decision, no heal target."""
    log = BASE_DIR / "logs" / "social-crosspost.log"
    if not log.exists():
        return _ok(STALLED, "social-crosspost log absent — posters/sessions never built",
                   "social-crosspost @redacted never built — no log, no poster session")
    lines = [l for l in _tail(log, 60) if l.strip()]
    if not lines:
        return _ok(STALLED, "social-crosspost log empty — never ran",
                   "social-crosspost @redacted never ran — empty log")
    state_lines = [l for l in lines if '"state"' in l]
    blocked = [l for l in state_lines if '"state": "blocked"' in l or '"state":"blocked"' in l]
    if state_lines and len(blocked) == len(state_lines):
        return _ok(STALLED, "social-crosspost: all runs blocked (no poster/session)",
                   "social-crosspost @redacted all-blocked — posters/sessions never built")
    return _ok(PRODUCING, "social-crosspost producing (non-blocked runs present)")


def _c_hailports_amplify() -> LaneResult:
    """OUTCOME: a real fanout post. Newest of amplify_x_counts date + the last fanout line that
    actually carries a posted[].url."""
    st = HUSTLE / "amplify_state.json"
    fan = HUSTLE / "amplify_fanout.jsonl"
    window_h = 48.0
    if not st.exists() and not fan.exists():
        return _ok(ABSENT, "no amplify state/fanout")
    d = _load_json(st) or {}
    counts_age = _daily_newest_age_h(d.get("amplify_x_counts") or {})
    fan_age = float("inf")
    for ln in reversed(_tail(fan, 40)):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if any(isinstance(p, dict) and p.get("url") for p in (r.get("posted") or [])):
            fan_age = _iso_age_h(str(r.get("ts", "")))
            break
    age = min(counts_age, fan_age)
    if age <= window_h:
        return _ok(PRODUCING, f"hailports amplify fanned out {age:.1f}h ago")
    return _ok(STALLED, f"hailports amplify NOT fanning (last real post {age:.1f}h > {window_h:.0f}h)",
               "hailports amplify no real fanout post — content-starved / reddit drain dead")


def _c_engagement_tracker() -> LaneResult:
    """OUTCOME: engagement-received ledger freshness (thin lane — PRODUCING if fresh)."""
    f = HUSTLE / "ima_engagement_received.jsonl"
    if not f.exists():
        return _ok(ABSENT, "no ima_engagement_received.jsonl")
    age = _age_h(f)
    if age <= 8.0:
        return _ok(PRODUCING, f"engagement tracker fresh ({age:.1f}h)")
    return _ok(STALLED, f"engagement tracker stale ({age:.1f}h > 8h)",
               "engagement tracker not recording — jsonl stale")


def _c_reengage_sender() -> LaneResult:
    """By design the queue is perpetually empty (nothing to send) — honest IDLE_OK, never STALLED."""
    return _ok(IDLE_OK, "reengage sender: no scan-and-leave dropouts — nothing to send, by design")


# ─────────────────────────────────────────────────────── OUTCOME checks (revenue / money lanes)
# THE ANTI-MASK RULE (same lesson as _c_linkedin's empty-queue branch): a money lane is graded on
# the POLLER / AGGREGATOR / SENDER staying ALIVE, never on the dollar count. $0 revenue behind a
# fresh poller = no demand = honest, NOT a fault. A stall = the machinery stopped producing/polling.
def _dotenv_missing(*keys) -> list[str]:
    """Which of these keys are absent from both the process env and the repo .env. Read-only —
    never mutates os.environ (the poller loads .env itself; this is only to name a token fault)."""
    have = {k for k in keys if os.environ.get(k)}
    try:
        for line in (BASE_DIR / ".env").read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                if k.strip() in keys and v.strip():
                    have.add(k.strip())
    except Exception:
        pass
    return [k for k in keys if k not in have]


def _c_revenue_scoreboard() -> LaneResult:
    """OUTCOME (ANTI-MASK): the AGGREGATOR heartbeat, never the dollar count. Newest row ts, file
    mtime as fallback. A stall = the scoreboard job stopped writing rows — not that nothing sold."""
    f = HUSTLE / "revenue_scoreboard.jsonl"
    window_h = 16.0
    if not f.exists():
        return _ok(ABSENT, "no revenue_scoreboard.jsonl")
    ts_age = float("inf")
    for ln in reversed(_tail(f, 5)):
        try:
            ts_age = _iso_age_h(str(json.loads(ln).get("ts", "")))
            break
        except Exception:
            continue
    age = min(ts_age, _age_h(f))
    if age <= window_h:
        return _ok(PRODUCING, f"revenue scoreboard heartbeat {age:.1f}h ago")
    return _ok(STALLED, f"revenue scoreboard stale ({age:.1f}h > {window_h:.0f}h) — aggregator not writing",
               "revenue scoreboard aggregator stale — no new heartbeat row")


def _c_stripe_fulfillment() -> LaneResult:
    """OUTCOME (ANTI-MASK): the POLLER's liveness (updated_at), never the fulfilled count. Zero paid
    sessions behind a fresh poll = no sales = fine. Fault = the poller stopped. Only irreducible
    owner-action is a missing STRIPE/RESEND key (named in the signature when that's the residual)."""
    f = HUSTLE / "stripe_fulfillment_state.json"
    window_h = 2.0
    d = _load_json(f)
    if d is None:
        return _ok(ABSENT, "no stripe_fulfillment_state.json")
    age = _iso_age_h(str(d.get("updated_at", "")))
    if age <= window_h:
        return _ok(PRODUCING, f"stripe fulfillment poller alive ({age:.1f}h)")
    missing = _dotenv_missing("STRIPE_SECRET_KEY", "RESEND_API_KEY")
    if missing:
        return _ok(STALLED, f"stripe fulfillment poller stale ({age:.1f}h) — missing {','.join(missing)}",
                   f"stripe fulfillment poller stalled — token missing: {','.join(missing)}")
    return _ok(STALLED, f"stripe fulfillment poller stale ({age:.1f}h > {window_h:.0f}h) — not polling",
               "stripe fulfillment poller stale — updated_at not advancing")


def _c_intent_service_deliver() -> LaneResult:
    """OUTCOME (ANTI-MASK): the PAID $99/mo fulfillment worker's liveness (heartbeat updated_at),
    never the delivered count. Zero active subscribers behind a fresh heartbeat = no demand = fine.
    Fault = the hourly worker stopped running (crash / exit-0-delivered-nothing). Only irreducible
    owner-action is a missing STRIPE/RESEND key. Mirrors _c_stripe_fulfillment's poller-liveness."""
    f = HUSTLE / "intent_service_heartbeat.json"
    window_h = 2.0  # hourly job; tolerate one missed run
    d = _load_json(f)
    if d is None:
        return _ok(ABSENT, "no intent_service_heartbeat.json yet")
    age = _iso_age_h(str(d.get("updated_at", "")))
    if age <= window_h:
        return _ok(PRODUCING, f"intent-service worker alive ({age:.1f}h)")
    missing = _dotenv_missing("STRIPE_SECRET_KEY", "RESEND_API_KEY")
    if missing:
        return _ok(STALLED, f"intent-service worker stale ({age:.1f}h) — missing {','.join(missing)}",
                   f"intent-service fulfillment stalled — token missing: {','.join(missing)}")
    return _ok(STALLED, f"intent-service worker stale ({age:.1f}h > {window_h:.0f}h) — not running",
               "intent-service fulfillment worker stale — heartbeat not advancing")


def _c_self_serve_funnel() -> LaneResult:
    """OUTCOME: the funnel SERVER is live (HTTP 200) and events are being recorded. Zero human
    conversion is NOT a fault (no demand) → IDLE_OK. The only fault is the server being down."""
    events = BASE_DIR / "products" / "self_serve" / "funnel_events.jsonl"
    window_h = 6.0
    try:
        with urllib.request.urlopen("http://127.0.0.1:8300/", timeout=8) as r:
            code = r.getcode()
    except Exception as e:
        return _ok(STALLED, f"self-serve funnel server down: {e}",
                   "self-serve funnel app @8300 down — not serving")
    if code != 200:
        return _ok(STALLED, f"self-serve funnel server returned {code}",
                   f"self-serve funnel app @8300 returned {code} — not serving 200")
    age = _age_h(events)
    if age <= window_h:
        return _ok(PRODUCING, f"self-serve funnel live (200), events {age:.1f}h ago")
    return _ok(IDLE_OK, f"self-serve funnel live (200), no events in {age:.1f}h — no traffic, not a fault")


def _c_broken_site_sender() -> LaneResult:
    """OUTCOME: a REAL send — newest {"ok":true} ts in broken_site_sent.jsonl. (The dead
    broken_site_sender_state.json counter has been stuck since 2026-06-08 — deliberately NOT used.)
    A stall = fresh prospects to send but no send in 48h. Prospect-starved = owner/enrichment."""
    sent = HUSTLE / "broken_site_sent.jsonl"
    prospects = HUSTLE / "broken_site_prospects.jsonl"
    window_h = 48.0
    if not sent.exists():
        return _ok(ABSENT, "no broken_site_sent.jsonl")
    send_age = float("inf")
    for ln in reversed(_tail(sent, 60)):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("ok"):
            send_age = _iso_age_h(str(r.get("ts", "")))
            break
    if send_age <= window_h:
        return _ok(PRODUCING, f"broken-site sender sent {send_age:.1f}h ago")
    shown = f"{send_age:.1f}" if send_age != float("inf") else ">window"
    if _age_h(prospects) <= 24.0:
        return _ok(STALLED,
                   f"broken-site sender NOT sending ({shown}h) despite fresh prospects (<24h)",
                   "broken-site sender stalled — fresh prospect queue but no send >48h")
    return _ok(STALLED,
               f"broken-site sender idle ({shown}h) — prospect queue also stale (upstream starvation)",
               "broken-site sender starved — no fresh prospects to send (enrichment/deliverability)")


def _c_broken_site_proof_queue() -> LaneResult:
    """OUTCOME: the discovery job refreshing the prospect queue. mtime of the prospects jsonl."""
    f = HUSTLE / "broken_site_prospects.jsonl"
    window_h = 30.0
    if not f.exists():
        return _ok(ABSENT, "no broken_site_prospects.jsonl")
    age = _age_h(f)
    if age <= window_h:
        return _ok(PRODUCING, f"broken-site proof queue fresh ({age:.1f}h)")
    return _ok(STALLED, f"broken-site proof queue stale ({age:.1f}h > {window_h:.0f}h) — discovery not running",
               "broken-site prospect discovery stale — prospects jsonl not refreshing")


def _c_geo_blitz() -> LaneResult:
    """No launchd job arms this lane (arming it is an owner decision) — ABSENT by design, never
    healed, never stalled."""
    f = HUSTLE / "geo_blitz_campaign.jsonl"
    if f.exists():
        return _ok(ABSENT, f"geo-blitz not armed — no launchd job (last campaign file {_age_h(f):.1f}h)")
    return _ok(ABSENT, "geo-blitz not armed — no launchd job, no campaign file")


def _c_ai_visibility_index() -> LaneResult:
    """OUTCOME: proof freshness — newest mtime under the index dir. No kickstart target: a stale
    index is FLAGGED (owner/enrichment refresh), never auto-healed, so it carries no owner-action."""
    d = HUSTLE / "ai_visibility_index"
    window_h = 72.0
    if not d.exists():
        return _ok(ABSENT, "no ai_visibility_index dir")
    newest = max((p.stat().st_mtime for p in d.glob("*") if p.is_file()), default=0.0)
    age = (time.time() - newest) / 3600.0 if newest else float("inf")
    if age <= window_h:
        return _ok(PRODUCING, f"ai-visibility index fresh ({age:.1f}h)")
    return _ok(STALLED, f"ai-visibility index stale ({age:.1f}h > {window_h:.0f}h) — proof aging, no refresh job",
               "ai-visibility index stale — no refresh job (owner/enrichment)")


def _c_hailports_outreach() -> LaneResult:
    """Gated by design: HAILPORT_OFF present = intentionally halted → IDLE_OK, NEVER stalled."""
    if (HUSTLE / "HAILPORT_OFF").exists():
        return _ok(IDLE_OK, "hailports outreach gated OFF by design (HAILPORT_OFF present)")
    return _ok(IDLE_OK, "hailports outreach armed (no HAILPORT_OFF) — send-gated, nothing to grade here")


def _c_maroon_sender() -> LaneResult:
    """Gated by design: MAROON_OFF present = every BrandA cold send halted → IDLE_OK, NEVER stalled."""
    if (HUSTLE / "MAROON_OFF").exists():
        return _ok(IDLE_OK, "BrandA sender gated OFF by design (MAROON_OFF present)")
    return _ok(IDLE_OK, "BrandA sender armed (no MAROON_OFF) — send-gated, nothing to grade here")


# ─────────────────────────────────────────────────────── the manifest
LANES: list[Lane] = [
    Lane("tiktok_poster", _c_tiktok,
         heal_label="com.claude-stack.hailports-tiktok-chrome",
         owner_action_hint="re-login the hailports TikTok CDP session (MFA) — poster is HELD "
                           "on hydration timeout, no video is publishing"),
    Lane("youtube_uploader", _c_youtube,
         heal_label="com.claude-stack.youtube-queue-upload",
         owner_action_hint="re-auth the YouTube Studio upload session (MFA) — queue non-empty, "
                           "nothing uploading"),
    # REPOINTED (Task 2): the dedicated hailports_li_page lane now owns the company-page owner
    # escalation, so this stays heal-only (kickstart) — its starvation/error STALLED must not
    # double-escalate the same fault. owner_action_hint dropped for that reason.
    Lane("linkedin_poster", _c_linkedin,
         heal_label="com.claude-stack.linkedin-deck",
         owner_action_hint=None),
    Lane("searxng", _c_searxng, heal_label=None),          # heal = colima known_fix
    Lane("colima_docker", _c_colima, heal_label=None),     # heal = colima known_fix
    Lane("email_triage", _c_email_triage,
         heal_label="com.claude-stack.email-triage"),
    Lane("work_rag_index", _c_work_rag, heal_label="com.claude-stack.work-rag-refresh"),
    Lane("content_factory", _c_content_factory,
         heal_label="com.claude-stack.content-factory"),
    # ── social / growth lanes (additive) ──
    Lane("linkedin_own_post", _c_linkedin_own_post,
         heal_label="com.claude-stack.social-cycler",
         owner_action_hint="re-check own LinkedIn posting lane"),
    # heal_label = the real LinkedIn cycler job (a kick won't approve a draft, so the owner still
    # escalates in live) — needed because the smoke invariant forbids a stalled no-heal-label lane
    # carrying an owner action (machinery sets held=False for no-heal-label, escalating even in dry).
    Lane("hailports_li_page", _c_hailports_li_page,
         heal_label="com.claude-stack.social-cycler",
         owner_action_hint="approve the queued hailports LinkedIn draft (--drafts/--approve) or "
                           "arm ramp autopilot — company page armed+running but 0 posts ever"),
    Lane("linkedin_deck", _c_linkedin_deck,
         heal_label="com.claude-stack.linkedin-deck",
         owner_action_hint="linkedin-deck carousels: no gate-passing draft ever — feed the deck queue"),
    Lane("tiktok_engage", _c_tiktok_engage,
         heal_label="com.claude-stack.hailports-tiktok-chrome",
         owner_action_hint="TikTok engage aborting: no CDP browser @18802 (profile contention) — "
                           "re-check persona1 CDP session"),
    Lane("tiktok_lite", _c_tiktok_lite,
         heal_label="com.claude-stack.hailports-tt-lite-commenter"),
    Lane("hailports_x_video", _c_hailports_x_video,
         heal_label="com.claude-stack.hailports-x-video",
         owner_action_hint="hailports X-video: no un-posted shorts in queue — feed shorts pipeline"),
    Lane("x_engage", _c_x_engage,
         heal_label="com.claude-stack.persona1-x-engage",
         owner_action_hint="re-check @hailports X engage device session (adb/composer flaky)"),
    Lane("x_intel", _c_x_intel,
         heal_label="com.claude-stack.x-intel-miner"),
    Lane("youtube_engage", _c_youtube_engage,
         heal_label="com.claude-stack.hailports-youtube-engage"),
    Lane("reddit_warmer", _c_reddit_warmer,
         heal_label="com.claude-stack.reddit-warmer",
         owner_action_hint="re-login Reddit account(s) flagged session-dead — posting silently "
                           "fails as not_logged_in (names in ledger)"),
    Lane("redditgrow", _c_redditgrow,
         heal_label="com.claude-stack.redditgrow"),
    Lane("social_orchestrator", _c_social_orchestrator,
         heal_label="com.claude-stack.social-orchestrator"),
    # spec said "no heal_label", but the launchd job DOES exist — kicking it re-runs (still
    # all-blocked), doesn't hold, and the owner still escalates in live. Real label also satisfies
    # the smoke invariant (no-heal-label + owner would escalate in dry).
    Lane("social_crosspost", _c_social_crosspost,
         heal_label="com.claude-stack.social-crosspost",
         owner_action_hint="social-crosspost (@redacted): posters/sessions never built — "
                           "build the session or disable the lane"),
    Lane("hailports_amplify", _c_hailports_amplify,
         heal_label="com.claude-stack.hailports-amplify",
         owner_action_hint="hailports-amplify: no real fanout post since last outcome — "
                           "content-starved / reddit drain dead"),
    Lane("engagement_tracker", _c_engagement_tracker,
         heal_label="com.claude-stack.engagement-tracker"),
    Lane("reengage_sender", _c_reengage_sender, heal_label=None),
    # ── revenue / money lanes (additive; ANTI-MASK: grade poller/aggregator/sender liveness,
    #    NEVER the $ count — $0 behind a fresh poller = no demand = honest, not a stall) ──
    Lane("revenue_scoreboard", _c_revenue_scoreboard,
         heal_label="com.claude-stack.revenue-scoreboard"),
    Lane("stripe_fulfillment", _c_stripe_fulfillment,
         heal_label="com.claude-stack.fulfillment-poller",
         owner_action_hint="restore STRIPE_SECRET_KEY / RESEND_API_KEY — the fulfillment poller "
                           "can't poll Stripe or send receipts without them"),
    Lane("self_serve_funnel", _c_self_serve_funnel,
         heal_label="com.claude-stack.self-serve"),
    Lane("intent_service_deliver", _c_intent_service_deliver,
         heal_label="com.claude-stack.intent-service-deliver",
         owner_action_hint="restore STRIPE_SECRET_KEY / RESEND_API_KEY — the paid intent-service "
                           "fulfillment worker can't pull subscribers or send receipts without them"),
    Lane("broken_site_sender", _c_broken_site_sender,
         heal_label="com.claude-stack.broken-site-outreach",
         owner_action_hint="broken-site sender: fresh prospects but no send — deliverability / "
                           "contact-enrichment starved (check bounce ramp + enrichment)"),
    Lane("broken_site_proof_queue", _c_broken_site_proof_queue,
         heal_label="com.claude-stack.prospect-discovery"),
    # geo_blitz: no launchd job exists — ABSENT by design, never healed (arming is an owner call).
    Lane("geo_blitz", _c_geo_blitz, heal_label=None),
    # ai_visibility_index: no refresh job — a stale index is flagged only (owner/enrichment), so it
    # carries NO owner_action_hint (a no-heal-label stalled lane with a hint would escalate in dry).
    Lane("ai_visibility_index", _c_ai_visibility_index, heal_label=None),
    # hailports_outreach / maroon_sender: gated OFF by design (HAILPORT_OFF / MAROON_OFF) → IDLE_OK.
    Lane("hailports_outreach", _c_hailports_outreach, heal_label=None),
    Lane("maroon_sender", _c_maroon_sender, heal_label=None),
]


# ─────────────────────────────────────────────────────── ledger (silent)
def _ledger(row: dict) -> None:
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **row}
    try:
        with LEDGER.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _approved(action_desc: str) -> tuple[bool, str]:
    """Gate any autonomous mutation through decision_modeling. Fail-safe = don't heal."""
    try:
        from core import decision_modeling
        d = decision_modeling.score(action_desc, {"irreducible_owner_action": False})
        stance = d.get("alex_would", "revise")
        return (stance == "approve", f"{stance}:{d.get('why','')[:80]}")
    except Exception as e:
        # can't consult the model -> don't take an autonomous mutation on a guess
        return (False, f"gate-unavailable:{e}")


def _kickstart(label: str) -> bool:
    r = _sh(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], timeout=30)
    return bool(r and r.returncode == 0)


# ── cross-sweep pending-heal state (fixes the "0/126 held" instant-recheck bug) ──
# A kickstart returns the instant the job is SPAWNED, but almost every lane grades ARTIFACT
# FRESHNESS (mtime windows of hours). Milliseconds after the kick the artifact is still the
# stale pre-kick one, so an instant lane.check() always says STALLED -> held=False even when the
# heal worked and fresh output lands seconds-to-minutes later. Instead we record the heal as
# PENDING and grade it on the NEXT sweep (~10min later), once the revived job has produced.
_PENDING_HEAL_FILE = BASE_DIR / "data" / "learning" / "ops_health_pending_heal.json"
HEAL_GRACE_S = float(os.environ.get("OPS_HEAL_GRACE_S", "") or 20 * 60)


def _pending_load() -> dict:
    try:
        return json.loads(_PENDING_HEAL_FILE.read_text())
    except Exception:
        return {}


def _pending_get(lane_name: str):
    return _pending_load().get(lane_name)


def _pending_set(lane_name: str) -> None:
    try:
        d = _pending_load()
        d[lane_name] = {"ts": time.time()}
        _PENDING_HEAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PENDING_HEAL_FILE.write_text(json.dumps(d))
    except Exception:
        pass


def _pending_clear(lane_name: str) -> None:
    try:
        d = _pending_load()
        if lane_name in d:
            del d[lane_name]
            _PENDING_HEAL_FILE.write_text(json.dumps(d))
    except Exception:
        pass


# ─────────────────────────────────────────────────────── heal one stalled lane
def _heal_lane(lane: Lane, res: LaneResult, *, heal: bool) -> dict:
    """Silent per-lane heal. Returns a ledger-ready dict. Escalation is decided by the caller.

    dry (heal=False): classify + record the PLAN, touch nothing. This is the smoke path.
    heal=True: apply the grounded fix and RE-VERIFY the lane's real outcome (not the heal exit).
    """
    from core import never_twice
    sig = {"source": "ops_health", "subject": lane.name, "body": res.signature or res.detail,
           "issue_key": f"ops-{lane.name}"}
    d = never_twice.seen(sig)
    out = {"lane": lane.name, "status": STALLED, "detail": res.detail,
           "disposition": d["status"], "action": None, "held": None, "owner_action": None,
           "nt_key": d.get("key")}

    # Cross-sweep verify: this lane was healed on a PRIOR sweep and is STILL stalled now.
    #   within grace  -> the revived job may still be regenerating; keep waiting (no re-kick,
    #                    no escalate). This is what lets a slow rag/content job actually finish.
    #   past grace     -> the heal did NOT hold. Escalate the residual once, clear pending so the
    #                    next sweep starts a fresh heal attempt.
    if heal:
        pend = _pending_get(lane.name)
        if pend:
            waited = time.time() - float(pend.get("ts", 0))
            if waited < HEAL_GRACE_S:
                out["action"] = "heal_pending_verify"
                out["held"] = None
                return out
            _pending_clear(lane.name)
            out["action"] = "heal_did_not_hold"
            out["held"] = False
            out["owner_action"] = lane.owner_action_hint
            return out

    if d["status"] == "known_accepted":
        # a decided-fine state — leave it, no heal, no alert.
        out["action"] = "left_accepted"
        out["held"] = True
        return out

    if d["status"] == "known_fix":
        plan = never_twice.apply_fix(d, dry=not heal)
        out["action"] = "known_fix"
        out["plan"] = plan.get("plan")
        if not heal:
            out["held"] = None  # dry — nothing applied
            out["owner_action"] = lane.owner_action_hint  # the target IF the heal doesn't hold
            return out
        # Gate phrasing MUST carry a verb decision_modeling's APPROVE rail recognizes
        # ("kickstart"); the real fix desc stays appended so the send/deploy/money excludes
        # still evaluate the actual command. (Old "auto-heal {lane}:" scored REVISE -> every
        # known_fix, incl. the ONLY heal path for searxng/colima, silently never ran.)
        approved, why = _approved(
            f"kickstart the {lane.name} job to recover it -- "
            f"{plan.get('plan', {}).get('desc', 'known fix')}")
        out["gate"] = why
        if not approved:
            out["action"] = "known_fix_gated_skip"
            out["held"] = False
            out["owner_action"] = lane.owner_action_hint
            return out
        applied = never_twice.apply_fix(d, dry=False)
        out["applied"] = applied.get("applied")
        out["reverify"] = (applied.get("plan") or {}).get("desc")
        verified = applied.get("verified")
        if verified is not None:
            # The fix carries its OWN grounded verifier (e.g. "colima reports running") — that
            # is the honest signal, not a stale instant lane.check(). Trust it.
            out["held"] = bool(verified)
            if not verified:
                out["owner_action"] = lane.owner_action_hint
        elif applied.get("applied"):
            # No verifier attached but heal_cmd ran: defer the grade to the next sweep, when the
            # regenerated artifact is fresh, instead of grading a still-stale one now.
            _pending_set(lane.name)
            out["action"] = "known_fix_pending_verify"
            out["held"] = None
        else:
            out["held"] = False  # heal_cmd itself failed to run -> genuine failure
            out["owner_action"] = lane.owner_action_hint
        return out

    # novel — attempt a generic heal (kickstart the lane's job), then re-verify. record_novel silently.
    never_twice.record_novel(d)
    out["action"] = "novel_generic_heal"
    if not lane.heal_label:
        out["held"] = False
        out["owner_action"] = lane.owner_action_hint
        return out
    if not heal:
        out["plan"] = {"kickstart": lane.heal_label}
        out["held"] = None
        out["owner_action"] = lane.owner_action_hint  # target IF generic heal doesn't hold
        return out
    approved, why = _approved(f"kickstart {lane.heal_label} to recover {lane.name}")
    out["gate"] = why
    if not approved:
        out["action"] = "novel_gated_skip"
        out["held"] = False
        out["owner_action"] = lane.owner_action_hint
        return out
    if not _kickstart(lane.heal_label):
        # Couldn't even spawn the job (disabled / quarantined / nonexistent label) -> real failure.
        out["action"] = "novel_kick_failed"
        out["held"] = False
        out["owner_action"] = lane.owner_action_hint
        return out
    # Kick spawned the job, but its freshness artifact is still stale THIS instant. Defer the
    # held-grade to the next sweep (~10min), which reads the now-regenerated artifact.
    _pending_set(lane.name)
    out["action"] = "novel_heal_pending_verify"
    out["held"] = None
    return out


# ─────────────────────────────────────────────────────── orchestration
def run(heal: bool = False, alert: bool = True) -> dict:
    """Check every lane on its OUTCOME -> silently heal the stalled ones -> re-verify.

    Nothing is reported on success or on a healed fault. The only outward signal is a single
    batched owner-action escalation for lanes that stay stalled and need Operator's MFA/token —
    and only when alert=True (the smoke path passes alert=False).
    """
    rows, heals, owner_actions, reauth_actions = [], [], [], []
    for lane in LANES:
        try:
            res = lane.check()
        except Exception as e:  # a broken check must not take the sweep down
            res = LaneResult(STALLED, f"check errored: {e}", f"{lane.name} check errored: {e}")
        row = {"lane": lane.name, "status": res.status, "detail": res.detail}
        rows.append(row)
        if res.status != STALLED:
            _pending_clear(lane.name)  # healthy now -> a prior heal HELD (or was never broken)
            _ledger({"lane": lane.name, "status": res.status, "detail": res.detail})
            continue
        healed = _heal_lane(lane, res, heal=heal)
        heals.append(healed)
        _ledger(healed)
        # escalate ONLY a verified residual (held is explicitly False, heal-mode) — never a dry
        # plan (held None) and never a lane that self-healed.
        if healed.get("owner_action") and healed.get("held") is False:
            # SESSION SELF-SUFFICIENCY: a dead-login lane first tries to re-auth ITSELF (headless
            # zero-touch if a 2FA seed is enrolled; visible window + ONE alert otherwise) instead
            # of just escalating. Deduped so it can't loop-spam windows/texts every cycle.
            if lane.name in SESSION_REAUTH_LANES:
                rr = _auto_reauth(lane.name, heal=heal, alert=alert)
                if rr is not None:
                    reauth_actions.append(rr)
                    _ledger({"reauth": rr})
                # the auto-reauth OWNS this escalation now — don't also batch the generic owner
                # action (that would double-alert the same dead session).
            else:
                owner_actions.append((lane.name, healed["owner_action"]))

    summary = {s: sum(1 for r in rows if r["status"] == s)
               for s in (PRODUCING, IDLE_OK, STALLED, ABSENT)}
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "healed": heal,
           "summary": summary, "rows": rows, "heals": heals,
           "owner_actions": [{"lane": n, "action": a} for n, a in owner_actions],
           "reauth_actions": reauth_actions}
    try:
        STATE.write_text(json.dumps(out, indent=1))
    except Exception:
        pass

    # THE ONLY ALERT: irreducible owner-actions, batched into one deduped line. Never a stream,
    # never a "green" report. Skipped entirely in dry/smoke (alert=False).
    if alert and heal and owner_actions:
        try:
            from core import alert_gateway
            body = "\n".join(f"- {n}: {a}" for n, a in owner_actions)
            alert_gateway.route("warn", "ops-health",
                                f"{len(owner_actions)} lane(s) need a login/token from you",
                                body, issue_key="ops-health-owner-actions")
        except Exception:
            pass
    return out


# ─────────────────────────────────────────────────────── plist staging (stage, never load)
def stage_plist() -> dict:
    """Write the REAL launchd plist to deploy/ and plutil-lint it. Does NOT load it."""
    py = BASE_DIR / ".venv" / "bin" / "python"
    script = BASE_DIR / "core" / "ops_health.py"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.claude-stack.ops-health</string>
	<key>ProgramArguments</key>
	<array>
		<string>{py}</string>
		<string>{script}</string>
		<string>--heal</string>
	</array>
	<key>WorkingDirectory</key>
	<string>{BASE_DIR}</string>
	<key>StartInterval</key>
	<integer>600</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardOutPath</key>
	<string>{BASE_DIR}/data/logs/ops-health.out</string>
	<key>StandardErrorPath</key>
	<string>{BASE_DIR}/data/logs/ops-health.err</string>
	<key>ProcessType</key>
	<string>Background</string>
</dict>
</plist>
"""
    STAGED_PLIST.parent.mkdir(parents=True, exist_ok=True)
    STAGED_PLIST.write_text(plist)
    lint = _sh(["plutil", "-lint", str(STAGED_PLIST)])
    ok = bool(lint and lint.returncode == 0)
    return {"staged": str(STAGED_PLIST), "plutil_ok": ok,
            "lint": (lint.stdout.strip() if lint else "plutil unavailable"),
            "loaded": False, "note": "staged only — Operator/devops loads it"}


# ─────────────────────────────────────────────────────── smoke (read-only, no live heal)
def _smoke() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")

    print("ops_health smoke — READ-ONLY over the real lanes (no heal, no post)")
    out = run(heal=False, alert=False)
    print(f"  summary: {out['summary']}")
    for r in out["rows"]:
        print(f"    [{r['status']:9}] {r['lane']:18} {r['detail'][:78]}")

    # 1) the sweep ran over every manifest lane
    check("all lanes graded", len(out["rows"]) == len(LANES))

    # 2) THE TRAP LOGIC (the TikTok lesson) — proven on a SYNTHETIC stalled input, decoupled from
    #    whatever the live lanes are doing. (This used to hard-assert the live tiktok_poster was
    #    STALLED; once tiktok started genuinely posting again that made the smoke a broken
    #    instrument — a healthy system failed its own self-test. We now grade the outcome-based
    #    classify+decide LOGIC directly: a fake "exit 0 but held on hydration timeout" outcome must
    #    be classified via never_twice and get a dry heal decision that APPLIES NOTHING.)
    synth_res = LaneResult(
        STALLED, "smoke synthetic: poster exited 0 but held on hydration timeout",
        "smoke-synthetic stalled outcome — trap-detection self-test, poster not publishing (held)")
    synth_lane = Lane("_smoke_synthetic_trap", lambda: synth_res,
                      heal_label="com.claude-stack.nonexistent-smoke-target",
                      owner_action_hint="re-login the (synthetic) session — MFA needed")
    synth_heal = _heal_lane(synth_lane, synth_res, heal=False)
    check("trap logic classified the stalled outcome (never_twice-consulted)",
          synth_heal.get("disposition") in ("known_fix", "known_accepted", "novel"))
    check("dry decision applied NOTHING (held is None = planned, not executed)",
          synth_heal.get("held") is None)
    check("stalled outcome carries an irreducible owner-action for escalation",
          bool(synth_heal.get("owner_action")))

    # 2b) and the live tiktok lane, whatever its current state, is still a HEALTHY-set or STALLED
    #     grade (never a crash / KeyError) — proves the outcome check itself runs clean.
    tk = next((r for r in out["rows"] if r["lane"] == "tiktok_poster"), None)
    check("tiktok lane graded on an outcome status (not a crash)",
          bool(tk) and tk["status"] in (HEALTHY_SET | {STALLED}))

    # 4) HEALTHY lanes produced ZERO alerts / zero heal actions (silent on green).
    healthy = [r for r in out["rows"] if r["status"] in HEALTHY_SET]
    healed_names = {h["lane"] for h in out["heals"]}
    check("healthy lanes triggered no heal action (silent on green)",
          all(r["lane"] not in healed_names for r in healthy))
    check("dry smoke escalated NOTHING (no verified residual in read-only mode)",
          out["owner_actions"] == [])

    # 5) plist stages real paths + lints, and is NOT loaded.
    st = stage_plist()
    check("plist staged + plutil-lint OK", st["plutil_ok"])
    check("plist NOT loaded", st["loaded"] is False)
    check("plist uses the real venv python", (BASE_DIR / ".venv" / "bin" / "python").exists())

    print("SMOKE PASS" if ok else "SMOKE FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--heal", action="store_true", help="apply grounded self-heals + re-verify")
    ap.add_argument("--no-alert", action="store_true", help="suppress owner-action escalation")
    ap.add_argument("--stage-plist", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.smoke:
        return _smoke()
    if a.stage_plist:
        print(json.dumps(stage_plist(), indent=1))
        return 0
    out = run(heal=a.heal, alert=not a.no_alert)
    if a.json:
        print(json.dumps(out))
    else:
        print(f"ops health @ {out['at']}  heal={out['healed']}  " +
              "  ".join(f"{k}={v}" for k, v in out["summary"].items()))
        for r in out["rows"]:
            mark = {PRODUCING: "✓", IDLE_OK: "·", STALLED: "✗", ABSENT: "○"}[r["status"]]
            print(f"  {mark} {r['lane']:<18} {r['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
