#!/bin/zsh
# post-commit hook → logs MEANINGFUL stack commits to the master changelog.
# Installed as ~/claude-stack/.git/hooks/post-commit (kept here so it's
# version-controlled + re-installable). Skips the constant auto-keeper noise.
REPO="$HOME/claude-stack"
subj=$(git -C "$REPO" log -1 --pretty=%s 2>/dev/null)
short=$(git -C "$REPO" log -1 --pretty=%h 2>/dev/null)
[ -z "$short" ] && exit 0
case "$subj" in
  auto\(*|"Sync mini"*|"Sync Mini"*|stack-engineer:*|"Update n8n-claw"*) exit 0 ;;
esac
nfiles=$(git -C "$REPO" show --stat --format= -1 2>/dev/null | grep -c '|')
python3 "$REPO/tools/system_changelog.py" --note "GIT ${short}: ${subj} (${nfiles} file(s))" >/dev/null 2>&1 || true
# Keep the brain's self-map (STACK_MAP, in memorySearch) fresh on every meaningful
# commit — background + non-blocking so the commit never waits on the AST scan.
nohup python3 "$REPO/tools/stack_map_digest.py" >/dev/null 2>&1 &
exit 0
