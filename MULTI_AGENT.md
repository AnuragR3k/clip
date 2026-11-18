# Running several agents on this project

Goal: multiple agents edit code in parallel while the live clipper keeps running.

## Why this is safe

- The running process is a **systemd user service**; it does not reload files.
- Each agent works in a **git worktree** with its own branch, so no agent can
  overwrite another agent's working files.
- Nothing is deployed or restarted until a human merges a branch and runs
  `systemctl --user restart spike-clipper`.

## Worktrees already created

| Directory | Branch | Suggested area |
|---|---|---|
| `~/Desktop/projects/clip` | `main` | integration / user-facing changes |
| `~/Desktop/projects/.worktrees/clip-agent-social` | `agent/social` | publisher, SupoClip API, OAuth docs |
| `~/Desktop/projects/.worktrees/clip-agent-layout` | `agent/layout` | layout.py, review UI, facecam crops |

List them: `git -C ~/Desktop/projects/clip worktree list`

Create another:
```bash
git -C ~/Desktop/projects/clip worktree add \
  -b agent/<name> ~/Desktop/projects/.worktrees/clip-agent-<name> main
```

## Starting an agent in a worktree

```bash
cd ~/Desktop/projects/.worktrees/clip-agent-social
opencode
```

Or, in an OpenCode session, use the harness worktree tools
(`worktree_create`, then `session_move`) to relocate the session before work
begins. Note the harness tool only sees worktrees of the session's own repo
root; plain `git worktree add` also works.

Give each agent the brief plus `AGENTS.md`, which it will read automatically.

## Merge flow

```bash
# in the main worktree
git checkout main
git merge --no-ff agent/social      # or open a PR on GitHub
.venv/bin/python -m ruff check . --exclude .venv
.venv/bin/python test_pipeline.py
.venv/bin/python test_publisher.py
systemctl --user restart spike-clipper   # only now does runtime change
```

If two branches touch the same shared file, resolve the conflict in `main`
before restarting. Saved clips and the review queue survive restarts.

## Rules for agents

See `AGENTS.md`. In short: do not stop the service, do not delete generated
data, run the four checks before committing, write memory notes, and never push
to `main` from an agent branch.
