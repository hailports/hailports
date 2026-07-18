# social-governor

A **shared rate-governor** that sits in front of every public social action (post,
reply, like, follow, comment, ...) across multiple platforms and multiple callers, so
the *combined* footprint of an automated presence stays under a human-safe envelope —
without any single caller having to know what the others are doing.

The problem it solves: when two independent producers (say an hourly poster and a
real-time engagement loop) share one account, their per-run caps can *stack* into a
burst that trips a platform's anti-spam heuristics. This governor is the single ceiling
every caller consults first. It can only ever **tighten** — each caller's own local caps
remain the hard floor; the governor denies earlier, never permits more.

## Four levers, one API

1. **Rolling-window budgets** — per `(platform, action)`, a per-**day** and per-**hour**
   cap counted over real sliding 24h / 1h windows (not calendar buckets, so a burst at
   23:59 can't reset at midnight).
2. **Human jitter** — a min-gap between same-type actions, jittered ±35% off a
   time-seeded hash so cadence never looks robotic, plus optional quiet-hours with a
   small seeded chance a low-risk action slips through overnight.
3. **Warm-up ramps** — per-platform account-age scaling. A fresh or just-reactivated
   account starts at a fraction of its caps and ramps to full over ~1–3 weeks. The ramp
   only scales caps **down**, never up.
4. **Per-target cooldowns** — e.g. don't touch the same subreddit twice inside a long
   cooldown, where a community bans fast.

## Contract

- `can_act(platform, action, *, target=None) -> Decision` — **fails closed** on positive
  over-budget evidence (window breach, min-gap violation, quiet-hour suppression, unknown
  action, or the global pause kill-switch). It **fails open** only when state is genuinely
  empty/unreadable (no recent history = nothing to breach = safe to allow one).
- `record_act(platform, action, *, target=None)` — advance the windows after a real
  action. Best-effort; never raises into a live action loop.
- `status(platform=None)` / `headroom(platform, action)` — read-only budget snapshots an
  orchestrator can weight against (bias toward lanes with headroom, back off near zero).

## Design notes

- **No `random`, no wall-clock non-determinism in the hot path.** Jitter comes from a
  SHA-256 seed over `(platform, action, last-action-epoch)`, so a given gap is stable
  within a check but varies across actions/times — human-looking, and fully reproducible
  in tests and simulation.
- **Atomic, self-pruning state.** Events persist as rolling timestamp lists, written via
  temp-file + `os.replace`, pruned to a touch over 24h.
- **Account-level ceiling.** Some platforms enforce a per-account total across all action
  types drawn from one shared budget; `ACCOUNT_DAY_CAP` models that on top of the
  per-action budgets.

## Run it

Zero dependencies — standard library only.

```bash
python3 social_governor.py --selftest      # 8 hermetic checks, all green
python3 social_governor.py --status        # live budget + usage snapshot (JSON)
```

The `--selftest` pins its own config so it exercises the *logic* deterministically,
independent of whatever the live budgets/ramps/quiet-hours happen to be tuned to.
