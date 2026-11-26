#!/bin/bash
# Read-only checks before a deploy: the expected commit is what the remote branch really has, the server is behind it on the SAME line (fast-forward only), and the tracked files are clean. Changes nothing.
#   ops/deploy/preflight.sh <expected-head-sha>
# Prints OLD, NEW, whether a migration file arrives and whether requirements.txt changes. Exit 0 = safe to continue.
set -euo pipefail
. "$(dirname "$0")/common.sh"
expect="${1:-}"
[[ "$expect" =~ ^[0-9a-f]{7,40}$ ]] || die "give the expected head commit (7 to 40 hex characters) as the first argument"
git_app fetch --quiet "$REMOTE" "$BRANCH" || die "could not fetch $REMOTE $BRANCH"
new="$(git_app rev-parse "$REMOTE/$BRANCH")"
old="$(git_app rev-parse HEAD)"
case "$new" in "$expect"*) ;; *) die "$REMOTE/$BRANCH is at ${new:0:12}, not at the expected ${expect:0:12}. Nothing was changed." ;; esac
git_app merge-base --is-ancestor "$old" "$new" || die "the server's HEAD ${old:0:12} is not an ancestor of ${new:0:12}: not a fast-forward. Nothing was changed."
[ -z "$(git_app status --porcelain --untracked-files=no)" ] || die "tracked files on the server are modified; commit or restore them first"
migration=no; deps=no
[ -n "$(git_app diff --name-only "$old" "$new" -- alembic/versions)" ] && migration=yes
git_app diff --quiet "$old" "$new" -- requirements.txt || deps=yes
say "OLD=$old"
say "NEW=$new"
say "MIGRATION=$migration"
say "DEPS=$deps"
say "commits: $(git_app rev-list --count "$old..$new")"
