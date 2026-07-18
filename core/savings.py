"""Canonical savings + cost calculation. Single source of truth.

Both the WebUI control center and the macOS menubar tracker import this so
the numbers match — previously each computed its own definition of "savings"
and they disagreed.

Reads three JSONL logs:
    data/logs/cost.jsonl            — actual Anthropic API spend
    data/logs/savings.jsonl         — pure-local Ollama calls (displaced API cost)
    data/logs/hybrid_savings.jsonl  — API-tool + local-analysis hybrids

Returns one dict with every metric either consumer might want; pick what you need.
"""

import json
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from core import mountain_now, _get_mountain_tz
from core.payoff_ledger import amount_from_entry

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "data" / "logs"

# Constants — change here, propagates everywhere
DEVICE_COST = 1138.49           # Mac mini ($1082.49) + anomaly API burn ($56)
MAX_MONTHLY = 100.00            # Claude Max subscription cost the local stack displaces
TZ = _get_mountain_tz()
START_DATE = datetime(2026, 4, 13, tzinfo=TZ)
AGGRESSIVE_MONTHLY_RUN_RATE = 175.00


def _now():
    return mountain_now()


def _read_jsonl(path: Path):
    if not path.exists():
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return


def _provider_bucket(local_model: str, tier: str) -> str:
    model = str(local_model or "").strip().lower()
    if tier != "free_pool":
        return "Local"
    if not model:
        return "Free Pool"
    if "groq" in model:
        return "Groq"
    if "gemini" in model or "google" in model:
        return "Gemini"
    if "cerebras" in model:
        return "Cerebras"
    if "sambanova" in model:
        return "SambaNova"
    if "openrouter" in model:
        return "OpenRouter"
    if "nvidia" in model:
        return "OpenRouter Nvidia"
    return model.replace("-", " ").title()


def _event_identity(entry: dict) -> str:
    explicit = str(entry.get("event_id") or entry.get("id") or "").strip()
    if explicit:
        return explicit
    raw = json.dumps(
        {
            "ts": entry.get("ts"),
            "date": entry.get("date"),
            "source": entry.get("source"),
            "kind": entry.get("kind"),
            "action": entry.get("action"),
            "metadata": entry.get("metadata") or {},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _automation_amount(entry: dict) -> float:
    try:
        count = max(int(entry.get("count") or 1), 1)
    except Exception:
        count = 1
    for key in ("saved_usd", "saved", "amount_usd", "value_usd"):
        try:
            value = float(entry.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > 0:
            return value
    try:
        unit = float(entry.get("api_equivalent_cost_usd") or entry.get("unit_value_usd") or 0.0)
    except Exception:
        unit = 0.0
    return max(unit * count, 0.0)


def _automation_count(entry: dict) -> int:
    try:
        return max(int(entry.get("count") or 1), 1)
    except Exception:
        return 1


_UNQUALIFIED_SAVINGS_SOURCES = {"estimated", "restored", "historical:savings"}
_NON_PROD_SOURCE_PREFIXES = ("test", "stress_test", "proof_test", "crash", "e2e_test")


def _entry_source(entry: dict) -> str:
    return str(entry.get("source") or entry.get("caller") or "unknown").strip().lower()


def _qualified_savings_entry(entry: dict) -> bool:
    """Return True for rows that are safe to show as current saved spend."""
    source = _entry_source(entry)
    if bool(entry.get("backfill")) or bool(entry.get("historical")):
        return False
    if source in _UNQUALIFIED_SAVINGS_SOURCES:
        return False
    if any(source.startswith(prefix) for prefix in _NON_PROD_SOURCE_PREFIXES):
        return False
    return True


def _qualified_saved_usd(entry: dict) -> float:
    if not _qualified_savings_entry(entry):
        return 0.0
    # Free-pool routes are useful off-platform calls, but they did not
    # displace paid API spend from this stack.
    if str(entry.get("tier") or "").strip().lower() == "free_pool":
        return 0.0
    return _automation_amount({"saved_usd": entry.get("saved") or entry.get("saved_usd") or 0})


def compute(now=None) -> dict:
    """Single canonical savings snapshot. Pure function — no side effects, safe to call from anywhere."""
    now = now or _now()
    today = now.strftime("%Y-%m-%d")
    days_active = max((now - START_DATE).days, 1)
    months_active = days_active / 30.0

    # ── Pull API spend from cost.jsonl ────────────────────────────────────
    api_total = 0.0
    api_today = 0.0
    api_calls_total = 0
    api_calls_today = 0
    by_model = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    for entry in _read_jsonl(LOG_DIR / "cost.jsonl"):
        cost = entry.get("cost", 0) or 0
        api_total += cost
        api_calls_total += 1
        if entry.get("date") == today:
            api_today += cost
            api_calls_today += 1
        model = entry.get("model", "unknown")
        # Normalize model names: claude-sonnet-4-20250514 -> sonnet
        short = model
        for marker in ("haiku", "sonnet", "opus"):
            if marker in model.lower():
                short = marker
                break
        by_model[short]["cost"] += cost
        by_model[short]["calls"] += 1

    # ── Pull local-displaced savings ──────────────────────────────────────
    local_saved_total = 0.0
    local_saved_today = 0.0
    local_calls_total = 0
    local_calls_today = 0
    local_only_calls_total = 0
    local_only_calls_today = 0
    free_api_calls_total = 0
    free_api_calls_today = 0
    free_api_by_provider_today = defaultdict(int)
    free_api_by_provider_total = defaultdict(int)
    by_source = defaultdict(lambda: {"saved": 0.0, "calls": 0})
    for entry in _read_jsonl(LOG_DIR / "savings.jsonl"):
        if not _qualified_savings_entry(entry):
            continue
        s = _qualified_saved_usd(entry)
        local_saved_total += s
        local_calls_total += 1
        tier = str(entry.get("tier") or "").strip().lower()
        provider = _provider_bucket(entry.get("local_model", ""), tier)
        if tier == "free_pool":
            free_api_calls_total += 1
            free_api_by_provider_total[provider] += 1
        else:
            local_only_calls_total += 1
        if entry.get("date") == today:
            local_saved_today += s
            local_calls_today += 1
            if tier == "free_pool":
                free_api_calls_today += 1
                free_api_by_provider_today[provider] += 1
            else:
                local_only_calls_today += 1
        source = entry.get("source", "unknown")
        # Normalize source: "exec_assistant:score" -> "exec_assistant"
        agent = source.split(":")[0] if source else "unknown"
        by_source[agent]["saved"] += s
        by_source[agent]["calls"] += 1

    # ── Pull hybrid savings ───────────────────────────────────────────────
    hybrid_saved_total = 0.0
    hybrid_saved_today = 0.0
    hybrid_calls_total = 0
    hybrid_calls_today = 0
    for entry in _read_jsonl(LOG_DIR / "hybrid_savings.jsonl"):
        if not _qualified_savings_entry(entry):
            continue
        s = _qualified_saved_usd(entry)
        hybrid_saved_total += s
        hybrid_calls_total += 1
        if entry.get("date") == today:
            hybrid_saved_today += s
            hybrid_calls_today += 1

    # ── Pull non-model machine avoided costs ──────────────────────────────
    automation_saved_total = 0.0
    automation_saved_today = 0.0
    automation_calls_total = 0
    automation_calls_today = 0
    automation_sources = defaultdict(lambda: {"saved": 0.0, "calls": 0})
    try:
        from core import automation_usage

        automation_events = []
        automation_events.extend(automation_usage.local_machine_api_equivalent_events(log_dir=LOG_DIR))
        automation_events.extend(automation_usage.read_events(log_dir=LOG_DIR))
        automation_events.extend(automation_usage.background_log_activity_events(log_dir=LOG_DIR))
        seen_automation: set[str] = set()
        for entry in automation_events:
            event_key = _event_identity(entry)
            if event_key in seen_automation:
                continue
            seen_automation.add(event_key)
            amount = _automation_amount(entry)
            count = _automation_count(entry)
            if amount <= 0 or count <= 0:
                continue
            automation_saved_total += amount
            automation_calls_total += count
            if automation_usage.entry_date(entry) == today:
                automation_saved_today += amount
                automation_calls_today += count
            source = str(entry.get("source") or "automation")
            kind = str(entry.get("kind") or "").strip()
            source_key = f"{source}:{kind}" if kind and kind not in source else source
            automation_sources[source_key]["saved"] += amount
            automation_sources[source_key]["calls"] += count
    except Exception:
        pass

    # ── Pull direct machine payoff credits ────────────────────────────────
    payoff_credit_total = 0.0
    payoff_credit_today = 0.0
    payoff_credit_calls_total = 0
    payoff_credit_calls_today = 0
    for entry in _read_jsonl(LOG_DIR / "machine_payoff_credits.jsonl"):
        amount = amount_from_entry(entry)
        if amount <= 0:
            continue
        payoff_credit_total += amount
        payoff_credit_calls_total += 1
        if entry.get("date") == today:
            payoff_credit_today += amount
            payoff_credit_calls_today += 1

    # ── Net (the headline number) ────────────────────────────────────────
    avoided_today = local_saved_today + hybrid_saved_today + automation_saved_today
    avoided_alltime = local_saved_total + hybrid_saved_total + automation_saved_total
    net_today = avoided_today - api_today
    net_alltime = avoided_alltime - api_total
    displaced_calls_today = local_calls_today + hybrid_calls_today + automation_calls_today
    displaced_calls_total = local_calls_total + hybrid_calls_total + automation_calls_total
    total_calls_today = displaced_calls_today + api_calls_today
    total_calls_total = displaced_calls_total + api_calls_total

    # ── vs. Claude Max subscription baseline ─────────────────────────────
    max_would_cost = MAX_MONTHLY * months_active
    savings_vs_max = max_would_cost - api_total
    monthly_avg_api = (api_total / days_active) * 30 if days_active else 0
    monthly_savings_run_rate = MAX_MONTHLY - monthly_avg_api + (
        (avoided_alltime / days_active) * 30
    ) + (
        (payoff_credit_total / days_active) * 30
    )
    aggressive_monthly_savings_run_rate = max(monthly_savings_run_rate, AGGRESSIVE_MONTHLY_RUN_RATE)

    # ── Device payoff ────────────────────────────────────────────────────
    paid_off_total = avoided_alltime + savings_vs_max + payoff_credit_total
    aggressive_paid_off_total = paid_off_total
    if aggressive_monthly_savings_run_rate > monthly_savings_run_rate:
        aggressive_paid_off_total += aggressive_monthly_savings_run_rate - monthly_savings_run_rate
    remaining_balance = DEVICE_COST - paid_off_total
    aggressive_remaining_balance = DEVICE_COST - aggressive_paid_off_total
    pct_paid_off = (paid_off_total / DEVICE_COST) * 100 if DEVICE_COST else 0
    aggressive_pct_paid_off = (aggressive_paid_off_total / DEVICE_COST) * 100 if DEVICE_COST else 0
    if monthly_savings_run_rate > 0 and paid_off_total < DEVICE_COST:
        months_to_payoff = remaining_balance / monthly_savings_run_rate
        payoff_date = (now + timedelta(days=months_to_payoff * 30)).strftime("%Y-%m-%d")
    else:
        months_to_payoff = None
        payoff_date = "PAID OFF" if paid_off_total >= DEVICE_COST else None
    if aggressive_monthly_savings_run_rate > 0 and aggressive_paid_off_total < DEVICE_COST:
        aggressive_months_to_payoff = aggressive_remaining_balance / aggressive_monthly_savings_run_rate
        aggressive_payoff_date = (now + timedelta(days=aggressive_months_to_payoff * 30)).strftime("%Y-%m-%d")
    else:
        aggressive_months_to_payoff = None
        aggressive_payoff_date = "PAID OFF" if aggressive_paid_off_total >= DEVICE_COST else None

    combined_sources = defaultdict(lambda: {"saved": 0.0, "calls": 0})
    for source_map in (by_source, automation_sources):
        for key, value in source_map.items():
            combined_sources[key]["saved"] += float(value.get("saved") or 0.0)
            combined_sources[key]["calls"] += int(value.get("calls") or 0)

    return {
        "as_of": now.isoformat(timespec="seconds"),
        "today": {
            "api_cost": round(api_today, 4),
            "local_saved": round(local_saved_today, 4),
            "hybrid_saved": round(hybrid_saved_today, 4),
            "automation_saved": round(automation_saved_today, 4),
            "avoided_cost": round(avoided_today, 4),
            "gross_saved": round(avoided_today, 4),
            "net_saved": round(net_today, 4),
            "local_calls": local_calls_today,
            "displaced_calls": displaced_calls_today,
            "total_calls": total_calls_today,
            "local_only_calls": local_only_calls_today,
            "free_api_calls": free_api_calls_today,
            "free_api_by_provider": dict(sorted(free_api_by_provider_today.items(), key=lambda item: (-item[1], item[0]))),
            "call_distribution": {
                "Local": local_only_calls_today,
                "Automation": automation_calls_today,
                "Free API": free_api_calls_today,
                "Hybrid": hybrid_calls_today,
                "Paid API": api_calls_today,
            },
            "hybrid_calls": hybrid_calls_today,
            "automation_calls": automation_calls_today,
            "api_calls": api_calls_today,
            "payoff_credits": round(payoff_credit_today, 4),
            "payoff_credit_calls": payoff_credit_calls_today,
        },
        "alltime": {
            "api_cost": round(api_total, 4),
            "local_saved": round(local_saved_total, 4),
            "hybrid_saved": round(hybrid_saved_total, 4),
            "automation_saved": round(automation_saved_total, 4),
            "avoided_cost": round(avoided_alltime, 4),
            "gross_saved": round(avoided_alltime, 4),
            "net_saved": round(net_alltime, 4),
            "local_calls": local_calls_total,
            "displaced_calls": displaced_calls_total,
            "total_calls": total_calls_total,
            "local_only_calls": local_only_calls_total,
            "free_api_calls": free_api_calls_total,
            "free_api_by_provider": dict(sorted(free_api_by_provider_total.items(), key=lambda item: (-item[1], item[0]))),
            "call_distribution": {
                "Local": local_only_calls_total,
                "Automation": automation_calls_total,
                "Free API": free_api_calls_total,
                "Hybrid": hybrid_calls_total,
                "Paid API": api_calls_total,
            },
            "hybrid_calls": hybrid_calls_total,
            "automation_calls": automation_calls_total,
            "api_calls": api_calls_total,
            "days_active": days_active,
            "payoff_credits": round(payoff_credit_total, 4),
            "payoff_credit_calls": payoff_credit_calls_total,
        },
        "vs_max": {
            "max_monthly": MAX_MONTHLY,
            "months_active": round(months_active, 2),
            "max_would_have_cost": round(max_would_cost, 2),
            "actual_api_cost": round(api_total, 4),
            "savings_vs_max": round(savings_vs_max, 2),
            "monthly_avg_api_spend": round(monthly_avg_api, 2),
        },
        "payoff": {
            "device_cost": DEVICE_COST,
            "amount_paid_off": round(paid_off_total, 2),
            "remaining": round(remaining_balance, 2),
            "remaining_balance": round(remaining_balance, 2),
            "owed": round(max(remaining_balance, 0.0), 2),
            "credit_value": round(payoff_credit_total, 2),
            "pct_paid_off": round(pct_paid_off, 1),
            "monthly_savings_run_rate": round(monthly_savings_run_rate, 2),
            "months_to_payoff": round(months_to_payoff, 1) if months_to_payoff else None,
            "estimated_payoff_date": payoff_date,
        },
        "aggressive_projection": {
            "monthly_savings_run_rate": round(aggressive_monthly_savings_run_rate, 2),
            "amount_paid_off": round(aggressive_paid_off_total, 2),
            "remaining_balance": round(aggressive_remaining_balance, 2),
            "pct_paid_off": round(aggressive_pct_paid_off, 1),
            "months_to_payoff": round(aggressive_months_to_payoff, 1) if aggressive_months_to_payoff else None,
            "estimated_payoff_date": aggressive_payoff_date,
            "assumption": "floor aggressive estimate at $175/mo run-rate without changing actual ledger values",
        },
        "by_model": {
            k: {"cost": round(v["cost"], 4), "calls": v["calls"]} for k, v in by_model.items()
        },
        "by_source": {
            k: {"saved": round(v["saved"], 4), "calls": v["calls"]}
            for k, v in sorted(combined_sources.items(), key=lambda x: -x[1]["saved"])
        },
    }
