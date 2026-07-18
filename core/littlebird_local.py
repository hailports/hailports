"""Littlebird AI — meeting notes, transcripts, daily summaries.
Reads from local IndexedDB (LevelDB) + app data. No API needed."""

import asyncio
import subprocess
import os
import json
import re
import logging
import shlex
from datetime import datetime, timedelta
from tools.base import BaseTool, make_tool_def

log = logging.getLogger(__name__)

LITTLEBIRD_DATA = os.path.expanduser("~/Library/Application Support/Littlebird")
INDEXEDDB_PATH = os.path.join(LITTLEBIRD_DATA, "IndexedDB/file__0.indexeddb.leveldb")
redacted_WORKSPACE = os.path.expanduser("~/.openclaw/workspace/CompanyA-local")
NOTE_PATTERN = (
    r"meeting notes?|call notes?|notes|summary|recap|work summary|daily work summary|"
    r"eod summary|personifiedTranscript"
)
TRANSCRIPT_PATTERN = r"transcript|personifiedTranscript|audio transcription|speaker"
ACTION_PATTERN = r"action items?|action_items|todoResults|to[- ]?do|follow[- ]?up|next steps?|owner|due"
SUMMARY_PATTERN = r"work summary|daily work summary|eod summary|end of day|recap|summary"

# Meeting ownership: if organizer/attendee contains "CompanyA" → Operator's meeting, otherwise → Operator2's
ALEX_DOMAIN = "CompanyA"
NICOLE_DOMAINS = ["kipi.ai", "cnovate.io", "nicoleredacted", "Operator2.Operator"]



def _grep_littlebird_strings(pattern, max_results=40, context_lines=0, regex=True):
    """Search Littlebird local browser data without using network/API calls."""
    try:
        max_results = max(1, min(int(max_results), 2000))
        context_lines = max(0, min(int(context_lines), 8))
    except Exception:
        max_results = 40
        context_lines = 0

    grep_mode = "-Ei" if regex else "-iF"
    context = f"-B{context_lines} -A{context_lines}" if context_lines else ""
    # Includes IndexedDB plus Electron session/cache files where Littlebird may
    # keep generated notes/transcript fragments. Generic Cache_Data is excluded:
    # it contains unrelated browser assets and makes note extraction noisy.
    cmd = (
        f'find {shlex.quote(LITTLEBIRD_DATA)} '
        r'\( -path "*/IndexedDB/*" -o -path "*/Session Storage/*" -o -name "notifications-v1.json" \) '
        r'-type f ! -name LOCK ! -name LOG ! -name "LOG.old" ! -name CURRENT ! -name "MANIFEST-*" -print0 2>/dev/null | '
        f'xargs -0 strings 2>/dev/null | grep {context} {grep_mode} -- {shlex.quote(pattern)} | '
        f'head -n {max_results}'
    )
    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=25,
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


def _extract_strings(path, pattern, max_results=20):
    """Extract matching strings from Littlebird local data."""
    return _grep_littlebird_strings(pattern, max_results=max_results, regex=True)


def _date_terms(start_date="", end_date="", days=7):
    """Build human/date tokens for week-window searches."""
    try:
        if start_date:
            start = datetime.fromisoformat(str(start_date)[:10])
        else:
            start = datetime.now() - timedelta(days=int(days) - 1)
        if end_date:
            end = datetime.fromisoformat(str(end_date)[:10])
        else:
            end = datetime.now()
    except Exception:
        return []

    terms = set()
    cur = start
    while cur.date() <= end.date():
        terms.update({
            cur.strftime("%Y-%m-%d"),
            cur.strftime("%m/%d/%Y").lstrip("0").replace("/0", "/"),
            cur.strftime("%m/%d").lstrip("0").replace("/0", "/"),
            cur.strftime("%b %-d"),
            cur.strftime("%B %-d"),
            cur.strftime("%a"),
        })
        cur += timedelta(days=1)
    return sorted(terms)


def _clean_lines(text, max_items=100):
    """Dedupe noisy LevelDB string output while preserving order."""
    seen = set()
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or len(line) < 3:
            continue
        key = line[:300]
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) >= max_items:
            break
    return lines


def _all_littlebird_strings():
    """Read all local Littlebird strings once for structured week extraction."""
    cmd = (
        f'find {shlex.quote(LITTLEBIRD_DATA)} '
        r'\( -path "*/IndexedDB/*" -o -path "*/Session Storage/*" -o -name "notifications-v1.json" \) '
        r'-type f ! -name LOCK ! -name LOG ! -name "LOG.old" ! -name CURRENT ! -name "MANIFEST-*" -print0 2>/dev/null | '
        r'xargs -0 strings 2>/dev/null'
    )
    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=35,
        )
        return _clean_lines(result.stdout, 100000)
    except Exception:
        return []


def _extract_week_blocks(lines, date_terms, max_blocks=1000):
    """Extract record-shaped blocks around week/date anchors without a 20-meeting cap."""
    if not lines:
        return []
    anchors = [
        term for term in date_terms
        if re.search(r"\d{4}-\d{2}-\d{2}|/\d{4}|[A-Za-z]{3,9} \d{1,2}", term)
    ]
    if not anchors:
        anchors = []

    records = []
    seen = set()
    signal = re.compile(
        r"createdAt|updatedAt|firstChunkTimestamp|name|tldr|summary|notes|"
        r"personifiedTranscript|transcript|prepContent|attendees|speakers|content",
        re.I,
    )
    junk = re.compile(
        r"MANIFEST|Recovering log|Reusing old log|searchIndexVersion|namespace-|"
        r"Cache_Data|aexp-static|nodeName|attributeName|css|leveldb",
        re.I,
    )
    for idx, line in enumerate(lines):
        if not any(term in line for term in anchors):
            continue
        start = max(0, idx - 20)
        end = min(len(lines), idx + 120)
        window = [item for item in lines[start:end] if not junk.search(item)]
        if not any(signal.search(item) for item in window):
            continue
        block_lines = [
            item for item in window
            if len(item) >= 3 and item not in {"0", "1", "true", "false", "null"}
        ][:100]
        if len(block_lines) < 5:
            continue
        text = "\n".join(block_lines)
        key = text[:700]
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "anchor": line,
            "excerpt": block_lines[:80],
        })
        if len(records) >= max_blocks:
            break
    return records


def _clean_littlebird_line(line):
    """Convert LevelDB string fragments into readable evidence lines."""
    text = str(line or "")
    if '"content":"' in text:
        text = text.split('"content":"', 1)[1]
        text = text.split('","threadId"', 1)[0]
    text = text.replace("\\n", "\n").replace("\\r", "\n").replace('\\"', '"')
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"[^\w\s@.,:;!?/#$%&()<>+=\-\[\]\"']", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_.,;:")
    if len(text) < 5:
        return ""
    if re.search(r"MANIFEST|Level-0 table|Compacted|Delete type=|Generated table|searchIndexVersion|namespace-", text, re.I):
        return ""
    letters = sum(1 for ch in text if ch.isalpha())
    if letters < 4:
        return ""
    if letters / max(1, len(text)) < 0.25:
        return ""
    return text[:900]


def _extract_title(lines):
    for line in lines:
        if "name" in line.lower():
            cleaned = _clean_littlebird_line(re.sub(r"^.*name[\"'):\s]*", "", line, flags=re.I))
            if cleaned and len(cleaned) <= 120:
                return cleaned
    for line in lines:
        cleaned = _clean_littlebird_line(line)
        if cleaned and re.search(r"Salesforce|Dashboard|Data|Sprint|Warranty|Sandbox|Deployment|Review|Discussion|Standup|Migration|Meeting", cleaned, re.I):
            return cleaned[:120]
    return ""


def _clean_week_records(blocks, limit=120):
    """Produce model-friendly Littlebird meeting records from raw string blocks."""
    records = []
    seen = set()
    for block in blocks:
        raw_lines = block.get("excerpt") or []
        clean_lines = [_clean_littlebird_line(line) for line in raw_lines]
        clean_lines = [line for line in clean_lines if line]
        if not clean_lines:
            continue

        date = ""
        for line in clean_lines:
            m = re.search(r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}", line)
            if m:
                date = m.group(0)
                break

        title = _extract_title(raw_lines)
        tldr = ""
        transcript = []
        actions = []
        for line in clean_lines:
            low = line.lower()
            if not tldr and ("tldr" in low or "summary" in low):
                tldr = re.sub(r"^.*tldr[\"':\s]*", "", line, flags=re.I)[:500]
            if re.search(r"\[(you|others|Operator|.+?)\]:", line, re.I):
                transcript.append(line)
            if re.search(r"\b(action|decision|next step|follow up|follow-up|owner|needs|todo|blocker|risk|pending|waiting)\b", line, re.I):
                actions.append(line)

        if not title and not tldr and not transcript and not actions:
            continue
        key = (date, title, (tldr or " ".join(clean_lines[:3]))[:160])
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "date": date,
            "title": title or "Littlebird meeting/note record",
            "summary": tldr or " ".join(clean_lines[:4])[:700],
            "transcript_excerpt": transcript[:8],
            "actionish_lines": actions[:10],
            "evidence_line_count": len(clean_lines),
        })
        if len(records) >= limit:
            break
    return records


def _render_clean_summary(records):
    if not records:
        return "No cleaned Littlebird meeting records extracted."
    lines = ["Cleaned Littlebird meeting/note records:"]
    for rec in records[:40]:
        header = f"- {rec.get('date') or 'undated'} | {rec.get('title')}"
        lines.append(header)
        if rec.get("summary"):
            lines.append(f"  Summary: {rec['summary'][:360]}")
        if rec.get("actionish_lines"):
            lines.append("  Action/decision signals:")
            for action in rec["actionish_lines"][:4]:
                lines.append(f"  - {action[:240]}")
        if rec.get("transcript_excerpt"):
            lines.append("  Transcript excerpt:")
            for item in rec["transcript_excerpt"][:3]:
                lines.append(f"  - {item[:220]}")
    return "\n".join(lines)


def _compact_week_blocks(blocks, limit=5):
    compact = []
    for block in blocks[:limit]:
        excerpt = []
        for line in block.get("excerpt") or []:
            cleaned = _clean_littlebird_line(line)
            if cleaned:
                excerpt.append(cleaned[:240])
            if len(excerpt) >= 8:
                break
        compact.append({
            "anchor": _clean_littlebird_line(block.get("anchor", ""))[:240],
            "excerpt": excerpt,
        })
    return compact


def _fit_json_payload(payload, max_chars, cleaned_records, cleaned_summary, sections):
    """Return valid JSON under the requested chat budget."""
    encoded = json.dumps(payload, indent=2)
    if len(encoded) <= max_chars:
        return encoded

    compact = dict(payload)
    compact["cleaned"] = {
        "record_count": len(cleaned_records),
        "records": cleaned_records[:20],
        "summary_markdown": "\n".join(cleaned_summary.splitlines()[:120]),
    }
    compact["sections"] = {
        key: (_compact_week_blocks(value, 5) if key == "week_record_blocks" else value[:20])
        for key, value in sections.items()
    }
    compact["truncated_for_chat"] = True
    compact["full_sweep_available_at"] = payload.get("saved_full_sweep")
    encoded = json.dumps(compact, indent=2)
    if len(encoded) <= max_chars:
        return encoded

    compact["cleaned"] = {
        "record_count": len(cleaned_records),
        "records": cleaned_records[:10],
        "summary_markdown": "\n".join(cleaned_summary.splitlines()[:70]),
    }
    compact["sections"] = {
        key: (_compact_week_blocks(value, 2) if key == "week_record_blocks" else value[:5])
        for key, value in sections.items()
    }
    encoded = json.dumps(compact, indent=2)
    if len(encoded) <= max_chars:
        return encoded

    compact["sections"] = {}
    compact["cleaned"] = {
        "record_count": len(cleaned_records),
        "records": cleaned_records[:5],
        "summary_markdown": "\n".join(cleaned_summary.splitlines()[:40]),
    }
    return json.dumps(compact, indent=2)


def _extract_notes_context(path, context_lines=3, max_results=10):
    """Extract meeting notes with surrounding context from LevelDB."""
    return _grep_littlebird_strings(NOTE_PATTERN, max_results=max_results * 10, context_lines=context_lines)


def _search_all_data(path, query, max_results=30):
    """Full text search across all Littlebird local data."""
    result = _grep_littlebird_strings(query, max_results=max_results, regex=False)
    return result if result else "No results found."


# ── V8/IndexedDB decoder ──────────────────────────────────────────────────────────────────────
# Littlebird's IndexedDB store is Chromium V8 structured-clone (NOT JSON): meeting notes live as
# markdown inside message `content` strings. Older grep/same-line cleaners returned framing junk
# (counts but "no cleaned signals"). This recovers the real text as printable runs (newlines kept)
# in BOTH latin1 and UTF-16, keeps runs carrying note markers, dedupes WAL/sstable replicas, and
# ranks by relevance. Pure on-disk read — no LevelDB lock, no browser, no API.
_NOTE_MARKERS = ("## summary", "## executive summary", "### executive summary", "**attendees",
                 "**date:**", "## topics", "action item", "next step", "meeting note",
                 "## decision", "decisions made", "quick recap", "## agenda", "## action",
                 "follow-up", "## discussion", "[others]:", "[you]:", "work log")
_NOTE_STOP = {"littlebird", "little", "bird", "notes", "note", "action", "items", "item", "pull",
              "show", "get", "give", "find", "the", "and", "for", "with", "about", "from", "please",
              "summary", "summaries", "meeting", "meetings", "transcript", "transcripts", "what",
              "tell", "sweep", "recent", "all", "any", "into", "over", "past", "last", "this"}


def _idb_printable_runs(data, minlen=140):
    """Printable byte runs that KEEP newlines/tabs so multi-line markdown notes stay whole."""
    out, cur = [], bytearray()
    for b in data:
        if b in (9, 10, 13) or 32 <= b <= 126:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                out.append(cur.decode("latin-1", "replace"))
            cur = bytearray()
    if len(cur) >= minlen:
        out.append(cur.decode("latin-1", "replace"))
    return out


def _strip_decoded_lead(s):
    if '"content":"' in s:
        s = s.split('"content":"')[-1]
    s = s.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'")
    s = re.sub(r"^[\s\d?`'\"~|*•\-\x00-\x1f]+", "", s)
    m = re.search(r"(#{1,4}\s|\[(You|Others)\]|\*\*[A-Z]|Meeting Notes|Executive Summary|CompanyA|"
                  r"[A-Z][a-z]+ )", s)
    return s[m.start():] if m else s


def _clean_decoded(s):
    s = re.split(r'",?"(threadId|role|createdAt|updatedAt|thinking|hidden|status|mode|fileIds|type|'
                 r'prepContent|eventId|sourceApp)"', s)[0]
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]{1,16}", " ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _decoded_kind(c):
    cl = c.lower()
    if any(h in cl for h in ("## executive summary", "### executive summary", "## topics",
                             "## summary", "## decision")):
        return "summary"
    if "[others]:" in cl or "[you]:" in cl:
        return "transcript"
    if "work log" in cl or "**attendees" in cl or "meeting notes" in cl:
        return "notes"
    return "note"


def _decoded_title(c):
    m = re.search(r"(?m)^#{1,3}\s*(.+)$", c)
    if m and "executive summary" not in m.group(1).lower():
        return m.group(1).strip()[:100]
    m = re.search(r"(?im)^\**\s*(meeting notes?[^\n*]*|CompanyA[^\n*]*)$", c)
    if m:
        return m.group(1).strip()[:100]
    first = next((l.strip() for l in c.splitlines() if len(l.strip()) > 8), "")
    return first[:100]


def _decoded_date(c):
    m = re.search(r"\*\*Date:\*\*\s*([^\n]+)", c)
    if m:
        return m.group(1).strip()[:40]
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", c)
    if m:
        return m.group(1)
    m = re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?[a-z]*,?\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|"
                  r"Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+20\d{2})?", c)
    if m:
        return m.group(0).strip()[:40]
    m = re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", c)
    return m.group(0) if m else ""


def _decode_chat_notes(query="", days=21, limit=40):
    """Return clean decoded Littlebird records: [{kind,title,date,chars,body}], relevance-ranked."""
    import glob as _glob
    try:
        files = sorted(_glob.glob(INDEXEDDB_PATH + "/*.log") + _glob.glob(INDEXEDDB_PATH + "/*.ldb"),
                       key=lambda f: -os.path.getmtime(f))[:8]
    except Exception:
        files = []
    runs = []
    for f in files:
        try:
            data = open(f, "rb").read()
        except Exception:
            continue
        runs += _idb_printable_runs(data)
        try:
            runs += _idb_printable_runs(data.decode("utf-16-le", "replace").encode("latin-1", "replace"))
        except Exception:
            pass
    terms = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", (query or "").lower())
             if w not in _NOTE_STOP]
    records, seen = [], set()
    for r in runs:
        rl = r.lower()
        if not any(m in rl for m in _NOTE_MARKERS):
            continue
        c = _clean_decoded(_strip_decoded_lead(r))
        if len(c) < 200:
            continue
        fp = re.sub(r"[^a-z0-9]", "", c.lower())[:220]
        if fp in seen:
            continue
        seen.add(fp)
        records.append(c)
    records.sort(key=len, reverse=True)
    if terms and records:
        low = [c.lower() for c in records]
        # Anchor on the user's PRIMARY topic: a named project code (dpp/spp) wins, else the first
        # query term. A record must contain the anchor to qualify — so an off-topic record (e.g. a
        # warranty/Tavant note) that only shares a generic word like 'dealer'/'portal' is excluded.
        codes = [c.lower() for c in re.findall(r"\b[A-Z]{2,5}\b", query) if c.lower() in terms]
        anchor = codes[0] if codes else terms[0]
        idxs = [i for i, cl in enumerate(low) if anchor in cl]
        if not idxs:
            idxs = [i for i, cl in enumerate(low) if any(t in cl for t in terms)] or list(range(len(records)))

        def _rank(i):
            cl = low[i]
            cov = sum(1 for t in terms if t in cl)              # distinct query terms matched
            occ = sum(cl.count(t) for t in terms)
            return (cov, occ, len(records[i]))
        idxs.sort(key=_rank, reverse=True)
        records = [records[i] for i in idxs]
    try:
        cap = max(1, min(int(limit or 40), 80))
    except Exception:
        cap = 40
    return [{"kind": _decoded_kind(c), "title": _decoded_title(c), "date": _decoded_date(c),
             "chars": len(c), "body": c} for c in records[:cap]]


def _render_decoded_notes(records, max_chars=8000, body_cap=2600):
    if not records:
        return "No Littlebird meeting notes/transcripts decoded for that window."
    head = (f"Decoded Littlebird records ({len(records)}) — clean meeting notes, summaries & "
            "transcripts from the local store (V8/IndexedDB decoded), most relevant first:")
    lines, used = [head], len(head)
    for i, r in enumerate(records):
        block = (f"\n\n### [{r['kind']}] {r['title']}" + (f"  ({r['date']})" if r["date"] else "")
                 + "\n" + r["body"][:body_cap])
        if used + len(block) > max_chars:
            lines.append(f"\n\n(+{len(records) - i} more decoded records truncated for length)")
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)


def _decoded_action_items(records, limit=50):
    out = []
    for r in records:
        for m in re.finditer(r"(?ims)^[#>*\s]*(action items?|next steps?|decisions?|follow[- ]?ups?|"
                             r"to-?dos?|blockers?)\b[^\n]*\n(.*?)(?=\n\s*#{1,3}\s|\n\s*\*\*[A-Z][a-z]|\Z)",
                             r["body"]):
            head = m.group(1).strip().title()
            for ln in m.group(2).splitlines():
                ln = ln.replace("\\[", "[").replace("\\]", "]").replace("**", "").strip(" \t-*•>[]:")
                if len(ln) > 6 and not ln.lower().startswith(("http", "view in", "shareable")):
                    out.append(f"[{r['title'][:38]}] {head}: {ln[:200]}")
    seen, ded = set(), []
    for x in out:
        k = re.sub(r"\W+", "", x.lower())[:80]
        if k in seen:
            continue
        seen.add(k)
        ded.append(x)
    return ded[:limit]


def _full_notes_sweep(limit=80, query="", days=7, start_date="", end_date="", max_chars=12000):
    """Return a structured Littlebird evidence sweep for notes/transcripts/actions."""
    try:
        # Output sample count, not a meeting cap. The scan reads all local
        # Littlebird app stores covered by _grep_littlebird_strings.
        limit = max(10, min(int(limit), 250))
    except Exception:
        limit = 80
    try:
        max_chars = max(4000, min(int(max_chars), 40000))
    except Exception:
        max_chars = 12000

    dates = _date_terms(start_date=start_date, end_date=end_date, days=days)
    date_pattern = "|".join(re.escape(term) for term in dates[:80])
    all_lines = _all_littlebird_strings()
    week_blocks = _extract_week_blocks(all_lines, dates, max_blocks=1000)
    cleaned_records = _decode_chat_notes(query=query, days=days, limit=min(limit, 60))
    cleaned_summary = _render_decoded_notes(cleaned_records, max_chars=max(6000, max_chars - 2000))

    notes = _grep_littlebird_strings(NOTE_PATTERN, max_results=limit * 4, context_lines=3)
    transcripts = _grep_littlebird_strings(TRANSCRIPT_PATTERN, max_results=limit * 3, context_lines=2)
    actions = _grep_littlebird_strings(ACTION_PATTERN, max_results=limit * 3, context_lines=2)
    summaries = _grep_littlebird_strings(SUMMARY_PATTERN, max_results=limit * 3, context_lines=2)
    date_hits = _grep_littlebird_strings(date_pattern, max_results=limit * 3, context_lines=2) if date_pattern else ""
    query_hits = _search_all_data(INDEXEDDB_PATH, query, max_results=limit * 2) if query else ""

    sections = {
        "notes_and_call_notes": _clean_lines(notes, limit),
        "transcripts": _clean_lines(transcripts, limit),
        "summaries": _clean_lines(summaries, limit),
        "action_items_and_followups": _clean_lines(actions, limit),
        "date_window_hits": _clean_lines(date_hits, limit),
        "query_hits": _clean_lines(query_hits, limit),
        "meeting_events": _clean_lines(_get_meeting_events(0), limit),
        "week_record_blocks": week_blocks,
    }
    found = {
        key: bool(value)
        for key, value in sections.items()
    }

    payload = {
        "source": "Littlebird local app data",
        "data_path": LITTLEBIRD_DATA,
        "indexeddb_path": INDEXEDDB_PATH,
        "indexeddb_exists": os.path.exists(INDEXEDDB_PATH),
        "scan_policy": {
            "store_scope": "all_local_littlebird_indexeddb_and_session_storage_strings",
            "sample_limit_per_section": limit,
            "week_record_block_cap": 1000,
            "sample_limit_is_not_a_meeting_cap": True,
            "date_terms": dates,
        },
        "calendar_only_warning": (
            "littlebird_my_meetings is calendar-reconciled only. This sweep searches "
            "Littlebird local note/transcript/summary/action strings and should be used "
            "for CompanyA catch-up before accepting 'no written notes'."
        ),
        "found": found,
        "counts_returned": {key: len(value) if isinstance(value, list) else 0 for key, value in sections.items()},
        "cleaned": {
            "record_count": len(cleaned_records),
            "records": cleaned_records,
            "summary_markdown": cleaned_summary,
        },
        "sections": sections,
    }
    try:
        os.makedirs(redacted_WORKSPACE, exist_ok=True)
        out_path = os.path.join(redacted_WORKSPACE, "littlebird-full-notes-sweep.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        payload["saved_full_sweep"] = out_path
    except Exception as e:
        payload["save_error"] = repr(e)

    return _fit_json_payload(payload, max_chars, cleaned_records, cleaned_summary, sections)


def _get_meeting_events(count=200):
    """Get meeting event IDs and dates from event_tracking.db."""
    db = os.path.join(LITTLEBIRD_DATA, "event_tracking.db")
    try:
        count = int(count)
    except Exception:
        count = 200
    limit_sql = "" if count <= 0 else f" LIMIT {max(1, min(count, 1000))}"
    try:
        result = subprocess.run(
            ["sqlite3", "-separator", "|", db,
             "SELECT item_id, kind, datetime(start_date/1000, 'unixepoch', 'localtime'), "
             "datetime(end_date/1000, 'unixepoch', 'localtime') "
             f"FROM event_tracking ORDER BY start_date DESC{limit_sql};"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


def _reconcile_meetings_with_calendar(user_id="", days=7):
    """Get meetings for a user. Operator = Outlook calendar (CompanyA). Operator2 = Apple Calendar.
    All Outlook events are Operator's — even marketing, sales, etc. They're all on his CompanyA account.
    Operator2's events sync via Apple Calendar / iCloud, not Outlook."""

    try:
        days_back = max(1, min(int(days), 60))
    except Exception:
        days_back = 7

    if user_id in ("partner", "Operator2"):
        # Operator2's meetings come from Apple Calendar
        try:
            script = '''
                tell application "Calendar"
                    set today to current date
                    set weekAgo to today - (''' + str(days_back) + ''' * days)
                    set output to ""
                    repeat with c in calendars
                        set evts to (every event of c whose start date >= weekAgo)
                        repeat with e in evts
                            set output to output & (start date of e as string) & " | " & (summary of e) & " | " & (name of c) & linefeed
                        end repeat
                    end repeat
                    if output = "" then return "No meetings found for Operator2."
                    return output
                end tell'''
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
            return result.stdout.strip() if result.stdout.strip() else "No meetings found for Operator2."
        except Exception as e:
            return f"Error reading Apple Calendar: {e}"
    else:
        # Operator's meetings — ALL Outlook calendar events are his
        try:
            script = '''
                tell application "Microsoft Outlook"
                    set today to current date
                    set weekAgo to today - (''' + str(days_back) + ''' * days)
                    set calEvents to (every calendar event whose start time >= weekAgo and start time < (today + 1 * days))
                    set output to ""
                    repeat with e in calEvents
                        set startStr to (start time of e as string)
                        set subj to subject of e
                        set loc to location of e
                        set org to organizer of e
                        set hasZoom to ""
                        if loc contains "zoom" then set hasZoom to " [ZOOM]"
                        set output to output & startStr & " | " & subj & hasZoom & " | " & org & linefeed & "  ID: " & (id of e as string) & linefeed
                    end repeat
                    if output = "" then return "No meetings found this week."
                    return output
                end tell'''
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
            return result.stdout.strip() if result.stdout.strip() else "No meetings found this week."
        except Exception as e:
            return f"Error reading Outlook calendar: {e}"


def _get_app_status():
    """Check if Littlebird is running."""
    try:
        result = subprocess.run(["pgrep", "-f", "Littlebird"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


class LittlebirdLocalTool(BaseTool):
    name = "littlebird"
    description = "Littlebird AI — meeting notes, transcripts, daily work summaries, action items"

    def get_definitions(self):
        return [
            make_tool_def("littlebird_recent_notes", "Get recent meeting notes and summaries from Littlebird.",
                          {"count": {"type": "integer", "description": "Max results (default 10)"}}, []),
            make_tool_def("littlebird_full_notes_sweep", "Sweep all local Littlebird note/cache strings for call notes, meeting notes, transcripts, summaries, and action items. Use for CompanyA catch-up; not calendar-only. Limit controls returned samples, not meeting count.",
                          {"limit": {"type": "integer", "description": "Returned sample hits per section, not meeting cap (default 80)"},
                           "days": {"type": "integer", "description": "Date window terms to search (default 7)"},
                           "start_date": {"type": "string", "description": "Optional YYYY-MM-DD date-window start"},
                           "end_date": {"type": "string", "description": "Optional YYYY-MM-DD date-window end"},
                           "max_chars": {"type": "integer", "description": "Max response chars; full sweep is saved locally"},
                           "query": {"type": "string", "description": "Optional extra literal search"}}, []),
            make_tool_def("littlebird_search", "Search Littlebird data for a keyword (meetings, notes, transcripts).",
                          {"query": {"type": "string"}}, ["query"]),
            make_tool_def("littlebird_transcripts", "Get recent meeting transcripts.",
                          {"count": {"type": "integer"}}, []),
            make_tool_def("littlebird_action_items", "Get action items from recent meetings.",
                          {}, []),
            make_tool_def("littlebird_daily_summary", "Get the most recent daily work summary.",
                          {}, []),
            make_tool_def("littlebird_meeting_events", "List tracked meeting events with dates.",
                          {"count": {"type": "integer"}}, []),
            make_tool_def("littlebird_my_meetings", "Get calendar-reconciled meetings for a specific user. This is not a written-notes/transcripts sweep.",
                          {"user": {"type": "string", "description": "User: 'Operator' or 'Operator2' (default: auto-detect from caller)"},
                           "days": {"type": "integer", "description": "Days back to include (default 7, max 60)"}}, []),
            make_tool_def("littlebird_status", "Check if Littlebird is running and data status.",
                          {}, []),
        ]

    async def handle(self, tool_name, tool_input):
        loop = asyncio.get_event_loop()

        if tool_name == "littlebird_recent_notes":
            count = tool_input.get("count", 10)
            result = await loop.run_in_executor(None, _extract_notes_context, INDEXEDDB_PATH, 5, count)
            if not result or result == "":
                return "No recent notes found in Littlebird local data."
            return result[:4000]

        elif tool_name == "littlebird_full_notes_sweep":
            limit = tool_input.get("limit", 80)
            query = tool_input.get("query", "")
            days = tool_input.get("days", 7)
            start_date = tool_input.get("start_date", "")
            end_date = tool_input.get("end_date", "")
            max_chars = tool_input.get("max_chars", 12000)
            result = await loop.run_in_executor(None, _full_notes_sweep, limit, query, days, start_date, end_date, max_chars)
            return result

        elif tool_name == "littlebird_search":
            query = tool_input["query"]
            result = await loop.run_in_executor(None, _search_all_data, INDEXEDDB_PATH, query)
            return result[:4000]

        elif tool_name == "littlebird_transcripts":
            count = tool_input.get("count", 5)
            result = await loop.run_in_executor(None, _extract_strings, INDEXEDDB_PATH, "transcript", count * 3)
            if not result:
                return "No transcripts found."
            return result[:4000]

        elif tool_name == "littlebird_action_items":
            q = tool_input.get("query", "")
            decoded = await loop.run_in_executor(None, _decode_chat_notes, q, 60, 60)
            items = _decoded_action_items(decoded, limit=50)
            if not items:
                return "No action items decoded from Littlebird notes."
            return ("LITTLEBIRD ACTION ITEMS / DECISIONS / NEXT STEPS (decoded from meeting notes):\n- "
                    + "\n- ".join(items))[:4500]

        elif tool_name == "littlebird_daily_summary":
            result = await loop.run_in_executor(None, _grep_littlebird_strings, SUMMARY_PATTERN, 80, 2, True)
            if not result:
                return "No daily summary found."
            return result[:4000]

        elif tool_name == "littlebird_meeting_events":
            count = tool_input.get("count", 200)
            result = await loop.run_in_executor(None, _get_meeting_events, count)
            if not result:
                return "No meeting events tracked."
            return result[:4000]

        elif tool_name == "littlebird_my_meetings":
            user = tool_input.get("user", "")
            days = tool_input.get("days", 7)
            result = await loop.run_in_executor(None, _reconcile_meetings_with_calendar, user, days)
            return result[:4000]

        elif tool_name == "littlebird_status":
            running = await loop.run_in_executor(None, _get_app_status)
            db_exists = os.path.exists(os.path.join(LITTLEBIRD_DATA, "event_tracking.db"))
            idb_exists = os.path.exists(INDEXEDDB_PATH)
            return json.dumps({
                "running": running,
                "event_db": db_exists,
                "indexeddb": idb_exists,
                "data_path": LITTLEBIRD_DATA,
            })

        else:
            return f"Unknown littlebird tool: {tool_name}"
