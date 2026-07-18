# self-healing

Two patterns for keeping an unattended machine alive without a human babysitting it —
one reactive (fix what just broke), one proactive (catch what dies *silently*).

## `jit_healer.py` — event-driven, cost-guarded repair

Fired by a launchd `WatchPaths` the moment any job writes to its error log. The key idea
is **deterministic-gate before spend**:

```
log write  →  cheap deterministic check "did a job actually fail?"
           →  NO  → return (no LLM call, $0)
           →  YES → local-first LLM heal → verify → record → bounded escalation
```

- **No failure, no call.** True just-in-time: a failure triggers healing; nothing is
  scheduled to poll. Most log writes are noise and cost nothing.
- **Local-first spend ordering.** On-box Ollama ($0, works even when the paid-LLM breaker
  is tripped) is always tried before any paid call. Paid is a last resort, behind a kill
  switch + circuit breaker + daily/monthly caps. This ordering is the fix for the classic
  "self-healer quietly bleeds the API budget" backfire.
- **Bounded escalation.** Per-job cooldown + a consecutive-fail cap; after N failed heals
  a job is declared chronic and escalated **once** (deduped/digested), not looped on.
- **Knows what it can't fix.** A Python `SyntaxError`/`IndentationError` can never be
  fixed by restarting the job, so it's detected up front and escalated without burning the
  heal budget on it.

## `eternal_guardian.py` — silent-death watchdog

A periodic sweep (e.g. every 6h) for the failures that *don't* throw an error and so can't
be caught by any liveness/restart healer:

1. **Domains expiring** — RDAP (with a whois fallback) expiry for every owned domain,
   tiered alerts well ahead of lapse (45/30/14/7/3/1 days). A lapsed domain silently 404s
   every storefront on it.
2. **Sessions rotting** — cookie-file freshness for the retained browser logins that keep
   a posting/revenue lane alive. Stale = the lane has quietly stopped; attempts a known
   self-heal, then alerts.
3. **Earning gone quiet** — if the money loop touches none of its activity signals for
   >48h, something upstream broke *without erroring*. Alert.
4. **Autoflow hung** — an autonomous loop that logged `START` but no `DONE` for >2h died
   mid-run and won't restart itself; kickstart its job.

Recoveries clear the symptom (a healthy re-check emits a "healed" event), and state is
keyed so the same tier never alerts twice.

```bash
python3 eternal_guardian.py            # one sweep
python3 eternal_guardian.py --report   # show status, send nothing
```

The watched domains / sessions / earning-signals are **config, not code** — set
`GUARDIAN_DOMAINS` (comma-separated) or edit the example lists at the top of the file.

## Shared-stack interfaces

These modules were lifted from a running stack and published for the *pattern*.
`jit_healer.py` calls four shared interfaces you'd swap for your own equivalents — the
escalation / cooldown / verify control flow is the point, not these specific backends:

| interface | role |
|---|---|
| `forage(tag, goal, backend, model)` | local-first LLM router; returns the heal result text, or an `ERR:`/`SKIP` sentinel |
| `verify_heal(job, settle_seconds)` | post-heal liveness re-check — did the job actually come back? |
| `record_heal(...)` | append-only heal ledger for auditability |
| `alert_gateway.route(severity, ...)` | dedup/digest notification choke point (imminent → page, routine → silent digest) |

`eternal_guardian.py` imports its notification gateway lazily inside a `try/except`, so it
degrades gracefully to silent if no gateway is wired.
