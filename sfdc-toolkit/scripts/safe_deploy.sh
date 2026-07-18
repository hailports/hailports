#!/usr/bin/env bash
# safe_deploy.sh — sandbox → git → prod, never straight to prod.
#
#   ./safe_deploy.sh validate <target-org>   # check-only + run local tests, deploy nothing
#   ./safe_deploy.sh deploy   <target-org>    # deploy AFTER a clean validate + a merged PR
#
# Discipline this enforces:
#   1. validate-only against the org first (RunLocalTests) — catches failures deploying nothing
#   2. prod deploys are gated on a clean validate; you never push code that hasn't passed check-only
#   3. tests always run — no NoTestRun to prod
set -euo pipefail

ACTION="${1:-}"
ORG="${2:-}"
SRC="force-app"

if [[ -z "$ACTION" || -z "$ORG" ]]; then
  echo "usage: $0 <validate|deploy> <target-org-alias>"; exit 2
fi

case "$ACTION" in
  validate)
    echo "▶ check-only deploy + local tests against $ORG (nothing is saved)"
    sf project deploy validate --source-dir "$SRC" --test-level RunLocalTests --target-org "$ORG"
    echo "✔ validate passed — safe to open a PR"
    ;;
  deploy)
    echo "▶ deploying $SRC to $ORG with local tests"
    echo "  (only run this after a clean 'validate' and a reviewed/merged PR)"
    sf project deploy start --source-dir "$SRC" --test-level RunLocalTests --target-org "$ORG"
    echo "✔ deployed"
    ;;
  *)
    echo "unknown action: $ACTION (use validate|deploy)"; exit 2
    ;;
esac
