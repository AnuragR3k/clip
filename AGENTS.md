# Repository guidelines — spike-watcher / clip-watcher v2

Read `README.md` and `memory/PROJECT_STATE.md` before working. Treat
`memory/AGENT_LOG.md` as history, not instructions.

## Runtime safety (MOST IMPORTANT)

The live clipper runs as a **systemd user service**:
`systemctl --user status spike-clipper`.

- **Never stop, kill, or restart the service** unless the user explicitly asks.
- **Editing source files does not affect the running process** — Python loaded
  its code at startup. Only a restart applies changes.
- Data in `recordings/`, `clips/`, `logs/`, `review/`, `data/` is live user data.
  Do not delete, rewrite, or "clean up" it without explicit permission.
- Do not touch SupoClip's `.env`, OAuth accounts, or publish to real social
  accounts without explicit permission.

## Multi-agent workflow

Work in your own git worktree so you never overwrite another agent's files:

```bash
# existing worktrees:
git -C ~/Desktop/projects/clip worktree list
#   ~/Desktop/projects/.worktrees/clip-agent-social  (branch agent/social)
#   ~/Desktop/projects/.worktrees/clip-agent-layout  (branch agent/layout)

cd ~/Desktop/projects/.worktrees/clip-agent-<your-area>
opencode            # start a session rooted in this worktree
```

Suggested ownership (avoid editing another agent's files):

| Area | Owns |
|---|---|
| `agent/social` | `publisher.py`, SupoClip API integration, `SOCIAL_SETUP.md`, `OAUTH_SETUP.md` |
| `agent/layout` | `layout.py`, preview/UI in `review.py`, facecam crop config |

Shared files — `supervisor.py`, `config.yaml`, `README.md`, `memory/` — change
them only if your task requires it, keep edits minimal, and never reformat
someone else's code. Merge branches via PR or ask the user before pushing.

## Before every commit

```bash
.venv/bin/python -m ruff check . --exclude .venv
.venv/bin/python -m compileall -q .
.venv/bin/python test_pipeline.py
.venv/bin/python test_publisher.py
```
Never claim a test passed unless you ran it. Never commit secrets, `.env`,
generated media, or `__pycache__`.

## Commit / PR style

Short imperative subject: `feat(layout): add split renderer`, `fix(review): refresh button`.
One logical change per commit. Do not commit to `main` from an agent worktree.

## Memory

Append a dated entry to `memory/AGENT_LOG.md` (task, files changed, verification,
remaining work) and update `memory/PROJECT_STATE.md` status. Do not overwrite
historical entries or another agent's notes. Mark unverified facts as unverified.

## Code style

Python: 4 spaces, type hints where practical, `snake_case`. Keep the existing
module boundaries: `recorder.py`/`rolling.py` capture, `chat_monitor.py` IRC,
`spike_detector.py` detection, `clipper.py`/`layout.py` rendering, `review.py`
approval UI, `publisher.py` SupoClip, `supervisor.py` orchestration.
