#!/usr/bin/env python3
"""Curate the extracted SalesIntel pool into sellable, ICP-sliced enriched lead lists.

FIRMOGRAPHICS-ONLY (2026-06-06 ToS-risk remediation): person-level fields
(contact_name/contact_linkedin/contact_email — incl. the pattern-GUESSED
first.last@domain) are no longer emitted; SalesIntel-derived contact PII must not
ship in paid deliverables (FIRST-DOLLAR directive: ToS-clean digital reports, not
PII CSVs). Supersedes the 2026-06-05 'owner-approved curated sale' note pending
Operator's explicit say-so. Prior PII CSVs quarantined at data.internal/hustle/quarantine/.

Writes enriched CSVs that products/self_serve `/fulfill` already serves
(it globs {niche}_pool_*_enriched.csv, newest first).

    PYTHONPATH=. .venv/bin/python tools/curate_salesintel_lists.py
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

BASE = Path.home() / "claude-stack"
SRC = BASE / "data.internal" / "hustle" / "salesintel_scraped.backup_20260605.json"  # full 2450-row set; live file shrank to 1004
# Internal only — must NOT write into the served packaged_pools dir. /fulfill globs
# packaged_pools and should fall back to the base intent pools (which carry real
# posting_url), not this SalesIntel-derived firmographics map (posting_url empty).
POOL_DIR = BASE / "data.internal" / "hustle" / "curated_firmographics"
POOL_DIR.mkdir(parents=True, exist_ok=True)
DATE = "2026-06-05"
CAP = 220  # quality $97 list, not a dump

# enriched schema /fulfill + the SF-admin list use
COLS = ["company", "job_title", "location", "posting_url", "source", "found_at",
        "domain", "contact_name", "contact_title", "contact_linkedin", "contact_email"]

# ICP slices — mutually exclusive, assigned in this order so a person lands in one list.
SLICES = [
    # sell_ segments are US-only high-resale; prioritized first so they fill before fallback pools
    ("revops-hubspot", {
        "segment": re.compile(r"sell_revops|sell_salesops|sell_hubspot|revops|sales_ops|head_revops", re.I),
        "title": re.compile(r"revops|revenue operations|revenue ops|sales operations|sales ops|hubspot|marketing ops|marketing operations", re.I),
    }),
    ("agentforce-ai", {
        "segment": re.compile(r"sell_sf_admin|sell_agentforce|sell_biz_systems|sell_crm|sf_ops|sf_admin|crm_admin|crm_manager|biz_systems|business_systems|new_hire_admin|founder_crm", re.I),
        "title": re.compile(r"salesforce|agentforce|crm|business systems|systems admin|revenue systems|salesforce architect|salesforce developer|salesforce consultant|\badmin\b", re.I),
    }),
    ("ai-ops-engineers", {
        "segment": re.compile(r"sell_ai_ops", re.I),
        "title": re.compile(r"ai operations|ai engineer|machine learning engineer|data engineer|ml engineer|ai ops", re.I),
    }),
    # last so it only takes rows the priority slices left (keeps their 220s intact)
    ("sf-admin", {
        "segment": re.compile(r"sell_sf_admin|sf_admin|sf_ops", re.I),
        "title": re.compile(r"salesforce|agentforce|crm|\badmin\b", re.I),
    }),
]


def _clean(tok: str) -> str:
    return re.sub(r"[^a-z]", "", (tok or "").lower())


def _likely_email(first: str, last: str, domain: str) -> str:
    f = _clean((first or "").split()[0] if first else "")
    l = _clean((last or "").split()[-1] if last else "")  # last token (drops maiden "(Albers)")
    if not (f and l and domain):
        return ""
    return f"{f}.{l}@{domain}"


def _matches(row: dict, pat: dict) -> bool:
    seg = row.get("segment", "") or ""
    title = (row.get("title", "") or "") + " " + (row.get("search_query", "") or "")
    return bool(pat["segment"].search(seg) or pat["title"].search(title))


def main() -> int:
    rows = json.loads(SRC.read_text())
    rows = rows if isinstance(rows, list) else rows.get("rows", [])
    used: set[tuple] = set()
    summary = []
    for niche, pat in SLICES:
        out = []
        for r in rows:
            key = (r.get("name", ""), r.get("company", ""))
            if key in used or not r.get("company") or not r.get("name"):
                continue
            if not _matches(r, pat):
                continue
            domain = r.get("email_domain", "") or r.get("primary_domain", "")
            out.append({
                "company": r.get("company", ""),
                "job_title": r.get("title", ""),
                "location": r.get("location", ""),
                "posting_url": "",
                "source": "salesintel_curated",
                "found_at": r.get("scraped_at", ""),
                "domain": domain,
                # Firmographics only — never person-level PII or guessed emails (see docstring).
                "contact_name": "",
                "contact_title": r.get("title", ""),
                "contact_linkedin": "",
                "contact_email": "",
            })
            used.add(key)
            if len(out) >= CAP:
                break
        dest = POOL_DIR / f"{niche}_pool_{DATE}_enriched.csv"
        with open(dest, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            w.writerows(out)
        with_li = sum(1 for o in out if o["contact_linkedin"])
        with_em = sum(1 for o in out if o["contact_email"])
        summary.append((niche, len(out), with_li, with_em, dest.name))

    for niche, n, li, em, name in summary:
        print(f"{niche}: {n} rows ({li} linkedin, {em} email) -> {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
