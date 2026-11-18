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

## 2026-11-18 — Feasibility study: chat-spike clips -> social publishing

**Status:** design/idea only. No code changed.

User goal: combine this tool (live chat-spike -> 30s-back clip) with the
uploaded-video clipping platform so clips from a live feed are published to
social media within <=5 min. Target platform is SupoClip (`../sudoclip`) — it
has OAuth connections and `POST /social/posts` publishing; `../clipforge` has
no publishing. SupoClip explicitly rejects live URLs, so the bridge must hand
it finished local files, not a live URL.

Findings / constraints that drive the design:
- `main.run_live()` calls `_handle_spike()` synchronously; `to_vertical()`
  took ~2m20s in the soak and blocks `watch_spikes`, so spikes in that window
  are handled late. Must move encode into a queue/worker to hit the 5-min SLA.
- Current settings: pre=30, post=15 -> ~45s clip; detection delayed by
  `settle_sec = post_sec + 5` = 20s. Good fit for "last 30 sec" trigger.
- One channel per process; "every big streamer" needs N instances, so keep
  recording/capture lightweight and centralize encoding/publishing.
- Single growing `.ts` per session has no retention; 1.2GB/21min (~80GB/day).
  A true 5-min buffer needs segment rotation (or HLS segments) + deletion.
- Publishing path confirmed in SupoClip: `POST /media/upload` -> `upload://…`;
  `POST /tasks` with `source.url`; `GET /tasks/{id}/clips` -> clip_id;
  `POST /social/posts` with task_id + clip_id + social_account_id.
  Connecting accounts requires session headers; posting accepts API keys.

Recommended shape: per-channel watchers -> fast stream-copy cut -> job queue ->
vertical encode -> SupoClip task -> `/social/posts` -> analytics loop.
Fastest variant adds a "passthrough ingest" in SupoClip so an already-rendered
clip becomes a `generated_clips` row without re-transcribing/re-rendering.

Next concrete action: user decision on (a) full SupoClip pipeline vs passthrough
publish, (b) which streamers/accounts, (c) auto-post vs review-queue. Only then
implement the bridge.

## 2026-11-18 — v2 implemented: multi-channel + approve-first + passthrough

**Status:** built and offline-verified. Not yet run on live Twitch; not yet
published to real social accounts (SupoClip social config is incomplete).

User decisions: approve-first; 4-5 channels (handles to be supplied later);
passthrough publish; targets YouTube Shorts + Instagram Reels.

New/changed files in `clip/`:
- `rolling.py` (new): `SegmentIndex`, `RollingRecorder` (streamlink -O piped to
  ffmpeg segment muxer, 10s numbered .ts, prune older than keep_sec=300),
  `concat_segments`.
- `review.py` (new): JSON-file `ReviewStore`, CLI (`list|approve|reject|serve`)
  and stdlib HTTP approve/reject UI on port 8765.
- `publisher.py` (new): SupoClip client — `POST /media/upload` ->
  `POST /tasks/ingest-rendered` -> `GET /social/connections` ->
  `POST /social/posts`; API key or HMAC signed headers.
- `supervisor.py` (new): N `ChannelRunner`s (recorder + chat + watcher), encode
  worker pool, publish worker, review UI; `--no-publish` flag.
- `main.py`: `resolve_streamlink()` now falls back to `.venv/bin/streamlink`.
- `config.yaml`: added `channels`, `segment_sec`, `keep_sec`, `encode_workers`,
  `review_dir`, `publish:` block. On 2026-11-18 the user set channels to
  `jasontheween`, `xqc`, `lacy` (from their Twitch URLs) and removed
  `shanks_ttv`; legacy `channel:` is `jasontheween`.
- `README.md` rewritten for v2; `.gitignore` adds `review/`.
- `test_pipeline.py`, `test_publisher.py` (new offline tests).

SupoClip change: `backend/src/api/routes/tasks.py` gained
`POST /tasks/ingest-rendered`, which accepts an absolute path or `upload://`
reference and inserts source/task(completed, progress=100)/generated_clips via
existing repositories, with no worker or re-render. Route registration verified
by importing the router into a FastAPI app (33 routes, route present). The
running Docker backend still has the old image — rebuild before use.

Verification: `ruff check` PASS, `compileall` PASS, `test_pipeline.py` PASS
(synthetic -> segments -> spike -> cut -> vertical -> review), `test_publisher.py`
PASS (mock HTTP server; correct headers and per-platform payloads), legacy
`main.py --test-mode` PASS, `supervisor.py --no-publish` smoke start PASS.

Blockers before real publishing (all in SupoClip `.env`/UI):
- `SOCIAL_PUBLISHING_ACCESS=off` -> must be `all` (or admins + mark user admin).
- `YOUTUBE_OAUTH_CLIENT_ID/SECRET` and `INSTAGRAM_APP_ID/SECRET` are empty.
- No user/API key exists in the fresh Docker DB -> register account, create key.
- YouTube + Instagram professional accounts must be connected in the UI.
- Instagram needs a public `NEXT_PUBLIC_APP_URL` (token media URL).

Next concrete action: user completes SupoClip config (API key, OAuth, rollout,
connected accounts, public URL), rebuilds the backend image, then runs
`python supervisor.py`. Channel list is already set to jasontheween/xqc/lacy.

## 2026-11-18 — Post-approval split-screen re-render (layout.py)

**Status:** implemented and offline-verified. Not published to real accounts.

Goal/spec: approved clips are re-rendered from the still-present RAW clip into
a hard-split 1080x1920 layout — facecam filling the top ~35% (672px), gameplay
the bottom ~65% (1248px), no blur/letterbox. Original `to_vertical()` untouched.

Files changed by this task:
- NEW `layout.py`: `to_split_vertical()` (single ffmpeg filter_complex, two
  crops + cover-scale + vstack; libx264 veryfast/crf20, `-c:a copy`,
  `+faststart`), crop normalization (0..1 floats = fractions; ints/floats>1 =
  pixels), `get_dimensions()` via ffprobe, `build_filter()`, `extract_preview()`
  (`python layout.py preview <clip> --out preview.jpg [--at N] [--suggest]`),
  optional OpenCV face hint (cv2 NOT installed, no dependency added), optional
  ASS hook title burn-in, and config validation helpers.
- `config.yaml`: added `facecam:` (default + per-channel boxes measured from
  real 1920x1080 frames: jasontheween top-right; xqc/lacy top-left; later also
  Kick n3on/adinross top-left) and `layout:` (672/1248, gameplay_box comment,
  hook_enabled: false). NOTE: during this task the user/another agent
  concurrently added Kick channels `{platform: kick, name: ...}`,
  `kick_interval_sec: 600`, and changed `spike_multiplier` to 2.0; those edits
  were preserved.
- `supervisor.py`: added `RenderWorker` between approval and publishing (render
  on approval off the HTTP thread; `edited_<stamp>.mp4`; record fields
  `edited_path`, `edit_status` done/fallback, `edit_error`; fallback to vertical
  on failure; re-renders approved records on restart; active even in
  `--no-publish`). `PublishWorker` now consumes from the render output queue.
  `load_config` validates facecam/layout. Review server gets `allowed_roots`.
- `publisher.py`: uploads `edited_path` when it is a real file, otherwise
  `vertical_path`; ingest filename follows the chosen file.
- `review.py`: table shows edited path/edit status and a `/preview/<clip_id>`
  link; new `/preview/<id>` and `/clips/<path>` routes with Range support,
  MIME types and `allowed_roots` path confinement.
- `README.md`: documented config, layout choice (gameplay = full frame; center
  crop removes corner webcams), crop-tuning workflow, preview CLI, fallback.
- `test_pipeline.py` / `test_publisher.py`: split render + edited-file publish
  assertions added.

Verification: `ruff check . --exclude .venv` PASS; compileall PASS;
`test_pipeline.py` PASS (vertical + split + review); `test_publisher.py` PASS
(uploads edited.mp4); `main.py --test-mode` PASS; real config load PASS with the
concurrent Kick changes. Full-size render from
`clips/jasontheween/raw_20261118_162620.mp4` -> `/tmp/opencode/frames/edited_full.mp4`
verified 1080x1920 H.264 yuv420p + AAC 48kHz stereo, 37.72s, clean full decode;
frame extracted and visually inspected (hard split visible). Hook text render
tested on a 4s sample. Review preview HTTP check PASS (200 video/mp4, 206 Range,
403 outside allowed root). SupoClip `/tasks/ingest-rendered` is registered in the
running backend (`/openapi.json`) and its read-only `ffprobe_duration()` accepts
the rendered file (37.716666), but a live authenticated ingest could NOT be run:
the dev DB has 0 users and no API key/session credential exists. Mock-server
publisher test covers the exact payload shape.

Running supervisor: the `--no-publish` instance was restarted by the user at
~17:04 (old PID gone; new PID 120230 with Twitch + Kick channels). This task did
NOT kill or modify that process. No commit made.

Next concrete action: user reviews a rendered sample with `layout.py preview`,
adjusts per-channel facecam boxes if needed, and supplies SupoClip credentials
before testing a real (non-posting) ingest and enabling publish.

## 2026-11-18 (later) — threshold, more channels, Kick timed clips
- `spike_multiplier` lowered from 3.0 to **2.0** to produce more clips.
- Added Twitch channel `theburntpeanut`; removed nothing. Six channels total.
- Added Kick support: `config.yaml` channels may be dicts
  (`platform: kick`, `name: n3on|adinross`). Streamlink 8.6.3 has a `kick`
  plugin, so `RollingRecorder` now builds `kick.com/<name>` URLs.
  Kick has no Twitch IRC chat, so `ChannelRunner._timed_watch()` cuts a 45s
  clip every `kick_interval_sec` (default 600) instead of using chat spikes.
  Kick files live under `recordings/kick_<name>/`, `clips/kick_<name>/`,
  `logs/kick_<name>/`. Chat-spike detection remains Twitch-only.
- Why timed clips: Kick's chat transport changed (Pusher key `eb1nrd` no longer
  valid; site now negotiates pusher/centrifugo credentials dynamically), so
  real Kick chat spikes were deferred. User chose timed clips.
- Review UI now has an explicit Refresh button plus 5s auto-refresh; rejected
  clips can be approved again; a missing vertical was restored from raw.
- NOTE: another agent is concurrently implementing the split-screen layout
  (`layout.py`, `RenderWorker` in `supervisor.py`, preview route in `review.py`).
  Their changes were preserved; Kick support was layered on top.
- Verification: `ruff check supervisor.py rolling.py` PASS, `compileall` PASS,
  `load_config` shows 4 twitch + 2 kick channels, streamlink resolved both Kick
  live URLs, both Kick recorders produced segments, all six channels started,
  no errors in supervisor log.
