#!/usr/bin/env python3
"""Ship-guard — the single auto-QA gate that keeps anything shitty from going out.

Any artifact we sell, send, or publish (Gumroad product, deliverable file, website
mockup, lead CSV) must clear this BEFORE it ships. Deterministic + $0 + fast, so it
can run inline on a send path AND as a scheduled sweep over the whole catalog.

It catches the things that make us look bad to a buyer:
  - placeholders / unfinished work ([brackets], TODO, lorem, "coming soon")
  - AI-slop tells ("as an AI", "in conclusion,", "I cannot", canned filler)
  - thin / empty-shell deliverables (a folder with nothing in it)
  - identity-footprint leaks (real name, /Users/Operator path) — never ship those
  - secrets / dangerous PII (reuses the clean-room scanner via ship_qa)
  - broken HTML (no body, unclosed) and dead-obvious template scaffolding

Usage:
  from core.ship_guard import inspect_text, inspect_file, ship_ok
  ok, issues = ship_ok("products/gumroad_ready/notion-crm")   # path or raw text

  python3 -m core.ship_guard --all        # sweep catalog; exit 1 + iMessage if any FAIL
  python3 -m core.ship_guard <path>       # check one file/dir
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Unfinished-work signatures that are NEVER acceptable in a finished artifact.
# NOTE: bracketed fill-in fields ([Your Name], [Company], [Date]) are INTENTIONAL in
# templates and are deliberately NOT here — they're handled as a soft warn only.
PLACEHOLDER_HARD = [
    "lorem ipsum", "your business here", "your company name", "company name here",
    "business name here", "todo:", "fixme", "[tbd", "tbd]", "coming soon",
    "under construction", "sample text here", "replace this text", "xxxxx",
    "insert text here", "content goes here", "edit this placeholder",
]
# Model tells that unambiguously mean "nobody edited this". Kept tight on purpose —
# common business words (unlock, deep dive, synergy, bottom line) are NOT tells.
AI_TELLS = [
    "as an ai", "as a language model", "i cannot fulfill", "i'm sorry, but i",
    "i am unable to", "i cannot provide", "in today's fast-paced", "in the ever-evolving",
    "delve into the", "tapestry of", "certainly! here", "sure! here's",
    "i hope this helps", "navigating the complexities", "it's worth noting that as",
]
# Operator's real-identity markers — must NEVER appear in an anon-brand artifact.
IDENTITY = re.compile(
    r"Operator|user|Operator\s+Operator|/users/Operator|CompanyA", re.I)

# Min useful text length per artifact kind (chars).
THIN_CHARS = {"product": 1500, "deliverable": 800, "mockup": 600, "text": 200}

TEXT_EXT = {".md", ".txt", ".html", ".htm", ".csv", ".json"}


@dataclass
class Verdict:
    ref: str
    passed: bool
    fails: list = field(default_factory=list)   # hard — blocks ship
    warns: list = field(default_factory=list)   # soft — review


# Real secrets/keys only — NOT emails/phones (deliverables legitimately contain
# contact info, so the generic PII scan over-blocks here).
_SECRET_RE = re.compile(
    r"sk_live_[A-Za-z0-9]{10}|sk-ant-[A-Za-z0-9]{10}|gh[ps]_[A-Za-z0-9]{30}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|xox[baprs]-[A-Za-z0-9-]{10}|"
    r"\bre_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9]{32,}")
_BRACKET_FIELD = re.compile(r"\[[A-Za-z][A-Za-z _/]{1,30}\]")


def inspect_text(text: str, kind: str = "text", ref: str = "") -> Verdict:
    text = text or ""
    low = text.lower()
    fails, warns = [], []

    m = IDENTITY.search(text)
    if m:
        fails.append(f"identity-footprint leak: {m.group(0)!r}")
    s = _SECRET_RE.search(text)
    if s:
        fails.append(f"secret/key leak: {s.group(0)[:12]}…")

    ph = [p for p in PLACEHOLDER_HARD if p in low]
    if ph:
        fails.append(f"unfinished/placeholder: {ph[:4]}")
    # A tell quoted as a bad example ("no 'in today's fast-paced world'", "AI tells like
    # 'delve'") is the product teaching readers to AVOID it — not slop. Only fail on a tell
    # that ISN'T in such an instructional/negated/quoted context.
    NEG = ("no ", "not ", "never", "avoid", "don't", "tells", "such as", "instead",
           "without", "like \"", "like '", "e.g.", "stop ", "kill the")
    tells = []
    for p in AI_TELLS:
        i = low.find(p)
        while i != -1:
            ctx = low[max(0, i - 40):i]
            quoted = i > 0 and low[i - 1] in "\"'"
            if not quoted and not any(w in ctx for w in NEG):
                tells.append(p); break
            i = low.find(p, i + 1)
    if tells:
        fails.append(f"AI-slop tell: {tells[:4]}")

    n = len(text.strip())
    if n < THIN_CHARS.get(kind, 200):
        warns.append(f"thin content ({n} chars)")

    # Bracketed fill-in fields are fine in templates, but a FINISHED non-template
    # deliverable riddled with them reads unfinished — warn past a threshold.
    brackets = _BRACKET_FIELD.findall(text)
    if len(brackets) > 25:
        warns.append(f"{len(brackets)} bracketed fields (template? confirm intentional)")
    return Verdict(ref or kind, not fails, fails, warns)


def _read_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages[:30])
        if ext == ".docx":
            import zipfile
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
            return re.sub(r"<[^>]+>", " ", xml)
        if ext in TEXT_EXT:
            return path.read_text(errors="ignore")
    except Exception as e:
        return f"__READ_ERROR__ {e}"
    return ""


def inspect_file(path, kind: str = "deliverable") -> Verdict:
    path = Path(path)
    if not path.exists():
        return Verdict(str(path), False, ["file missing"])
    text = _read_text(path)
    if text.startswith("__READ_ERROR__"):
        return Verdict(str(path), False, [f"unreadable: {text[14:80]}"])
    v = inspect_text(text, kind=kind, ref=str(path.relative_to(ROOT)) if ROOT in path.parents else str(path))
    if path.suffix.lower() in (".html", ".htm"):
        low = text.lower()
        if "<body" in low and not re.search(r"<body[^>]*>\s*\S", low):
            v.fails.append("empty <body>")
        if low.count("<html") and "</html>" not in low:
            v.warns.append("unclosed <html>")
        v.passed = not v.fails
    return v


def inspect_product_dir(d, kind: str = "product") -> Verdict:
    d = Path(d)
    deliverables = [p for p in d.rglob("*")
                    if p.is_file() and p.suffix.lower() in
                    (TEXT_EXT | {".pdf", ".docx", ".xlsx", ".zip"})]
    if not deliverables:
        return Verdict(str(d.name), False, ["EMPTY SHELL — no deliverable files"])
    total = sum(p.stat().st_size for p in deliverables)
    fails, warns = [], []
    if total < 2000:
        fails.append(f"thin product ({total} bytes total)")
    for f in deliverables:
        if f.suffix.lower() in (".zip", ".xlsx"):
            continue
        fv = inspect_file(f, kind="deliverable")
        fails += [f"{f.name}: {x}" for x in fv.fails]
        warns += [f"{f.name}: {x}" for x in fv.warns]
    return Verdict(d.name, not fails, fails, warns)


def ship_ok(path_or_text) -> tuple:
    """Single inline gate for send/fulfill paths. Accepts a path (file/dir) or raw text."""
    p = Path(str(path_or_text))
    if p.exists():
        v = inspect_product_dir(p) if p.is_dir() else inspect_file(p)
    else:
        v = inspect_text(str(path_or_text))
    return v.passed, v.fails + [f"(warn) {w}" for w in v.warns]


# ── batch sweep over the whole catalog ───────────────────────────────────────
def audit_all() -> dict:
    results = {"product": [], "mockup": [], "sku_files": []}
    gr = ROOT / "products" / "gumroad_ready"
    for d in sorted(gr.iterdir()) if gr.exists() else []:
        if d.is_dir() and d.name != "_packaged":
            results["product"].append(inspect_product_dir(d))
    mk = ROOT / "products_internal" / "landing" / "mockups"
    mocks = sorted(mk.glob("*.html"))[:40] if mk.exists() else []
    for f in mocks:
        results["mockup"].append(inspect_file(f, kind="mockup"))
    # duplicate-deliverable detector: same file mapped to >1 SKU = wrong-product risk
    app = ROOT / "products" / "self_serve" / "app.py"
    if app.exists():
        pairs = re.findall(r'\(\s*"([^"]+)"\s*,\s*"([^"]+\.(?:html|pdf|md|csv|zip|docx))"\s*\)',
                           app.read_text(errors="ignore"))
        seen = {}
        for keyword, f in pairs:
            seen.setdefault(f, []).append(keyword)
        for f, kws in seen.items():
            if len(kws) > 1:
                # Most multi-keyword maps are intentional fuzzy aliases for ONE product
                # (e.g. "follow-up"/"follow up"). Flag as a warn for human triage, not a
                # hard fail — EXCEPT the known wrong-product case to keep it visible.
                hard = "affirmation" in f.lower()
                results["sku_files"].append(Verdict(
                    f, not hard,
                    ([f"distinct products share this file: {kws}"] if hard else []),
                    ([] if hard else [f"file reused by {len(kws)} keyword(s): {kws} — confirm same product"])))
    return results


def _print_report(results: dict) -> int:
    fails = 0
    for cat, verds in results.items():
        bad = [v for v in verds if not v.passed]
        print(f"\n== {cat}: {len(verds)} checked | {len(bad)} FAIL ==")
        for v in bad:
            fails += 1
            print(f"  ✗ {v.ref}")
            for x in v.fails[:5]:
                print(f"      {x}")
        warns = [v for v in verds if v.passed and v.warns]
        for v in warns[:8]:
            print(f"  ⚠ {v.ref}: {v.warns[0]}")
    print(f"\n=== ship-guard: {fails} artifact(s) would ship SHITTY ===")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="auto-QA gate — block anything shitty from shipping")
    ap.add_argument("target", nargs="?", help="file/dir to check (default: --all sweep)")
    ap.add_argument("--all", action="store_true", help="sweep the whole catalog")
    ap.add_argument("--alert", action="store_true", help="iMessage on failures (for scheduled runs)")
    args = ap.parse_args()

    if args.target and not args.all:
        ok, issues = ship_ok(args.target)
        print(("PASS" if ok else "FAIL") + f": {args.target}")
        for i in issues:
            print("  " + i)
        return 0 if ok else 1

    fails = _print_report(audit_all())
    if fails and args.alert:
        try:
            from tools.imsg_bridge import send_imessage
            send_imessage(f"⚠️ ship-guard: {fails} product/artifact would ship shitty — "
                          "run `python3 -m core.ship_guard --all` to see.")
        except Exception:
            pass
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
