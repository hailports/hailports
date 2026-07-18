#!/usr/bin/env python3
"""Self-contained cost / savings / ROI / usage page generator.

Preserves the spend tracker that "died with MyOS" as a standalone artifact that
does NOT depend on MyOS, the webui_bridge server, or any network: it renders the
live numbers (tools.spend_dashboard.build_report + lifetime_estimate) straight
into one static HTML file with the data baked in. A <meta refresh> makes the open
page re-read the file from disk, and a launchd job regenerates it on an interval,
so it stays current with zero server. Open it over file:// or serve it statically
— both work identically.

    PYTHONPATH=. .venv/bin/python tools/cost_page.py            # write frontends/cost.html
    PYTHONPATH=. .venv/bin/python tools/cost_page.py --print    # also echo path
"""
from __future__ import annotations

import html
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_HTML = BASE_DIR / "frontends" / "cost.html"
OUT_JSON = BASE_DIR / "frontends" / "cost.json"
REFRESH_SECONDS = 120  # browser re-reads the file; launchd regenerates underneath


def _money(v) -> str:
    try:
        v = float(v or 0)
    except Exception:
        v = 0.0
    return f"${v:,.2f}" if abs(v) >= 1 else f"${v:.4f}"


def _num(v) -> str:
    try:
        return f"{int(round(float(v or 0))):,}"
    except Exception:
        return str(v)


def _esc(s) -> str:
    return html.escape(str(s))


def _card(label, value, sub="", tone="") -> str:
    cls = f"card {tone}".strip()
    sub_html = f'<div class="sub">{_esc(sub)}</div>' if sub else ""
    return (f'<div class="{cls}"><div class="lbl">{_esc(label)}</div>'
            f'<div class="val">{_esc(value)}</div>{sub_html}</div>')


def _row(*cells) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _table(headers, rows) -> str:
    if not rows:
        return '<p class="empty">— none —</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def build_html(report: dict, lifetime: dict) -> str:
    can = report.get("canonical", {}) or {}
    today = can.get("today", {}) if "_error" not in can else {}
    alltime = can.get("alltime", {}) if "_error" not in can else {}
    vs_max = can.get("vs_max", {}) if "_error" not in can else {}
    payoff = can.get("payoff", {}) if "_error" not in can else {}
    proj = can.get("aggressive_projection", {}) if "_error" not in can else {}
    by_source = can.get("by_source", {}) if "_error" not in can else {}
    by_model = can.get("by_model", {}) if "_error" not in can else {}
    wk = report.get("seven_day", {}) or {}
    rev = report.get("revenue", {}) or {}
    auto = report.get("automation", {}) or {}
    as_of = report.get("as_of", "")

    # ── hero cards ──────────────────────────────────────────────────────────
    net_all = alltime.get("net_saved", 0)
    spend_all = float(alltime.get("api_cost", 0) or 0)
    revenue = float(rev.get("revenue", 0) or 0)
    if revenue > 0 and spend_all > 0:
        roi_txt = f"{((revenue - spend_all) / spend_all) * 100:,.0f}%"
        roi_sub = f"rev {_money(revenue)} vs spend {_money(spend_all)}"
    elif revenue > 0:
        roi_txt, roi_sub = _money(revenue), "spend ~$0"
    else:
        roi_txt = _money(alltime.get("avoided_cost", 0))
        roi_sub = "value delivered (no revenue logged yet)"

    hero = "".join([
        _card("All-time net saved", _money(net_all),
              f"over {alltime.get('days_active', 0)} days", "good"),
        _card("Paid API spend (all-time)", _money(spend_all),
              f"{alltime.get('api_calls', 0)} paid calls",
              "bad" if spend_all > 1 else "good"),
        _card("Today net saved", _money(today.get("net_saved", 0)),
              f"{_num(today.get('total_calls', 0))} calls", "good"),
        _card("Last 7 days net", _money(wk.get("net_saved", 0)),
              f"{_num(wk.get('total_calls', 0))} calls", "good"),
        _card("ROI / value", roi_txt, roi_sub, "accent"),
        _card("vs $100/mo max plan", _money(vs_max.get("savings_vs_max", 0)),
              f"would've cost {_money(vs_max.get('max_would_have_cost', 0))}", "good"),
    ])

    # ── today / 7-day / all-time detail tables ──────────────────────────────
    def kv_rows(pairs):
        return "".join(_row(f'<span class="k">{_esc(k)}</span>', v) for k, v in pairs)

    today_tbl = _table(["Today", ""], [kv_rows([
        ("Paid API spend", f"{_money(today.get('api_cost', 0))} ({today.get('api_calls', 0)} calls)"),
        ("Local + free saved", f"{_money(today.get('local_saved', 0))} ({today.get('local_calls', 0)} calls)"),
        ("Hybrid saved", f"{_money(today.get('hybrid_saved', 0))} ({today.get('hybrid_calls', 0)} calls)"),
        ("Automation saved", f"{_money(today.get('automation_saved', 0))} ({_num(today.get('automation_calls', 0))} runs)"),
        ("Gross avoided", _money(today.get("avoided_cost", 0))),
        ("NET (saved − spend)", f'<b class="good">{_money(today.get("net_saved", 0))}</b>'),
    ])])

    wk_tbl = _table([f"Last 7 days (since {wk.get('since', '?')})", ""], [kv_rows([
        ("Paid API spend", f"{_money(wk.get('paid_spend', 0))} ({wk.get('paid_calls', 0)} calls)"),
        ("Local saved", f"{_money(wk.get('local_saved', 0))} ({wk.get('local_calls', 0)} calls)"),
        ("Free-pool calls", f"{wk.get('free_calls', 0)} calls ($0 displaced)"),
        ("Hybrid saved", f"{_money(wk.get('hybrid_saved', 0))} ({wk.get('hybrid_calls', 0)} calls)"),
        ("Gross avoided", _money(wk.get("avoided_cost", 0))),
        ("NET (saved − spend)", f'<b class="good">{_money(wk.get("net_saved", 0))}</b>'),
        ("Total calls", _num(wk.get("total_calls", 0))),
    ])])

    alltime_tbl = _table(["All-time", ""], [kv_rows([
        ("Paid API spend", f"{_money(alltime.get('api_cost', 0))} ({alltime.get('api_calls', 0)} calls)"),
        ("Gross avoided", _money(alltime.get("avoided_cost", 0))),
        ("NET", f'<b class="good">{_money(alltime.get("net_saved", 0))}</b> over {alltime.get("days_active", 0)} days'),
        ("Avg monthly API spend", _money(vs_max.get("monthly_avg_api_spend", 0))),
        ("Months active", f"{vs_max.get('months_active', 0)}"),
    ])])

    # ── device payoff ───────────────────────────────────────────────────────
    pct = float(payoff.get("pct_paid_off", 0) or 0)
    pct_proj = float(proj.get("pct_paid_off", 0) or 0)
    payoff_tbl = _table(["Device payoff", ""], [kv_rows([
        ("Device cost", _money(payoff.get("device_cost", 0))),
        ("Paid off", f'{_money(payoff.get("amount_paid_off", 0))} ({pct}%)'),
        ("Remaining", _money(payoff.get("remaining_balance", payoff.get("remaining", 0)))),
        ("Savings run-rate", f"{_money(payoff.get('monthly_savings_run_rate', 0))}/mo"),
        ("Months to payoff", f"{payoff.get('months_to_payoff', 0)}"),
        ("Est. payoff date", _esc(payoff.get("estimated_payoff_date", "—"))),
    ])])
    proj_note = _esc(proj.get("assumption", "")) if proj else ""
    proj_tbl = _table(["Aggressive projection", ""], [kv_rows([
        ("Run-rate", f"{_money(proj.get('monthly_savings_run_rate', 0))}/mo"),
        ("Paid off", f'{_money(proj.get("amount_paid_off", 0))} ({pct_proj}%)'),
        ("Months to payoff", f"{proj.get('months_to_payoff', 0)}"),
        ("Est. payoff date", _esc(proj.get("estimated_payoff_date", "—"))),
    ])]) if proj else ""

    bar = (f'<div class="bar"><div class="fill" style="width:{min(100, pct):.1f}%"></div>'
           f'<div class="fill proj" style="width:{min(100, pct_proj):.1f}%"></div></div>'
           f'<div class="barlbl">{pct}% paid off '
           f'<span class="muted">(→ {pct_proj}% projected)</span></div>')

    # ── top savers / by model ───────────────────────────────────────────────
    src_rows = [_row(_esc(s.split(":")[0]), _num(d.get("calls", 0)), _money(d.get("saved", 0)))
                for s, d in sorted(by_source.items(), key=lambda kv: -float(kv[1].get("saved", 0)))[:12]]
    src_tbl = _table(["Source", "Calls", "Saved"], src_rows)

    model_rows = [_row(_esc(m), _num(d.get("calls", 0)), _money(d.get("cost", 0)))
                  for m, d in sorted(by_model.items(), key=lambda kv: -float(kv[1].get("cost", 0)))]
    model_tbl = _table(["Paid model", "Calls", "Cost"], model_rows)

    # ── call distribution + free providers ──────────────────────────────────
    dist = alltime.get("call_distribution", {}) or {}
    dist_rows = [_row(_esc(k), _num(v)) for k, v in sorted(dist.items(), key=lambda kv: -kv[1])]
    dist_tbl = _table(["Route (all-time)", "Calls"], dist_rows)
    free_prov = alltime.get("free_api_by_provider", {}) or {}
    prov_rows = [_row(_esc(k), _num(v)) for k, v in sorted(free_prov.items(), key=lambda kv: -kv[1])]
    prov_tbl = _table(["Free API provider", "Calls"], prov_rows)

    # ── automation health ───────────────────────────────────────────────────
    if "_error" in auto:
        auto_html = f'<p class="empty">automation read failed: {_esc(auto["_error"])}</p>'
    else:
        failing = auto.get("failing", [])
        fail_list = ", ".join(f"{_esc(j['label'].split('.')[-1])}({j.get('status')})" for j in failing)
        auto_html = _table(["Automation health", ""], [kv_rows([
            ("Jobs tracked", _num(auto.get("total", 0))),
            ("Running now", _num(len(auto.get("running", [])))),
            ("Last exit clean", _num(len(auto.get("clean", [])))),
            ("Signaled (ok)", _num(len(auto.get("signaled", [])))),
            ("FAILING (exit>0)", f'<b class="{"bad" if failing else "good"}">{len(failing)}</b>'
             + (f' <span class="muted">{fail_list}</span>' if failing else "")),
        ])])

    # ── lifetime value scenarios ────────────────────────────────────────────
    life_html = ""
    if lifetime and "scenarios" in lifetime:
        sc = lifetime["scenarios"]
        sc_rows = []
        for key in ("conservative", "likely", "optimistic"):
            s = sc.get(key, {})
            sc_rows.append(_row(
                _esc(key.title()),
                _money(s.get("logged_net_saved", 0)),
                _money(s.get("pre_instrumentation_extrapolated", 0)),
                _money(s.get("docs_value", 0)),
                _money(s.get("social_value", 0)),
                f'<b class="good">{_money(s.get("total_value", 0))}</b>',
                f'{s.get("pct_of_device", 0)}%',
            ))
        life_tbl = _table(
            ["Scenario", "Logged", "Extrapolated", "Docs", "Social", "Total value", "% device"],
            sc_rows)
        life_meta = (f'{lifetime.get("days_owned", "?")} days owned · '
                     f'{lifetime.get("days_logged", "?")} logged · '
                     f'{_num(lifetime.get("docs_generated", 0))} docs · '
                     f'{_num(lifetime.get("social_posts_logged", 0))} posts logged')
        life_html = (f'<section><h2>Lifetime value estimate</h2>'
                     f'<p class="muted">{_esc(life_meta)}</p>{life_tbl}'
                     f'<p class="note">{_esc(lifetime.get("note", ""))}</p></section>')

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Cost &amp; Savings · Claude Stack</title>
<style>
:root{{--bg:#0b0e14;--panel:#141925;--line:#222a3a;--ink:#e6e9ef;--muted:#8a93a6;
--good:#3ddc97;--bad:#ff6b6b;--warn:#ffd166;--accent:#7aa2ff;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
-webkit-font-smoothing:antialiased;padding:28px 22px 60px}}
h1{{font-size:20px;margin:0 0 2px;letter-spacing:.2px}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);
margin:26px 0 10px;font-weight:600}}
.head{{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;
border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:18px}}
.asof{{color:var(--muted);font-size:12px}}
.asof b{{color:var(--ink)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.card .lbl{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.6px}}
.card .val{{font-size:24px;font-weight:650;margin-top:6px;letter-spacing:-.5px}}
.card .sub{{color:var(--muted);font-size:11px;margin-top:3px}}
.card.good .val{{color:var(--good)}}
.card.bad .val{{color:var(--bad)}}
.card.accent .val{{color:var(--accent)}}
.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}}
table{{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:7px 12px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}}
th{{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;
background:#10141e}}
tr:last-child td{{border-bottom:none}}
td:not(:first-child),th:not(:first-child){{text-align:right;font-variant-numeric:tabular-nums}}
.k{{color:var(--muted)}}
.good{{color:var(--good)}} .bad{{color:var(--bad)}} .muted{{color:var(--muted)}}
.empty{{color:var(--muted);font-size:13px;padding:8px 2px}}
.note{{color:var(--muted);font-size:11.5px;margin-top:8px;max-width:760px}}
.bar{{position:relative;height:14px;background:#10141e;border:1px solid var(--line);
border-radius:8px;overflow:hidden;margin:10px 0 4px}}
.bar .fill{{position:absolute;top:0;left:0;height:100%;background:var(--good);border-radius:8px 0 0 8px}}
.bar .fill.proj{{background:transparent;border-right:2px dashed var(--accent);z-index:2}}
.barlbl{{font-size:12px;color:var(--ink)}}
.foot{{margin-top:34px;color:var(--muted);font-size:11px;border-top:1px solid var(--line);padding-top:12px}}
.tick{{font-variant-numeric:tabular-nums}}
</style>
</head>
<body>
<div class="head">
  <div>
    <h1>💸 Cost &amp; Savings</h1>
    <div class="asof">claude-stack spend / ROI / usage · standalone (no server)</div>
  </div>
  <div class="asof">as of <b>{_esc(as_of)}</b> · auto-refresh <span id="tick" class="tick">{REFRESH_SECONDS}</span>s</div>
</div>

<section><h2>At a glance</h2><div class="cards">{hero}</div></section>

<section><h2>Breakdown</h2><div class="grid2">{today_tbl}{wk_tbl}{alltime_tbl}</div></section>

<section><h2>Device payoff</h2>{bar}<div class="grid2">{payoff_tbl}{proj_tbl}</div>
<p class="note">{proj_note}</p></section>

<section><h2>Top savers &amp; paid models</h2><div class="grid2">{src_tbl}{model_tbl}</div></section>

<section><h2>Where the calls go</h2><div class="grid2">{dist_tbl}{prov_tbl}</div></section>

<section><h2>Automation health</h2>{auto_html}</section>

{life_html}

<div class="foot">Source: tools/spend_dashboard.build_report() + lifetime_estimate() over live ledgers ·
read-only · regenerated by com.claude-stack.cost-page · this page re-reads itself every {REFRESH_SECONDS}s.</div>

<script>
// offline countdown to the meta-refresh; no network, works over file://
var n={REFRESH_SECONDS},el=document.getElementById('tick');
setInterval(function(){{n--;if(el)el.textContent=n>0?n:0;}},1000);
</script>
</body>
</html>
"""
    return page


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    sys.path.insert(0, str(BASE_DIR))
    from tools.spend_dashboard import build_report, lifetime_estimate

    report = build_report()
    try:
        lifetime = lifetime_estimate()
    except Exception:
        lifetime = {}

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(build_html(report, lifetime))
    OUT_JSON.write_text(json.dumps({"report": report, "lifetime": lifetime}, indent=2, default=str))
    if "--print" in argv or "--verbose" in argv:
        print(f"wrote {OUT_HTML}")
        print(f"wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
