"""Runtime pressure classifier for routing decisions.

Keeps the live UI responsive by letting callers detect when the local box is
too stressed to be the first-choice inference path.
"""

from __future__ import annotations

import re
import subprocess
import time

_CACHE_TTL_S = 10
_cache_ts = 0.0
_cache_value: dict | None = None


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _swap_mb() -> float:
    out = _run(["sysctl", "vm.swapusage"])
    if "used" not in out:
        return 0.0
    try:
        return float(out.split("used = ")[1].split("M")[0])
    except Exception:
        return 0.0


def _headroom_gb() -> float:
    out = _run(["/bin/zsh", "-lc", "vm_stat | head -6"])
    if not out:
        return 0.0
    try:
        lines = out.splitlines()
        page_size = 16384
        free = int(lines[1].split()[-1].rstrip("."))
        inactive = int(lines[3].split()[-1].rstrip("."))
        return round((free + inactive) * page_size / 1024 / 1024 / 1024, 2)
    except Exception:
        return 0.0


def _memory_pressure() -> int:
    out = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    try:
        return int(out)
    except Exception:
        return 1


def _load_1m() -> float:
    out = _run(["sysctl", "-n", "vm.loadavg"])
    if not out:
        return 0.0
    try:
        nums = re.findall(r"[\d.]+", out)
        return float(nums[0]) if nums else 0.0
    except Exception:
        return 0.0


def _stale_swap_only(metrics: dict) -> bool:
    """macOS can hold onto swap long after pressure clears.

    Treat elevated swap by itself as a softer signal when the box otherwise
    looks healthy, so we do not suppress local inference all day on stale swap.
    """
    swap_mb = float(metrics.get("swap_mb") or 0.0)
    headroom_gb = float(metrics.get("headroom_gb") or 0.0)
    memory_pressure = int(metrics.get("memory_pressure") or 0)
    load_1m = float(metrics.get("load_1m") or 0.0)
    return (
        swap_mb >= 3072
        and swap_mb < 8192
        and headroom_gb >= 6.0
        and memory_pressure <= 1
        and load_1m < 4.0
    )


def get_runtime_pressure(force: bool = False) -> dict:
    global _cache_ts, _cache_value

    now = time.time()
    if not force and _cache_value is not None and (now - _cache_ts) < _CACHE_TTL_S:
        return dict(_cache_value)

    metrics = {
        "swap_mb": round(_swap_mb()),
        "headroom_gb": _headroom_gb(),
        "memory_pressure": _memory_pressure(),
        "load_1m": round(_load_1m(), 2),
    }

    hard_reasons = []
    if metrics["memory_pressure"] >= 4:
        hard_reasons.append(f"memory pressure={metrics['memory_pressure']}")
    if metrics["swap_mb"] >= 3072 and not _stale_swap_only(metrics):
        hard_reasons.append(f"swap={metrics['swap_mb']}MB")
    if metrics["headroom_gb"] and metrics["headroom_gb"] < 1.0:
        hard_reasons.append(f"headroom={metrics['headroom_gb']}GB")
    if metrics["load_1m"] >= 12.0:
        hard_reasons.append(f"load1={metrics['load_1m']}")

    soft_reasons = []
    if metrics["memory_pressure"] >= 2:
        soft_reasons.append(f"memory pressure={metrics['memory_pressure']}")
    if metrics["swap_mb"] >= 1536:
        soft_reasons.append(f"swap={metrics['swap_mb']}MB")
    if metrics["headroom_gb"] and metrics["headroom_gb"] < 2.5:
        soft_reasons.append(f"headroom={metrics['headroom_gb']}GB")
    if metrics["load_1m"] >= 8.0:
        soft_reasons.append(f"load1={metrics['load_1m']}")

    if hard_reasons:
        mode = "overloaded"
        reasons = hard_reasons
    elif soft_reasons:
        mode = "constrained"
        reasons = soft_reasons
    else:
        mode = "normal"
        reasons = []

    _cache_value = {
        "mode": mode,
        "prefer_remote": mode in {"constrained", "overloaded"},
        "avoid_local": mode == "overloaded",
        "reasons": reasons,
        "metrics": metrics,
        "as_of": now,
    }
    _cache_ts = now
    return dict(_cache_value)
