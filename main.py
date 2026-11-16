#!/usr/bin/env python3
"""clip-watcher entry point.

Real mode: record a channel, watch its chat, and turn activity spikes into
vertical clips for manual review. Test mode: use a synthetic recording and
chat log to exercise the whole pipeline without contacting Twitch.
"""

from __future__ import annotations

import argparse
import logging
import math
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

from chat_monitor import ChatMonitor
from clipper import ClipperError, cut_clip, get_ffmpeg, to_vertical
from recorder import (
    RecorderError,
    StreamRecorder,
    find_recording_for_spike,
    recording_start_epoch,
    resolve_streamlink,
)
from spike_detector import Spike, find_spikes, watch_spikes

LOGGER = logging.getLogger("clip-watcher")

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")


def load_config(config_path: str | Path) -> dict:
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"{path} must contain a YAML mapping.")

    config = {
        "channel": str(raw.get("channel", "")).strip().lstrip("#"),
        "recordings_dir": Path(str(raw.get("recordings_dir", "./recordings"))),
        "clips_dir": Path(str(raw.get("clips_dir", "./clips"))),
        "logs_dir": Path(str(raw.get("logs_dir", "./logs"))),
        "spike_window_sec": float(raw.get("spike_window_sec", 10)),
        "spike_multiplier": float(raw.get("spike_multiplier", 3.0)),
        "spike_baseline_sec": float(raw.get("spike_baseline_sec", 300)),
        "spike_min_messages": int(raw.get("spike_min_messages", 3)),
        "spike_cooldown_sec": float(raw.get("spike_cooldown_sec", 60)),
        "pre_sec": int(raw.get("pre_sec", 30)),
        "post_sec": int(raw.get("post_sec", 15)),
    }

    if config["spike_window_sec"] <= 0:
        raise ValueError("spike_window_sec must be greater than zero.")
    if config["spike_multiplier"] <= 0:
        raise ValueError("spike_multiplier must be greater than zero.")
    if config["spike_baseline_sec"] <= 0:
        raise ValueError("spike_baseline_sec must be greater than zero.")
    if config["spike_min_messages"] < 1:
        raise ValueError("spike_min_messages must be at least one.")
    if config["pre_sec"] < 0 or config["post_sec"] < 0:
        raise ValueError("pre_sec and post_sec must not be negative.")
    if config["spike_cooldown_sec"] < 0:
        raise ValueError("spike_cooldown_sec must not be negative.")

    return config


def _prepare_directories(config: dict) -> None:
    for key in ("recordings_dir", "clips_dir", "logs_dir"):
        Path(config[key]).mkdir(parents=True, exist_ok=True)


def _validate_real_channel(channel: str) -> None:
    if channel in {"", "your_channel_name"}:
        raise ValueError(
            "Set `channel` in config.yaml to the Twitch channel to monitor."
        )
    if not _CHANNEL_RE.fullmatch(channel):
        raise ValueError(
            "Twitch channel names may only contain letters, numbers, and "
            f"underscores (got {channel!r})."
        )


def _run_test_ffmpeg(command: list[str], description: str) -> None:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg timed out while {description}") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run ffmpeg: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-2000:]
        raise RuntimeError(f"ffmpeg failed while {description}:\n{detail}")


def _make_synthetic_recording(
    output_path: Path, duration: int, ffmpeg: str, record_start: float | None = None
) -> float:
    """Create a test pattern + tone recording. Returns its start time."""
    if record_start is None:
        record_start = time.time()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "60",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-f",
        "mpegts",
        str(output_path),
    ]
    _run_test_ffmpeg(command, "creating the synthetic test recording")
    return record_start


def _write_synthetic_chat_log(
    log_path: Path,
    record_start: float,
    duration: int,
    window_sec: float,
    multiplier: float,
    min_messages: int,
    post_sec: int,
) -> float:
    """Write a fake chat log with one obvious spike. Returns spike bucket start."""
    spike_target = record_start + max(80.0, duration * 0.65)
    spike_bucket_start = math.ceil(spike_target / window_sec) * window_sec
    latest_allowed = record_start + duration - post_sec - window_sec - 3
    if spike_bucket_start > latest_allowed:
        spike_bucket_start = math.floor(latest_allowed / window_sec) * window_sec
    if spike_bucket_start <= record_start + 60:
        raise RuntimeError("The synthetic recording is too short for a baseline spike.")

    # Baseline chatter: roughly one message every two seconds.
    baseline: list[tuple[float, str, str]] = []
    for second in range(0, duration, 2):
        timestamp = record_start + second + 0.25
        if spike_bucket_start <= timestamp < spike_bucket_start + window_sec:
            continue
        baseline.append(
            (timestamp, f"viewer{second % 7}", f"baseline message {second}")
        )

    # One large burst, entirely inside a single bucket.
    spike_messages = max(
        50,
        min_messages + 10,
        int(multiplier * max(1.0, window_sec / 2.0) * 2) + 20,
    )
    spike_span = min(window_sec * 0.8, 9.0)
    spike_lines: list[tuple[float, str, str]] = []
    for index in range(spike_messages):
        fraction = index / max(1, spike_messages - 1)
        timestamp = spike_bucket_start + 0.1 + fraction * spike_span
        spike_lines.append(
            (timestamp, f"spammer{index % 5}", f"POG spike message {index}")
        )

    lines = sorted(baseline + spike_lines, key=lambda item: item[0])
    with log_path.open("w", encoding="utf-8") as handle:
        for timestamp, username, message in lines:
            handle.write(f"{timestamp:.3f}\t{username}\t{message}\n")

    return spike_bucket_start


def run_test_mode(config: dict) -> int:
    """Run the full pipeline with generated media; never contacts Twitch."""
    _prepare_directories(config)
    ffmpeg = get_ffmpeg()

    channel = config["channel"]
    if not _CHANNEL_RE.fullmatch(channel) or channel == "your_channel_name":
        channel = "test_channel"

    duration = max(
        180,
        int(config["pre_sec"] + config["post_sec"] + 120),
        int(config["spike_window_sec"] * 12 + 60),
    )
    recordings_dir = Path(config["recordings_dir"])
    logs_dir = Path(config["logs_dir"])
    clips_dir = Path(config["clips_dir"])

    # Keep synthetic recordings out of the production recordings/ folder so a
    # later real-mode run cannot mistake test media for a live recording.
    test_recordings_dir = recordings_dir / "test"
    test_recordings_dir.mkdir(parents=True, exist_ok=True)

    # Keep test artifacts separate from the production chat log.
    chat_log = logs_dir / "test_chat.log"
    # Backdate the simulated stream so the spike lands in the recent past,
    # making test clip filenames look like real ones.
    record_start = time.time() - duration
    stamp = datetime.fromtimestamp(record_start).strftime("%Y%m%d_%H%M%S")
    recording = test_recordings_dir / f"{channel}_{stamp}.ts"

    LOGGER.info("TEST MODE: creating a %ss synthetic recording...", duration)
    record_start = _make_synthetic_recording(
        recording, duration, ffmpeg, record_start=record_start
    )

    spike_bucket_start = _write_synthetic_chat_log(
        chat_log,
        record_start,
        duration,
        config["spike_window_sec"],
        config["spike_multiplier"],
        config["spike_min_messages"],
        config["post_sec"],
    )
    LOGGER.info("TEST MODE: synthetic chat log at %s", chat_log)
    LOGGER.info("TEST MODE: looking for spikes...")

    spikes = find_spikes(
        chat_log,
        config["spike_window_sec"],
        config["spike_multiplier"],
        baseline_sec=config["spike_baseline_sec"],
        min_messages=config["spike_min_messages"],
    )
    if not spikes:
        raise RuntimeError(
            "No spike was detected in the synthetic chat log. "
            "Check spike_multiplier/min_messages in config.yaml."
        )

    spike = min(spikes, key=lambda item: abs(item.start_time - spike_bucket_start))
    LOGGER.info(
        "Detected spike at %s (%s messages, expected bucket %s).",
        datetime.fromtimestamp(spike.start_time).strftime("%Y-%m-%d %H:%M:%S"),
        spike.message_count,
        datetime.fromtimestamp(spike_bucket_start).strftime("%H:%M:%S"),
    )

    clip_stamp = datetime.fromtimestamp(spike.start_time).strftime("%Y%m%d_%H%M%S")
    raw_path = clips_dir / f"raw_{clip_stamp}.mp4"
    vertical_path = clips_dir / f"vertical_{clip_stamp}.mp4"

    cut_clip(
        recording,
        spike.start_time,
        raw_path,
        pre_sec=config["pre_sec"],
        post_sec=config["post_sec"],
        recording_start=record_start,
    )
    to_vertical(raw_path, vertical_path)

    print()
    print("=" * 72)
    print("TEST PASSED: chat log -> spike -> raw clip -> vertical clip")
    print(f"  Recording : {recording.resolve()}")
    print(f"  Chat log  : {chat_log.resolve()}")
    print(
        "  Spike     : "
        f"{datetime.fromtimestamp(spike.start_time).strftime('%Y-%m-%d %H:%M:%S')} "
        f"({spike.message_count} messages)"
    )
    print(f"  Raw clip  : {raw_path.resolve()}")
    print(f"  VERTICAL  : {vertical_path.resolve()}")
    print("=" * 72)
    return 0


def _print_clip_ready(
    spike: Spike, raw_path: Path, vertical_path: Path
) -> None:
    timestamp = datetime.fromtimestamp(spike.start_time).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    print()
    print("=" * 72)
    print("CLIP READY FOR MANUAL REVIEW")
    print(f"  Spike    : {timestamp} ({spike.message_count} messages)")
    print(f"  Raw clip : {raw_path.resolve()}")
    print(f"  Vertical : {vertical_path.resolve()}")
    print("=" * 72)
    print(flush=True)


def _handle_spike(spike: Spike, config: dict) -> Path | None:
    recordings_dir = Path(config["recordings_dir"])
    clips_dir = Path(config["clips_dir"])

    recording = find_recording_for_spike(recordings_dir, spike.start_time)
    if recording is None:
        LOGGER.warning("Spike detected, but no .ts recording is available yet.")
        return None

    recording_start = recording_start_epoch(recording)
    if recording_start is not None and spike.start_time < recording_start:
        LOGGER.warning(
            "Latest recording %s starts after the spike; skipping.",
            recording.name,
        )
        return None

    clip_stamp = datetime.fromtimestamp(spike.start_time).strftime("%Y%m%d_%H%M%S")
    raw_path = clips_dir / f"raw_{clip_stamp}.mp4"
    vertical_path = clips_dir / f"vertical_{clip_stamp}.mp4"

    LOGGER.info(
        "Cutting spike at %s from %s...",
        datetime.fromtimestamp(spike.start_time).strftime("%H:%M:%S"),
        recording.name,
    )
    cut_clip(
        recording,
        spike.start_time,
        raw_path,
        pre_sec=config["pre_sec"],
        post_sec=config["post_sec"],
        recording_start=recording_start,
    )
    to_vertical(raw_path, vertical_path)
    _print_clip_ready(spike, raw_path, vertical_path)
    return vertical_path


def run_live(config: dict) -> int:
    _validate_real_channel(config["channel"])
    _prepare_directories(config)

    # Fail fast with a useful message instead of looping in a background thread.
    resolve_streamlink()
    get_ffmpeg()

    recordings_dir = Path(config["recordings_dir"])
    logs_dir = Path(config["logs_dir"])
    chat_log = logs_dir / "chat.log"

    stop = threading.Event()
    recorder = StreamRecorder(
        config["channel"],
        recordings_dir,
        logs_dir,
        check_interval=60,
    )
    monitor = ChatMonitor(config["channel"], chat_log)

    LOGGER.info("Monitoring #%s. Press Ctrl+C to stop.", config["channel"])
    LOGGER.info("Recordings: %s", recordings_dir.resolve())
    LOGGER.info("Clips: %s", Path(config["clips_dir"]).resolve())

    last_handled_spike = -math.inf
    try:
        recorder.start()
        monitor.start()

        for spike in watch_spikes(
            chat_log,
            config["spike_window_sec"],
            config["spike_multiplier"],
            baseline_sec=config["spike_baseline_sec"],
            min_messages=config["spike_min_messages"],
            poll_sec=2.0,
            settle_sec=config["post_sec"] + 5,
            stop_event=stop,
        ):
            if spike.start_time - last_handled_spike < config["spike_cooldown_sec"]:
                LOGGER.debug(
                    "Ignoring spike at %s during cooldown.",
                    datetime.fromtimestamp(spike.start_time).strftime("%H:%M:%S"),
                )
                continue

            last_handled_spike = spike.start_time
            try:
                _handle_spike(spike, config)
            except ClipperError as exc:
                LOGGER.error("Could not create clip for spike: %s", exc)
            except Exception:  # keep the watcher alive
                LOGGER.exception("Unexpected error while handling a spike.")
    except KeyboardInterrupt:
        LOGGER.info("Interrupted; shutting down.")
    finally:
        stop.set()
        monitor.stop()
        recorder.stop()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record Twitch, detect chat spikes, and cut vertical clips."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="path to config.yaml",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="run the full pipeline with synthetic media; does not contact Twitch",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(args.config)
        if args.test_mode:
            return run_test_mode(config)
        return run_live(config)
    except KeyboardInterrupt:
        return 130
    except (
        FileNotFoundError,
        TypeError,
        ValueError,
        RuntimeError,
        ClipperError,
        RecorderError,
    ) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
