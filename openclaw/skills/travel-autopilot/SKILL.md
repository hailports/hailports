---
name: myos-travel-autopilot
description: Plan and prepare travel autonomously through MyOS, with final purchase governed by admin policy.
---

# MyOS Travel Autopilot

Use this skill when the user asks an agent to plan or book travel.

Default autonomy:

- Search flights and hotels.
- Compare route/date/price tradeoffs.
- Check calendar conflicts.
- Prepare checkout or itinerary drafts.
- Create draft calendar holds when policy allows.
- Stop before final paid purchase unless MyOS Admin policy explicitly allows it.

Primary commands:

```bash
cd ~/claude-stack
python3 scripts/openclaw_myos.py travel-search --origin OMA --destination LGA --depart-date 2026-06-10 --return-date 2026-06-13
python3 scripts/openclaw_myos.py tools search travel
python3 scripts/openclaw_myos.py tools search calendar
python3 scripts/openclaw_myos.py tools search browser
```

NO-IT mode can use retained browser sessions and desktop automation, but the agent must not reauthenticate, export cookies, or delete session data.
