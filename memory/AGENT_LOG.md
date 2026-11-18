# Agent Log

## 2026-11-16 — Build clip-watcher v1

- **Agent/role:** OpenCode coding agent.
- **Task:** Build the minimal `clip-watcher` Python project from scratch:
  Streamlink recording, anonymous Twitch IRC logging, spike detection, ffmpeg
  cutting/vertical formatting, orchestration, and `--test-mode`.
- **Files created:** `config.yaml`, `recorder.py`, `chat_monitor.py`,
  `spike_detector.py`, `clipper.py`, `main.py`, `README.md`,
  `requirements.txt`, `.gitignore`.
- **Environment:** Python 3.14 venv at `.venv`; streamlink 8.6.3; PyYAML 6.0.3.
  ffmpeg 9.0.2 static binary linked at `~/.local/bin/ffmpeg` from a temporary
  imageio-ffmpeg install. No git repository in this directory.
- **Verification:**
  - `python main.py --test-mode` PASS; generated
    `clips/vertical_20261116_193620.mp4` (1080x1920, 45.03s, H.264+AAC) from a
    synthetic 180s recording and fake chat log with a 50-message spike.
  - Visual frame inspection confirmed blurred background + centered source.
  - Real-time tail watcher synthetic test PASS.
  - Anonymous IRC TLS connection succeeded; parser/RECONNECT tests PASS.
  - `ruff check` and `compileall` PASS.
- **Commit/PR:** None (not a git repository).
- **Remaining work:** Real live-channel soak test; long-running reliability and
  disk-retention strategy; durable system ffmpeg install.

## 2026-11-16 — Full report / usage documentation

- **Agent/role:** OpenCode coding agent.
- **Task:** Provide the user with a full project report and operating guide.
- **Code changes:** None.
- **Verification:** Re-checked the newer test artifacts
  `clips/vertical_20261116_195140.mp4` (1080x1920, 45.02s, H.264+AAC) and
  `logs/test_chat.log` (detected 50-message spike at 19:51:40).
- **Remaining work:** Same as above; real live-channel test still pending.

## 2026-11-16 — Live soak on shanks_ttv

- **Agent/role:** Cursor coding agent.
- **Task:** Run the pending live-channel soak. Channel was already
  `shanks_ttv` in `config.yaml`.
- **Code changes:** None. Updated this log and `PROJECT_STATE.md`.
- **Verification:** `python main.py --verbose` for ~21 minutes (venv on PATH).
  IRC connected, 994 chat lines (~49/min), recording
  `recordings/shanks_ttv_20261116_200818.ts` (1.2 GB). Three vertical clips
  decoded clean at 1080x1920 H.264+AAC, ~45s. A fourth spike was cooldown-
  suppressed. Shutdown on SIGINT was clean (exit 0).
- **Remaining work:** Manual review of the three clips; retention and durable
  ffmpeg before 24/7; multi-hour soak.

## 2026-11-18 — Study: combining live spike clips with social publishing

- **Agent/role:** OpenCode (Hy4 flash Free).
- **Task:** Study `clip-watcher` and explain how to combine it with the user's
  clipping platform so live-feed spike clips get published to social media.
- **Code changes:** None (idea/report only).
- **Files read:** `README.md`, `config.yaml`, `main.py`, `recorder.py`,
  `chat_monitor.py`, `spike_detector.py`, `clipper.py`, both memory files;
  SupoClip `README.md`, `docs/architecture.md`, `api/routes/social.py`,
  `api/routes/tasks.py`, `api/routes/media.py`, `media/upload_ingest.py`,
  `stream_source.py`, `mcp/server.py`, memory state.
- **Key findings:** SupoClip has direct publishing (`POST /social/posts`) but
  rejects live URLs; bridge must submit finished files via `/media/upload` +
  `/tasks`. Synchronous vertical encode in `run_live` (~2m20s in soak) blocks
  spike detection and threatens the <=5 min target. One channel per process;
  no recording retention; keyframe-seek offset.
- **Verification:** code + docs read only; no build/test run.
- **Result:** Design/report delivered to user. No implementation, no commits.

## 2026-11-18 — Implement v2: multi-channel approve-first pipeline

- **Agent/role:** OpenCode (Hy4 flash Free).
- **Task:** after user chose approve-first, 4-5 channels, passthrough publish,
  YouTube Shorts + Instagram Reels, build the bridge and multi-channel watcher.
- **Files changed (clip/):** new `rolling.py`, `review.py`, `publisher.py`,
  `supervisor.py`, `test_pipeline.py`, `test_publisher.py`; edited
  `config.yaml`, `recorder.py`, `README.md`, `.gitignore`, both memory files.
- **Files changed (sudoclip/):** `backend/src/api/routes/tasks.py` — added
  `POST /tasks/ingest-rendered`.
- **Verification:** `ruff check . --exclude .venv` PASS; `compileall` PASS;
  `test_pipeline.py` PASS; `test_publisher.py` PASS; legacy `main.py --test-mode`
  PASS; supervisor `--no-publish` smoke start PASS; SupoClip router import shows
  the new route registered. No live Twitch or real social post was made.
- **Result:** code complete, offline-verified. Pending: user's 4-5 channel
  handles and SupoClip OAuth/rollout/API-key configuration; Docker backend must
  be rebuilt to include the new route. No commits.

## 2026-11-18 — Split-screen post-approval renderer

- **Agent/role:** OpenCode (Hy4 flash Free).
- **Task:** after approval, re-render from RAW into a facecam-top/gameplay-bottom
  1080x1920 hard split; configurable per-channel crops; preview helper;
  review-UI preview; edited file published via SupoClip passthrough.
- **Files created/changed (clip/):** new `layout.py`; edited `supervisor.py`
  (RenderWorker + config validation), `config.yaml` (facecam/layout sections),
  `publisher.py` (edited_path preference), `review.py` (edited path, preview
  routes, Range, path confinement), `README.md`, `test_pipeline.py`,
  `test_publisher.py`, both memory files.
- **Verification:** ruff PASS, compileall PASS, test_pipeline PASS,
  test_publisher PASS, main.py --test-mode PASS, real config load PASS.
  Rendered sample from a real jasontheween raw clip: 1080x1920 H.264 yuv420p +
  AAC, clean decode, preview frame inspected. Review preview returned 200 with
  Range 206 and 403 for a file outside allowed roots. SupoClip route registered
  and its ffprobe helper accepted the sample; no authenticated ingest possible
  (0 users in dev DB, no API key). No real posting, no commits.
- **Concurrency note:** user/another agent added Kick channels and
  `kick_interval_sec` to config/supervisor mid-task and restarted the running
  `--no-publish` supervisor (~17:04); their edits were preserved and re-verified.
- **Remaining work:** human tuning of per-channel crops from preview stills;
  SupoClip credentials/OAuth and real ingest test before enabling publish.

## 2026-11-18 — More channels + Kick timed clips
- Agent: OpenCode (Hy4 flash Free).
- Task: lower spike_multiplier to 2.0; add theburntpeanut (Twitch), n3on and
  adinross (Kick). User chose timed clips for Kick.
- Files changed: `config.yaml` (multiplier, platform-aware channel entries,
  `kick_interval_sec: 600`), `supervisor.py` (ChannelRunner platform support,
  `_timed_watch`, load_config), `rolling.py` (kick.com URL), `README.md`, memory.
  Another agent's concurrent layout.py / RenderWorker / preview work preserved.
- Verification: ruff/compileall PASS; load_config shows 4 twitch + 2 kick;
  streamlink Kick URLs resolved; both Kick recorders produced segments; all six
  channels started with no errors.
- Result: running with 6 channels in --no-publish mode. No commits.

## 2026-11-18 — Platform branding + speech captions
- Agent: OpenCode (Hy4 flash Free). Worktree: `agent/brand-captions`.
- Task: after approval, overlay Twitch/Kick logo + channel URL and burn real
  speech subtitles into the 1080x1920 split edit.
- Files changed: new `captions.py`, `assets/{twitch,kick}.svg`,
  `test_captions.py`; edited `layout.py`, `supervisor.py`, `review.py`,
  `config.yaml`, `requirements.txt`, `README.md`, memory.
- Verification: ruff/compileall PASS; three tests PASS; real Whisper tiny
  transcription produced 2 timed cues; visual frame check passed after fixing
  ASS centisecond timestamps, caption MarginV and logo/handle overlap.
- Result: committed on branch `agent/brand-captions` (not main). Local systemd
  service deliberately untouched; merge + explicit restart required.
