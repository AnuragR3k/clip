#!/usr/bin/env python3
"""Multi-channel, approve-first spike clipping supervisor.

Runs 4-5 channel watchers in one process: each records a rolling 5-minute
buffer, tails chat for spikes, and cuts a ~45s clip. Cutting is cheap; the slow
vertical encode runs in a worker pool, and finished clips go into a review
queue. Only clips a human approves are passed to the publisher.
"""

from __future__ import annotations

import argparse
import logging
import math
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

from chat_monitor import ChatMonitor
from clipper import ClipperError, cut_clip, to_vertical
from layout import facecam_box_for, to_split_vertical, validate_layout_config
from publisher import Publisher, PublisherError
from review import ReviewStore, serve
from rolling import RollingRecorder, SegmentIndex, concat_segments
from spike_detector import Spike, watch_spikes

LOGGER = logging.getLogger("spike-supervisor")

_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")


def load_config(path: str | Path) -> dict:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError("config.yaml must contain a mapping.")

    channels = raw.get("channels") or []
    if isinstance(channels, str):
        channels = [channels]
    if not isinstance(channels, list) or not channels:
        raise ValueError(
            "Set `channels:` in config.yaml to a list of channel names. "
            "Kick channels use: {platform: kick, name: <login>}."
        )
    normalized: list[dict[str, str]] = []
    for item in channels:
        platform = "twitch"
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip().lstrip("#")
            platform = str(item.get("platform") or "twitch").strip().lower()
        elif isinstance(item, str):
            name = item.strip().lstrip("#")
        else:
            raise TypeError(f"Invalid channel entry: {item!r}")
        if platform not in {"twitch", "kick"}:
            raise ValueError(f"Unsupported platform {platform!r} for {name!r}.")
        if not _CHANNEL_RE.fullmatch(name):
            raise ValueError(f"Invalid channel name: {name!r}")
        normalized.append({"name": name, "platform": platform})
    keys = [(item["platform"], item["name"]) for item in normalized]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate channels in config.yaml.")

    config = {
        "channels": normalized,
        "kick_interval_sec": int(raw.get("kick_interval_sec", 600)),
        "recordings_dir": Path(str(raw.get("recordings_dir", "./recordings"))),
        "clips_dir": Path(str(raw.get("clips_dir", "./clips"))),
        "logs_dir": Path(str(raw.get("logs_dir", "./logs"))),
        "review_dir": Path(str(raw.get("review_dir", "./review"))),
        "segment_sec": int(raw.get("segment_sec", 10)),
        "keep_sec": int(raw.get("keep_sec", 300)),
        "encode_workers": int(raw.get("encode_workers", 2)),
        "spike_window_sec": float(raw.get("spike_window_sec", 10)),
        "spike_multiplier": float(raw.get("spike_multiplier", 3.0)),
        "spike_baseline_sec": float(raw.get("spike_baseline_sec", 300)),
        "spike_min_messages": int(raw.get("spike_min_messages", 3)),
        "spike_cooldown_sec": float(raw.get("spike_cooldown_sec", 60)),
        "pre_sec": int(raw.get("pre_sec", 30)),
        "post_sec": int(raw.get("post_sec", 15)),
        "publish": raw.get("publish") if isinstance(raw.get("publish"), dict) else {},
        "facecam": raw.get("facecam") if isinstance(raw.get("facecam"), dict) else {},
        "layout": raw.get("layout") if isinstance(raw.get("layout"), dict) else {},
    }
    if config["segment_sec"] < 2 or config["keep_sec"] < config["segment_sec"] * 3:
        raise ValueError("keep_sec must be at least 3x segment_sec.")
    if config["encode_workers"] < 1:
        raise ValueError("encode_workers must be at least 1.")
    if config["kick_interval_sec"] < 60:
        raise ValueError("kick_interval_sec must be at least 60.")
    validate_layout_config(config["facecam"], config["layout"])
    return config


def _chat_excerpt(log_path: Path, spike_time: float, window_sec: float = 30) -> str:
    lines: list[str] = []
    if not log_path.is_file():
        return ""
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    stamp = float(line.split("\t", 1)[0])
                except (ValueError, IndexError):
                    continue
                if spike_time - window_sec <= stamp <= spike_time + window_sec:
                    parts = line.rstrip("\n").split("\t", 2)
                    if len(parts) >= 3:
                        lines.append(f"{parts[1]}: {parts[2]}")
    except OSError:
        return ""
    if len(lines) > 25:
        lines = lines[:12] + ["..."] + lines[-12:]
    return "\n".join(lines)


class ChannelRunner:
    """One channel: rolling recorder + chat monitor + spike watcher."""

    def __init__(
        self,
        channel: dict,
        config: dict,
        encode_queue: queue.Queue,
        stop: threading.Event,
    ) -> None:
        self.channel = channel["name"]
        self.platform = channel.get("platform", "twitch")
        self.config = config
        self.encode_queue = encode_queue
        self.stop = stop

        dir_name = self.channel if self.platform == "twitch" else f"kick_{self.channel}"
        self.recordings_dir = Path(config["recordings_dir"]) / dir_name / "segments"
        self.clips_dir = Path(config["clips_dir"]) / dir_name
        self.logs_dir = Path(config["logs_dir"]) / dir_name
        for directory in (self.recordings_dir, self.clips_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.index = SegmentIndex(
            self.recordings_dir,
            config["keep_sec"],
            segment_sec=config["segment_sec"],
        )
        self.recorder = RollingRecorder(
            self.channel,
            self.recordings_dir,
            self.logs_dir,
            segment_sec=config["segment_sec"],
            keep_sec=config["keep_sec"],
            platform=self.platform,
            index=self.index,
        )
        self.chat_log = self.logs_dir / "chat.log"
        # Kick has no Twitch IRC feed; those channels use timed clips instead.
        self.monitor = (
            ChatMonitor(self.channel, self.chat_log)
            if self.platform == "twitch"
            else None
        )
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.recorder.start()
        if self.monitor is not None:
            self.monitor.start()
        target = self._watch if self.monitor is not None else self._timed_watch
        self._thread = threading.Thread(
            target=target, name=f"watcher-{self.platform}-{self.channel}", daemon=True
        )
        self._thread.start()

    def stop_all(self) -> None:
        self.stop.set()
        if self.monitor is not None:
            self.monitor.stop()
        self.recorder.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _timed_watch(self) -> None:
        """Kick fallback: cut a fixed window every kick_interval_sec."""
        interval = self.config["kick_interval_sec"]
        LOGGER.info(
            "Timed clipping for kick:%s every %ss", self.channel, interval
        )
        while not self.stop.wait(interval):
            try:
                moment = time.time() - self.config["post_sec"]
                self._handle_spike(Spike(moment, moment + 10, 0), timed=True)
            except Exception:
                LOGGER.exception("Timed clip failed for kick:%s", self.channel)

    def _watch(self) -> None:
        last_handled = -math.inf
        try:
            for spike in watch_spikes(
                self.chat_log,
                self.config["spike_window_sec"],
                self.config["spike_multiplier"],
                baseline_sec=self.config["spike_baseline_sec"],
                min_messages=self.config["spike_min_messages"],
                poll_sec=2.0,
                settle_sec=self.config["post_sec"] + 5,
                stop_event=self.stop,
            ):
                if spike.start_time - last_handled < self.config["spike_cooldown_sec"]:
                    continue
                last_handled = spike.start_time
                try:
                    self._handle_spike(spike)
                except Exception:  # keep the watcher alive
                    LOGGER.exception("Failed to handle spike on #%s", self.channel)
        except Exception:
            LOGGER.exception("Spike watcher for #%s crashed", self.channel)

    def _handle_spike(self, spike: Spike, *, timed: bool = False) -> None:
        pre, post = self.config["pre_sec"], self.config["post_sec"]
        segments = self.index.segments_for_window(
            spike.start_time - pre - 4, spike.start_time + post + 4
        )
        if not segments:
            LOGGER.warning("Spike on #%s but no segments cover it yet.", self.channel)
            return

        first_seen = self.index.first_seen(segments[0]) or (spike.start_time - pre)
        stamp = datetime.fromtimestamp(spike.start_time).strftime("%Y%m%d_%H%M%S")
        staged = self.clips_dir / f"stage_{stamp}.ts"
        raw_path = self.clips_dir / f"raw_{stamp}.mp4"
        concat_segments(segments, staged)
        cut_clip(
            staged,
            spike.start_time,
            raw_path,
            pre_sec=pre,
            post_sec=post,
            recording_start=first_seen,
        )
        staged.unlink(missing_ok=True)
        self.encode_queue.put(
            {
                "channel": self.channel,
                "spike_time": spike.start_time,
                "raw_path": str(raw_path),
                "message_count": spike.message_count,
                "chat_excerpt": _chat_excerpt(
                    self.chat_log, spike.start_time, pre + post
                ),
            }
        )
        label = "Timed clip" if timed else "spike"
        LOGGER.info(
            "Queued %s for #%s at %s (%s messages).",
            label,
            self.channel,
            datetime.fromtimestamp(spike.start_time).strftime("%H:%M:%S"),
            spike.message_count,
        )


class EncodeWorker:
    """Turns raw clips into vertical MP4s, then files them for review."""

    def __init__(
        self,
        queue_in: queue.Queue,
        store: ReviewStore,
        config: dict,
        stop: threading.Event,
    ) -> None:
        self.queue_in = queue_in
        self.store = store
        self.config = config
        self.stop = stop
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="encoder", daemon=True)
        self._thread.start()

    def stop_all(self) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                job = self.queue_in.get(timeout=1)
            except queue.Empty:
                continue
            try:
                raw = Path(job["raw_path"])
                vertical = raw.with_name(raw.name.replace("raw_", "vertical_", 1))
                to_vertical(raw, vertical)
                platforms = self.config["publish"].get("platforms") or []
                clip_id = self.store.add(
                    job["channel"],
                    job["spike_time"],
                    raw,
                    vertical,
                    chat_excerpt=job.get("chat_excerpt", ""),
                    title=f"{job['channel']} - {datetime.fromtimestamp(job['spike_time']):%H:%M:%S}",
                    platforms=list(platforms) if isinstance(platforms, list) else [],
                )
                LOGGER.info("Clip %s ready for review: %s", clip_id, vertical)
            except ClipperError:
                LOGGER.exception("Encode failed for %s", job.get("raw_path"))
            except Exception:
                LOGGER.exception("Unexpected encode error")
            finally:
                self.queue_in.task_done()


class RenderWorker:
    """Re-renders approved clips into the split layout before publishing.

    Approval is the trigger, but rendering happens off the HTTP request thread.
    The original vertical clip stays in the record as a fallback: if the split
    render fails, the record is flagged ``edit_status: fallback`` and the
    publisher uploads the vertical clip instead.
    """

    def __init__(
        self,
        queue_in: queue.Queue,
        queue_out: queue.Queue,
        store: ReviewStore,
        config: dict,
        stop: threading.Event,
    ) -> None:
        self.queue_in = queue_in
        self.queue_out = queue_out
        self.store = store
        self.config = config
        self.stop = stop
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Approvals from an earlier --no-publish run still get rendered.
        for record in self.store.list():
            if record.get("status") == "approved":
                self.queue_in.put(record["id"])
        self._thread = threading.Thread(target=self._run, name="renderer", daemon=True)
        self._thread.start()

    def stop_all(self) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                clip_id = self.queue_in.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._render(clip_id)
            except Exception as exc:
                self.store.update(
                    clip_id, edited_path="", edit_status="fallback", edit_error=str(exc)[:1000]
                )
                LOGGER.exception(
                    "Split render failed for %s; falling back to the vertical clip", clip_id
                )
                self.queue_out.put(clip_id)
            finally:
                self.queue_in.task_done()

    def _render(self, clip_id: str) -> None:
        record = self.store.get(clip_id)
        if record is None or record.get("status") != "approved":
            return
        layout_cfg = self.config["layout"]
        raw = Path(record.get("raw_path") or "")
        if not raw.is_file() or raw.stat().st_size == 0:
            raise ClipperError(f"Raw clip is missing for {clip_id}: {raw}")
        edited = raw.with_name(raw.name.replace("raw_", "edited_", 1))

        hook_text = None
        if layout_cfg.get("hook_enabled"):
            template = str(layout_cfg.get("hook_template") or "{title}")
            try:
                hook_text = template.format(
                    title=record.get("title", ""),
                    channel=record.get("channel", ""),
                    spike_time=record.get("spike_time", ""),
                )
            except (KeyError, ValueError):
                hook_text = str(record.get("title") or "")

        to_split_vertical(
            raw,
            edited,
            facecam_box_for(self.config["facecam"], record.get("channel", "")),
            top_px=int(layout_cfg.get("top_px", 672)),
            bottom_px=int(layout_cfg.get("bottom_px", 1248)),
            gameplay_box=layout_cfg.get("gameplay_box"),
            hook_text=hook_text,
        )
        self.store.update(
            clip_id,
            edited_path=str(edited.resolve()),
            edit_status="done",
            edit_error="",
        )
        self.queue_out.put(clip_id)
        LOGGER.info("Split render for %s ready: %s", clip_id, edited)


class PublishWorker:
    """Publishes clips that a human approved (and that were rendered)."""

    def __init__(
        self,
        queue_in: queue.Queue,
        store: ReviewStore,
        publisher,
        stop: threading.Event,
    ) -> None:
        self.queue_in = queue_in
        self.store = store
        self.publisher = publisher
        self.stop = stop
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Clips approved during a --no-publish run should still go out once
        # publishing is enabled, even though their approval happened earlier.
        for record in self.store.list():
            if record.get("status") == "approved" and not record.get("posts"):
                self.queue_in.put(record["id"])
        self._thread = threading.Thread(target=self._run, name="publisher", daemon=True)
        self._thread.start()

    def stop_all(self) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                clip_id = self.queue_in.get(timeout=1)
            except queue.Empty:
                continue
            record = self.store.get(clip_id)
            if record is None or record.get("status") != "approved":
                self.queue_in.task_done()
                continue
            try:
                result = self.publisher.publish(record)
                self.store.update(clip_id, status="published", posts=result, error="")
                LOGGER.info("Published %s: %s", clip_id, result)
            except Exception as exc:
                self.store.update(clip_id, status="error", error=str(exc)[:2000])
                LOGGER.exception("Publish failed for %s", clip_id)
            finally:
                self.queue_in.task_done()


def run(config: dict, *, no_publish: bool = False) -> int:
    stop = threading.Event()
    store = ReviewStore(config["review_dir"])
    encode_queue: queue.Queue = queue.Queue()
    render_queue: queue.Queue = queue.Queue()
    publish_queue: queue.Queue = queue.Queue()

    publisher = None
    if not no_publish and config["publish"].get("enabled", True):
        publisher = Publisher(config["publish"])

    def on_approve(record: dict) -> None:
        render_queue.put(record["id"])

    runners = [
        ChannelRunner(channel, config, encode_queue, stop)
        for channel in config["channels"]
    ]
    encoders = [
        EncodeWorker(encode_queue, store, config, stop)
        for _ in range(config["encode_workers"])
    ]
    renderer = RenderWorker(render_queue, publish_queue, store, config, stop)
    publisher_worker = PublishWorker(publish_queue, store, publisher, stop) if publisher else None

    for runner in runners:
        runner.start()
    for encoder in encoders:
        encoder.start()
    renderer.start()
    if publisher_worker is not None:
        publisher_worker.start()

    web_thread = threading.Thread(
        target=serve,
        kwargs={
            "store": store,
            "port": int(config["publish"].get("review_port", 8765)),
            "on_approve": on_approve,
            "allowed_roots": [config["clips_dir"], config["review_dir"]],
        },
        name="review-ui",
        daemon=True,
    )
    web_thread.start()

    LOGGER.info(
        "Watching %s channels: %s",
        len(runners),
        ", ".join(
            f"{item['platform']}:{item['name']}" for item in config["channels"]
        ),
    )
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        LOGGER.info("Stopping supervisor.")
    finally:
        stop.set()
        for runner in runners:
            runner.stop_all()
        for encoder in encoders:
            encoder.stop_all()
        renderer.stop_all()
        if publisher_worker is not None:
            publisher_worker.stop_all()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="run capture/review only; never call SupoClip",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return run(load_config(args.config), no_publish=args.no_publish)
    except (OSError, ValueError, TypeError, PublisherError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
