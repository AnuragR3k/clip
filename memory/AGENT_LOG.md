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
