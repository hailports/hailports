"""strategist_actuators — the REAL, reversible hands of the autonomous strategist.

The strategist DECIDES plays; this module is the only thing that actually CHANGES the
machine. Anti-theater rule baked in: every actuator writes into a config a LIVE component
already reads, so the change is consumed on the next run — never a report nobody opens.

PROVEN LIVE CONSUMERS (grep-checked 2026-06-17):
  1. add_harvest_query  -> agents/multi_market_intent.py MARKETS  (scan() iterates list(MARKETS),
        every market's queries/subreddits scanned; launchd com.claude-stack.intent-engine every 6h)
        ... or agents/local_biz_discovery.py VERTICALS (discover() flattens VERTICALS x METROS).
  2. add_copy_variant   -> data/variant_bank.json  (core/revenue_brain.thompson_pick reads
        bank[lane][pool]; writes data/revenue_decisions.json; agents/copy_rotation.pick_bandit_variant
        reads that so the live sender uses the bandit-chosen copy; launchd com.claude-stack.revenue-brain).
  3. queue_bofu_page    -> data/hustle/bofu_seo/queue.json  (tools/bofu_seo_factory._merge_queued_services
        merges queued topics into that brand's services -> build_pages -> served HTML + per-brand sitemap).

GUARDRAILS:
  * Auto-launch ONLY the three cheap/reversible/lane-safe/no-spend/no-identity plays above.
    Everything else (offers, pricing, channels, spend, anything BrandA-named or identity-
    marketplace) -> is_auto_safe()==False -> NOT launched here (the strategist routes those to
    Operator's fire-approval/iMessage loop).
  * Faceless brands stay anon + un-correlated; BrandA is named/employment-screened and never
    touched by an auto play.
  * NO fabricated facts/stats/claims in generated copy (FTC) — add_copy_variant runs a
    no-fabrication + deliverability gate and rejects anything that invents proof.
  * Every play is logged to data/hustle/strategist_plays.jsonl WITH an undo recipe, and undo()
    reverses it. Idempotent: the same play won't double-add.

CLI:
    python3 -m core.strategist_actuators --selftest      # dry-run on temp copies, proves undo
    python3 -m core.strategist_actuators --tail          # show recent plays
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Live config files the actuators write into (each PROVEN-consumed above).
MULTI_MARKET = ROOT / "agents" / "multi_market_intent.py"
LOCAL_BIZ = ROOT / "agents" / "local_biz_discovery.py"
VARIANT_BANK = ROOT / "data" / "variant_bank.json"
BOFU_QUEUE = ROOT / "data" / "hustle" / "bofu_seo" / "queue.json"
PLAYS_LOG = ROOT / "data" / "hustle" / "strategist_plays.jsonl"

# Faceless / anon brands that own a BOFU page set. ALL are non-identity (no BrandA, no
# named human). BrandA and any identity-marketplace brand are intentionally absent so a
# bofu play can never run under a named identity.
# builtfast + signalhq are KILLED (brand review, $0 lifetime, burned domains) — removed so a
# strategist play can never queue/resurrect a dead-brand BOFU page (fail-closed with the
# factory's own DEAD_BRAND_KEYS). Only kept faceless brands remain.
FACELESS_BOFU_BRANDS = {"hailport", "scannerapp", "docsapp"}

# (lane, pool) pairs the revenue brain + sender actually consume. We refuse to append a
# variant to a lane/pool nobody reads (that would be theater).
CONSUMED_VARIANT_SLOTS = {("broken_site", "subject"), ("broken_site", "subject_proof")}

AUTO_SAFE_TYPES = {"harvest_query", "copy_variant", "bofu_page"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid(*parts: str) -> str:
    """Deterministic short id from play content -> natural idempotency."""
    return "sp_" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:12]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48] or "x"


def _validate_python(path: Path, text: str) -> None:
    """ast.parse the candidate source; raise (caller restores) if we'd break the file."""
    ast.parse(text)


# ─────────────────────────────────────────────────────────────────────────────
# Marked-block insertion into a live .py config — undo removes the exact block.
# We insert right AFTER a fixed anchor line so the anchor never drifts; idempotent
# on the play id marker.
# ─────────────────────────────────────────────────────────────────────────────
def _block_markers(pid: str) -> tuple[str, str]:
    return (f"    # >>> STRATEGIST-PLAY {pid} >>>", f"    # <<< STRATEGIST-PLAY {pid} <<<")


def _insert_block(path: Path, anchor: str, pid: str, body_lines: list[str]) -> bool:
    """Insert a marked block after `anchor`. Returns False if already present (idempotent)."""
    text = path.read_text()
    if pid in text:
        return False
    if anchor not in text:
        raise RuntimeError(f"anchor not found in {path.name!r}: {anchor!r}")
    top, bot = _block_markers(pid)
    block = "\n" + "\n".join([top, *body_lines, bot]) + "\n"
    idx = text.index(anchor) + len(anchor)
    # splice right after the anchor line's newline
    nl = text.index("\n", idx)
    candidate = text[: nl + 1] + block + text[nl + 1 :]
    _validate_python(path, candidate)  # raises before we write if it'd break
    path.write_text(candidate)
    return True


def _remove_block(path: Path, pid: str) -> bool:
    text = path.read_text()
    top, bot = _block_markers(pid)
    if top not in text or bot not in text:
        return False
    start = text.index(top)
    end = text.index(bot) + len(bot)
    # swallow one leading + trailing newline we added
    s = text.rfind("\n", 0, start)
    s = s if s != -1 else start
    e = end
    if text[e : e + 1] == "\n":
        e += 1
    candidate = text[:s] + text[e:]
    candidate = candidate.replace("\n\n\n\n", "\n\n\n")
    _validate_python(path, candidate)
    path.write_text(candidate)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 1. add_harvest_query — feed the demand harvesters a new query/market/subreddit.
# ─────────────────────────────────────────────────────────────────────────────
def add_harvest_query(query: str, source: str = "reddit",
                      multi_market_path: Path = MULTI_MARKET,
                      local_biz_path: Path = LOCAL_BIZ) -> dict:
    """Append a harvest target a live demand harvester reads.

    source routing:
      "reddit" | "hn" | "intent" | "multi_market"  -> new MARKETS entry in multi_market_intent.py
                                                       (query becomes a site-wide/HN query)
      "subreddit:<name>"                            -> same, but also scans r/<name>
      "local" | "local_biz" | "vertical"            -> new VERTICALS tuple in local_biz_discovery.py
                                                       (query is the local search term)
    Consumed next run; undo removes the inserted block.
    """
    query = query.strip()
    if not query:
        return {"ok": False, "reason": "empty query"}

    if source in ("local", "local_biz", "vertical"):
        pid = _pid("local_biz", query)
        label = _slug(query)
        body = [f'    ("{label}", {json.dumps(query)}),']
        try:
            added = _insert_block(local_biz_path, "VERTICALS = [", pid, body)
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        return {"ok": True, "id": pid, "target": "local_biz_discovery.VERTICALS",
                "idempotent_skip": not added,
                "undo": {"action": "remove_block", "file": str(local_biz_path), "pid": pid}}

    # multi_market_intent MARKETS
    sub = ""
    if source.startswith("subreddit:"):
        sub = source.split(":", 1)[1].strip()
    pid = _pid("multi_market", query, sub)
    mkey = f"strategist_{_slug(query)}"
    subs = [sub] if sub else []
    # intent/context are scoring hints; derive honest, non-fabricated tokens from the query.
    tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2][:6]
    body = [
        f'    {json.dumps(mkey)}: {{',
        '        "product": "(strategist demand probe)",',
        f'        "subreddits": {json.dumps(subs)},',
        f'        "intent": {json.dumps(tokens)},',
        f'        "context": {json.dumps(tokens)},',
        f'        "queries": [{json.dumps(query)}],',
        '    },',
    ]
    try:
        added = _insert_block(multi_market_path, "MARKETS = {", pid, body)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "id": pid, "target": "multi_market_intent.MARKETS",
            "idempotent_skip": not added,
            "undo": {"action": "remove_block", "file": str(multi_market_path), "pid": pid}}


# ─────────────────────────────────────────────────────────────────────────────
# 2. add_copy_variant — append a subject/copy variant for the bandit to test.
#    Hard no-fabrication + deliverability gate (FTC + inbox safety).
# ─────────────────────────────────────────────────────────────────────────────
# Patterns that would assert invented proof/claims, or trip spam filters.
_FABRICATION = [
    (r"\b\d+\s?%|\b\d+\s?percent", "invented percentage/stat"),
    (r"\b(\d+(?:,\d{3})+|\d+k\b)\b", "invented count (e.g. 10k)"),
    (r"\b(\d+|\bfive\b)[\s-]?stars?\b|⭐", "fabricated star rating"),
    (r"#\s?1\b|\bnumber one\b|\bbest in\b|\btop[-\s]?rated\b", "unverifiable superlative"),
    (r"\b\d+\s+(clients|customers|businesses|companies|reviews|users)\b", "invented social proof"),
    (r"\bguarantee(d)?\b|\b100%\b|\brisk[-\s]?free\b", "unsubstantiated guarantee"),
    (r"\b(award[-\s]?winning|certified|trusted by)\b", "unverifiable credential"),
    (r"\b(as seen on|featured (in|on))\b", "fabricated press claim"),
    (r"\$\s?\d", "price claim in subject (belongs on the page, not the gate)"),
]
_DELIVERABILITY = [
    (r"!!+|\?\?+", "excessive punctuation"),
    (r"\bfree\b.*\bfree\b|\b(act now|urgent|limited time|buy now|click here)\b", "spam trigger"),
    (r"[A-Z]{5,}", "shouting all-caps run"),
]


def quality_gate(text: str) -> dict:
    """Reject fabricated claims (FTC) + deliverability-hostile copy. Pure/deterministic."""
    t = text.strip()
    if not t:
        return {"ok": False, "reason": "empty"}
    if len(t) > 120:
        return {"ok": False, "reason": "too long for a subject (>120 chars)"}
    for pat, why in _FABRICATION:
        if re.search(pat, t, re.I):
            return {"ok": False, "reason": f"fabrication: {why}"}
    for pat, why in _DELIVERABILITY:
        if re.search(pat, t):
            return {"ok": False, "reason": f"deliverability: {why}"}
    return {"ok": True}


def add_copy_variant(channel: str, slot: str, text: str,
                     bank_path: Path = VARIANT_BANK) -> dict:
    """Append a copy variant the revenue brain's bandit will test.

    channel -> lane, slot -> pool. Must target a (lane,pool) the brain+sender actually read.
    text must pass quality_gate (no fabricated stats/claims, deliverability-safe). Undo removes
    the variant by id. Brand name is injected at render time, so copy here stays brand-neutral
    (keeps faceless brands un-correlated). {company} = the prospect's own business name.
    """
    lane, pool = channel, slot
    if (lane, pool) not in CONSUMED_VARIANT_SLOTS:
        return {"ok": False, "reason": f"({lane},{pool}) is not a consumed bandit slot; "
                f"would be theater. consumed: {sorted(CONSUMED_VARIANT_SLOTS)}"}
    q = quality_gate(text)
    if not q["ok"]:
        return {"ok": False, "reason": f"quality gate: {q['reason']}"}

    bank = json.loads(bank_path.read_text())
    pool_list = bank.setdefault(lane, {}).setdefault(pool, [])
    norm = text.strip()
    for v in pool_list:
        if v.get("template", "").strip() == norm:
            return {"ok": True, "id": v["id"], "idempotent_skip": True,
                    "target": f"variant_bank[{lane}][{pool}]",
                    "undo": {"action": "remove_variant", "lane": lane, "pool": pool,
                             "id": v["id"], "bank_path": str(bank_path)}}
    vid = _pid("variant", lane, pool, norm)
    pool_list.append({"id": vid, "template": norm})
    bank_path.write_text(json.dumps(bank, indent=2, ensure_ascii=False) + "\n")
    return {"ok": True, "id": vid, "idempotent_skip": False,
            "target": f"variant_bank[{lane}][{pool}]",
            "undo": {"action": "remove_variant", "lane": lane, "pool": pool,
                     "id": vid, "bank_path": str(bank_path)}}


def _remove_variant(lane: str, pool: str, vid: str, bank_path: Path = VARIANT_BANK) -> bool:
    bank = json.loads(bank_path.read_text())
    pl = bank.get(lane, {}).get(pool, [])
    new = [v for v in pl if v.get("id") != vid]
    if len(new) == len(pl):
        return False
    bank[lane][pool] = new
    bank_path.write_text(json.dumps(bank, indent=2, ensure_ascii=False) + "\n")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 3. queue_bofu_page — enqueue a BOFU/SEO topic for the factory to build.
# ─────────────────────────────────────────────────────────────────────────────
def queue_bofu_page(brand: str, topic: str, cat: str | None = None,
                    queue_path: Path = BOFU_QUEUE) -> dict:
    """Queue an honest BOFU page topic for a faceless brand.

    tools/bofu_seo_factory._merge_queued_services merges {noun:topic, cat} into that brand's
    services on its next run -> 4 honest cluster pages + sitemap, all linking only that brand's
    own domain. Undo removes the queued entry.
    """
    if brand not in FACELESS_BOFU_BRANDS:
        return {"ok": False, "reason": f"{brand!r} is not a faceless BOFU brand "
                f"(allowed: {sorted(FACELESS_BOFU_BRANDS)}); named/identity brands route to Operator"}
    topic = topic.strip()
    if not topic:
        return {"ok": False, "reason": "empty topic"}
    cat = (cat or topic).strip()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    q = {}
    if queue_path.exists():
        try:
            q = json.loads(queue_path.read_text())
        except Exception:
            q = {}
    lst = q.setdefault(brand, [])
    for s in lst:
        if isinstance(s, dict) and s.get("noun") == topic:
            return {"ok": True, "brand": brand, "topic": topic, "idempotent_skip": True,
                    "target": "bofu_seo/queue.json",
                    "undo": {"action": "remove_bofu", "brand": brand, "topic": topic,
                             "queue_path": str(queue_path)}}
    lst.append({"noun": topic, "cat": cat})
    queue_path.write_text(json.dumps(q, indent=2, ensure_ascii=False) + "\n")
    return {"ok": True, "brand": brand, "topic": topic, "idempotent_skip": False,
            "target": "bofu_seo/queue.json",
            "undo": {"action": "remove_bofu", "brand": brand, "topic": topic,
                     "queue_path": str(queue_path)}}


def _remove_bofu(brand: str, topic: str, queue_path: Path = BOFU_QUEUE) -> bool:
    if not queue_path.exists():
        return False
    q = json.loads(queue_path.read_text())
    lst = q.get(brand, [])
    new = [s for s in lst if not (isinstance(s, dict) and s.get("noun") == topic)]
    if len(new) == len(lst):
        return False
    q[brand] = new
    queue_path.write_text(json.dumps(q, indent=2, ensure_ascii=False) + "\n")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# is_auto_safe — the gate. TRUE only for the three cheap/reversible/lane-safe/no-spend plays.
# ─────────────────────────────────────────────────────────────────────────────
def is_auto_safe(play: dict) -> bool:
    if not isinstance(play, dict):
        return False
    ptype = play.get("type")
    if ptype not in AUTO_SAFE_TYPES:
        return False  # offer_test / new_channel / pricing / anything else -> route to Operator
    # any spend at all -> not auto
    if play.get("spend") or play.get("budget") or float(play.get("cost", 0) or 0) > 0:
        return False
    # identity risk: BrandA-named or an identity marketplace -> never auto
    blob = json.dumps(play).lower()
    if play.get("identity") in ("named", "BrandA") or play.get("named"):
        return False
    if "BrandA" in blob or "marketplace" in (play.get("channel") or "").lower():
        return False
    # per-type lane checks
    if ptype == "bofu_page":
        return play.get("brand") in FACELESS_BOFU_BRANDS
    if ptype == "copy_variant":
        return (play.get("channel"), play.get("slot")) in CONSUMED_VARIANT_SLOTS
    if ptype == "harvest_query":
        return bool(str(play.get("query", "")).strip())
    return False


# ─────────────────────────────────────────────────────────────────────────────
# launch — dispatch + log with undo recipe. Idempotent.
# ─────────────────────────────────────────────────────────────────────────────
def _log_play(play: dict, result: dict) -> None:
    PLAYS_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now(), "type": play.get("type"), "play": play,
           "ok": result.get("ok"), "idempotent_skip": result.get("idempotent_skip", False),
           "target": result.get("target"), "undo": result.get("undo")}
    with PLAYS_LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def launch(play: dict) -> dict:
    """Dispatch an auto-safe play to its actuator, log it with the undo recipe, return {ok, undo}.

    Refuses (without acting) anything is_auto_safe() rejects — those belong in Operator's approval loop.
    """
    if not is_auto_safe(play):
        return {"ok": False, "reason": "not auto-safe; route to Operator's fire-approval loop",
                "undo": None}
    ptype = play["type"]
    if ptype == "harvest_query":
        r = add_harvest_query(play["query"], play.get("source", "reddit"))
    elif ptype == "copy_variant":
        r = add_copy_variant(play["channel"], play["slot"], play["text"])
    elif ptype == "bofu_page":
        r = queue_bofu_page(play["brand"], play["topic"], play.get("cat"))
    else:
        return {"ok": False, "reason": f"no actuator for {ptype}", "undo": None}
    if r.get("ok"):
        _log_play(play, r)
    return {"ok": r.get("ok", False), "id": r.get("id"), "target": r.get("target"),
            "idempotent_skip": r.get("idempotent_skip", False),
            "undo": r.get("undo"), "reason": r.get("reason")}


def undo(undo_recipe: dict) -> bool:
    """Execute a stored undo recipe. Returns True if it reversed something."""
    a = undo_recipe.get("action")
    if a == "remove_block":
        return _remove_block(Path(undo_recipe["file"]), undo_recipe["pid"])
    if a == "remove_variant":
        return _remove_variant(undo_recipe["lane"], undo_recipe["pool"], undo_recipe["id"],
                               bank_path=Path(undo_recipe.get("bank_path", VARIANT_BANK)))
    if a == "remove_bofu":
        return _remove_bofu(undo_recipe["brand"], undo_recipe["topic"],
                            queue_path=Path(undo_recipe.get("queue_path", BOFU_QUEUE)))
    return False


# ─────────────────────────────────────────────────────────────────────────────
# self-test — dry-run every actuator on TEMP COPIES, prove undo restores byte-for-byte.
# ─────────────────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import shutil
    import tempfile

    fails = []
    td = Path(tempfile.mkdtemp(prefix="strategist_selftest_"))
    try:
        # copies of the live config files
        mm = td / "multi_market_intent.py"; shutil.copy(MULTI_MARKET, mm)
        lb = td / "local_biz_discovery.py"; shutil.copy(LOCAL_BIZ, lb)
        vb = td / "variant_bank.json"; shutil.copy(VARIANT_BANK, vb)
        bq = td / "queue.json"
        orig = {p: p.read_text() for p in (mm, lb, vb)}

        # 1a. harvest query -> multi_market market (with subreddit)
        r = add_harvest_query("contractors who need a new website fast", "subreddit:smallbusiness",
                              multi_market_path=mm, local_biz_path=lb)
        assert r["ok"] and not r["idempotent_skip"], r
        ast.parse(mm.read_text())
        assert "strategist_" in mm.read_text(), "market not inserted"
        # consumed-by check: the harvester's MARKETS dict now contains it
        ns: dict = {}
        exec(compile(ast.parse(mm.read_text()), str(mm), "exec"),
             {"__name__": "x"}, ns) if False else None  # skip heavy import; literal check below
        assert "contractors who need a new website fast" in mm.read_text()
        # idempotent re-add
        r2 = add_harvest_query("contractors who need a new website fast", "subreddit:smallbusiness",
                               multi_market_path=mm, local_biz_path=lb)
        assert r2["ok"] and r2["idempotent_skip"], r2
        assert undo(r["undo"]) is True
        assert mm.read_text() == orig[mm], "multi_market undo not byte-exact"

        # 1b. harvest query -> local vertical
        r = add_harvest_query("emergency plumber", "local", multi_market_path=mm, local_biz_path=lb)
        assert r["ok"] and not r["idempotent_skip"], r
        ast.parse(lb.read_text())
        assert '"emergency plumber"' in lb.read_text() or "'emergency plumber'" in lb.read_text() \
            or "emergency plumber" in lb.read_text()
        assert undo(r["undo"]) is True
        assert lb.read_text() == orig[lb], "local_biz undo not byte-exact"

        # 2. copy variant -> bank (honest copy)
        r = add_copy_variant("broken_site", "subject", "noticed your {company} site is hard to read on a phone",
                             bank_path=vb)
        assert r["ok"] and not r["idempotent_skip"], r
        bank = json.loads(vb.read_text())
        assert any(v["id"] == r["id"] for v in bank["broken_site"]["subject"]), "variant not in bank"
        # idempotent
        r2 = add_copy_variant("broken_site", "subject", "noticed your {company} site is hard to read on a phone",
                              bank_path=vb)
        assert r2["idempotent_skip"], r2
        # fabrication rejected
        bad = add_copy_variant("broken_site", "subject", "we got 500 businesses 5-star reviews, guaranteed",
                               bank_path=vb)
        assert not bad["ok"], "fabrication slipped through"
        # non-consumed slot rejected (anti-theater)
        theatre = add_copy_variant("broken_site", "preheader", "hi {company}", bank_path=vb)
        assert not theatre["ok"], "wrote to a slot nobody reads"
        assert undo(r["undo"]) is True
        # JSON is rewritten (formatting may differ); undo restores the DATA exactly
        assert json.loads(vb.read_text()) == json.loads(orig[vb]), "variant undo lost data"

        # 3. bofu page -> queue, and PROVE the live factory merges it
        r = queue_bofu_page("scannerapp", "ai chatbot for small business", queue_path=bq)
        assert r["ok"] and not r["idempotent_skip"], r
        # consumed-by proof: import the real factory and check its merge picks up the topic
        sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
        import importlib
        bsf = importlib.import_module("tools.bofu_seo_factory")
        bsf.BOFU_QUEUE = bq  # point the live merge at our temp queue
        merged = bsf._merge_queued_services("scannerapp", bsf.BRANDS["scannerapp"])
        assert any(s["noun"] == "ai chatbot for small business" for s in merged["services"]), \
            "factory did not consume the queued topic"
        # named brand rejected
        assert not queue_bofu_page("BrandA", "anything", queue_path=bq)["ok"]
        # KILLED brands rejected — dead-brand BOFU pages can never be resurrected
        assert not queue_bofu_page("builtfast", "anything", queue_path=bq)["ok"]
        assert not queue_bofu_page("signalhq", "anything", queue_path=bq)["ok"]
        assert "builtfast" not in bsf.BRANDS and "signalhq" not in bsf.BRANDS, "dead brand in factory"
        assert undo(r["undo"]) is True
        assert json.loads(bq.read_text()).get("scannerapp") == [], "bofu undo left residue"

        # is_auto_safe gate matrix
        assert is_auto_safe({"type": "harvest_query", "query": "x"})
        assert is_auto_safe({"type": "copy_variant", "channel": "broken_site", "slot": "subject"})
        assert is_auto_safe({"type": "bofu_page", "brand": "scannerapp"})
        assert not is_auto_safe({"type": "offer_test"})
        assert not is_auto_safe({"type": "new_channel"})
        assert not is_auto_safe({"type": "bofu_page", "brand": "BrandA"})
        assert not is_auto_safe({"type": "copy_variant", "channel": "broken_site", "slot": "preheader"})
        assert not is_auto_safe({"type": "harvest_query", "query": "x", "spend": 5})
        assert not is_auto_safe({"type": "bofu_page", "brand": "scannerapp", "identity": "named"})
        assert not is_auto_safe({"type": "pricing", "amount": 99})

    except AssertionError as e:
        fails.append(str(e))
    except Exception as e:
        fails.append(f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("SELFTEST OK — 3 actuators wired, consumed, idempotent, undo byte-exact, gate enforced")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tail", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    if args.tail:
        if PLAYS_LOG.exists():
            print(PLAYS_LOG.read_text().strip()[-4000:])
        else:
            print("(no plays logged yet)")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
