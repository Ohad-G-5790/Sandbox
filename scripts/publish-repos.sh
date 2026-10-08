#!/usr/bin/env bash
# Publishes every program under programs/ as its own public GitHub repository.
#
# Prerequisites (run once):
#   gh auth login          # log the GitHub CLI into your account
#   gh auth setup-git      # let git push with the gh credentials
#
# Usage:
#   scripts/publish-repos.sh            # create + push all 10 repositories
#   DRY_RUN=1 scripts/publish-repos.sh  # show what would happen, touch nothing on GitHub
#   OWNER=someone scripts/publish-repos.sh   # publish under another account or org
set -euo pipefail

cd "$(dirname "$0")/.."

DRY_RUN="${DRY_RUN:-}"
if [[ -z "$DRY_RUN" ]]; then
  gh auth status >/dev/null 2>&1 || { echo "gh is not logged in; run: gh auth login" >&2; exit 1; }
  OWNER="${OWNER:-$(gh api user --jq .login)}"
else
  OWNER="${OWNER:-<owner>}"
fi

for dir in programs/*/; do
  name="$(basename "$dir")"
  desc="$(sed -nE 's/^description = "(.*)"$/\1/p' "$dir/Cargo.toml" | head -n1)"
  url="https://github.com/$OWNER/$name"

  tmp="$(mktemp -d)"
  cp -R "$dir". "$tmp"/
  rm -rf "$tmp/target"
  git -C "$tmp" init -q -b main
  git -C "$tmp" add -A
  git -C "$tmp" commit -q -m "Initial commit: $name"

  if [[ -n "$DRY_RUN" ]]; then
    echo "[dry run] would create $url and push $(git -C "$tmp" ls-files | wc -l | tr -d ' ') files"
  elif gh repo view "$OWNER/$name" >/dev/null 2>&1; then
    echo "$url already exists; pushing to it"
    git -C "$tmp" remote add origin "$url.git"
    git -C "$tmp" push -u origin main
  else
    gh repo create "$OWNER/$name" --public --description "$desc" --source "$tmp" --remote origin --push
  fi

  rm -rf "$tmp"
  echo "Published $url"
done
