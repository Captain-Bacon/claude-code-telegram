# Beads Workflow Context

> **Context Recovery**: Run `bd prime` after compaction, clear, or new session
> Hooks auto-call this in Claude Code when .beads/ detected

## Task Lifecycle Scripts

This project uses wrapper scripts for beads + git ceremony. Use these instead of individual bd/git commands for task tracking and committing.

### Claiming work
```bash
task-claim <id>
```
Returns: task description, repo health warnings (dirty files, stale in_progress tasks). Marks in_progress silently.

### Completing work
```bash
task-done <id> [<id2> ...] --reason="..." --files "f1" --files "f2" --msg "commit message"
```
Closes bead(s), checks for unexpected changes, stages files + .beads/issues.jsonl, commits. Aborts without closing beads if unexpected changes found.

- **Quick tasks**: skip claim, just run task-done when finished
- **Multiple beads**: pass multiple IDs before the flags
- **Non-task commits** (no bead involved): use git directly

### If scripts break
If `task-claim` or `task-done` error or don't behave correctly, create a bead for it and fall back to manual bd/git commands for that task. Do not silently work around script issues.

## Session Close

Work is not done until pushed. After `task-done`, run:
```bash
bd sync
git push
```

## Core Rules
- **Default**: Use beads for ALL task tracking
- **Prohibited**: Do NOT use TodoWrite, TaskCreate, or markdown files for task tracking
- **Workflow**: Create beads issue BEFORE writing code
- Git workflow: hooks auto-sync, run `bd sync` at session end
- Session management: check `bd ready` for available work

## Direct bd Commands

These are still used directly — the scripts only wrap claim/done ceremony.

### Finding Work
- `bd ready` — show issues ready to work (no blockers)
- `bd list --status=open` — all open issues
- `bd list --status=in_progress` — active work
- `bd show <id>` — detailed issue view with dependencies

### Creating & Updating
- `bd create --title="..." --type=task|bug|feature --priority=2`
  - Priority: 0-4 or P0-P4 (0=critical, 2=medium, 4=backlog)
- `bd update <id> --title/--description/--notes/--design`
- `bd close <id> --reason="explanation"` — rare, prefer task-done
- **WARNING**: Do NOT use `bd edit` — opens $EDITOR, blocks agents

### Dependencies & Blocking
- `bd dep add <issue> <depends-on>` — add dependency
- `bd blocked` — show all blocked issues

### Sync & Health
- `bd sync` — sync with git remote
- `bd stats` — project statistics
- `bd doctor` — check for issues
