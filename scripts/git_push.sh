#!/bin/bash
set -e

MSG_FILE=".local/.commit_message"
DEFAULT_MSG="Mirage Scraper Engine — auto push"

if [ -f "$MSG_FILE" ]; then
  MSG=$(cat "$MSG_FILE")
else
  MSG="$DEFAULT_MSG"
fi

# Remove any stale lock files left by interrupted operations
rm -f .git/config.lock .git/index.lock .git/HEAD.lock .git/MERGE_HEAD.lock 2>/dev/null || true

git config user.email "mirage@scraper.local"
git config user.name "Mirage Bot"

# ── Recovery: always land on main ─────────────────────────────────────────────
CURRENT=$(git --no-optional-locks rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached")
if [ "$CURRENT" != "main" ]; then
  echo "Recovering to main (was on: $CURRENT) ..."
  git checkout -f main
  git branch | grep -E '^\s+_push_' | xargs -r git branch -D 2>/dev/null || true
fi

# ── Build a clean temp index — secrets never enter the tree ───────────────────
# GIT_INDEX_FILE points git at a fresh empty index, so only the files we
# explicitly list below are included. scripts/github_push.py and
# scripts/github_pull.py are never added and can never leak to GitHub.
TMPINDEX=$(mktemp /tmp/git-push-idx-XXXX)
rm -f "$TMPINDEX"   # git creates the file itself; must not pre-exist

GIT_INDEX_FILE="$TMPINDEX" git add \
  app.py server.ts scrapers_run.py scrapers_registry.json \
  core/ scrapers/ src/ \
  do_worker/ .do/ \
  scripts/check_db.py scripts/check_db_full.py scripts/check_products_table.py \
  scripts/db_inspect.py scripts/db_inventory.py \
  scripts/fix_categories.py scripts/fix_coach_csv.py \
  scripts/fix_tags_all.py scripts/git_push.sh scripts/patch_descriptions_tags_sizes.py scripts/post-merge.sh \
  scripts/re_export_csv.py scripts/run_cruise.py \
  package.json package-lock.json tsconfig.json \
  index.html vite.config.ts replit.nix replit.md requirements.txt pyproject.toml \
  start.sh start.ps1 .gitignore .gitattributes README.md \
  2>/dev/null || true

TREE=$(GIT_INDEX_FILE="$TMPINDEX" git write-tree)
rm -f "$TMPINDEX"

echo "Clean tree: $TREE"

# ── Create orphan commit (no parent = no Replit checkpoint history) ────────────
CLEAN_COMMIT=$(git commit-tree "$TREE" -m "$MSG")
echo "Orphan commit: $CLEAN_COMMIT"

# ── Push to GitHub ─────────────────────────────────────────────────────────────
# Use personal access token from environment via git credential store
if [ -n "$GITHUB_PERSONAL_ACCESS_TOKEN" ]; then
  # Setup credential store
  git config --global credential.helper store
  echo "https://x-access-token:${GITHUB_PERSONAL_ACCESS_TOKEN}@github.com" > ~/.git-credentials
  chmod 600 ~/.git-credentials
  
  # Update remote URL
  git remote set-url origin "https://github.com/rudra-saxena2020/webscrapper-pro.git" 2>/dev/null || true
  
  # Suppress interactive prompts
  export GIT_ASKPASS=/bin/true
  export GIT_TERMINAL_PROMPT=0
  
  GIT_LFS_SKIP_PUSH=1 git push origin "${CLEAN_COMMIT}:refs/heads/main" --force
else
  GIT_LFS_SKIP_PUSH=1 git push origin "${CLEAN_COMMIT}:refs/heads/main" --force
fi

# ── Advance local main so next run sees no diff ───────────────────────────────
git add \
  app.py server.ts scrapers_run.py scrapers_registry.json \
  core/ scrapers/ src/ scripts/ \
  package.json package-lock.json tsconfig.json \
  index.html vite.config.ts replit.nix replit.md requirements.txt pyproject.toml \
  start.sh start.ps1 .gitignore .gitattributes README.md \
  2>/dev/null || true

if ! git diff --cached --quiet; then
  git commit -m "$MSG"
fi

echo ""
echo "Done! https://github.com/rudra-saxena2020/webscrapper-pro/tree/main"
