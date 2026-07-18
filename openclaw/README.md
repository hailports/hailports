# MyOS OpenClaw Adapter

This package makes OpenClaw an autonomy runtime over the existing MyOS stack.

OpenClaw should own long-running agent behavior: objectives, retries, workers, channels, and background loops. MyOS keeps custody of the brittle assets that must not be broken: Outlook/Zoom/Salesforce integrations, retained browser sessions, cookies, local SQLite access, model policy, write approvals, and audit logs.

## Boundary

```text
OpenClaw agent runtime
  -> MyOS plugin / scripts/openclaw_myos.py
    -> MyOS Admin http://127.0.0.1:8310
    -> Integration Gateway http://127.0.0.1:8330
      -> existing Outlook / Zoom / Salesforce / browser / local tools
```

Do not reauthenticate, move, copy, regenerate, or delete browser session state for TikTok, YouTube, LinkedIn, or other retained profiles. Session custody remains in MyOS.

## Install Shape

When OpenClaw is installed, this directory is the plugin package:

```bash
openclaw plugins install /path/to/claude-stack/openclaw/myos
```

Until OpenClaw is installed, the JSON bridge is executable directly:

```bash
cd ~/claude-stack
python3 scripts/openclaw_myos.py health
python3 scripts/openclaw_myos.py identity
python3 scripts/openclaw_myos.py policy
python3 scripts/openclaw_myos.py tools search outlook
python3 scripts/openclaw_myos.py sales-status --format detailed
```

The plugin reads `CHATGPT_ACTIONS_API_KEY` from the OpenClaw process environment
or from `${CLAUDE_STACK_DIR}/.env` and uses it
only as a local bearer token for the MyOS Integration Gateway.

## Autonomy Defaults

The admin policy is intentionally configurable. The default is:

- Unattended: read, research, draft, score, prepare, queue, inventory, summarize.
- Gated: purchases, contracts, payment commitments, destructive writes, and unapproved outbound sends.
- Forbidden as daily model: 30B-class local workers.
- Daily resident worker: `qwen2.5:7b`.
- Heavier local worker: `qwen3:14b` only on demand behind a killable queue.
- OpenClaw runtime actor: `openclaw`.
- Command owner / approval actor: `operator`.
- OpenClaw can queue, list, and dry-run approval-ledger actions. It can execute only actions already approved or policy-authorized by MyOS. It cannot approve or reject its own actions.

This is not hard-coded babysitting. It is a policy surface so the admin app can raise or lower autonomy later without rebuilding the agents.
