"""Deterministic mailbox intelligence for chat.

This is intentionally model-free: if a user asks a basic question about their
email boxes, answer from local mailbox indexes before any LLM can hallucinate
or ask for context the system already has.
"""

from __future__ import annotations

import re
import csv
import html as html_lib
import json
import zipfile
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Chicago")

TRAVEL_TERMS = [
    "hotel", "hotels", "lodging", "flight", "flights", "airfare",
    "airline", "airlines", "itinerary", "boarding pass", "reservation",
    "receipt", "invoice", "expense", "uber", "lyft", "rideshare",
    "ride share", "taxi", "cab", "rental car", "hertz", "avis",
    "enterprise", "national car", "delta", "united airlines",
    "american airlines", "southwest", "marriott", "hilton", "hyatt",
    "airbnb", "booking.com", "expedia", "concur",
]

TRAVEL_VENDOR_TERMS = {
    "airline", "airlines", "delta", "united", "american airlines",
    "southwest", "hotel", "hotels", "hilton", "marriott", "hyatt",
    "sonesta", "home2", "uber", "lyft", "taxi", "cab", "hertz",
    "avis", "enterprise", "expedia", "booking.com", "airbnb",
}

TRAVEL_ACTION_TERMS = {
    "receipt", "confirmation", "confirmed", "itinerary", "reservation",
    "boarding pass", "invoice", "folio", "trip", "flight receipt",
}

PROMO_NOISE_TERMS = (
    "save up to", "book now", "moving soon", "flights under", "deal",
    "offer", "points", "double points", "spring & beyond", "newsletter",
    "news alert", "appeals court", "humanoid robots", "mother's day",
)

WORK_NOISE_TERMS = (
    "salesforce weekly sprint", "in-flight sprint", "verification required",
    "qad", "aftermarket 3 yr average", "userstory", "pipeline monitor",
)

MAILBOX_RE = re.compile(
    r"\b("
    r"email|emails|mail|mailbox|inbox|outlook|apple mail|gmail|icloud|"
    r"receipt|receipts|expense|expenses|travel|hotel|hotels?|flight|flights|"
    r"rideshare|ride share|uber|lyft|taxi|rental car|itinerary|boarding pass"
    r")\b",
    re.I,
)

SEARCH_RE = re.compile(
    r"\b("
    r"find|search|look|pull|pull together|show|summari[sz]e|audit|list|get|"
    r"put|save|export|download|bundle|copy|collect|capture|make|create|"
    r"where|what|which|any|all"
    r")\b",
    re.I,
)

EXPORT_RE = re.compile(
    r"\b("
    r"put|save|export|download|bundle|zip|copy|collect|capture|folder|"
    r"somewhere|file|files|make.*download|can download"
    r")\b",
    re.I,
)

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def is_mailbox_question(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    # Strong mailbox terms can route directly. Search verbs avoid catching
    # unrelated words like "flight" in a non-mailbox context.
    lower = clean.lower()
    if "travel" in lower and any(x in lower for x in ("expense", "receipt", "email", "mail", "last month", "hotel", "flight", "uber", "lyft", "ride")):
        return True
    return bool(MAILBOX_RE.search(clean) and SEARCH_RE.search(clean))


def _date_window(text: str) -> tuple[int | None, int | None, str]:
    lower = str(text or "").lower()
    now = datetime.now(LOCAL_TZ)
    if "last month" in lower and "this month" in lower:
        first_this = datetime(now.year, now.month, 1, tzinfo=LOCAL_TZ)
        start = (first_this - timedelta(days=1)).replace(day=1)
        end = datetime(now.year + (now.month // 12), (now.month % 12) + 1, 1, tzinfo=LOCAL_TZ)
        return int(start.timestamp()), int(end.timestamp()), f"{start.strftime('%B')} through {now.strftime('%B %Y')}"
    if "last month" in lower or "entire last month" in lower:
        first_this = datetime(now.year, now.month, 1, tzinfo=LOCAL_TZ)
        last_month_end = first_this
        last_month_start = (first_this - timedelta(days=1)).replace(day=1)
        return int(last_month_start.timestamp()), int(last_month_end.timestamp()), last_month_start.strftime("%B %Y")

    for name, month in MONTHS.items():
        if re.search(rf"\b{re.escape(name)}\b", lower):
            year = now.year
            m = re.search(r"\b(20\d{2})\b", lower)
            if m:
                year = int(m.group(1))
            start = datetime(year, month, 1, tzinfo=LOCAL_TZ)
            end = datetime(year + (month // 12), (month % 12) + 1, 1, tzinfo=LOCAL_TZ)
            return int(start.timestamp()), int(end.timestamp()), start.strftime("%B %Y")

    if "today" in lower:
        start = datetime(now.year, now.month, now.day, tzinfo=LOCAL_TZ)
        return int(start.timestamp()), None, "today"
    if "this week" in lower:
        start = datetime(now.year, now.month, now.day, tzinfo=LOCAL_TZ) - timedelta(days=now.weekday())
        return int(start.timestamp()), None, "this week"
    return None, None, "all available dates"


def _terms(text: str) -> list[str]:
    lower = str(text or "").lower()
    user_terms: list[str] = []
    generic_terms: list[str] = []

    quoted = re.findall(r'"([^"]{2,80})"', text or "")
    user_terms.extend(quoted)

    # Add useful noun phrases after common search words, minus broad control words.
    cleaned = re.sub(r"\b(last month|entire last month|this month|this week|today|april|may|june|july|august|september|october|november|december|january|february|march)\b", " ", lower)
    cleaned = re.sub(r"\b(please|just|look|for|all|my|the|and|or|emails?|mail|mailbox|inbox|pull|together|related|entire|stays?|systems?|documented|during|those|periods?|ineed|need|absolutely|several|both|trips?|there|should|one|two|plus|from|with|about)\b", " ", cleaned)
    for token in re.findall(r"[a-z][a-z0-9+.-]{2,}", cleaned):
        if len(token) > 2:
            user_terms.append(token)

    if any(x in lower for x in ("travel", "hotel", "flight", "rideshare", "ride share", "uber", "lyft", "expense", "receipt")):
        generic_terms.extend(TRAVEL_TERMS)

    out = []
    seen = set()
    for term in user_terms + generic_terms:
        term = str(term or "").strip().lower()
        if len(term) < 3 or term in seen:
            continue
        seen.add(term)
        out.append(term)
    return out[:32]


def _haystack(row: dict) -> str:
    return " ".join(str(row.get(k, "") or "") for k in row.keys()).lower()


def _money(text: str) -> list[float]:
    values = []
    for raw in re.findall(r"\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+(?:\.[0-9]{2}))", str(text or "")):
        try:
            values.append(float(raw.replace(",", "")))
        except Exception:
            pass
    return values


def _search_outlook(terms: list[str], start_ts: int | None, end_ts: int | None, limit: int = 80) -> list[dict]:
    try:
        from tools.outlook_local import _db_query, _ts_to_local
    except Exception:
        return []

    if not terms:
        return []
    text_fields = [
        "coalesce(m.Message_NormalizedSubject,'')",
        "coalesce(m.Message_SenderList,'')",
        "coalesce(m.Message_SenderAddressList,'')",
        "coalesce(m.Message_DisplayTo,'')",
        "coalesce(m.Message_ToRecipientAddressList,'')",
        "coalesce(m.Message_Preview,'')",
        "coalesce(f.Folder_Name,'')",
    ]
    or_parts = []
    params = []
    for term in terms:
        group = " OR ".join(f"lower({field}) LIKE ?" for field in text_fields)
        or_parts.append(f"({group})")
        params.extend([f"%{term.lower()}%"] * len(text_fields))

    where = [f"({' OR '.join(or_parts)})"]
    if start_ts is not None:
        where.append("m.Message_TimeReceived >= ?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("m.Message_TimeReceived < ?")
        params.append(end_ts)
    params.append(int(limit))

    sql = f"""
        SELECT
            m.Record_RecordID,
            m.Message_NormalizedSubject,
            m.Message_SenderList,
            m.Message_SenderAddressList,
            m.Message_DisplayTo,
            m.Message_TimeReceived,
            m.Message_ReadFlag,
            m.Message_Preview,
            m.Message_HasAttachment,
            m.PathToDataFile,
            f.Folder_Name
        FROM Mail m
        LEFT JOIN Folders f ON m.Record_FolderID = f.Record_RecordID
        WHERE {' AND '.join(where)}
        ORDER BY m.Message_TimeReceived DESC
        LIMIT ?
    """
    try:
        rows = _db_query(sql, tuple(params))
    except Exception:
        return []

    out = []
    for row in rows:
        subject = row.get("Message_NormalizedSubject") or "(no subject)"
        preview = row.get("Message_Preview") or ""
        sender = row.get("Message_SenderList") or row.get("Message_SenderAddressList") or ""
        folder = row.get("Folder_Name") or ""
        ts = row.get("Message_TimeReceived")
        out.append({
            "source": "Outlook",
            "id": str(row.get("Record_RecordID") or ""),
            "date": _ts_to_local(ts),
            "ts": int(ts or 0),
            "sender": str(sender),
            "subject": str(subject),
            "folder": str(folder),
            "path": str(row.get("PathToDataFile") or ""),
            "has_attachment": bool(row.get("Message_HasAttachment") or False),
            "preview": str(preview)[:300],
        })
    return out


def _parse_apple_dt(value: str) -> int:
    value = str(value or "").strip()
    if not value:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value[:19], fmt).replace(tzinfo=LOCAL_TZ).timestamp())
        except Exception:
            pass
    return 0


def _search_apple_mail(terms: list[str], start_ts: int | None, end_ts: int | None, limit: int = 80) -> list[dict]:
    try:
        from core.apple_mail_index import search_messages
    except Exception:
        return []

    merged: OrderedDict[str, dict] = OrderedDict()
    per_term = max(5, min(12, int(limit / max(1, min(len(terms), 12)))))
    for term in terms[:24]:
        try:
            rows = search_messages(term, limit=per_term)
        except Exception:
            rows = []
        for row in rows:
            ts = _parse_apple_dt(row.get("received") or row.get("sent") or "")
            if start_ts is not None and ts and ts < start_ts:
                continue
            if end_ts is not None and ts and ts >= end_ts:
                continue
            key = row.get("id") or row.get("rowid") or f"{row.get('subject')}:{row.get('received')}"
            if key in merged:
                continue
            merged[str(key)] = {
                "source": "Apple Mail",
                "id": str(row.get("id") or row.get("rowid") or ""),
                "rowid": str(row.get("rowid") or ""),
                "mailbox_url": str(row.get("mailbox") or ""),
                "date": str(row.get("received") or row.get("sent") or ""),
                "ts": ts,
                "sender": str(row.get("sender_str") or row.get("from_email") or ""),
                "subject": str(row.get("subject") or "(no subject)"),
                "folder": str(row.get("mailbox") or row.get("account_email") or ""),
                "preview": str(row.get("preview") or "")[:300],
            }
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break
    return list(merged.values())


def _relevance(row: dict, terms: list[str]) -> tuple[int, list[str]]:
    hay = _haystack(row)
    matched = []
    score = 0
    high_value = {
        "hotel", "hotels", "flight", "flights", "airfare", "itinerary",
        "boarding pass", "receipt", "invoice", "expense", "uber", "lyft",
        "rideshare", "ride share", "taxi", "rental car", "hertz", "avis",
        "enterprise", "delta", "united airlines", "american airlines",
        "southwest", "marriott", "hilton", "hyatt", "airbnb",
        "booking.com", "expedia", "concur",
    }
    for term in terms:
        t = str(term or "").lower().strip()
        if not t:
            continue
        if " " in t or "." in t:
            hit = t in hay
        else:
            hit = bool(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", hay))
        if hit:
            matched.append(t)
            score += 3 if t in high_value else 1
    return score, matched


def _dedupe(rows: list[dict], terms: list[str] | None = None) -> list[dict]:
    out = []
    seen = set()
    terms = terms or []
    enriched = []
    for row in rows:
        score, matched = _relevance(row, terms)
        if terms and score <= 0:
            continue
        row = dict(row)
        row["matched_terms"] = matched[:8]
        row["score"] = score
        enriched.append(row)
    for row in sorted(enriched, key=lambda r: (int(r.get("score") or 0), int(r.get("ts") or 0)), reverse=True):
        key = (row.get("source"), row.get("id"))
        fallback = (row.get("subject", "").lower(), row.get("date", ""), row.get("sender", "").lower())
        use_key = key if row.get("id") else fallback
        if use_key in seen:
            continue
        seen.add(use_key)
        out.append(row)
    return out


def _is_expense_report_request(text: str) -> bool:
    lower = str(text or "").lower()
    return bool("expense" in lower or "receipt" in lower or ("travel" in lower and "pull" in lower))


def _is_receipt_export_request(text: str) -> bool:
    lower = str(text or "").lower()
    if not EXPORT_RE.search(lower):
        return False
    return bool(
        "receipt" in lower
        or "expense" in lower
        or any(term in lower for term in ("hotel", "flight", "travel", "reservation", "confirmation", "invoice"))
    )


def _body_for_row(row: dict) -> str:
    try:
        if row.get("source") == "Apple Mail":
            rowid = str(row.get("rowid") or "").strip()
            mailbox = str(row.get("mailbox_url") or row.get("folder") or "").strip()
            if rowid:
                from core.apple_mail_index import read_body
                return read_body(rowid, mailbox, max_chars=5000)
        if row.get("source") == "Outlook":
            path = str(row.get("path") or "").strip()
            if path:
                from tools.outlook_local import read_email_body_parts
                parts = read_email_body_parts(path)
                return str(parts.get("text") or parts.get("html") or "")[:5000]
    except Exception:
        return ""
    return ""


def _clean_sender(sender: str) -> str:
    sender = re.sub(r"\s*<[^>]+>\s*", "", str(sender or "")).strip()
    return sender or "Unknown"


def _best_amount(text: str) -> float | None:
    values = [v for v in _money(text) if 1 <= abs(v) <= 25000]
    if not values:
        return None
    # Receipts usually contain several amounts; the largest visible amount is
    # the best deterministic clue without sending private bodies to an LLM.
    return max(values, key=lambda value: abs(float(value)))


def _confirmation_key(row: dict, body: str = "") -> str:
    hay = f"{row.get('subject','')} {row.get('preview','')} {body}".lower()
    patterns = [
        r"confirmation(?: number| #| no\.?)?\s*[:#]?\s*([a-z0-9-]{5,})",
        r"\bconf(?:irmation)?\s*[:#]\s*([a-z0-9-]{5,})",
        r"\bitinerary\s*[:#]\s*([a-z0-9-]{5,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, hay, re.I)
        if match:
            return match.group(1).upper()
    subject = re.sub(r"\s+", " ", str(row.get("subject") or "").lower()).strip()
    sender = _clean_sender(str(row.get("sender") or "")).lower()
    return f"{sender}:{subject}"


def _classify_expense_row(row: dict) -> tuple[str, str]:
    hay = _haystack(row)
    if any(token in hay for token in WORK_NOISE_TERMS):
        return "noise", "work/unrelated keyword hit"
    vendor_hit = any(token in hay for token in TRAVEL_VENDOR_TERMS)
    action_hit = any(token in hay for token in TRAVEL_ACTION_TERMS)
    if vendor_hit and ("hope you enjoyed your stay" in hay or "your stay at" in hay):
        return "likely", "completed hotel stay"
    if vendor_hit and action_hit:
        return "likely", "travel receipt/confirmation"
    if action_hit and any(token in hay for token in ("flight", "hotel", "lodging", "airline", "rental car", "uber", "lyft")):
        return "likely", "travel action match"
    if any(token in hay for token in PROMO_NOISE_TERMS):
        return "noise", "marketing/news/no receipt"
    if vendor_hit:
        return "possible", "travel vendor mention"
    return "noise", "weak keyword match"


def _safe_filename(value: str, fallback: str = "message", max_len: int = 90) -> str:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", " ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip().replace(" ", "_")
    clean = clean.strip("._-")
    if not clean:
        clean = fallback
    return clean[:max_len].strip("._-") or fallback


def _outputs_root() -> Path:
    configured = None
    try:
        from core import SETTINGS
        configured = (SETTINGS.get("outputs") or {}).get("default_path")
    except Exception:
        configured = None
    return Path(configured or "~/Documents/Claude Outputs").expanduser()


def _receipt_export_root() -> Path:
    return _outputs_root() / "Mailbox Receipts"


def _build_receipt_candidates(rows: list[dict], include_possible: bool = False, body_budget: int = 40) -> tuple[list[dict], list[dict], int]:
    exportable: list[dict] = []
    ignored: list[dict] = []
    duplicate_count = 0
    seen = set()

    for row in rows:
        bucket, reason = _classify_expense_row(row)
        row = dict(row)
        row["bucket"] = bucket
        row["reason"] = reason
        if bucket not in {"likely", "possible"} or (bucket == "possible" and not include_possible):
            ignored.append(row)
            continue

        body = ""
        if body_budget > 0:
            body = _body_for_row(row)
            body_budget -= 1
        row["body"] = body
        excerpt = re.sub(r"\s+", " ", body).strip()
        if excerpt.count("http") > 1 or len(re.findall(r"[A-Za-z]{4,}", excerpt)) < 8:
            excerpt = re.sub(r"\s+", " ", str(row.get("preview") or "")).strip()
        row["body_excerpt"] = excerpt[:500]
        row["amount"] = _best_amount(f"{row.get('subject','')} {row.get('preview','')} {body}")
        row["dedupe_key"] = _confirmation_key(row, body)
        key = (row.get("bucket"), row.get("dedupe_key") or row.get("source"), row.get("id"))
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        exportable.append(row)

    exportable.sort(key=lambda r: (0 if r.get("bucket") == "likely" else 1, -(int(r.get("ts") or 0))))
    return exportable, ignored, duplicate_count


def _write_receipt_html(row: dict, path: Path) -> None:
    title = str(row.get("subject") or "(no subject)")
    body = str(row.get("body") or row.get("preview") or "")
    metadata = [
        ("Source", row.get("source")),
        ("Date", row.get("date")),
        ("From", row.get("sender")),
        ("Folder", row.get("folder")),
        ("Matched", ", ".join(row.get("matched_terms") or [])),
        ("Confidence", row.get("bucket")),
        ("Reason", row.get("reason")),
        ("Amount clue", f"${float(row['amount']):,.2f}" if row.get("amount") is not None else "not visible"),
    ]
    meta_html = "\n".join(
        f"<tr><th>{html_lib.escape(str(k))}</th><td>{html_lib.escape(str(v or ''))}</td></tr>"
        for k, v in metadata
    )
    doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html_lib.escape(title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #111827; }}
    table {{ border-collapse: collapse; width: 100%; margin: 0 0 24px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ width: 160px; background: #f3f4f6; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f9fafb; border: 1px solid #d1d5db; padding: 16px; }}
  </style>
</head>
<body>
  <h1>{html_lib.escape(title)}</h1>
  <table>{meta_html}</table>
  <h2>Extracted Message Body</h2>
  <pre>{html_lib.escape(body)}</pre>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


def _export_receipts(text: str, rows: list[dict], label: str, terms: list[str]) -> str:
    exportable, ignored, duplicate_count = _build_receipt_candidates(rows)
    root = _receipt_export_root()
    stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
    bundle_id = f"receipt_export_{stamp}"
    bundle_dir = root / bundle_id
    messages_dir = bundle_dir / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = []
    for idx, row in enumerate(exportable, 1):
        subject = _safe_filename(str(row.get("subject") or "receipt"))
        date_part = _safe_filename(str(row.get("date") or "no_date"), fallback="no_date", max_len=24)
        filename_base = f"{idx:02d}_{date_part}_{subject}"
        html_path = messages_dir / f"{filename_base}.html"
        txt_path = messages_dir / f"{filename_base}.txt"
        _write_receipt_html(row, html_path)
        txt_path.write_text(
            "\n".join([
                f"Subject: {row.get('subject') or '(no subject)'}",
                f"Source: {row.get('source') or ''}",
                f"Date: {row.get('date') or ''}",
                f"From: {row.get('sender') or ''}",
                f"Folder: {row.get('folder') or ''}",
                f"Confidence: {row.get('bucket') or ''}",
                f"Reason: {row.get('reason') or ''}",
                f"Amount clue: ${float(row['amount']):,.2f}" if row.get("amount") is not None else "Amount clue: not visible",
                "",
                str(row.get("body") or row.get("preview") or ""),
            ]),
            encoding="utf-8",
        )
        manifest_rows.append({
            "index": idx,
            "source": row.get("source"),
            "date": row.get("date"),
            "sender": _clean_sender(row.get("sender")),
            "subject": row.get("subject"),
            "folder": row.get("folder"),
            "confidence": row.get("bucket"),
            "reason": row.get("reason"),
            "amount": row.get("amount"),
            "matched_terms": row.get("matched_terms") or [],
            "html_file": str(html_path.relative_to(bundle_dir)),
            "text_file": str(txt_path.relative_to(bundle_dir)),
        })

    (bundle_dir / "manifest.json").write_text(json.dumps({
        "created_at": datetime.now(LOCAL_TZ).isoformat(),
        "request": text,
        "date_scope": label,
        "terms": terms,
        "exported_count": len(exportable),
        "ignored_count": len(ignored),
        "duplicate_count": duplicate_count,
        "items": manifest_rows,
    }, indent=2, default=str), encoding="utf-8")

    with (bundle_dir / "receipt_index.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["index", "source", "date", "sender", "subject", "folder", "confidence", "reason", "amount", "html_file", "text_file"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in manifest_rows:
            writer.writerow({field: row.get(field) for field in fields})

    amount_rows = [row for row in exportable if row.get("amount") is not None]
    total = sum(float(row["amount"]) for row in amount_rows)
    readme_lines = [
        "Mailbox Receipt Export",
        "",
        f"Request: {text}",
        f"Scope searched: {label}",
        f"Exported receipt candidates: {len(exportable)}",
        f"Ignored low-confidence/noise hits: {len(ignored) + duplicate_count}",
    ]
    if amount_rows:
        readme_lines.append(f"Visible total from parsed text: ${total:,.2f} across {len(amount_rows)} item(s)")
    readme_lines.extend([
        "",
        "Open receipt_index.csv for the short list, or messages/*.html for the extracted receipt bodies.",
        "This export used local mailbox indexes and deterministic parsing only; no paid LLM call was needed.",
    ])
    (bundle_dir / "README.txt").write_text("\n".join(readme_lines), encoding="utf-8")

    zip_path = bundle_dir / f"{bundle_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in bundle_dir.rglob("*"):
            if path == zip_path:
                continue
            zf.write(path, path.relative_to(bundle_dir))

    link = f"/api/download/{quote(str(zip_path), safe='')}"
    lines = [
        f"Done. I exported {len(exportable)} receipt candidate(s) into a downloadable bundle.",
        "",
        f"Download: {link}",
        f"Folder: {bundle_dir}",
        f"Path: {zip_path}",
    ]
    if exportable:
        lines.append("")
        lines.append("Included:")
        for idx, row in enumerate(exportable[:8], 1):
            amount = f" | ${float(row['amount']):,.2f}" if row.get("amount") is not None else " | amount not visible"
            lines.append(f"{idx}. {row.get('date') or 'no date'} | {_clean_sender(row.get('sender'))} | {row.get('subject') or '(no subject)'}{amount}")
        if len(exportable) > 8:
            lines.append(f"...and {len(exportable) - 8} more in receipt_index.csv.")
    else:
        lines.append("")
        lines.append("No receipt-grade items were strong enough to export. I still created the folder with the manifest so the search is auditable.")
    lines.append("")
    lines.append("Fast/cost note: this used local mailbox indexes and deterministic parsing only; no paid LLM call was needed.")
    return "\n".join(lines)


def _expense_report(text: str, rows: list[dict], label: str, terms: list[str]) -> str:
    likely = []
    possible = []
    noise = []
    body_budget = 10
    for row in rows:
        bucket, reason = _classify_expense_row(row)
        row = dict(row)
        row["reason"] = reason
        if bucket in {"likely", "possible"} and body_budget > 0:
            body = _body_for_row(row)
            body_budget -= 1
        else:
            body = ""
        excerpt = re.sub(r"\s+", " ", body).strip()
        if excerpt.count("http") > 1 or len(re.findall(r"[A-Za-z]{4,}", excerpt)) < 8:
            excerpt = re.sub(r"\s+", " ", str(row.get("preview") or "")).strip()
        row["body_excerpt"] = excerpt[:360]
        row["amount"] = _best_amount(f"{row.get('subject','')} {row.get('preview','')} {body}")
        row["dedupe_key"] = _confirmation_key(row, body)
        if bucket == "likely":
            likely.append(row)
        elif bucket == "possible":
            possible.append(row)
        else:
            noise.append(row)

    deduped_likely = []
    seen = set()
    duplicate_count = 0
    for row in likely:
        key = row.get("dedupe_key") or row.get("id")
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        deduped_likely.append(row)

    amount_rows = [row for row in deduped_likely if row.get("amount") is not None]
    total = sum(float(row["amount"]) for row in amount_rows)
    searched = f"Searched Outlook all folders + Apple Mail local index for {label}."
    lines = [
        searched,
        f"Found {len(deduped_likely)} likely travel expense item(s), {len(possible)} possible lead(s), and ignored {len(noise) + duplicate_count} low-confidence/noise/duplicate hit(s).",
    ]
    if amount_rows:
        lines.append(f"Visible total from parsed receipt text: ${total:,.2f} across {len(amount_rows)} item(s).")
    else:
        lines.append("No reliable dollar total was visible in the local snippets/bodies I could read; the report below identifies what to open for exact amounts.")

    if deduped_likely:
        lines.append("")
        lines.append("Likely expenses to capture:")
        for idx, row in enumerate(deduped_likely[:8], 1):
            amount = f" | amount clue ${float(row['amount']):,.2f}" if row.get("amount") is not None else " | amount not visible"
            lines.append(
                f"{idx}. {row.get('date') or 'no date'} | {_clean_sender(row.get('sender'))} | "
                f"{row.get('subject') or '(no subject)'}{amount}"
            )
            if row.get("body_excerpt") and row.get("amount") is None:
                lines.append(f"   clue: {row['body_excerpt'][:180]}")
    else:
        lines.append("")
        lines.append("No receipt/confirmation-grade travel expenses were found.")

    if possible:
        lines.append("")
        lines.append("Possible leads, not counted:")
        for row in possible[:5]:
            lines.append(f"- {row.get('date') or 'no date'} | {_clean_sender(row.get('sender'))} | {row.get('subject') or '(no subject)'}")

    if noise:
        sample = []
        for row in noise[:6]:
            sample.append(str(row.get("subject") or "").strip() or str(row.get("sender") or "").strip())
        if sample:
            lines.append("")
            lines.append("Ignored as noise: " + "; ".join(sample[:6]))

    lines.append("")
    lines.append("Fast/cost note: this used local mailbox indexes and deterministic parsing only; no paid LLM call was needed.")
    return "\n".join(lines)


async def answer_mailbox_question(text: str, limit: int = 40) -> str:
    wants_export = _is_receipt_export_request(text)
    if wants_export:
        limit = max(int(limit or 40), 80)
    terms = _terms(text)
    start_ts, end_ts, label = _date_window(text)
    if not terms:
        terms = ["email"]

    outlook = _search_outlook(terms, start_ts, end_ts, limit=limit)
    apple = _search_apple_mail(terms, start_ts, end_ts, limit=limit)
    rows = _dedupe(outlook + apple, terms)[:limit]

    term_display = ", ".join(terms[:18])
    searched = f"Searched Outlook all folders + Apple Mail local index for {label}."
    if not rows:
        return (
            f"{searched}\n\n"
            f"No matching mailbox items found for: {term_display}.\n\n"
            "That means I checked the local mailbox indexes I have access to; I did not just check the Outlook inbox. "
            "No expense total can be calculated without matching receipts/confirmations."
        )

    if wants_export:
        return _export_receipts(text, rows, label, terms)

    if _is_expense_report_request(text):
        return _expense_report(text, rows, label, terms)

    money_values = []
    for row in rows:
        money_values.extend(_money(f"{row.get('subject','')} {row.get('preview','')}"))

    lines = [
        searched,
        f"Found {len(rows)} likely matching item(s).",
        f"Query terms: {term_display}",
    ]
    if money_values:
        lines.append(f"Visible dollar amounts found in snippets: ${sum(money_values):,.2f} across {len(money_values)} amount(s).")
    lines.append("")
    lines.append("Top matches:")
    for idx, row in enumerate(rows[:20], 1):
        folder = row.get("folder") or "unknown folder"
        preview = row.get("preview") or ""
        if len(preview) > 160:
            preview = preview[:157] + "..."
        lines.append(
            f"{idx}. [{row.get('source')}] {row.get('date') or 'no date'} | "
            f"{row.get('sender') or 'unknown sender'} | {row.get('subject') or '(no subject)'}"
        )
        lines.append(f"   Folder/account: {folder}")
        if row.get("matched_terms"):
            lines.append(f"   Matched: {', '.join(row.get('matched_terms') or [])}")
        if preview:
            lines.append(f"   Snippet: {preview}")

    if len(rows) > 20:
        lines.append(f"\n{len(rows) - 20} more matches were found but hidden to keep this readable.")
    return "\n".join(lines)
