#!/usr/bin/env python3
"""never_twice.py — the REPEAT-ERROR layer: the stack never handles the same error twice.

The problem this closes: alerters re-fire the SAME condition on a schedule (gh-watchdog every
6h, searxng/colima whenever the docker VM naps, disk L4 whenever the jump files pile up). Today
each recurrence pages Operator — even for a state we've already DECIDED is fine, or a fix we've run
by hand a dozen times. That violates the SILENT-SELF-CORRECT mandate. This module is the memory
of "we've seen this before, here's what we did", so a recurrence gets APPLIED or MUTED, never
re-paged.

seen(error_signature) -> {status, action, evidence_refs, ...}
  status = "known_fix"       -> a deterministic heal exists; caller applies it silently + verifies
         = "known_accepted"  -> a decided-fine state; caller mutes it so it stops re-firing
         = "novel"           -> genuinely new + unfixable; caller escalates the IRREDUCIBLE
                                owner-action, minimally (and record_novel() so it crystallizes)

Three retrieval tiers, cheapest first (PILFERed from sf_resolver_library's deterministic-registry
pattern, then graceful RAG like all_brain_rag):
  1. ACCEPTANCE LEDGER  (data/learning/never_twice_accepted.json) — states we've muted before.
  2. DETERMINISTIC REGISTRY (SIGNATURES below) — the crystallized fixes/acceptances, like
     sf_resolver_library.default_library(). Regex/predicate over the signature text.
  3. RAG FALLBACK (graph_memory + episodic_memory + all_brain_rag + correction_rules) — best-effort,
     degrades to [] if an index is cold or Ollama is down. EVIDENCE-ONLY: it attaches evidence_refs
     + a non-acting hint ("prior_fix"/"possible_accepted") to a NOVEL result. It never silently
     mutes or heals on similarity alone — that would risk hiding a real fault. Silent acting is
     reserved for the grounded tiers (ledger + registry); RAG just makes the escalation informed.

SILENT: nothing here texts Operator. apply_fix() heals + verifies; mute() records acceptance. The only
path that reaches the pager is a NOVEL, unfixable signature — routed by the caller, minimally.

HARD LANE FIREWALL: retrieval is lane-scoped (default hustle = stack-infra lane) and every tier is
wrapped so a cold/failing store returns [] — one lane being down never spills into another.

    .venv/bin/python -m core.never_twice          # run the smoke proof (dry, no live heal)
    .venv/bin/python -m core.never_twice seen "searxng dry / colima down"

STAGED — the alert_gateway.route() wire-in is written and COMMENTED at the bottom of this file;
nothing here is hooked into the live pager yet. apply_fix(dry=True) never touches a service.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

LEARN_DIR = BASE_DIR / "data" / "learning"
LEARN_DIR.mkdir(parents=True, exist_ok=True)
ACCEPTED_LEDGER = LEARN_DIR / "never_twice_accepted.json"   # muted states
NOVEL_LOG = LEARN_DIR / "never_twice_novel.jsonl"           # crystallization raw material

WORK, HUSTLE, PERSONAL = "work", "hustle", "personal"
# stack-infra alerts live on the hustle side of the firewall (the money-machine plumbing)
DEFAULT_LANE = HUSTLE

HOME = Path.home()
VENV_PY = BASE_DIR / ".venv" / "bin" / "python"
# L4 disk-freeze line: free space (GB on /) must climb back ABOVE this for a heal to count as HELD.
# Sourced from tools/redacted_sentinel.DISK_L4_GB (10 GB — the work-reserve freeze threshold).
DISK_L4_FREE_GB = 10.0
DISK_CHECK_PATH = "/"


def _disk_free_gb(path: str = DISK_CHECK_PATH) -> float:
    """Free GB on `path` via statvfs — same measure as redacted_sentinel.disk_free_gb()."""
    st = os.statvfs(path)
    return (st.f_bavail * st.f_frsize) / (1024 ** 3)


def _verify_disk_l4(threshold_gb: float = DISK_L4_FREE_GB, path: str = DISK_CHECK_PATH) -> bool:
    """REAL verify: the L4 heal held ONLY if free space actually climbed back above the L4 line.

    Reads bytes-free and compares to the threshold — never a `df` proxy that exits 0 no matter what.
    Fails CLOSED (can't read disk -> can't claim a heal held -> False), so a fault that did NOT
    clear escalates instead of being silently marked self_healed.
    """
    try:
        return _disk_free_gb(path) >= threshold_gb
    except Exception:
        return False


# ───────────────────────────────────────────────────────────── signature normalization
def normalize(signature) -> tuple[str, str]:
    """Collapse a signature (str | {source, subject, body, issue_key}) into (key, text).

    key  = a stable, low-cardinality id used for the acceptance ledger + dedup.
    text = the human-readable blob the matchers + RAG run against.
    Mirrors alert_gateway._issue_key so a signature here lines up with a page there.
    """
    if isinstance(signature, dict):
        source = str(signature.get("source", "")).strip()
        subject = str(signature.get("subject", "")).strip()
        body = str(signature.get("body", "")).strip()
        explicit = signature.get("issue_key")
        text = " ".join(p for p in (source, subject, body) if p)
        if explicit:
            key = str(explicit)
        else:
            key = _slug(f"{source}:{subject}") or _slug(text)
        return key, text
    text = str(signature).strip()
    return _slug(text), text


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:80]


# ───────────────────────────────────────────────────────────── deterministic registry
@dataclass
class Fix:
    """A deterministic heal. heal_cmd runs, then verify_cmd must exit 0 for it to count as held.

    In dry mode apply_fix() returns the PLAN and runs nothing — the smoke path.
    """
    desc: str
    heal_cmd: list[str]
    verify_cmd: list[str] | None = None
    # verify_fn takes precedence over verify_cmd: a Python predicate that READS the real outcome
    # (e.g. bytes-free) and returns True only if the heal genuinely held. Use it wherever an
    # exit-0-unconditional shell probe (like `df`) would falsely report a heal that never happened.
    verify_fn: Callable[[], bool] | None = None
    timeout_s: int = 120


@dataclass
class KnownSignature:
    key: str
    matcher: Callable[[str], bool]
    disposition: str                       # "known_fix" | "known_accepted"
    reason: str
    evidence_refs: list[str] = field(default_factory=list)
    fix: Fix | None = None


def _kw(*words):
    """Predicate: every keyword (case-insensitive) appears in the text. Cheap + explicit."""
    lows = [w.lower() for w in words]
    return lambda text: all(w in text.lower() for w in lows)


def _any_kw(*words):
    lows = [w.lower() for w in words]
    return lambda text: any(w in text.lower() for w in lows)


def _match_gh_watchdog(text: str) -> bool:
    """Match ONLY the one decided-fine state: the anon GitHub mirror is intentionally unprovisioned.

    Requires the SPECIFIC provisioning signal — never bare 'github'/'watchdog', which would mute a
    genuine future github fault (fail-dangerous). A generic github-down/push-failed/rate-limited
    alert carries none of these tokens, so it escalates as novel instead of being silently muted.
    """
    t = text.lower()
    provisioning_signal = any(s in t for s in (
        "unprovisioned", "not provisioned", "anon mirror", "anonymous mirror",
        "mirror not provisioned", "push.env"))
    if not provisioning_signal:
        return False
    # anchor to a git/mirror subject so an unrelated 'unprovisioned' elsewhere can't trip the mute
    return any(s in t for s in ("git", "mirror", "push.env", "remote"))


def _match_searxng_colima(text: str) -> bool:
    t = text.lower()
    return _any_kw("searxng", "colima", "docker")(t) and _any_kw(
        "dry", "down", "unreachable", "no results", "empty", "not running", "connection refused")(t)


def _match_disk_l4(text: str) -> bool:
    t = text.lower()
    return _any_kw("disk", "space", "free")(t) and _any_kw(
        "l4", "level 4", "freeze", "critical", "jump")(t)


# ── stack self-heal actions (the gateway runs these INSTEAD of texting Operator) ──────────
_UID = os.getuid()
STACK_HEAL = BASE_DIR / "tools" / "stack_heal.py"


def _kickstart(label: str) -> list[str]:
    return ["/bin/launchctl", "kickstart", "-k", f"gui/{_UID}/{label}"]


def _kickstart_if_stream_down(label: str) -> list[str]:
    """Kickstart the YouTube live encoder ONLY if it is actually down. A bare `kickstart -k` here
    SIGTERM+respawns the supervisor and drops the RTMP feed -> YouTube flips OFFLINE. The matcher
    runs against normalize()'s source+subject+BODY, and callers like session_watchdog stuff up to
    4000 chars of SCRAPED PAGE TEXT into body (and outcome-heal sends "the UPstream ... is down") —
    so the yt-stream signature false-fires on unrelated faults no matcher can reliably exclude. This
    guard makes the heal a no-op whenever ffmpeg is already pushing RTMP to YouTube, so a HEALTHY
    livestream is never killed; a genuinely-dead encoder still gets kickstarted. (killer fixed 2026-07-13)"""
    return ["/bin/sh", "-c",
            f"pgrep -f 'ffmpeg.*rtmp.*youtube' >/dev/null 2>&1 && exit 0 || "
            f"exec /bin/launchctl kickstart -k gui/{_UID}/{label}"]


def _verify_url(url: str, timeout: int = 6, poll_s: int = 18):
    """Poll the URL through a restart window: kickstart -k drops the port for a few seconds,
    so a single immediate probe would falsely fail. Retry until it answers or poll_s elapses."""
    def _v() -> bool:
        import urllib.request
        deadline = time.time() + poll_s
        while True:
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    code = getattr(r, "status", None) or r.getcode()
                    if 200 <= code < 400:
                        return True
            except Exception:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(1.5)
    return _v


def _verify_job_running(label: str):
    def _v() -> bool:
        try:
            out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                                 text=True, timeout=15).stdout
            for ln in out.splitlines():
                parts = ln.split("\t") if "\t" in ln else ln.split()
                if len(parts) >= 3 and parts[2] == label:
                    return parts[0].lstrip("-").isdigit()  # a real PID = running
            return False
        except Exception:
            return False
    return _v


def _match_surface_8360(text: str) -> bool:
    t = text.lower()
    return ("surface offline" in t or "surface is offline" in t or "offline:" in t) \
        and ("8360" in t or "dashboard" in t)


def _match_mockups_undeployed(text: str) -> bool:
    t = text.lower()
    return "undeployed" in t and "mockup" in t


def _match_yt_stream(text: str) -> bool:
    t = text.lower()
    # Must be the YouTube live ENCODER down, not any text that merely contains "stream".
    # BUG (fixed 2026-07-13): the old guard `... or "stream" in t` was a tautology (the first
    # clause already required "stream"), so it collapsed to `"stream" and (down|offline)`. The
    # outcome-heal:intent_leads chronic body ("the UPstream multi_market lead scan ... is down")
    # false-matched — "stream" lives inside "upstream" — and this fix kickstart-killed the HEALTHY
    # livestream every ~17min, dropping RTMP -> YouTube OFFLINE. Require a real broadcast marker.
    if not ("down" in t or "offline" in t or "dropped" in t):
        return False
    return any(k in t for k in (
        "youtube", "yt live", "yt-live", "yt stream", "rtmp",
        "livestream", "live stream", "dashboard-livestream", "broadcast", "encoder"))


def _match_dead_jobs(text: str) -> bool:
    t = text.lower()
    return ("jobs dead" in t or "bad exits" in t or "dead job" in t
            or ("launchd" in t and ("crash" in t or "bad exit" in t)))


def default_registry() -> list[KnownSignature]:
    """The crystallized fixes/acceptances — the same shape as sf_resolver_library.default_library().

    Every entry traces to a REAL, evidenced state (no fabricated lessons):
      - gh-watchdog: the anon GitHub mirror is intentionally unprovisioned (Operator, 2026-07-10 — no
        Gumroad-style token; the OneDrive bare mirror is the real backup). Accepted -> mute.
      - searxng/colima: docker VM naps -> docker-dependent probes go dry; `colima restart` heals it.
      - disk L4: jump files pile up -> disk-guardian jump-reaper frees them.
    """
    return [
        KnownSignature(
            key="gh-watchdog-anon-mirror-unprovisioned",
            matcher=_match_gh_watchdog,
            disposition="known_accepted",
            reason=("anon GitHub mirror is intentionally unprovisioned — no token rotation "
                    "(Operator 2026-07-10); the CompanyA OneDrive BARE mirror is the real backup, so "
                    "an unprovisioned GitHub remote is a decided-fine state, not a fault."),
            evidence_refs=[
                "MEMORY: feedback_no_gumroad_token_rotation_2026-07-10.md",
                "MEMORY: project_redacted_git_onedrive_mirror_2026-07-10.md",
            ],
        ),
        KnownSignature(
            key="searxng-dry-colima-down",
            matcher=_match_searxng_colima,
            disposition="known_fix",
            reason=("searxng/docker-dependent probes go dry when the colima docker VM naps; "
                    "restarting the VM restores them. Deterministic + idempotent."),
            evidence_refs=[
                "MEMORY: project_stack_perf_disk_io_2026-07-09.md",
                "runbook: colima restart -> re-probe searxng health",
            ],
            fix=Fix(
                desc="restart the colima docker VM, then verify it reports running",
                heal_cmd=["colima", "restart"],
                verify_cmd=["colima", "status"],
                timeout_s=180,
            ),
        ),
        KnownSignature(
            key="disk-l4-freeze-jump-reaper",
            matcher=_match_disk_l4,
            disposition="known_fix",
            reason=("disk L4 freeze is driven by jump/temp files piling up; the disk-guardian "
                    "jump-reaper frees them (rm is harness-blocked -> find -delete)."),
            evidence_refs=[
                "MEMORY: project_disk_reboot_external_integration_2026-07-08.md",
                "MEMORY: project_stack_perf_disk_io_2026-07-09.md",
            ],
            fix=Fix(
                # Invoke the REAL disk-guardian reaper (tools/storage_reaper.py --force-aggressive) —
                # the same bounded+reversible pass disk_guardian.sh runs on a fast-jump. The old
                # heal pointed find at BASE_DIR/.disk-guardian.jump-reaper, which doesn't exist
                # (that's a cooldown STAMP file in HOME, not a dir of jump files) -> heal no-op'd.
                desc="run the disk-guardian storage reaper (bounded, reversible) to clear the L4 freeze",
                heal_cmd=[str(VENV_PY), str(BASE_DIR / "tools" / "storage_reaper.py"),
                          "--force-aggressive"],
                # REAL verify: reads free bytes and asserts they crossed back above the L4 line.
                # (the old `df -h` verify exited 0 unconditionally -> falsely reported held=True even
                # when nothing was freed — the exact silent-fault-hiding this module exists to prevent.)
                verify_cmd=None,
                verify_fn=_verify_disk_l4,
                timeout_s=300,
            ),
        ),
        KnownSignature(
            key="hailports-surface-8360-offline",
            matcher=_match_surface_8360,
            disposition="known_fix",
            reason=("the public case-study dashboard (:8360) hung/died -> psw pages 'surface "
                    "offline'. Kickstart its launchd job (the in-process page cache keeps renders "
                    "under the 5s probe); verify the port answers before marking healed."),
            evidence_refs=["MEMORY: project_runaway_guard_quarantine_cascade_2026-07-12.md"],
            fix=Fix(
                desc="restart case-study-dashboard, verify :8360 responds",
                heal_cmd=_kickstart("com.claude-stack.case-study-dashboard"),
                verify_fn=_verify_url("http://127.0.0.1:8360/", 6),
                timeout_s=60,
            ),
        ),
        KnownSignature(
            key="hailports-mockups-undeployed",
            matcher=_match_mockups_undeployed,
            disposition="known_fix",
            reason=("rebuilt mockups sitting undeployed means mockup-regen stalled or was disabled; "
                    "it self-deploys via deploy_hailports.sh. Kickstart it and verify it's running. "
                    "(If deploy keeps failing the gateway's 24h chronic breaker surfaces it as a digest.)"),
            evidence_refs=["MEMORY: project_runaway_guard_quarantine_cascade_2026-07-12.md"],
            fix=Fix(
                desc="kickstart mockup-regen (regenerates + deploys), verify running",
                heal_cmd=_kickstart("com.claude-stack.mockup-regen"),
                verify_fn=_verify_job_running("com.claude-stack.mockup-regen"),
                timeout_s=90,
            ),
        ),
        KnownSignature(
            key="hailports-yt-stream-down",
            matcher=_match_yt_stream,
            disposition="known_fix",
            reason=("the YouTube live encoder dropped; restart the livestream job and verify an "
                    "ffmpeg->rtmp encoder is back up (keep the encoder exempt from QoS kills)."),
            evidence_refs=["runbook: stream_health_watchdog restarts the ffmpeg rtmp encoder"],
            fix=Fix(
                desc="restart the livestream job ONLY if the encoder is actually down, verify ffmpeg rtmp encoder running",
                heal_cmd=_kickstart_if_stream_down("com.claude-stack.dashboard-livestream"),
                verify_cmd=["/bin/sh", "-c", "pgrep -f 'ffmpeg.*rtmp.*youtube' >/dev/null"],
                timeout_s=60,
            ),
        ),
        KnownSignature(
            key="stack-dead-jobs-revive",
            matcher=_match_dead_jobs,
            disposition="known_fix",
            reason=("'N stack jobs dead' / launchd bad-exits -> re-enable + kickstart the crashed "
                    "com.claude-stack jobs (skips guards/sentinels). Verify zero dead remain."),
            evidence_refs=["tools/stack_heal.py revive-dead"],
            fix=Fix(
                desc="revive dead launchd jobs, verify none remain dead",
                heal_cmd=[str(VENV_PY), str(STACK_HEAL), "revive-dead"],
                verify_cmd=[str(VENV_PY), str(STACK_HEAL), "count-dead"],
                timeout_s=120,
            ),
        ),
    ]


REGISTRY = default_registry()


# ───────────────────────────────────────────────────────────── acceptance ledger (mute memory)
def _load_accepted() -> dict:
    if not ACCEPTED_LEDGER.exists():
        return {}
    try:
        return json.loads(ACCEPTED_LEDGER.read_text())
    except Exception:
        return {}


def mute(key: str, reason: str, evidence_refs: list[str] | None = None) -> dict:
    """Record that `key` is a decided-fine state so it stops re-firing. Idempotent.

    This is the (b) branch of the mandate made durable: once muted, a later seen() short-circuits
    at tier 1 and never reaches the pager.
    """
    led = _load_accepted()
    entry = led.get(key, {"first_muted": time.time(), "count": 0})
    entry["reason"] = reason
    entry["evidence_refs"] = evidence_refs or entry.get("evidence_refs", [])
    entry["last_muted"] = time.time()
    entry["count"] = entry.get("count", 0) + 1
    led[key] = entry
    tmp = ACCEPTED_LEDGER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(led, indent=2))
    tmp.replace(ACCEPTED_LEDGER)
    return entry


def is_muted(key: str) -> dict | None:
    return _load_accepted().get(key)


# ───────────────────────────────────────────────────────────── RAG fallback (best-effort)
_ACCEPT_MARKERS = re.compile(
    r"\b(intentional|by design|expected|accepted|decided|won'?t fix|no[- ]?op|ignore|"
    r"known[- ]good|not a (bug|fault|problem))\b", re.I)
_FIX_MARKERS = re.compile(
    r"\b(fixed|restart|reap|heal|resolved|the fix|remediat|workaround|re-?run|kick)\b", re.I)


def _retrieve_evidence(text: str, lane: str) -> list[dict]:
    """Pull prior fixes/lessons across graph + episodic + all_brain_rag + correction_rules.

    Every tier is wrapped: a cold index or unreachable Ollama returns [] rather than raising, so
    this NEVER blocks a heal decision and never crosses a lane. Returns normalized evidence dicts.
    """
    ev: list[dict] = []

    def _safe(label, fn):
        try:
            return fn() or []
        except Exception as e:  # noqa: BLE001
            print(f"[never_twice] {label} retrieval skipped: {e}", file=sys.stderr)
            return []

    def _graph():
        from core import graph_memory
        r = graph_memory.traverse(text, lane=lane, max_hops=2)
        # only real decision NODES are promotable evidence; the synthesized `answer` is a smoothed
        # convenience string (not a fact) — never let it drive a silent mute/heal.
        return [{"src": "graph", "text": d.get("name", ""), "ref": d.get("id", "graph")}
                for d in (r.get("decisions") or [])]

    def _episodic():
        from core import episodic_memory
        rows = episodic_memory.recall(text, lane, limit=6)
        return [{"src": "episodic", "text": (e.get("summary") or ""),
                 "ref": e.get("id") or e.get("source", "episodic")} for e in rows]

    def _rag():
        from core import all_brain_rag
        hits = all_brain_rag.search(text, lane, k=5)
        return [{"src": "rag", "text": (h.get("text") or "")[:300],
                 "ref": h.get("ref") or h.get("source", "rag")} for h in hits]

    def _rules():
        rf = LEARN_DIR / "correction_rules.md"
        if not rf.exists():
            return []
        toks = [w for w in re.findall(r"[a-z0-9]{4,}", text.lower())]
        out = []
        for line in rf.read_text().splitlines():
            low = line.lower()
            if line.strip() and any(t in low for t in toks):
                out.append({"src": "correction_rule", "text": line.strip(), "ref": "correction_rules.md"})
        return out[:5]

    ev += _safe("graph", _graph)
    ev += _safe("episodic", _episodic)
    ev += _safe("all_brain_rag", _rag)
    ev += _safe("correction_rules", _rules)
    return ev


_STOP = {"the", "and", "for", "with", "down", "error", "alert", "failed", "issue", "warn"}


def _hint_from_evidence(text: str, ev: list[dict]) -> str | None:
    """Return a non-acting HINT ("prior_fix" | "possible_accepted") if RELEVANT recalled evidence
    carries a marker — else None. This NEVER promotes to a silently-acting status.

    Why not auto-act on RAG: semantic recall always returns nearest neighbors, and infra memory is
    saturated with "fixed"/"restart"/"expected" — acting on a keyword in a merely-similar chunk
    would risk silently MUTING a real fault (or running a wrong heal). Silent mute/heal is reserved
    for the grounded tiers (acceptance ledger + deterministic registry). RAG only informs the
    escalation so the owner-action stays minimal + evidenced.

    Relevance gate: the marker-bearing chunk must share a content token with the signature.
    """
    toks = {t for t in re.findall(r"[a-z0-9]{4,}", text.lower()) if t not in _STOP}
    if not toks:
        return None
    accepted = fix = False
    for e in ev:
        et = e.get("text", "")
        if not et or not (toks & {t for t in re.findall(r"[a-z0-9]{4,}", et.lower())}):
            continue  # not actually about this signature — ignore
        if _ACCEPT_MARKERS.search(et):
            accepted = True
        if _FIX_MARKERS.search(et):
            fix = True
    if accepted:
        return "possible_accepted"
    if fix:
        return "prior_fix"
    return None


# ───────────────────────────────────────────────────────────── the entry point
def seen(error_signature, *, lane: str = DEFAULT_LANE, use_rag: bool = True) -> dict:
    """Have we handled this error before? Returns {status, action, key, ...evidence_refs}.

    status: "known_fix" | "known_accepted" | "novel"
    action: "mute" | "apply_fix" | "escalate"  (what the caller should do — silently for the first two)
    Read-only: seen() decides; apply_fix()/mute() do the acting.
    """
    key, text = normalize(error_signature)

    # tier 1 — already muted?
    m = is_muted(key)
    if m:
        return {"status": "known_accepted", "action": "mute", "key": key, "text": text,
                "reason": m.get("reason", "previously accepted"),
                "evidence_refs": m.get("evidence_refs", []), "source_tier": "acceptance_ledger"}

    # tier 2 — deterministic registry (crystallized)
    for sig in REGISTRY:
        try:
            hit = sig.matcher(text)
        except Exception:
            hit = False
        if hit:
            out = {"status": sig.disposition, "key": sig.key, "text": text,
                   "reason": sig.reason, "evidence_refs": list(sig.evidence_refs),
                   "source_tier": "registry"}
            if sig.disposition == "known_accepted":
                out["action"] = "mute"
                out["fix"] = None
            else:
                out["action"] = "apply_fix"
                out["fix"] = sig.fix
            return out

    # tier 3 — RAG fallback: EVIDENCE-ONLY. Enriches the novel escalation with precedent + a hint;
    # never auto-mutes/heals (that stays with the grounded tiers 1-2). A hint means "seen something
    # like this before — here are the refs", so the owner-action is minimal + informed, not blind.
    ev = _retrieve_evidence(text, lane) if use_rag else []
    refs = [f"{e['src']}:{e['ref']}" for e in ev][:8]
    hint = _hint_from_evidence(text, ev) if ev else None
    reason = "no prior fix or acceptance found across registry + recalled memory"
    if hint == "possible_accepted":
        reason = "recall suggests a possibly-accepted state — needs owner confirm to mute (not auto-muted)"
    elif hint == "prior_fix":
        reason = "recall suggests a prior fix exists but no deterministic recipe is registered yet"
    return {"status": "novel", "action": "escalate", "key": key, "text": text,
            "reason": reason, "evidence_refs": refs, "source_tier": "rag" if ev else "none",
            "hint": hint, "fix": None}


# ───────────────────────────────────────────────────────────── act (silent)
def apply_fix(decision: dict, *, dry: bool = True) -> dict:
    """Run the known fix and verify it HELD (verify the real outcome, not a proxy — the Blake rule).

    dry=True (default + the smoke path): returns the PLAN, runs nothing, touches no service.
    dry=False: runs heal_cmd, then verify_cmd — verified iff verify_cmd exits 0.
    """
    fix = decision.get("fix")
    if decision.get("status") != "known_fix" or not isinstance(fix, Fix):
        return {"applied": False, "verified": False,
                "reason": "no deterministic fix recipe attached (recall-only or non-fix decision)",
                "key": decision.get("key")}
    plan = {"key": decision.get("key"), "desc": fix.desc,
            "heal_cmd": fix.heal_cmd, "verify_cmd": fix.verify_cmd,
            "verify_fn": getattr(fix.verify_fn, "__name__", None)}
    if dry:
        return {"applied": False, "verified": None, "dry": True, "plan": plan}

    try:
        h = subprocess.run(fix.heal_cmd, capture_output=True, text=True, timeout=fix.timeout_s)
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "verified": False, "plan": plan, "error": f"heal raised: {e!r}"}
    # verify the REAL outcome. verify_fn (reads the actual state) wins over verify_cmd (exit code).
    # verified stays None only when NO verifier is attached; any attached verifier that can't prove
    # the heal held returns False -> caller escalates the still-open fault, never marks it healed.
    verified = None
    if fix.verify_fn is not None:
        try:
            verified = bool(fix.verify_fn())
        except Exception as e:  # noqa: BLE001
            verified = False
            plan["verify_error"] = repr(e)
    elif fix.verify_cmd:
        try:
            v = subprocess.run(fix.verify_cmd, capture_output=True, text=True, timeout=fix.timeout_s)
            verified = (v.returncode == 0)
        except Exception as e:  # noqa: BLE001
            verified = False
            plan["verify_error"] = repr(e)
    return {"applied": (h.returncode == 0), "verified": verified, "plan": plan,
            "heal_rc": h.returncode}


def record_novel(decision: dict) -> dict:
    """Log a novel, escalated signature as crystallization raw material.

    The growth loop: a later promotion pass folds recurring novel shapes into default_registry()
    as new KnownSignature entries — the same pattern sf_resolver_library.record_new_pattern() uses
    to retire recurring AI calls. This is how the stack gets smarter every day.
    """
    entry = {"at": time.time(), "key": decision.get("key"), "text": decision.get("text"),
             "reason": decision.get("reason"), "evidence_refs": decision.get("evidence_refs", [])}
    with NOVEL_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


# ───────────────────────────────────────────────────────────── STAGED wire-in (commented)
# alert_gateway.route() consults never_twice BEFORE paging. Drop this at the TOP of route(),
# right after `ik = _issue_key(...)`, guarded by an env flag so it ships dark:
#
#     if _envf("NEVER_TWICE", "0") == "1" and not healed:
#         from core import never_twice
#         d = never_twice.seen({"source": source, "subject": subject,
#                               "body": body, "issue_key": ik})
#         if d["status"] == "known_accepted":
#             never_twice.mute(d["key"], d["reason"], d["evidence_refs"])   # stops re-firing
#             return {"action": "self_muted", "issue_key": ik, "why": d["reason"]}
#         if d["status"] == "known_fix":
#             res = never_twice.apply_fix(d, dry=False)                     # silent heal
#             if res.get("verified"):
#                 register_fix(source, issue_key=ik, subject=subject)       # mark recovered, no page
#                 return {"action": "self_healed", "issue_key": ik, "fix": d["key"]}
#             # heal didn't hold -> fall through to normal paging (real, unresolved fault)
#         else:  # novel
#             never_twice.record_novel(d)   # crystallization raw material; then page as usual
#     # ... existing route() body (paging) continues unchanged ...
#
# Net effect: known_accepted -> muted, known_fix -> healed+verified, only a NOVEL unfixable
# signature ever reaches Operator — the SILENT-SELF-CORRECT mandate, enforced at the pager's door.


# ───────────────────────────────────────────────────────────── smoke proof (dry)
def _smoke() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        ok = ok and cond

    # (a) gh-watchdog -> known_accepted / mute, with evidence
    gh = seen({"source": "gh-watchdog", "subject": "anon mirror remote unprovisioned"},
              use_rag=False)
    check("gh-watchdog is known_accepted", gh["status"] == "known_accepted")
    check("gh-watchdog action is mute", gh["action"] == "mute")
    check("gh-watchdog has evidence_refs", len(gh["evidence_refs"]) > 0)
    print("      ->", gh["reason"][:70], "| refs:", gh["evidence_refs"])

    # (b) searxng/colima -> known_fix / apply_fix (restart colima), verified DRY (no live heal)
    sx = seen({"source": "searxng-probe", "subject": "searxng dry — colima docker VM down"},
              use_rag=False)
    check("searxng is known_fix", sx["status"] == "known_fix")
    check("searxng action is apply_fix", sx["action"] == "apply_fix")
    check("searxng fix restarts colima",
          isinstance(sx["fix"], Fix) and sx["fix"].heal_cmd[:2] == ["colima", "restart"])
    check("searxng has evidence_refs", len(sx["evidence_refs"]) > 0)
    applied = apply_fix(sx, dry=True)
    check("apply_fix DRY runs nothing (applied False, plan present)",
          applied["applied"] is False and applied.get("dry") is True and "plan" in applied)
    print("      -> plan:", applied["plan"]["heal_cmd"], "verify:", applied["plan"]["verify_cmd"])

    # disk L4 -> known_fix too
    dk = seen({"source": "disk-guardian", "subject": "disk L4 freeze — free space critical"},
              use_rag=False)
    check("disk L4 is known_fix", dk["status"] == "known_fix")

    # (c) novel -> escalate (rag off so it can't be promoted by a cold index)
    nv = seen({"source": "flux-capacitor", "subject": "temporal desync at 88mph"}, use_rag=False)
    check("novel signature is novel", nv["status"] == "novel")
    check("novel action is escalate", nv["action"] == "escalate")
    rec = record_novel(nv)
    check("novel logged for crystallization", NOVEL_LOG.exists() and rec["key"] == nv["key"])

    # mute() durability: a muted key short-circuits at tier 1
    mute(nv["key"], "test acceptance", ["smoke:manual"])
    again = seen({"source": "flux-capacitor", "subject": "temporal desync at 88mph"}, use_rag=False)
    check("muted key now returns known_accepted", again["status"] == "known_accepted")
    check("muted key sourced from acceptance_ledger", again["source_tier"] == "acceptance_ledger")
    # clean up the smoke's ledger entry so we don't pollute real state
    led = _load_accepted()
    led.pop(nv["key"], None)
    ACCEPTED_LEDGER.write_text(json.dumps(led, indent=2))

    # (d) apply_fix(dry=False) VERIFY — a disk fault that did NOT clear must ESCALATE, not self_heal.
    #     Proves the verify genuinely READS free space + compares (not an exit-0 proxy), both sides:
    #     an unreachable threshold -> held False (escalate); a trivially-met one -> held True.
    HUGE_GB = 10 ** 9  # more free GB than any disk will ever have -> heal can never be claimed held
    not_cleared = {"status": "known_fix", "key": "disk-l4-smoke-not-cleared",
                   "fix": Fix(desc="smoke: no-op heal, disk stays below the L4 line",
                              heal_cmd=["true"],
                              verify_fn=lambda: _verify_disk_l4(threshold_gb=HUGE_GB))}
    r_bad = apply_fix(not_cleared, dry=False)
    check("un-cleared disk fault -> verified is False (heal ran, free space still below L4)",
          r_bad.get("verified") is False)
    # caller contract (see the staged wire-in): verified False => NOT self_healed => escalate/page
    check("un-cleared disk fault ESCALATES, never marked self_healed",
          (not r_bad.get("verified")) is True)
    cleared = {"status": "known_fix", "key": "disk-l4-smoke-cleared",
               "fix": Fix(desc="smoke: verify against a trivially-met threshold",
                          heal_cmd=["true"],
                          verify_fn=lambda: _verify_disk_l4(threshold_gb=0.0))}
    r_ok = apply_fix(cleared, dry=False)
    check("verify actually READS the disk (threshold 0 -> held True) — not stuck always-False",
          r_ok.get("verified") is True)

    # (e) gh matcher: ONLY the intentionally-unprovisioned mirror signal mutes; a generic github
    #     fault must stay novel/escalate (never silently muted by a bare 'github'/'watchdog' token).
    ghdown = seen({"source": "gh-watchdog", "subject": "github push failed — remote returned 500"},
                  use_rag=False)
    check("generic github fault is NOT muted (novel + escalate)",
          ghdown["status"] == "novel" and ghdown["action"] == "escalate")
    check("gh matcher rejects a bare github/watchdog alert",
          _match_gh_watchdog("github watchdog down") is False)
    check("gh matcher still accepts the intentional anon-mirror-unprovisioned signal",
          _match_gh_watchdog("anon mirror remote unprovisioned") is True)

    print("\n" + ("ALL SMOKE CHECKS PASSED" if ok else "SMOKE FAILED"))
    return 0 if ok else 1


def _main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "seen":
        d = seen(sys.argv[2])
        print(json.dumps({k: (v.desc if isinstance(v, Fix) else v)
                          for k, v in d.items()}, indent=2, default=str))
        return 0
    return _smoke()


if __name__ == "__main__":
    raise SystemExit(_main())
