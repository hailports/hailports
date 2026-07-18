# Architecture

This repo is a curated slice of a self-running automation stack — the parts that stand on
their own as reference patterns. It is not the whole system; it's the load-bearing ideas,
cleaned up to read without the surrounding operation.

## Design principles

**Fail closed on money and identity, fail open on liveness.** Anything that spends,
sends, or exposes must have positive authorization to proceed (a gate that denies on
uncertainty). Anything that merely keeps the lights on should degrade gracefully rather
than block the world when its own state is unreadable. The social governor is the clearest
example: over-budget → deny; no history yet → allow one.

**Deterministic gate before expensive spend.** Nothing pays for an LLM call (or any costly
action) until a cheap deterministic check proves the work is real. The self-healer does a
free "did a job actually fail?" check before ever reaching for a model; the visibility and
site-health probes derive every claim from a real measurement, never a guess.

**Local-first, paid-last.** LLM work routes on-box (Ollama, $0) first, to a free pool
second, to a paid API only as a last resort — and even then behind a kill switch, a
circuit breaker, and daily/monthly caps. This is the durable fix for the "autonomous loop
quietly drains the budget" failure everyone hits once.

**Single choke points for the dangerous verbs.** One governor in front of every social
action; one gateway in front of every alert (dedup + digest, so a shared dependency going
red can't produce an escalation flood); one publish gate in front of anything public. A
verb with many callers gets exactly one place where the rule lives.

**Config is not code.** Domains to watch, rate budgets, warm-up windows, model choices —
all live as data the operator can raise or lower without touching logic. The behavior is
the asset; the tuning is a dial.

**Silent death is the real enemy.** The failures that hurt an unattended machine don't
throw exceptions — a domain lapses, a login cookie goes stale, a money loop just stops
producing, a run dies between `START` and `DONE`. A liveness watchdog that only restarts
crashed processes never sees any of these, so there's a separate proactive sweep for them.

## What's here

| directory | what it is |
|---|---|
| [`social-governor/`](social-governor/) | shared anti-burst rate-governor in front of every public social action — rolling windows, human jitter, warm-up ramps, per-target cooldowns. Standalone, stdlib-only, self-testing. |
| [`self-healing/`](self-healing/) | two resilience patterns: an event-driven cost-guarded repair loop (`jit_healer`) and a silent-death watchdog (`eternal_guardian`). |
| [`openclaw/`](openclaw/) | an adapter that hands long-running agent behavior to an external autonomy runtime while keeping custody of the brittle assets (sessions, policy, approvals) local. Config + manifests, secrets excluded. |
| [`backoffice/`](backoffice/) | a self-running double-entry general ledger + G&A process engine. |
| [`sfdc-toolkit/`](sfdc-toolkit/) | a generic Salesforce trigger-handler framework, a safe sandbox→prod deploy harness, and the methodology behind them. |
| [`infra/`](infra/) | headless-device infrastructure — a dynamic display autoresizer and a tailnet-only reverse proxy. |

## How the pieces relate

A caller (a poster, an engager, a builder loop) never acts directly. It asks the
**governor** whether an action is within budget, does the work, records it. The **alert
gateway** is the only path to a human, so noise is deduped and batched. When a job dies,
the **self-healer** fires reactively on the error-log write; what the healer can't see —
the silent deaths — the **guardian** sweeps for on a timer. Long-horizon autonomy
(objectives, retries, background workers) can be delegated to an external runtime through
the **openclaw** adapter, which still routes every brittle or irreversible action back
through the same local gates.

Everything published here shares one bias: **be aggressive by default, but never let an
unverified assumption drive an irreversible or expensive action.**
