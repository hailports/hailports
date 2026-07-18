"""trade_risk.py — shared, research-calibrated position sizing + circuit breakers.

Encodes the survival rules from data/trading/STRATEGY_PLAYBOOK.md (Kelly/Thorp,
risk-of-ruin math, and the retail-loss studies: 70-97% of retail loses, driven by
oversizing + overtrading, NOT bad luck). The lesson is unanimous: at $25-100,
survival dominates edge-finding. This module is the single place that math lives so
every bot sizes the same disciplined way. $0, stdlib only.
"""
from __future__ import annotations

# Defaults are the practitioner-standard values from the research, overridable per lane.
KELLY_FRACTION = 0.25          # quarter-Kelly: ~75% of growth at a fraction of the variance
MAX_RISK_PER_TRADE = 0.02      # hard cap: never risk >2% of equity to the stop
MAX_POSITION_PCT = 0.15        # concentration cap per position
DAILY_HALT_PCT = 0.05          # stop new entries after -5% on the day
TOTAL_HALT_PCT = 0.18          # pause the lane after -18% from peak (no auto-resume)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def kelly_fraction(win_prob: float, payoff_ratio: float, fraction: float = KELLY_FRACTION) -> float:
    """Fractional-Kelly bet fraction of equity. f* = (b*p - q)/b, then scaled down.
    Never negative (no bet on a non-positive edge). payoff_ratio b = avg_win/avg_loss."""
    p = _clamp(win_prob, 0.0, 1.0)
    b = max(1e-6, payoff_ratio)
    full = (b * p - (1.0 - p)) / b
    return max(0.0, fraction * full)


def position_notional(equity: float, *, win_prob: float, payoff_ratio: float, stop_pct: float,
                      max_risk_pct: float = MAX_RISK_PER_TRADE, kelly_frac: float = KELLY_FRACTION,
                      max_position_pct: float = MAX_POSITION_PCT) -> float:
    """Dollar notional = MIN of three independent caps (survive-first):
      1. risk cap:    (max_risk_pct * equity) / stop_pct   — the stop never loses >max_risk_pct
      2. kelly cap:   fractional-Kelly * equity
      3. concentration cap: max_position_pct * equity
    Returns 0 on a non-positive edge or nonsensical inputs."""
    if equity <= 0 or stop_pct <= 0:
        return 0.0
    risk_notional = (max_risk_pct * equity) / stop_pct
    kelly_notional = kelly_fraction(win_prob, payoff_ratio, kelly_frac) * equity
    conc_notional = max_position_pct * equity
    return round(max(0.0, min(risk_notional, kelly_notional, conc_notional)), 4)


def circuit_breaker(*, equity: float, day_start_equity: float, peak_equity: float,
                    daily_halt: float = DAILY_HALT_PCT, total_halt: float = TOTAL_HALT_PCT) -> list[str]:
    """Return halt reasons (empty = clear to trade). Daily + peak-drawdown breakers."""
    reasons: list[str] = []
    if day_start_equity > 0 and (day_start_equity - equity) / day_start_equity >= daily_halt:
        reasons.append(f"daily circuit breaker: -{(day_start_equity-equity)/day_start_equity*100:.1f}% >= {daily_halt*100:.0f}%")
    if peak_equity > 0 and (peak_equity - equity) / peak_equity >= total_halt:
        reasons.append(f"drawdown circuit breaker: -{(peak_equity-equity)/peak_equity*100:.1f}% >= {total_halt*100:.0f}%")
    return reasons


def round_trip_cost(fee_bps: float, slippage_bps: float) -> float:
    """Fractional round-trip cost (both legs): fees + slippage."""
    return 2.0 * (fee_bps + slippage_bps) / 10000.0


def edge_clears_cost(expected_move: float, fee_bps: float, slippage_bps: float, margin: float = 1.5) -> bool:
    """Anti-churn gate: only trade when the expected move beats round-trip cost by a margin.
    This is THE rule that kills fee-bleeding micro-churn (naive signals die at ~10bps cost)."""
    return expected_move > round_trip_cost(fee_bps, slippage_bps) * margin


def risk_of_ruin(edge: float, units: int) -> float:
    """Rough risk-of-ruin for a symmetric bet with `edge` (win_rate-loss_rate) over
    `units` of capital-at-risk. RoR = ((1-edge)/(1+edge))^units. For reporting/sanity."""
    edge = _clamp(edge, 1e-6, 0.999)
    return round(((1 - edge) / (1 + edge)) ** max(1, units), 6)


if __name__ == "__main__":
    eq = 25.0
    print("quarter-Kelly examples (equity $25):")
    for wp, b, sp in [(0.55, 1.5, 0.02), (0.50, 1.0, 0.02), (0.60, 2.0, 0.03)]:
        n = position_notional(eq, win_prob=wp, payoff_ratio=b, stop_pct=sp)
        print(f"  win_prob={wp} payoff={b} stop={sp:.0%} -> notional ${n:.2f} ({n/eq*100:.1f}% of equity)")
    print("circuit breaker (-6% day):", circuit_breaker(equity=23.5, day_start_equity=25, peak_equity=25))
    print("edge gate 0.2% move @ Kraken taker:", edge_clears_cost(0.002, 40, 15))
    print("edge gate 3.0% move @ Kraken taker:", edge_clears_cost(0.030, 40, 15))
    print("risk_of_ruin(edge=0.04, 10 units):", risk_of_ruin(0.04, 10))
