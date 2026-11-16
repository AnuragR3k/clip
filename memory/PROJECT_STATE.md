# clip-watcher — Project State

**Last updated:** 2026-11-16
**Status:** v1 complete. Short live soak on `shanks_ttv` passed (~21 min). Multi-hour 24/7 soak and disk retention are still pending.

## Objective

Monitor one Twitch channel 24/7, record while live with Streamlink, read chat
anonymously, detect chat-activity spikes, and produce vertical MP4 clips for
manual review. No uploading, subtitles, official Clip API, or Kick support.

## Completed

- `config.yaml` with channel, paths, spike threshold, baseline, cooldown, and
  clip window settings.
- `recorder.py`: Streamlink live check via `--stream-url`, background recording
  thread, timestamped `.ts` files, graceful SIGTERM/SIGKILL shutdown, recording
  selection helpers.
- `chat_monitor.py`: anonymous Twitch IRC over TLS using `justinfan<digits>`,
  PING/PONG, reconnect loop, `timestamp\tusername\tmessage` log.
- `spike_detector.py`: 10s buckets, rolling 300s average, multiplier threshold,
  minimum messages, batch `find_spikes`, real-time `watch_spikes` tailing.
- `clipper.py`: ffmpeg fast-seek cut and 1080x1920 blurred-background vertical
  formatter with AAC audio copy.
- `main.py`: orchestration, cooldown, recording selection, manual-review output,
  Ctrl+C cleanup, and synthetic `--test-mode`.
- `README.md`, `requirements.txt`, `.gitignore`.
- `.venv` created with streamlink 8.6.3 and PyYAML 6.0.3.
- User-level ffmpeg available at `/home/ry3k/.local/bin/ffmpeg` (symlink to the
  static binary from imageio-ffmpeg in `/tmp/opencode/clipvenv`; this target is
  temporary and may disappear after reboot).

## Verification performed

- `python main.py --test-mode`: PASS. Generated synthetic recording in
  `recordings/test/`, detected a 50-message spike, produced raw and vertical
  clips. Final vertical:
  `clips/vertical_20261116_193620.mp4` (1080x1920, H.264, AAC, 45.03s).
- A second test run was also observed and checked on 2026-11-16 ~19:52:
  `clips/vertical_20261116_195140.mp4` is 1080x1920, H.264/AAC, 45.02s, and
  `logs/test_chat.log` reports the expected 50-message spike at 19:51:40.
- Extracted frames confirmed centered 16:9 foreground over blurred fill.
- `ffmpeg -i clips/vertical_20261116_193620.mp4 -f null -`: decoded cleanly.
- Batch spike CLI reported the expected spike from `logs/test_chat.log`.
- Real-time watcher test with a synthetic tail: PASS, yielded a 20-message
  spike bucket.
- Anonymous IRC TLS connection: connected as `justinfan72376` to `#twitch`
  (no messages during the 8s check); parser and RECONNECT handling unit-tested.
- Streamlink offline check for a nonexistent channel returned exit 1 as
  expected.
- `ruff check . --exclude .venv`: PASS.
- `python -m compileall -q .`: PASS.

## Live soak (2026-11-16, 20:08–20:29)

`config.yaml` channel is `shanks_ttv`. `python main.py --verbose` must be run
with `.venv` activated; `streamlink` is resolved from `PATH`, not from the
venv interpreter.

- IRC connected as `justinfan63789`. Chat log grew to 994 lines (~49 msgs/min).
- Recording: `recordings/shanks_ttv_20261116_200818.ts` (1.2 GB, ~21 min).
- Spikes at 20:14:20 (29 msgs, 21 unique), 20:16:20 (23 / 19), 20:21:50
  (38 / 34). The 20:22:00 bucket (26 / 22) was suppressed by the 60s cooldown.
- Verticals, all 1080x1920 H.264 yuv420p + AAC, ~45s, full decode clean:
  `clips/vertical_20261116_201420.mp4`, `clips/vertical_20261116_201620.mp4`,
  `clips/vertical_20261116_202150.mp4`. One frame showed centered gameplay and
  facecam over the blurred fill.
- Each vertical encode took about 2m20s and blocked the spike watcher. No IRC
  or Streamlink errors. Ctrl+C shut down cleanly.
- These four spikes were multi-user reactions, not single-user spam. Chat
  stayed near 50 msgs/min, so `spike_multiplier: 3.0` produced about 3 clips
  in 21 minutes rather than a flood.

## Not yet verified

- Multi-hour 24/7 behavior, disk growth, and Streamlink reconnect over hours.
  This 21-minute file was already 1.2 GB (~80 GB/day at the same bitrate).
- Python 3.10 runtime specifically (only Python 3.14 is installed here).
- ffmpeg after reboot (still the user-level symlink into a temporary binary).

## Files

Project root: `/home/ry3k/Desktop/projects/clip`

Core: `config.yaml`, `recorder.py`, `chat_monitor.py`, `spike_detector.py`,
`clipper.py`, `main.py`, `README.md`, `requirements.txt`.

Generated (ignored): `recordings/`, `clips/`, `logs/`, `.venv/`, `__pycache__/`.

## Branch / commits

Directory is not a git repository; no branch or commits.

## Known issues / limitations

- No automatic disk retention for 24/7 recordings.
- Keyframe seek can shift clip boundaries by a few seconds.
- Recording filename time is set just before Streamlink connects, so the first
  video bytes may be slightly later than the filename timestamp.
- Real-time watcher does not handle external log rotation/replacement.
- The `ffmpeg` symlink points into a temporary path; install system ffmpeg for
  a durable setup.

## Next action

Watch the three live verticals and judge how many are worth keeping. Leave
`spike_multiplier` at 3.0 unless that review says the hit rate is too low or
too high. Before unattended 24/7 use, add recording retention and a durable
system ffmpeg.
