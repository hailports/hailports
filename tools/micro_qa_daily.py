#!/usr/bin/env python3
"""Daily QA gate for the micro-sale store (builtfast digital goods).

1. Re-runs the micro_fulfillment self-test (engine regression check).
2. Sweeps the last 48h of real buyer orders in data/hustle/micro_orders/ for any
   manifest with ok=false (a deliverable that failed its ship_guard gate).
Pages via core.alert_gateway on any failure — a broken register or a bad ship
must reach Operator before the next buyer hits it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(os.path.expanduser("~/claude-stack"))
sys.path.insert(0, str(BASE))
ORDERS = BASE / "data" / "hustle" / "micro_orders"
PY = str(BASE / "venv" / "bin" / "python")


def _alert(subject: str, body: str, level: str = "critical") -> None:
    try:
        from core.alert_gateway import notify
        notify("micro-qa", subject, body, issue_key="micro-qa:" + subject[:40], level=level)
    except Exception as e:
        print(f"[micro-qa] alert failed ({e}): {subject} | {body}", file=sys.stderr)


def selftest() -> bool:
    env = {**os.environ, "MICRO_LLM_ENRICH": "0"}  # deterministic path = what a buyer gets worst-case
    r = subprocess.run([PY, "-m", "agents.micro_fulfillment", "--selftest"],
                       cwd=str(BASE), env=env, capture_output=True, text=True, timeout=300)
    ok = r.returncode == 0 and "self-test PASSED" in (r.stdout + r.stderr)
    if not ok:
        tail = (r.stdout + r.stderr).strip().splitlines()[-8:]
        _alert("micro engine self-test FAILED",
               "Buyers may receive broken goods. Tail:\n" + "\n".join(tail))
    return ok


def _is_synthetic_order(m: dict) -> bool:
    """True for test/self-test/synthetic orders (invalid/test buyer domains or test SKUs) so the
    make-good page only fires for REAL failed deliveries. mo_..._empty_test / user@example.com was a
    self-test artifact paging as if a customer were stiffed."""
    email = str(m.get("buyer_email", "")).strip().lower()
    sku = str(m.get("sku", "")).strip().lower()
    dom = email.rsplit("@", 1)[-1] if "@" in email else ""
    bad_domains = {"b.invalid", "invalid", "test", "example.com", "example.org", "test.com"}
    if (not email) or dom in bad_domains or dom.endswith((".invalid", ".test", ".example")):
        return True
    if sku in {"_empty_test", "test", "selftest"} or sku.startswith(("_test", "test_", "_empty")):
        return True
    return bool(m.get("synthetic") or m.get("test"))


def sweep_recent_orders(hours: int = 48) -> list[str]:
    bad = []
    cutoff = time.time() - hours * 3600
    if not ORDERS.is_dir():
        return bad
    for mf in ORDERS.glob("*/manifest.json"):
        try:
            if mf.stat().st_mtime < cutoff:
                continue
            m = json.loads(mf.read_text())
            if _is_synthetic_order(m):
                continue  # test/synthetic orders never have a real buyer to make good — don't page
            if not m.get("ok"):
                bad.append(f"{m.get('order_id')} {m.get('sku')} buyer={m.get('buyer_email','')}")
        except Exception:
            bad.append(f"{mf.parent.name} (unreadable manifest)")
    if bad:
        _alert("micro order(s) shipped gate-FAILED",
               "Re-fulfill + make-good these orders:\n" + "\n".join(bad[:10]))
    return bad


def main() -> int:
    ok = selftest()
    bad = sweep_recent_orders()
    print(f"[micro-qa] selftest={'PASS' if ok else 'FAIL'} bad_recent_orders={len(bad)}")
    return 0 if (ok and not bad) else 1


if __name__ == "__main__":
    sys.exit(main())
