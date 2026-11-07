#!/usr/bin/env bash
# Sync the ops clone and consolidate open Dependabot PRs.
# Serialised with flock: a run waits on CI for minutes, so an overlapping
# invocation (cron tick + manual run) must never touch the same clone.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

LOCK="${HOME}/.hermes/cache/dependabot-consolidate.lock"
exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "dependabot-consolidate: another run is active, skipping"
    exit 0
fi

git fetch --quiet origin --prune || exit 1
git checkout --quiet -B master origin/master || exit 1
git reset --quiet --hard origin/master
git clean --quiet -fd -e run_consolidate.sh
git branch --list 'chore/dependabot-*' | xargs -r -n1 git branch -D >/dev/null 2>&1
exec python3.11 tools/dependabot_consolidate.py --repo-dir "$PWD" "$@"
