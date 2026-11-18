# spike-watcher v2

Multi-channel, approve-first clipping from **live** Twitch feeds.

Watch 4-5 channels at once, record only a rolling **5-minute buffer**, detect
chat-activity spikes, cut the last 30 seconds (+15s after), render a 1080x1920
vertical MP4, and hold it in a review queue. **Nothing is posted until you
approve it.** On approval the saved RAW clip is re-rendered into a hard-split
1080x1920 layout (facecam top ~35%, gameplay bottom ~65%), then handed to
SupoClip's own API and published as YouTube Shorts / Instagram Reels.

```
live feed ─► rolling 5-min .ts segments ─┐
chat IRC ─► 10s buckets / 3x baseline ─► spike ─► cut 45s ─► encode queue
                                                              │
                                                              ▼
                             review UI (approve / reject) ─► split re-render from RAW ─► SupoClip ingest-rendered
                                                              │
                                                              ▼
                                                  /social/posts -> YT Shorts + IG Reels
```

## What is new vs v1

| v1 (`main.py`) | v2 (`supervisor.py`) |
|---|---|
| One channel | `channels:` list (4-5) in one process |
| One ever-growing `.ts` file (~80 GB/day) | 10s segments, only last 300s kept |
| Encode ran in the spike loop (~2m20s, blocked detection) | Encode runs in a worker pool |
| Printed path for manual review | Review queue + web UI at `http://localhost:8765` |
| No publishing | Passthrough ingest + publish to YouTube/Instagram after approval |

`main.py --test-mode` still works and exercises the original single-channel
pipeline.

## Setup

1. Install dependencies and ffmpeg as before (see "Requirements" below).
2. Edit `config.yaml`:
   - put your Twitch login handles under `channels:`. Kick channels use
     `- platform: "kick"` with `name: "<login>"` and get timed clips instead.
   - leave `pre_sec: 30`, `post_sec: 15` for a ~45s clip
   - set `publish.api_url` to your SupoClip backend (default `http://localhost:8000`)
   - check the `facecam:` boxes for your channels (see below) before approving
     any clip; the defaults were measured from real frames on 2026-11-18
3. Export a SupoClip credential (one of):
   - `SUPOCLIP_API_KEY=sk_...` — create it at SupoClip `/settings/api-keys`
   - or `SUPOCLIP_USER_ID=<uuid>` + `SUPOCLIP_AUTH_SECRET=<BACKEND_AUTH_SECRET>`
4. Start the stack:
   ```bash
   source .venv/bin/activate
   python supervisor.py --verbose
   ```
5. Open `http://localhost:8765`, watch pending clips, and click
   **Approve & publish** or **Reject**. Only approved clips reach SupoClip.

Use `--no-publish` to run capture + review only (no calls to SupoClip).

Review from the terminal instead:

```bash
python review.py list
python review.py approve <clip_id>
python review.py reject <clip_id>
```

## Split-screen output and facecam crops

Approval triggers a dedicated render worker. It re-encodes the still-present
`raw_*.mp4` (not the blurred vertical) into `edited_<stamp>.mp4`:

- top `layout.top_px` (default 672): facecam crop, scaled to cover the full
  1080px width with no letterboxing or blurred padding;
- bottom `layout.bottom_px` (default 1248): gameplay, scaled to cover the full
  width directly below the facecam.

The output is always 1080x1920 H.264 yuv420p + AAC (copied), `+faststart`.
`top_px + bottom_px` must equal 1920. Gameplay uses the **full source frame**
by default: after "cover" scaling the center crop naturally excludes corner
webcams. Set `layout.gameplay_box` only if the action is off-center.

`config.yaml` additions:

```yaml
facecam:
  default: {x: 0.78, y: 0.0, w: 0.22, h: 0.27}
  channels:
    jasontheween: {x: 0.785, y: 0.0, w: 0.215, h: 0.264}
    xqc:          {x: 0.0, y: 0.042, w: 0.193, h: 0.222}
    lacy:         {x: 0.0, y: 0.0, w: 0.276, h: 0.278}
    n3on:         {x: 0.0, y: 0.042, w: 0.24, h: 0.247}
    adinross:     {x: 0.0, y: 0.0, w: 0.22, h: 0.222}

layout:
  top_px: 672
  bottom_px: 1248
  # gameplay_box: {x: 0.0, y: 0.0, w: 1.0, h: 1.0}
  hook_enabled: false       # stretch goal: burn the title into the gameplay top
  hook_template: "{title}"
```

Coordinates are **fractions of the source frame** when written as floats in
0..1; integers (or floats > 1) are absolute pixels. Each channel can use its
own box; unknown channels use `facecam.default`.

### Tuning a channel's crop

1. Extract a still from a saved raw clip and look at it:

   ```bash
   python layout.py preview clips/<channel>/raw_<stamp>.mp4 --out preview.jpg
   ```

   Add `--at <seconds>` to seek elsewhere, or `--suggest` for an optional
   OpenCV face-detection hint (OpenCV is *not* required or installed here).
2. Measure the webcam rectangle in the 1920x1080 still and convert to
   fractions: `x = left / 1920`, `y = top / 1080`, `w = width / 1920`,
   `h = height / 1080`. Keep a small margin outside the face so the whole
   webcam shot is visible.
3. Put those numbers under `facecam.channels.<channel>` and re-approve a clip.
   The review page shows the edited path and a **Preview** link
   (`/preview/<clip_id>`) so you can check the result before/after publishing.

If a split render fails, the review record gets `edit_status: fallback` and the
publisher uploads the original vertical clip instead. `--no-publish` still
renders on approval; it just never calls SupoClip.

## SupoClip side (required before publishing will work)

This project calls SupoClip's own API. In SupoClip (`../sudoclip`) you must:

1. Create an account and generate an API key in `/settings/api-keys`, or use
   the signed-header credentials above.
2. Enable social publishing: `SOCIAL_PUBLISHING_ACCESS=all` (or `admins` and
   mark the user admin).
3. Configure OAuth apps: `YOUTUBE_OAUTH_CLIENT_ID/SECRET` and
   `INSTAGRAM_APP_ID/SECRET`; register the redirect URIs.
4. Connect a YouTube account and an Instagram **professional** account in the
   SupoClip UI. Instagram Reels need a publicly reachable
   `NEXT_PUBLIC_APP_URL` because Meta fetches the video from a token URL.
5. Apply the new `POST /tasks/ingest-rendered` route (already added to
   `sudoclip/backend/src/api/routes/tasks.py`) and rebuild/recreate the
   backend container, then `GET /docs` should list the route.

The publisher uploads the rendered MP4 via `POST /media/upload`, registers it
with `POST /tasks/ingest-rendered` (task created as `completed`, no re-render),
looks up active accounts via `GET /social/connections`, and calls
`POST /social/posts` for each platform in `publish.platforms`.

## Files

```text
config.yaml       channels, buffer, thresholds, publish settings
recorder.py       v1 single-file recorder (kept for main.py)
rolling.py        segmented rolling recorder + segment index
chat_monitor.py   anonymous Twitch IRC reader (shared)
spike_detector.py bucket + rolling-average detector (shared)
clipper.py        raw cut + 1080x1920 blurred vertical formatter (shared)
layout.py         split-screen renderer, preview still + crop helpers
supervisor.py     multi-channel supervisor, encode/render/publish workers
review.py         approve-first store, CLI and small web UI
publisher.py      SupoClip passthrough ingest + social posting client
main.py           v1 entry point + --test-mode
test_pipeline.py  offline end-to-end test of the new pipeline
```

Generated (ignored): `recordings/`, `clips/`, `logs/`, `review/`, `.venv/`.

## Tests / verification

```bash
.venv/bin/python -m ruff check . --exclude .venv
.venv/bin/python -m compileall -q .
.venv/bin/python test_pipeline.py
.venv/bin/python main.py --test-mode     # legacy v1 pipeline
```

`test_pipeline.py` builds a synthetic recording + chat log, segments it,
selects the spike window, cuts, renders vertically and as a split clip, then
files both in the review store. No network access.

Manual split-render smoke test with a real clip:

```bash
python layout.py preview clips/<channel>/raw_<stamp>.mp4 --out preview.jpg
python -c "from layout import to_split_vertical; \
  to_split_vertical('clips/<channel>/raw_<stamp>.mp4', '/tmp/split.mp4', \
                    {'x': 0.785, 'y': 0.0, 'w': 0.215, 'h': 0.264})"
ffprobe -v error -show_entries stream=codec_name,width,height,pix_fmt /tmp/split.mp4
```

## Limits

- Twitch channels use anonymous IRC chat-spike detection. Kick channels are
  supported via `platform: kick` entries but use **timed clips** every
  `kick_interval_sec` (600s default) because Kick chat is not wired up; see
  `config.yaml`.
- Chat spikes are a proxy for "big moment" (raids/spam can trigger); a
  unique-chatter/spam filter is a good next improvement.
- Keyframe seeking can shift cut boundaries by a few seconds.
- Instagram only supports Reels with `privacy_level: public`.
- Clipping other people's streams for revenue needs their permission or a
  sound legal basis; check Twitch and each platform's terms.

## Docker / VPS deployment

The clipper is a long-running, stateful process, so Vercel or Cloudflare Workers
are not suitable. A VPS or any host that runs Docker is the right target.

```bash
# local build / smoke test
docker compose build
REVIEW_PORT=8765 EXTRA_ARGS=--no-publish docker compose up -d
docker compose logs -f clipper
```

Volumes under `./data/` hold recordings, clips, logs and the review queue, so
rebuilding or redeploying never loses saved clips. `config.yaml` is mounted
read-only — changing it requires a container restart (`docker compose up -d`).

To enable publishing, set `EXTRA_ARGS` to an empty string and provide
`SUPOCLIP_API_KEY` (or the signed-header pair) via the environment/compose.
The container must be able to reach the SupoClip backend, not just the frontend.

`.github/workflows/deploy.yml` deploys on every push to `main` over SSH. Set the
repo secrets `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, and `VPS_APP_DIR`; on the VPS,
clone the repo once into that directory and make sure `deploy.sh` is executable.

## Free local option: systemd user service

No VPS needed. A `systemd --user` unit keeps the clipper running, restarts it
automatically on crash, and sends logs to `journalctl`. A template is in
`systemd/spike-clipper.service.example`.

```bash
# install (adjust paths in the template first)
mkdir -p ~/.config/systemd/user
cp systemd/spike-clipper.service.example ~/.config/systemd/user/spike-clipper.service
systemctl --user daemon-reload
systemctl --user enable --now spike-clipper

systemctl --user status spike-clipper
journalctl --user -u spike-clipper -f
systemctl --user restart spike-clipper
```

The unit starts at login. To survive logout/reboot without logging in, enable
lingering once: `sudo loginctl enable-linger $USER`.

Editing code does not restart the service — only `systemctl --user restart`
does. Saved clips and the review queue are never touched by a restart.
