# sfdc-toolkit

Battle-tested Salesforce patterns and a safe deploy discipline — the reusable core an SFDC developer reaches for on every org, with none of any employer's code in it. Generic objects (`Account`, `Contact`) only; drop the patterns into your own package.

## What's here

- **`force-app/.../TriggerHandler.cls`** — a virtual trigger-handler framework: one trigger per object, all logic in a handler, per-context dispatch, a recursion guard, and a runtime bypass API for data loads and test isolation.
- **`force-app/.../AccountTriggerHandler.cls`** — a worked example handler.
- **`force-app/.../TriggerHandlerTest.cls`** — full coverage of the framework (dispatch, bypass, recursion guard).
- **`scripts/safe_deploy.sh`** — a `sandbox → git → prod` deploy harness over the `sf` CLI: validate-only first, run local tests, never deploy straight to prod.
- **`METHODOLOGY.md`** — the operating rules that keep a shared org safe (partial-validate before deploy, permission sets replace the whole component, verify with the CLI not prod queries).

## Why a trigger framework

Salesforce runs one set of triggers per object with no ordering guarantees. Putting logic directly in triggers gives you recursion, untestable branches, and merge conflicts. One thin trigger delegating to a handler fixes all three:

```apex
trigger AccountTrigger on Account (before insert, before update, after insert, after update) {
    new AccountTriggerHandler().run();
}
```

## Requirements

- Salesforce CLI (`sf`)
- API 59.0+
