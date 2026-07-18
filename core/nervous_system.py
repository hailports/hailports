#!/usr/bin/env python3
"""Centralized nervous system — per-lane brain + event bus spine for the stack.

Unifies what already exists (it does NOT rebuild reflexes or replace alert_gateway). One class
NervousSystem(lane) with five fail-open verbs, backed by ONE sqlite file per lane:

  observe(kind, source, ...)   afferent  — append an event to the per-lane event bus
  remember(text, confidence)   afferent  — upsert a durable learning into the facts table
  recall(query)                efferent  — grounded answer (facts + hustle-only rag leg)
  context(entrypoint)          efferent  — capped markdown block for read-on-start injection
  state(window_s)              efferent  — recent events folded into machine-state

FIREWALL BY CONSTRUCTION (the load-bearing property — hustle ⟂ work):
  * two physically separate db files: data/nervous/hustle.db, data/nervous/work.db
  * every row lane-stamped; NervousSystem(lane) raises on an unknown lane
  * the module-level convenience fns resolve lane from arg or $NERVOUS_LANE and DROP (never
    default to hustle) when the lane is unset — so a shared choke point (alert_gateway, a
    harvester) can NEVER silently launder work data into the hustle brain
  * recall()'s rag leg runs ONLY for lane='hustle' (the hustle corpus); work recall is
    facts-only until a dedicated work index exists — it never queries the hustle corpus
  * this module imports NOTHING from the work brain (apps.redacted_brain / core.redacted_session_ctx
    / voice); --selftest asserts that statically

FAIL-OPEN: every public method wraps its body; a failure returns {ok:False,...} or a partial and
NEVER raises into a caller. observe() is non-blocking: on sqlite lock contention it DROPS the event
rather than stall a caller (a critical page must never wait on the bus).

  python3 core/nervous_system.py --lane hustle observe --kind funnel.sale --source stripe
  python3 core/nervous_system.py --lane hustle context --entrypoint session
  python3 core/nervous_system.py --lane hustle recall "how does cold email gating work"
  python3 core/nervous_system.py --selftest
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NERVOUS_DIR = ROOT / "data" / "nervous"
VALID_LANES = ("hustle", "work")
_SEVERITIES = ("info", "notice", "warn", "error", "critical")
_PRUNE_KEEP_SEVERITY = ("error", "critical")  # never age-pruned
# each lane's durable-learning feeder store (kept per-lane; work never shares the hustle jsonl)
_LANE_INSTINCT = {
    "hustle": ROOT / "data" / "instincts" / "instincts.jsonl",
    "work": ROOT / "data" / "instincts" / "work.jsonl",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  lane TEXT NOT NULL,
  kind TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'info'
      CHECK (severity IN ('info','notice','warn','error','critical')),
  subject TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  dedup TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_dedup ON events(lane, dedup) WHERE dedup IS NOT NULL;
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  lane TEXT NOT NULL,
  text TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.7,
  tags TEXT NOT NULL DEFAULT '[]',
  created REAL NOT NULL,
  last_seen REAL NOT NULL,
  hits INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_facts_conf ON facts(confidence);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _now() -> float:
    return time.time()


def _iid(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text.lower()).strip().encode()).hexdigest()[:12]


def _norm_severity(sev: str) -> str:
    s = str(sev or "info").strip().lower()
    return s if s in _SEVERITIES else "info"  # coerce a typo, never lose the event to a CHECK


class NervousSystem:
    """One lane's brain. Construct explicitly with a valid lane; there is no shared default file."""

    def __init__(self, lane: str = "hustle", db_path: str | Path | None = None):
        if lane not in VALID_LANES:
            raise ValueError(f"unknown lane {lane!r}; must be one of {VALID_LANES}")
        self.lane = lane
        self.db_path = Path(db_path) if db_path else NERVOUS_DIR / f"{lane}.db"
        self._init_db()

    # --- storage plumbing -------------------------------------------------
    def _init_db(self) -> None:
        """Create the schema + set WAL ONCE per instance (WAL persists on the file; not re-run
        on the hot path). Fail-open: a failed init leaves methods to no-op safely."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=5)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass  # methods below each fail-open

    @contextlib.contextmanager
    def _conn(self, *, busy_ms: int = 2000):
        """A short-lived connection. busy_ms bounds how long we wait on a writer lock before
        raising sqlite3.OperationalError — callers on hot paths pass busy_ms low and DROP."""
        conn = sqlite3.connect(self.db_path, timeout=max(0.05, busy_ms / 1000.0))
        try:
            conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- afferent ---------------------------------------------------------
    def observe(self, kind: str, source: str = "", *, subject: str = "",
                severity: str = "info", payload: dict | None = None,
                dedup: str | None = None) -> dict:
        """Append an event to the bus. NON-BLOCKING: on lock contention the event is DROPPED
        (a critical page must never wait on telemetry). Idempotent when dedup is given."""
        try:
            row = (_now(), self.lane, str(kind)[:120], str(source)[:120],
                   _norm_severity(severity), str(subject)[:500],
                   json.dumps(payload or {}, ensure_ascii=False)[:8000], dedup)
            with self._conn(busy_ms=200) as conn:  # short: fire-and-forget
                try:
                    conn.execute(
                        "INSERT INTO events(ts,lane,kind,source,severity,subject,payload,dedup) "
                        "VALUES(?,?,?,?,?,?,?,?)", row)
                except sqlite3.IntegrityError:
                    return {"ok": True, "deduped": True}  # dedup UNIQUE hit
            return {"ok": True}
        except sqlite3.OperationalError:
            return {"ok": False, "dropped": True, "reason": "bus-busy"}  # never block the caller
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    def remember(self, text: str, *, confidence: float = 0.7, tags=None) -> dict:
        """Upsert a durable learning into the facts table (reinforce-on-dupe). The facts table is
        the spine's own durable store — we deliberately do NOT write the instinct_loop jsonl here
        (the harvest job owns that file; a concurrent write would clobber it)."""
        try:
            text = str(text or "").strip()
            if len(text) < 12:
                return {"ok": False, "reason": "too-short"}
            iid = _iid(text)
            conf = max(0.0, min(1.0, float(confidence)))
            now = _now()
            with self._conn() as conn:
                cur = conn.execute("SELECT confidence,hits FROM facts WHERE id=?", (iid,))
                r = cur.fetchone()
                if r:
                    newc = round(min(1.0, r[0] + 0.02 * (1 - r[0])), 4)
                    conn.execute("UPDATE facts SET confidence=?,hits=?,last_seen=? WHERE id=?",
                                 (newc, r[1] + 1, now, iid))
                    return {"ok": True, "id": iid, "reinforced": True, "confidence": newc}
                conn.execute(
                    "INSERT INTO facts(id,lane,text,confidence,tags,created,last_seen,hits) "
                    "VALUES(?,?,?,?,?,?,?,1)",
                    (iid, self.lane, text[:1000], round(conf, 4),
                     json.dumps([str(t).lower() for t in (tags or [])][:5]), now, now))
            return {"ok": True, "id": iid, "confidence": round(conf, 4)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    # --- efferent ---------------------------------------------------------
    def _top_facts(self, *, min_conf: float = 0.0, limit: int = 20) -> list[dict]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT text,confidence,hits FROM facts WHERE confidence>=? "
                    "ORDER BY confidence DESC, hits DESC LIMIT ?", (min_conf, limit)).fetchall()
            return [{"text": t, "confidence": round(c, 3), "hits": h} for t, c, h in rows]
        except Exception:
            return []

    def recall(self, query: str, *, k: int = 20, keep: int = 5) -> dict:
        """Grounded answer, degrading leg-by-leg. Facts leg is always available (deterministic).
        The rag leg runs ONLY for lane='hustle' (the hustle corpus) — work recall is facts-only
        and NEVER touches the hustle rag index (firewall)."""
        out = {"query": query, "lane": self.lane, "facts": [], "answer": None,
               "sources": [], "served": "facts"}
        try:
            q = str(query or "")
            qtokens = {w for w in re.findall(r"[a-z0-9]{3,}", q.lower())}
            facts = self._top_facts(limit=200)
            scored = sorted(
                facts,
                key=lambda f: (len(qtokens & set(re.findall(r"[a-z0-9]{3,}", f["text"].lower()))),
                               f["confidence"]),
                reverse=True)
            out["facts"] = scored[:keep]
            if self.lane == "hustle":  # rag leg is hustle-corpus only; never for work
                try:
                    from tools.rag import ask  # lazy, best-effort
                    r = ask(q, k=k, keep=keep)
                    if isinstance(r, dict):
                        out["answer"] = r.get("answer")
                        out["sources"] = r.get("sources", [])
                        out["served"] = "rag"
                except Exception:
                    pass
        except Exception as exc:
            out["error"] = str(exc)[:200]
        return out

    def context(self, entrypoint: str = "session", *, top: int = 6,
                min_conf: float = 0.6, max_chars: int = 1600) -> str:
        """A capped, promptable markdown block for read-on-start injection. Returns '' when the
        brain has nothing yet (honest — no fabricated header)."""
        try:
            facts = self._top_facts(min_conf=min_conf, limit=top)
            st = self.state()
            lines = []
            if facts:
                lines.append(f"## Stack brain ({self.lane}) — learned facts")
                used = len(lines[0])
                for f in facts:
                    ln = f"- {f['text']}"
                    if used + len(ln) + 1 > max_chars:
                        break
                    lines.append(ln)
                    used += len(ln) + 1
            rk = st.get("recent_kinds") or {}
            if rk:
                top_kinds = ", ".join(f"{k}×{v}" for k, v in list(rk.items())[:6])
                lines.append(f"\nrecent signals ({st.get('total', 0)} in window): {top_kinds}")
                if st.get("last_critical"):
                    lines.append(f"last critical: {st['last_critical']}")
            return "\n".join(lines).strip()
        except Exception:
            return ""

    def state(self, *, window_s: float = 6 * 3600) -> dict:
        """Fold recent events into machine-state — the clean subscription a controller reads
        instead of re-reading N raw files."""
        try:
            since = _now() - window_s
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT kind,severity,subject,ts FROM events WHERE ts>=? ORDER BY ts DESC",
                    (since,)).fetchall()
            kinds: dict[str, int] = {}
            sev: dict[str, int] = {}
            last_crit = None
            for kind, s, subject, ts in rows:
                kinds[kind] = kinds.get(kind, 0) + 1
                sev[s] = sev.get(s, 0) + 1
                if s == "critical" and last_crit is None:
                    last_crit = f"{kind}: {subject}"[:200]
            kinds = dict(sorted(kinds.items(), key=lambda kv: kv[1], reverse=True))
            return {"ok": True, "lane": self.lane, "total": len(rows),
                    "recent_kinds": kinds, "severity": sev, "last_critical": last_crit}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200], "total": 0, "recent_kinds": {}}

    # --- maintenance ------------------------------------------------------
    def prune(self, *, event_days: int = 45, fact_floor: float = 0.2,
              half_life_days: float = 30.0) -> dict:
        """Bound the store: drop old non-critical events, age-decay fact confidence, drop the
        stale/low fact tail. Meant to run on a schedule (a launchd job)."""
        try:
            now = _now()
            cutoff = now - event_days * 86400
            keep_sev = ",".join(f"'{s}'" for s in _PRUNE_KEEP_SEVERITY)
            with self._conn() as conn:
                cur = conn.execute(
                    f"DELETE FROM events WHERE ts<? AND severity NOT IN ({keep_sev})", (cutoff,))
                dropped_events = cur.rowcount
                dropped_facts = 0
                for iid, conf, last_seen in conn.execute(
                        "SELECT id,confidence,last_seen FROM facts").fetchall():
                    age = (now - last_seen) / 86400.0
                    newc = round(conf * (0.5 ** (age / max(1.0, half_life_days))), 4)
                    if newc < fact_floor:
                        conn.execute("DELETE FROM facts WHERE id=?", (iid,))
                        dropped_facts += 1
                    else:
                        conn.execute("UPDATE facts SET confidence=?,last_seen=? WHERE id=?",
                                     (newc, now, iid))
            # checkpoint on a fresh connection (can't run inside the write txn above)
            try:
                with self._conn() as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            return {"ok": True, "dropped_events": dropped_events, "dropped_facts": dropped_facts}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}


# --- module-level convenience fns: FIREWALL-SAFE lane resolution ----------
# These resolve lane from an explicit arg or $NERVOUS_LANE. If the lane is unset/invalid they
# DROP (never default to hustle) so a shared choke point can't launder work data into the hustle
# brain. Hustle callers set NERVOUS_LANE=hustle (or pass lane=), work callers NERVOUS_LANE=work.
_INSTANCES: dict[str, NervousSystem] = {}


def _resolve_lane(lane: str | None) -> str | None:
    lane = lane or os.environ.get("NERVOUS_LANE")
    return lane if lane in VALID_LANES else None


def _ns(lane: str) -> NervousSystem:
    if lane not in _INSTANCES:
        _INSTANCES[lane] = NervousSystem(lane)
    return _INSTANCES[lane]


def observe(kind: str, source: str = "", *, lane: str | None = None, **kw) -> dict:
    lane = _resolve_lane(lane)
    if lane is None:
        return {"ok": False, "dropped": True, "reason": "lane-unset"}  # fail-closed, never hustle
    return _ns(lane).observe(kind, source, **kw)


def remember(text: str, *, lane: str | None = None, **kw) -> dict:
    lane = _resolve_lane(lane)
    if lane is None:
        return {"ok": False, "dropped": True, "reason": "lane-unset"}
    return _ns(lane).remember(text, **kw)


def recall(query: str, *, lane: str | None = None, **kw) -> dict:
    lane = _resolve_lane(lane)
    if lane is None:
        return {"ok": False, "dropped": True, "reason": "lane-unset"}
    return _ns(lane).recall(query, **kw)


def context(entrypoint: str = "session", *, lane: str | None = None, **kw) -> str:
    lane = _resolve_lane(lane)
    if lane is None:
        return ""
    return _ns(lane).context(entrypoint, **kw)


def state(*, lane: str | None = None, **kw) -> dict:
    lane = _resolve_lane(lane)
    if lane is None:
        return {"ok": False, "dropped": True, "reason": "lane-unset"}
    return _ns(lane).state(**kw)


# --- selftest -------------------------------------------------------------
def _selftest() -> int:
    import tempfile
    td = Path(tempfile.mkdtemp(prefix="nervous_selftest_"))
    hs = NervousSystem("hustle", db_path=td / "hustle.db")
    wk = NervousSystem("work", db_path=td / "work.db")
    checks: dict[str, bool] = {}

    # observe + dedup
    checks["observe_ok"] = hs.observe("funnel.sale", "stripe", subject="$39 kit").get("ok") is True
    hs.observe("watchdog.up", "x", dedup="k1")
    checks["dedup_coalesces"] = hs.observe("watchdog.up", "x", dedup="k1").get("deduped") is True
    # severity typo coerced, not lost
    checks["bad_severity_coerced"] = hs.observe("t.x", "s", severity="oops").get("ok") is True

    # remember + reinforce
    r1 = hs.remember("cold email is broken_site lane only, Tue/Wed/Thu <=5/day", confidence=0.9)
    r2 = hs.remember("cold email is broken_site lane only, Tue/Wed/Thu <=5/day", confidence=0.9)
    checks["remember_ok"] = r1.get("ok") is True
    checks["reinforce_dupe"] = r2.get("reinforced") is True

    # context reflects facts; state reflects events
    checks["context_has_fact"] = "cold email" in hs.context(min_conf=0.5)
    st = hs.state()
    checks["state_counts_events"] = st.get("total", 0) >= 3 and "funnel.sale" in st.get("recent_kinds", {})

    # LANE ISOLATION: work writes never land in hustle db and vice versa
    wk.observe("sf.deploy", "sfdc", subject="apex trigger")
    checks["work_event_isolated"] = (wk.state().get("total", 0) == 1
                                     and "sf.deploy" not in hs.state().get("recent_kinds", {}))
    wk.remember("SFDC deploys go sandbox->github->prod, never direct", confidence=0.95)
    checks["work_fact_isolated"] = (len(wk._top_facts()) == 1 and len(hs._top_facts()) == 1)

    # work recall must be facts-only (never invoke the hustle rag leg)
    wr = wk.recall("deploy path")
    checks["work_recall_facts_only"] = wr.get("served") == "facts"

    # module-level convenience DROPS on unset lane (never silent-hustle)
    old = os.environ.pop("NERVOUS_LANE", None)
    checks["unset_lane_drops"] = observe("x.y", "s").get("dropped") is True
    if old is not None:
        os.environ["NERVOUS_LANE"] = old
    # invalid lane raises
    try:
        NervousSystem("CompanyA")
        checks["invalid_lane_raises"] = False
    except ValueError:
        checks["invalid_lane_raises"] = True

    # prune runs
    checks["prune_ok"] = hs.prune().get("ok") is True

    # STATIC FIREWALL: this module must not IMPORT the work brain (match import lines only, so
    # the docstring/comments that name these modules to say "we don't import them" don't trip it).
    src = Path(__file__).read_text()
    bad_import = re.search(
        r"^\s*(?:import|from)\s+\S*(redacted_brain|redacted_session_ctx|voice)\b", src, re.M)
    checks["no_work_brain_import"] = bad_import is None

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print("NERVOUS-SYSTEM SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 core/nervous_system.py",
                                 description="Per-lane nervous-system spine.")
    ap.add_argument("--lane", choices=VALID_LANES, help="lane (or set $NERVOUS_LANE)")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    o = sub.add_parser("observe"); o.add_argument("--kind", required=True); o.add_argument("--source", default="")
    o.add_argument("--subject", default=""); o.add_argument("--severity", default="info")
    o.add_argument("--dedup", default=None); o.add_argument("--payload", default=None)
    rp = sub.add_parser("remember"); rp.add_argument("text"); rp.add_argument("--confidence", type=float, default=0.7)
    rc = sub.add_parser("recall"); rc.add_argument("query")
    cx = sub.add_parser("context"); cx.add_argument("--entrypoint", default="session")
    cx.add_argument("--top", type=int, default=6); cx.add_argument("--min-conf", dest="min_conf", type=float, default=0.6)
    cx.add_argument("--max-chars", dest="max_chars", type=int, default=1600)
    sub.add_parser("state")
    sub.add_parser("prune")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if args.selftest:
        return _selftest()
    lane = _resolve_lane(args.lane)
    if lane is None:
        print("ERROR: no lane. Pass --lane {hustle|work} or set $NERVOUS_LANE.", file=sys.stderr)
        return 2
    ns = NervousSystem(lane)
    if args.cmd == "observe":
        payload = json.loads(args.payload) if args.payload else None
        print(json.dumps(ns.observe(args.kind, args.source, subject=args.subject,
                                     severity=args.severity, dedup=args.dedup, payload=payload)))
    elif args.cmd == "remember":
        print(json.dumps(ns.remember(args.text, confidence=args.confidence)))
    elif args.cmd == "recall":
        print(json.dumps(ns.recall(args.query), indent=2))
    elif args.cmd == "context":
        print(ns.context(args.entrypoint, top=args.top, min_conf=args.min_conf, max_chars=args.max_chars))
    elif args.cmd == "state":
        print(json.dumps(ns.state(), indent=2))
    elif args.cmd == "prune":
        print(json.dumps(ns.prune()))
    else:
        ap.print_usage()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
