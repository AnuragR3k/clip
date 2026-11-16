# clip-watcher

Minimal 24/7 Twitch monitor: record one channel while it is live, watch chat
activity, and cut a vertical MP4 around a chat spike for **manual review**.

This v1 deliberately contains no publishing, no Whisper subtitles, no Twitch
clip API, and no Kick support.

## How it works

1. `recorder.py` asks Streamlink whether the channel is live and, when it is,
   records with the normal `streamlink` CLI to `recordings/<channel>_<time>.ts`.
2. `chat_monitor.py` connects anonymously to `irc.chat.twitch.tv:6697` over TLS
   as `justinfan<digits>` (no OAuth token) and appends
   `timestamp<TAB>username<TAB>message` to `logs/chat.log`.
3. `spike_detector.py` groups chat into 10-second buckets and compares each
   bucket with the rolling average of the previous 5 minutes. A bucket is a
   spike when it is more than `spike_multiplier` times that average.
4. `main.py` waits for a spike, finds the recording that covers it, cuts
   `pre_sec` before through `post_sec` after, then creates a 1080x1920 version
   with a blurred, cropped background and the original video centered.
5. The vertical file path is printed for manual review. Nothing is uploaded.

The IRC reader is implemented directly with Python's `socket`/`ssl` modules,
so no extra IRC client library or Twitch token is required.

The recording timestamp is stored in the filename, so spike times from
`time.time()` can be mapped back to a position in the video. Clips use
`ffmpeg -ss` before `-i`, which seeks to a nearby keyframe. A small offset of a
few seconds is expected and is tunable with `pre_sec`/`post_sec`.

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) with `libx264` on `PATH`
- Streamlink (installed as a Python package; called through its CLI)
- PyYAML

On Debian/Ubuntu:

```bash
sudo apt install ffmpeg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The code also recognizes `FFMPEG_BIN` and `STREAMLINK_BIN` environment
variables if the executables are not on `PATH`. If `imageio-ffmpeg` happens to
be installed, its bundled ffmpeg is used as a last-resort fallback.

## Configure

Edit `config.yaml`:

```yaml
channel: "your_channel_name"   # Twitch login name, not a URL

recordings_dir: "./recordings"
clips_dir: "./clips"
logs_dir: "./logs"

spike_window_sec: 10           # bucket size
spike_multiplier: 3.0          # spike when count > 3x rolling average
spike_baseline_sec: 300        # rolling average window
spike_min_messages: 3          # ignore tiny buckets
spike_cooldown_sec: 60         # avoid multiple clips from one burst

pre_sec: 30                    # seconds of context before the spike
post_sec: 15                   # seconds after the spike
```

Relative paths are resolved from the directory where you run the program, so
start it from the project root. The first six buckets (60 seconds with the
default settings) of a new chat log are treated as warm-up so the rolling
average is not built from zeros.

## Real mode

```bash
source .venv/bin/activate
python main.py
```

The recorder and chat monitor run in background threads; the spike watcher is
the main loop. When a spike is detected:

```text
CLIP READY FOR MANUAL REVIEW
  Spike    : 2026-11-16 20:14:30 (84 messages)
  Raw clip : /path/to/clips/raw_20261116_201430.mp4
  Vertical : /path/to/clips/vertical_20261116_201430.mp4
```

Open the vertical file and check the moment. If the spike is too early or too
late in the clip, adjust `pre_sec` / `post_sec` (not the detector threshold).

Stop cleanly with `Ctrl+C`. Streamlink's process group is terminated, and the
chat socket is closed.

## Test mode (acceptance test)

```bash
source .venv/bin/activate
python main.py --test-mode
```

Test mode makes **no Twitch connection and does not use Streamlink**. It
generates a 3-minute synthetic 1280x720 test-pattern recording with audio, a
fake `logs/test_chat.log` with a baseline and one clear spike, then runs the
same detector and ffmpeg pipeline used in real mode. Expected output:

```text
TEST PASSED: chat log -> spike -> raw clip -> vertical clip
  ...
  VERTICAL  : /path/to/clips/vertical_YYYYMMDD_HHMMSS.mp4
```

The vertical file should be 1080x1920, with the original 16:9 image centered
over a blurred, cropped background, and with audio preserved.

Useful checks:

```bash
ls -lh clips/
ffmpeg -i clips/vertical_*.mp4 -f null -      # decode check
ffmpeg -i clips/vertical_*.mp4 -ss 5 -frames:v 1 frame.jpg
```

## Standalone spike inspection

```bash
python spike_detector.py logs/chat.log
python spike_detector.py logs/chat.log --watch --multiplier 2.0
python spike_detector.py logs/chat.log --baseline-sec 300 --min-messages 3
```

Output columns are `start_epoch`, `end_epoch`, `message_count`, and a
human-readable interval.

## Files

```text
config.yaml          channel, paths, detection and clip settings
recorder.py          Streamlink live checks + recording loop
chat_monitor.py      anonymous Twitch IRC reader with reconnect
spike_detector.py    batch detection and real-time tailing
clipper.py           ffmpeg cut + vertical formatting
main.py              orchestration and --test-mode
README.md            this file
requirements.txt     streamlink + PyYAML
```

Generated files:

```text
recordings/<channel>_YYYYMMDD_HHMMSS.ts
recordings/test/<channel>_YYYYMMDD_HHMMSS.ts   (test mode only)
logs/chat.log
logs/streamlink.log
logs/test_chat.log                 (test mode only)
clips/raw_YYYYMMDD_HHMMSS.mp4
clips/vertical_YYYYMMDD_HHMMSS.mp4
```

## v1 limitations

- One channel only.
- No automatic publishing, uploads, or subtitles.
- No Twitch Clip API: the official API cannot select an exact start point, so
  this project records continuously and cuts locally.
- Keyframe seeking can shift the cut by a few seconds.
- The recording filename is created just before Streamlink connects, so the
  first bytes of video may be a few seconds after the filename timestamp.
- Continuous 24/7 recording grows disk usage without limit. Add retention or
  monitor free space for unattended deployments.
- Chat is read anonymously. Twitch may rate-limit or change the undocumented
  IRC behavior; the monitor reconnects automatically.
