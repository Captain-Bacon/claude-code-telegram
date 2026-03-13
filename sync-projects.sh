#!/bin/bash
# Regenerate projects.yaml from the N most recently active projects
# Usage: ./sync-projects.sh [count]  (default: 10)
#
# YAML schema expected by the bot:
#   slug: lowercase-kebab-case identifier (unique)
#   name: human-readable display name (unique)
#   path: relative path from APPROVED_DIRECTORY (unique, must exist)

set -euo pipefail

GITHUB_DIR="/Users/jonathanluker/GitHub"
CONFIG_FILE="$(dirname "$0")/config/projects.yaml"
COUNT="${1:-10}"

echo "projects:" > "$CONFIG_FILE"

# Always-included workspaces (not discovered by the auto-scan)
cat >> "$CONFIG_FILE" <<'PINNED'

  - slug: telegram
    name: "Telegram"
    path: "claude_strategic_workspaces/telegram"
PINNED

# Find projects with .claude dirs, sorted by most recent file modification
for dir in "$GITHUB_DIR"/*/.claude; do
    [ -d "$dir" ] || continue
    project_dir=$(dirname "$dir")
    newest=$(find "$dir" -type f -exec stat -f '%m' {} + 2>/dev/null | sort -rn | head -1)
    [ -z "$newest" ] && newest=0
    raw_name=$(basename "$project_dir")
    echo "$newest|$raw_name|$project_dir"
done | sort -rn | head -"$COUNT" | while IFS='|' read -r _ts raw_name project_dir; do
    # slug: lowercase, spaces/underscores to hyphens, strip special chars
    slug=$(echo "$raw_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '--' | sed 's/[^a-z0-9-]//g' | sed 's/--*/-/g')
    # path: relative to APPROVED_DIRECTORY
    rel_path=$(basename "$project_dir")

    cat >> "$CONFIG_FILE" <<ENTRY

  - slug: ${slug}
    name: "${raw_name}"
    path: "${rel_path}"
ENTRY
done

echo "Updated $CONFIG_FILE with top $COUNT projects"
