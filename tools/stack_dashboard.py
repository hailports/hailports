#!/usr/bin/env python3
"""ONE lean internal ops dashboard for the revenue stack. Localhost-only.

A single self-contained HTML page served by a tiny stdlib http.server bound to
127.0.0.1:8350 (NEVER public, never a brand domain — owner's private aggregate
ops view). Reuses existing engines; computes no new metrics.

  GET  /        -> the page (revenue / health / ROI / snapshot-compare)
  POST /roi     -> persist edited ROI factors to data/roi_factors.json

    PYTHONPATH=. .venv/bin/python tools/stack_dashboard.py            # serve :8350
    PYTHONPATH=. .venv/bin/python tools/stack_dashboard.py --selfcheck # render once, exit
"""
from __future__ import annotations

import html
import json
import socket
import sys
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

HOST, PORT = "127.0.0.1", 8350
SNAP = BASE / "data" / "stack_snapshots.jsonl"
ROI_FILE = BASE / "data" / "roi_factors.json"

# ROI savings assumption (stated on-page): each call the stack served locally /
# on a free tier / via automation WOULD have cost this much on a paid API. This
# is the same blended Haiku-class rate core.savings prices against.
DEFAULT_ROI = {
    "investment_to_date": 1138.49,        # device cost
    "est_monthly_cost": 0.03,             # from core.savings vs_max (run-rate)
    "avg_paid_api_price_per_call": 0.003, # ASSUMPTION: blended paid-API $/call
    "pipeline_value_usd": 0.0,            # owner's manual pipeline estimate
}


def _esc(s) -> str:
    return html.escape(str(s))


def _money(v) -> str:
    try:
        return f"${float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def load_snaps() -> list[dict]:
    rows = []
    if SNAP.exists():
        for line in SNAP.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def load_roi() -> dict:
    factors = dict(DEFAULT_ROI)
    if ROI_FILE.exists():
        try:
            factors.update(json.loads(ROI_FILE.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    return factors


def save_roi(updates: dict):
    factors = load_roi()
    for k in DEFAULT_ROI:
        if k in updates:
            try:
                factors[k] = float(updates[k])
            except (TypeError, ValueError):
                pass
    ROI_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROI_FILE.write_text(json.dumps(factors, indent=2))


# ── HEALTH: cheap boolean probes ────────────────────────────────────────────
def _port_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def health() -> list[tuple[str, bool, str]]:
    out = []
    # senders loaded: outbound key present + a send logged in last 24h
    env_ok = False
    try:
        env = (BASE / ".env").read_text()
        env_ok = "RESEND_API_KEY=" in env or "STRIPE_SECRET_KEY=" in env
    except OSError:
        pass
    recent_send = False
    sent = BASE / "data" / "hustle" / "broken_site_sent.jsonl"
    if sent.exists():
        cutoff = (datetime.now().astimezone() - timedelta(hours=24))
        for line in sent.read_text().splitlines()[-50:]:
            try:
                ts = json.loads(line).get("ts", "")
                if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff:
                    recent_send = True
                    break
            except (json.JSONDecodeError, ValueError):
                pass
    out.append(("senders loaded", env_ok, "outbound key + send in 24h"
                if (env_ok and recent_send) else "key present" if env_ok else "no key"))
    # storefront
    store_up = _port_up(8300)
    out.append(("storefront", store_up, "self-serve shop :8300"))
    # searxng
    out.append(("searxng", _port_up(8890), "search :8890"))
    # clone-guard / invariants
    inv = Path.home() / ".bee-hive-breaker.json"
    cg = (BASE / "data" / "hustle" / "brand_lane_firewall_sweep.jsonl").exists()
    out.append(("clone-guard", cg, "brand-lane firewall sweep present"))
    return out


# ── COMPARE: period rollups + deltas ────────────────────────────────────────
METRICS = [("sends_total", "sends"), ("replies_total", "replies"),
           ("leads_total", "leads"), ("revenue_usd", "$ revenue"),
           ("paid_orders", "orders")]


def _row_for(rows: list[dict], date: str) -> dict:
    for r in rows:
        if r.get("date") == date:
            return r
    return {}


def _sum_range(rows: list[dict], start: str, end: str) -> dict:
    acc = {k: 0 for k, _ in METRICS}
    for r in rows:
        if start <= r.get("date", "") <= end:
            for k, _ in METRICS:
                acc[k] += r.get(k) or 0
    return acc


def compare_periods(rows: list[dict]) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    dbefore = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    wk_start = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    pwk_start = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
    pwk_end = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    def pack(cur: dict, prev: dict) -> list[dict]:
        return [{"label": lbl, "key": k, "cur": cur.get(k) or 0,
                 "delta": (cur.get(k) or 0) - (prev.get(k) or 0)} for k, lbl in METRICS]

    return {
        "today": pack(_row_for(rows, today), _row_for(rows, yday)),
        "yesterday": pack(_row_for(rows, yday), _row_for(rows, dbefore)),
        "week": pack(_sum_range(rows, wk_start, today),
                     _sum_range(rows, pwk_start, pwk_end)),
    }


# ── RENDER ──────────────────────────────────────────────────────────────────
def render(rows: list[dict], roi: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = rows[-1] if rows else {}

    # A) REVENUE — anonymous lanes are aggregated; lane identities stay withheld.
    by_brand = today.get("sends_by_brand", {}) or {}
    anonymous_sends = sum(n for n in by_brand.values() if isinstance(n, (int, float)))
    brand_rows = (
        f"<tr><td>anonymous lanes</td><td>{anonymous_sends}</td><td class=muted>withheld</td>"
        f"<td class=muted>withheld</td><td class=muted>withheld</td></tr>"
        if by_brand else ""
    )
    total_row = (f"<tr class=tot><td><b>TOTAL</b></td><td><b>{today.get('sends_total',0)}</b></td>"
                 f"<td><b>{today.get('replies_total',0)}</b></td>"
                 f"<td><b>{today.get('leads_total',0)}</b></td>"
                 f"<td><b>{_money(today.get('revenue_usd'))}</b></td></tr>")
    rev_tbl = (f"<table><tr><th>lane</th><th>sends</th><th>replies</th><th>leads</th><th>$</th></tr>"
               f"{brand_rows}{total_row}</table>"
               "<p class=note>anonymous lane identities and ordering are withheld from this surface; "
               "payment attribution is withheld.</p>")

    # B) HEALTH dots
    dots = "".join(
        f'<div class="dot-row"><span class="dot {"ok" if ok else "bad"}"></span>'
        f'<b>{_esc(name)}</b><span class=muted> — {_esc(detail)}</span></div>'
        for name, ok, detail in health())

    # C) ROI
    api_calls = today.get("api_calls") or 0
    tokens = today.get("tokens") or 0
    jobs = today.get("automation_jobs") or 0
    inv = float(roi["investment_to_date"])
    monthly = float(roi["est_monthly_cost"])
    price = float(roi["avg_paid_api_price_per_call"])
    pipeline = float(roi["pipeline_value_usd"])
    revenue = float(today.get("revenue_usd") or 0)
    # savings from running locally vs paid API (today's displaced calls x price),
    # plus the canonical all-time net saved from core.savings.
    savings_today = api_calls * price
    savings_alltime = float(today.get("net_saved_alltime") or 0)
    monthly_savings = savings_today * 30
    value = revenue + pipeline + monthly_savings
    roi_pct = ((value - (inv * 0 + monthly)) / inv * 100) if inv else 0
    payback = value / (monthly + price) if (monthly + price) else 0

    roi_cards = "".join([
        _card("api calls today", f"{api_calls:,}", "core.savings displaced calls"),
        _card("tokens today", f"{tokens:,}", "local router + cost ledger"),
        _card("automation jobs", f"{jobs:,}", "local scheduler"),
        _card("est monthly cost", _money(monthly), "paid API run-rate"),
        _card("savings/mo (local)", _money(monthly_savings),
              f"{api_calls:,} calls/day x {_money(price)}", "good"),
        _card("net saved all-time", _money(savings_alltime), "core.savings", "good"),
        _card("ROI", f"{roi_pct:,.0f}%", "value vs monthly cost", "good" if roi_pct > 0 else "bad"),
    ])
    roi_form = f"""<form method=post action=/roi class=roi-form>
  <label>investment to date $<input name=investment_to_date value="{inv}"></label>
  <label>est monthly cost $<input name=est_monthly_cost value="{monthly}"></label>
  <label>avg paid $/call<input name=avg_paid_api_price_per_call value="{price}"></label>
  <label>pipeline value $<input name=pipeline_value_usd value="{pipeline}"></label>
  <button type=submit>save</button>
  <span class=muted>persists to data/roi_factors.json (hand-editable too)</span>
</form>"""
    roi_note = (f"<p class=note>SAVINGS ASSUMPTION: every call served locally / free-tier / via "
                f"automation would have cost <b>{_money(price)}</b> on a paid API (blended "
                f"Haiku-class). Today's {api_calls:,} calls x {_money(price)} = "
                f"{_money(savings_today)}/day -> {_money(monthly_savings)}/mo avoided. "
                f"ROI = (revenue {_money(revenue)} + pipeline {_money(pipeline)} + monthly savings "
                f"{_money(monthly_savings)}) vs est monthly cost {_money(monthly)}.</p>")

    # D) COMPARE
    cmp_data = compare_periods(rows)
    panes = ""
    for period in ("today", "yesterday", "week"):
        cells = ""
        for m in cmp_data[period]:
            d = m["delta"]
            arrow = "▲" if d > 0 else ("▼" if d < 0 else "·")
            cls = "good" if d > 0 else ("bad" if d < 0 else "muted")
            val = _money(m["cur"]) if "$" in m["label"] else m["cur"]
            dtxt = (_money(abs(d)) if "$" in m["label"] else abs(d)) if d else ""
            cells += (f"<div class=cmp-cell><div class=cmp-lbl>{_esc(m['label'])}</div>"
                      f"<div class=cmp-val>{val}</div>"
                      f"<div class='cmp-d {cls}'>{arrow} {dtxt}</div></div>")
        active = " active" if period == "today" else ""
        panes += f'<div class="cmp-pane{active}" data-p="{period}">{cells}</div>'

    # Named lane, reported separately; never blend it into anonymous totals.
    m = today.get("BrandA", {}) or {}
    m_warn = ""
    if m.get("off_flag_present") and not m.get("flag_honored", True):
        m_warn = ("<p class=note style='color:#e0a050'>&#9888; MAROON_OFF flag is set but the lane "
                  "is still sending &mdash; the kill-switch is not being honored.</p>")
    maroon_html = (
        "<div class=cards>"
        f"<div class=card><div class=lbl>status</div><div class=val>{_esc(m.get('status','—'))}</div>"
        "<div class=sub>named lane · separate from anonymous lanes</div></div>"
        f"<div class=card><div class=lbl>sends today</div><div class=val>{m.get('sends',0)}</div>"
        "<div class=sub>own ledger</div></div>"
        f"<div class=card><div class=lbl>aggie leads</div><div class=val>{m.get('leads',0)}</div>"
        "<div class=sub>own pool</div></div>"
        f"</div>{m_warn}"
        "<p class=note>the named lane is tracked apart from the anonymous lanes so "
        "the lane wall holds even here.</p>")

    return PAGE.format(now=_esc(now), rev_tbl=rev_tbl, dots=dots, BrandA=maroon_html,
                       roi_cards=roi_cards, roi_form=roi_form, roi_note=roi_note,
                       cmp_panes=panes)


def _card(label, value, sub="", tone=""):
    return (f'<div class="card {tone}"><div class=lbl>{_esc(label)}</div>'
            f'<div class=val>{_esc(value)}</div><div class=sub>{_esc(sub)}</div></div>')


PAGE = """<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#0b0e14">
<title>Stack Dashboard (private)</title><style>
:root{{--bg:#0b0e14;--panel:#141925;--line:#222a3a;--ink:#e6e9ef;--muted:#8a93a6;
--good:#3ddc97;--bad:#ff6b6b;--accent:#7aa2ff;
--sat:env(safe-area-inset-top);--sab:env(safe-area-inset-bottom);
--sal:env(safe-area-inset-left);--sar:env(safe-area-inset-right)}}
*{{box-sizing:border-box}}
html,body{{margin:0;height:100%}}
body{{background:var(--bg);color:var(--ink);overflow:hidden;
font:14px/1.5 -apple-system,system-ui,sans-serif;-webkit-text-size-adjust:100%}}

/* ── swipe deck: each pane is one full screen, native CSS scroll-snap ── */
.deck{{display:flex;flex-direction:row;width:100vw;height:100dvh;height:100svh;
overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;
-webkit-overflow-scrolling:touch;scrollbar-width:none;overscroll-behavior-x:contain}}
.deck::-webkit-scrollbar{{display:none}}
.pane{{flex:0 0 100vw;width:100vw;height:100dvh;height:100svh;
scroll-snap-align:start;scroll-snap-stop:always;overflow-y:auto;
-webkit-overflow-scrolling:touch}}
.pane-inner{{min-height:100%;display:flex;flex-direction:column;
padding:calc(18px + var(--sat)) calc(18px + var(--sar))
        calc(64px + var(--sab)) calc(18px + var(--sal))}}
.grow{{flex:1;display:flex;flex-direction:column;justify-content:center;min-height:0}}

.pane-head{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:6px}}
.asof{{color:var(--muted);font-size:12px;white-space:nowrap}}
h1{{font-size:clamp(19px,5.5vw,24px);margin:0}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:1.1px;
color:var(--muted);margin:18px 0 12px;font-weight:700}}

/* pager dots */
.pager{{position:fixed;left:0;right:0;bottom:calc(14px + var(--sab));z-index:20;
display:flex;justify-content:center;gap:12px}}
.pager button{{position:relative;width:11px;height:11px;border-radius:50%;border:0;
background:#33405c;padding:0;cursor:pointer;transition:transform .15s,background .15s}}
.pager button::before{{content:"";position:absolute;inset:-16px}}
.pager button.on{{background:var(--accent);transform:scale(1.4)}}

table{{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);font-size:15px}}
th{{color:var(--muted);font-size:11px;text-transform:uppercase;background:#10141e}}
tr.tot td{{background:#10141e}}
.muted{{color:var(--muted)}}.good{{color:var(--good)}}.bad{{color:var(--bad)}}
.note{{color:var(--muted);font-size:12px;max-width:780px;margin-top:12px}}
.dot-row{{padding:11px 0;font-size:16px}}
.dot{{display:inline-block;width:14px;height:14px;border-radius:50%;
margin-right:10px;vertical-align:middle}}
.dot.ok{{background:var(--good)}}.dot.bad{{background:var(--bad)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.card .lbl{{color:var(--muted);font-size:11px;text-transform:uppercase}}
.card .val{{font-size:clamp(24px,7vw,30px);font-weight:650;margin-top:4px}}
.card .sub{{color:var(--muted);font-size:11px}}
.card.good .val{{color:var(--good)}}.card.bad .val{{color:var(--bad)}}
.roi-form{{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin-top:14px}}
.roi-form label{{display:flex;flex-direction:column;font-size:11px;color:var(--muted)}}
.roi-form input{{background:#10141e;border:1px solid var(--line);color:var(--ink);
border-radius:8px;padding:11px 10px;width:150px;margin-top:4px;font-size:16px;min-height:44px}}
.roi-form button{{background:var(--accent);border:0;color:#06101f;font-weight:700;
border-radius:9px;padding:0 22px;min-height:44px;cursor:pointer;font-size:15px}}
.tabs{{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}}
.tab{{background:var(--panel);border:1px solid var(--line);color:var(--muted);
border-radius:10px;padding:10px 18px;min-height:44px;cursor:pointer;font-weight:600;font-size:14px}}
.tab.active{{color:var(--ink);border-color:var(--accent);background:#18203a}}
.cmp-pane{{display:none;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}}
.cmp-pane.active{{display:grid}}
.cmp-cell{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 15px}}
.cmp-lbl{{color:var(--muted);font-size:11px;text-transform:uppercase}}
.cmp-val{{font-size:clamp(24px,7vw,30px);font-weight:650;margin:4px 0}}.cmp-d{{font-size:13px}}
.stores{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.store{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:16px;min-height:44px;text-decoration:none;display:flex;flex-direction:column;gap:4px}}
.store:active,.store:hover{{border-color:var(--accent);background:#18203a}}
.store b{{color:var(--ink);font-size:17px}}.store span{{color:var(--muted);font-size:14px}}

/* ── DESKTOP / wide: drop paging, stack panes as a normal scrolling page ── */
@media(min-width:820px){{
  body{{overflow:auto}}
  .deck{{display:block;width:auto;height:auto;overflow:visible;scroll-snap-type:none}}
  .pane{{flex:none;width:auto;height:auto;min-height:0;overflow:visible;
  scroll-snap-align:none;border-bottom:1px solid var(--line)}}
  .pane:last-child{{border-bottom:0}}
  .pane-inner{{max-width:1000px;margin:0 auto;min-height:0;
  padding:8px 24px 36px}}
  .grow{{display:block;flex:none}}
  .pager{{display:none}}
  h1{{font-size:20px}}h2{{margin:24px 0 12px}}
  .card .val,.cmp-val{{font-size:22px}}
  th,td{{padding:7px 12px;font-size:13px}}
  .store b{{font-size:14px}}.store span{{font-size:12px}}
}}
</style></head><body>

<div class=deck id=deck>

<section class=pane><div class=pane-inner>
<div class=pane-head><h1>🛰️ Stack Dashboard</h1><div class=asof>private · localhost · {now}</div></div>
<h2>Public Surface</h2>
<div class=grow><div class=stores>
<div class=store><b>Public board</b><span>withheld from this dashboard surface</span></div>
<div class=store><b>Anonymous lanes</b><span>withheld from this dashboard surface</span></div>
</div></div>
</div></section>

<section class=pane><div class=pane-inner>
<h2>A · Revenue — per brand + total, today</h2>
<div class=grow>{rev_tbl}</div>
</div></section>

<section class=pane><div class=pane-inner>
<h2>♦ Named lane</h2>
<div class=grow>{BrandA}</div>
</div></section>

<section class=pane><div class=pane-inner>
<h2>B · Health</h2>
<div class=grow>{dots}</div>
</div></section>

<section class=pane><div class=pane-inner>
<h2>C · ROI calculator</h2>
<div class=grow><div class=cards>{roi_cards}</div>{roi_form}{roi_note}</div>
</div></section>

<section class=pane><div class=pane-inner>
<h2>D · Snapshot compare</h2>
<div class=grow>
<div class=tabs>
<button class="tab active" data-p=today>today</button>
<button class=tab data-p=yesterday>yesterday</button>
<button class=tab data-p=week>this week</button></div>
{cmp_panes}
<p class=note>deltas vs the prior equivalent period (today vs yesterday · yesterday vs day-before · week vs prior 7d).</p>
</div>
</div></section>

</div>

<div class=pager id=pager>
<button data-i=0 class=on></button><button data-i=1></button><button data-i=2></button>
<button data-i=3></button><button data-i=4></button><button data-i=5></button>
</div>

<script>
(function(){{
  var deck=document.getElementById('deck'),pager=document.getElementById('pager');
  var dots=[].slice.call(pager.querySelectorAll('button'));
  function sync(){{
    if(!deck.clientWidth) return;
    var i=Math.round(deck.scrollLeft/deck.clientWidth);
    dots.forEach(function(d,j){{d.classList.toggle('on',j===i);}});
  }}
  deck.addEventListener('scroll',function(){{window.requestAnimationFrame(sync);}},{{passive:true}});
  dots.forEach(function(d,j){{d.onclick=function(){{
    deck.scrollTo({{left:j*deck.clientWidth,behavior:'smooth'}});
  }};}});
  sync();
}})();
document.querySelectorAll('.tab').forEach(function(t){{t.onclick=function(){{
document.querySelectorAll('.tab').forEach(function(x){{x.classList.remove('active')}});
document.querySelectorAll('.cmp-pane').forEach(function(x){{x.classList.remove('active')}});
t.classList.add('active');
document.querySelector('.cmp-pane[data-p="'+t.dataset.p+'"]').classList.add('active');}};}});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, code=200, ctype="text/html"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            return self._send("not found", 404, "text/plain")
        try:
            self._send(render(load_snaps(), load_roi()))
        except Exception as e:
            self._send(f"render error: {_esc(e)}", 500, "text/plain")

    def do_POST(self):
        if self.path != "/roi":
            return self._send("not found", 404, "text/plain")
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n).decode("utf-8") if n else ""
        save_roi({k: v[0] for k, v in urllib.parse.parse_qs(body).items()})
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *a):
        pass


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if "--selfcheck" in argv:
        out = render(load_snaps(), load_roi())
        print(f"rendered ok, {len(out)} bytes, snaps={len(load_snaps())}")
        return 0
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"stack dashboard -> http://{HOST}:{PORT}  (localhost only, ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
