---
name: myos-control
description: Use MyOS as the integration custody and admin policy layer from OpenClaw.
---

# MyOS Control

Use this skill when an OpenClaw agent needs access to the operator's local integrations, model policy, browser sessions, or IT/NO-IT mode.

Principles:

- MyOS owns credentials, cookies, browser session custody, local SQLite access, audit logs, and write policy.
- OpenClaw owns autonomous objectives, long-running loops, retries, channels, and worker orchestration.
- Do not reauthenticate accounts unless the admin explicitly asks.
- Do not copy, delete, move, or regenerate retained browser sessions.
- Prefer reads, drafts, and queued actions unless MyOS Admin policy permits execution.

Useful commands:

```bash
cd ~/claude-stack
python3 scripts/openclaw_myos.py health
python3 scripts/openclaw_myos.py policy
python3 scripts/openclaw_myos.py sessions
python3 scripts/openclaw_myos.py tools search outlook
python3 scripts/openclaw_myos.py invoke outlook_create_draft --input @draft.json
```

Use `--execute --approval --idempotency-key <stable-key>` only when the current policy and explicit human approval allow a write.
