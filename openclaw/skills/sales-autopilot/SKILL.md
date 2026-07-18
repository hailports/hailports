---
name: myos-sales-autopilot
description: Run autonomous revenue loops through MyOS tools while respecting sender identity, compliance, and write policy.
---

# MyOS Sales Autopilot

Use this skill when an OpenClaw agent is assigned a revenue objective such as lead research, meeting generation, follow-up, or pipeline cleanup.

Autonomous loop:

1. Pull current pipeline and lead queues.
2. Score accounts and contacts.
3. Research business context.
4. Draft personalized outreach or follow-up.
5. Check sender identity, suppression, masked emails, and compliance.
6. Queue or send only according to MyOS Admin policy.
7. Watch replies and classify intent.
8. Prepare meetings, Zoom links, CRM updates, and next tasks.
9. Repeat against the next best target.

Primary commands:

```bash
cd ~/claude-stack
python3 scripts/openclaw_myos.py sales-status --format detailed
python3 scripts/openclaw_myos.py tools search revenue
python3 scripts/openclaw_myos.py tools search salesforce
python3 scripts/openclaw_myos.py tools search outlook
python3 scripts/openclaw_myos.py tools search hands_browser
```

Never send to masked addresses like `*****@domain.com`. Treat masked analytics addresses as non-sendable evidence, not recipients.

Purchases, contracts, unapproved outbound sends, destructive CRM writes, and commitments on behalf of the operator require policy permission and a stable idempotency key.
