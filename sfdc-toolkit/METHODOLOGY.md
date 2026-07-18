# Deploy methodology — keeping a shared org safe

Rules learned the hard way shipping to production orgs with real users. None of these are org-specific; they apply anywhere a mistake is expensive.

## The pipeline

**sandbox → version control → production.** Never author or hotfix directly in production. Every change is a commit, reviewed in a PR, deployed from the branch. Production is a deploy target, never an editor.

## Before every deploy

1. **Validate-only first.** Run a check-only deploy (`sf project deploy validate`) with `RunLocalTests`. It exercises the full deploy and test suite and saves nothing — failures cost you a re-run, not a broken org.
2. **Tests always run.** Deploy with `RunLocalTests` (or `RunSpecifiedTests` for large orgs). Never `NoTestRun` to production.
3. **Partial-validate risky changes** in a scratch org or sandbox before they touch the pipeline.

## Component gotchas

- **Permission sets deploy as a whole component.** A permission-set deploy *replaces* the entire component — any grant not in your source is removed. Always deploy the full, current definition, never a partial.
- **Prefer additive schema changes.** Adding fields/objects is reversible; renaming/deleting is not. Stage destructive changes separately and deliberately.

## Verify like an engineer

- **Prove the change with the toolchain, not a production query.** Confirm a deploy with `sf project deploy report` / the deploy id and your VCS, not by eyeballing prod data — a record existing is not proof the change is live and correct for an end user.
- **Trace the user-visible path.** "The deploy went green" ≠ "the user can see it." Follow the render/permission path and compare against a working peer before calling it done.

## Stop-and-escalate

- After two failed attempts at the same fix, stop and re-scope — don't keep throwing deploys at it.
- High-risk, irreversible, or broad-blast changes get a human gate. Low-risk, reversible changes can ship autonomously once they pass validate.
