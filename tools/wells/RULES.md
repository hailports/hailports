# RULES.md — Operating Rules (universal)

These rules exist because each one has cost real money or real time. They apply
to every agent, every model, every session. They are injected into every
prompt, enforced at the tool boundary where machine-checkable
(`.wells/rules.yaml`), audited by the reviewer, and re-surfaced at the moment
they become relevant. "I forgot" is not an available failure mode.

---

## R1 — Paid resources are liabilities until proven closed

Anything that bills by the hour (cloud GPU, VM, pod, cluster) is a **liability**
from the moment it starts. A task is NOT complete while a liability is open.

- Before starting a paid resource, state the termination plan.
- After the work finishes — success OR failure — terminate the resource and
  **verify termination with a fresh status check**, not memory.
- If a job crashes, the instance does not get to idle: fix-and-relaunch or
  terminate. Never leave a paid resource running "to look at later."
- The harness tracks open liabilities and will not let a run close silently
  with one open.

## R2 — Budget before launch, hard-stop on threshold

- Set an explicit budget before any paid job. No budget given → assume $10 max.
- Compute cost from **measured** throughput (see R6), never from specs.
- Stop at 80% of budget regardless of job status. Log spend periodically.

## R3 — Calculate resource requirements before provisioning

Before provisioning disk/memory/storage for any long job: write out the actual
arithmetic (outputs × size × retention + inputs + overhead + safety buffer)
and provision ≥ 1.5× the result. **Show the calculation — if you didn't show
it, you skipped it.** Clean up intermediate artifacts as you go (keep N latest,
delete the rest), and put that cleanup in the retry path too — a disk-full
crash that retries onto the same full disk fails every retry.

## R4 — Verify before claiming. Evidence or it didn't happen

Never say "X is running", "Y is set up", or "Z is handled" without a check run
in the last two minutes whose specific output you can quote (PID, file size,
log line, API response). "Should be fine", "will handle it", and "you're
covered" are banned phrases unless followed by the evidence.
Distinguish explicitly between "I intend to do X" and "I have confirmed X."

## R5 — Smoke-test before the expensive run

Before any long or paid run: execute a small end-to-end slice first (a few
iterations, a tiny file, a cheap instance) and confirm the full path works —
including saving outputs and uploading artifacts. Discovering a crash after
paying for startup is avoidable. Test the *full* launch sequence on the cheap
tier, not on the expensive target.

## R6 — Estimates come from measurements, not extrapolation

Never quote a time or cost estimate without a measured rate from a smoke test
or a directly comparable prior run — quote the source line. Changing any major
knob (model/data size, batch, hardware, flags) can shift throughput 2–5×.
If there's no measurement yet, say "I need a smoke test first" instead of
guessing.

## R7 — Verify credentials before depending on them

Before launching anything long-running that needs external auth (API tokens,
cloud creds, registry access): make one live authenticated call and confirm it
succeeds. Cached tokens expire. Failed auth before a long run = stop and
notify, never launch-and-hope.

## R8 — Monitor processes, not log strings

A monitor for an unattended job must check **process liveness** (pid/pgrep)
and, where a health endpoint exists, **read every field of it** and assign
each field an action. Grepping logs for "FAILED" misses tracebacks, OOM kills,
and silent deaths. Monitors themselves need supervision: after any restart or
recovery, restart the monitor too and confirm its PID — a monitor that died
with the job it watched protects nothing.

## R9 — A running process is not a healthy process

Liveness is necessary, not sufficient. Watch the health signals that indicate
the work is actually progressing (throughput, loss/error rates, sync
freshness) and define stall/divergence thresholds with actions before the run
starts, not after.

## R10 — Verify the measuring instrument before trusting the measurement

Before trusting any gate, metric, or eval score, ask: *what is the cheapest
garbage output that would still pass this check?* If the answer is "plenty,"
fix the check first. Spot-check the raw outputs behind a score by eye. A
passing metric from a broken check is worse than no metric — it certifies
failure as success.

## R11 — Two consecutive failures = stop and escalate

If the same operation (or close variants) fails twice in a row, STOP. Do not
keep patching in a loop — two failures means the root cause is unknown, and
unknown root causes burn money while you guess. Report what failed, what you
tried, and what you need. (The harness also enforces this at 3 strikes.)

## R12 — Copy only what the job needs

When transferring code/data to a remote machine: copy the specific files
required, never the whole project tree. Exclude archives, logs, caches, build
artifacts, and anything gitignored. Check destination free space before AND
after. Pre-stage large datasets to fast shared storage (object store, hub)
before renting compute — never upload big data from a slow link on the meter.

## R13 — Automation must be non-interactive and fail loud

Commands in scripts and automated flows must never block on interactive
prompts (use `--yes` / `-y` / `--confirm:false` variants). Cleanup /
self-destruct logic runs **only on verified success** (exit 0 + outputs
confirmed persisted) — on failure, preserve the evidence and notify instead.

## R14 — Outputs must exist somewhere durable before they count

An artifact only "exists" when it is verified (present, non-zero size,
loadable) in at least one location that survives the machine dying. For long
jobs, persist intermediates periodically — not just at the end.

## R15 — Deadlines and timeouts get slack, not precision

Hard kill-deadlines on supervised jobs must be ≥ 2× the current best ETA, and
re-checked as the ETA evolves — extend early, not at the buzzer. Early-run
rates underestimate steady-state. An extra hour of runtime is cheap; a job
killed at 95% costs the entire run.

## R16 — Verify the execution environment from the program's own output

Before declaring a long job "running", confirm from the job's own startup
output that it is using the intended environment — the right device/GPU, the
right interpreter, the right versions. Launching on the wrong device can
silently cost 3–4× per run, forever.

## R17 — Plan data composition before building datasets

Before generating or curating any dataset: state the intended composition
(formats, categories, distributions), build, then **count and verify** the
actual distribution matches the plan (±10%). A dataset skewed to one output
shape teaches exactly that skew.

## R18 — Status updates on a schedule when unattended

When operating autonomously on anything that costs money: report status at
regular intervals, never go silent past an hour, and alert immediately on
failure, threshold breach, or anything you cannot confidently recover.
If you can't recover it confidently — terminate the paid resources first,
then report.

---

*Machine-enforceable versions of these rules live in `.wells/rules.yaml`
(blocks, confirmations, and liability tracking at the tool boundary). Add new
rules there when a new failure teaches one; keep the story here.*
