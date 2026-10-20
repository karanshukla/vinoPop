#!/usr/bin/env bash
# Commit subjects on stdin, one per line, must be conventional: git-cliff
# builds CHANGELOG.md from them (cliff.toml). Used by .githooks/commit-msg and
# CI's commits job.
set -euo pipefail

types="feat|fix|perf|refactor|revert|docs|test|style|ci|build|chore"
pattern="^(${types})(\([a-z0-9._/-]+\))?!?: [^ ]"
bad=0
while IFS= read -r subject; do
    [[ -z "$subject" ]] && continue
    # git's own merge and revert subjects
    [[ "$subject" =~ ^(Merge|Revert\ \") ]] && continue
    if ! [[ "$subject" =~ $pattern ]]; then
        echo "not a conventional commit: $subject" >&2
        bad=1
    fi
done
if [[ $bad -ne 0 ]]; then
    echo "use <type>(<scope>): <summary>, with type one of ${types//|/, }" >&2
fi
exit $bad
