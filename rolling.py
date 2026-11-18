#!/usr/bin/env python3
"""Rolling, segmented live recording with a bounded disk buffer.

Instead of one ever-growing ``.ts`` file, the live stream is written through an
ffmpeg ``segment`` muxer into small numbered MPEG-TS files. A companion index
records when each segment first appeared and prunes anything older than
``keep_sec``, giving a true sliding window of recent video (default 5 minutes).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from clipper import ClipperError, get_ffmpeg
from recorder import RecorderError, resolve_streamlink

LOGGER = logging.getLogger(__name__)

_SEGMENT_RE = re.compile(
    r"^(?P<channel>[A-Za-z0-9_]+)_(?P<stamp>\d{8}_\d{6})_(?P<number>\d{6})\.ts$"
)
_INDEX_NAME = "segments.json"


class SegmentIndex:
    """Thread-safe map of segment filename -> first-seen epoch, with pruning."""

    def __init__(
        self,
        directory: str | Path,
        keep_sec: float,
        segment_sec: float = 10.0,
        check_interval: float = 1.0,
    ):
        self.directory = Path(directory)
        self.keep_sec = float(keep_sec)
        self.segment_sec = float(segment_sec)
        self.check_interval = float(check_interval)
        self._entries: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.directory / _INDEX_NAME

    def load(self) -> None:
        """Load the index from disk, dropping entries whose files vanished."""
        self.directory.mkdir(parents=True, exist_ok=True)
        entries: dict[str, float] = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for name, value in data.items():
                        if isinstance(name, str) and isinstance(value, (int, float)):
                            entries[name] = float(value)
            except (OSError, ValueError):
                LOGGER.warning("Could not read segment index; rebuilding from files.")
        now = time.time()
        for name in sorted(self.directory.glob("*.ts")):
            if name.name not in entries:
                entries[name.name] = now
        with self._lock:
            self._entries = entries
        self._save()

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._entries, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            LOGGER.warning("Could not write segment index: %s", exc)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.load()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="segment-index", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        with self._lock:
            self._save()

    def _run(self) -> None:
        while not self._stop.wait(self.check_interval):
            self.refresh()

    def refresh(self) -> None:
        """Discover new segments and prune ones outside the retention window."""
        now = time.time()
        with self._lock:
            changed = False
            for path in self.directory.glob("*.ts"):
                if path.name not in self._entries:
                    # A segment's first-seen time is close to the moment it was
                    # finalized. Subtract one interval to approximate its start.
                    self._entries[path.name] = now
                    changed = True
            cutoff = now - self.keep_sec
            for name, seen in list(self._entries.items()):
                if seen < cutoff and (self.directory / name).exists():
                    try:
                        (self.directory / name).unlink()
                    except OSError as exc:
                        LOGGER.debug("Could not prune %s: %s", name, exc)
                    del self._entries[name]
                    changed = True
                elif seen < cutoff:
                    del self._entries[name]
                    changed = True
            if changed:
                self._save()

    def segments_for_window(self, start: float, end: float) -> list[Path]:
        """Return segments whose [seen-interval, seen] span overlaps a window."""
        with self._lock:
            items = list(self._entries.items())
        chosen: list[tuple[int, Path]] = []
        for name, seen in items:
            # ``seen`` is approximately the moment ffmpeg opened the segment,
            # i.e. its start. Add slack for keyframe boundaries and polling.
            if seen + self.segment_sec + 2.0 < start or seen - 2.0 > end:
                continue
            path = self.directory / name
            if not path.is_file():
                continue
            match = _SEGMENT_RE.match(name)
            sort_key = (
                (match.group("stamp"), int(match.group("number")))
                if match
                else ("", 0)
            )
            chosen.append((sort_key, path))
        chosen.sort(key=lambda item: item[0])
        return [path for _sort_key, path in chosen]

    def first_seen(self, path: str | Path) -> float | None:
        with self._lock:
            return self._entries.get(Path(path).name)


def concat_segments(segments: list[Path], output_path: str | Path, ffmpeg: str | None = None) -> Path:
    """Concatenate MPEG-TS segments into a single file without re-encoding."""
    if not segments:
        raise ClipperError("No segments to concatenate.")
    binary = ffmpeg or get_ffmpeg()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    list_path = output.with_suffix(".concat.txt")
    list_path.write_text(
        "".join(f"file '{path.resolve()}'\n" for path in segments), encoding="utf-8"
    )
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(output),
    ]
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClipperError(f"Could not concatenate segments: {exc}") from exc
    if result.returncode != 0:
        raise ClipperError(f"ffmpeg concat failed: {result.stderr[-1500:]}")
    if not output.is_file() or output.stat().st_size == 0:
        raise ClipperError("ffmpeg concat produced an empty file.")
    return output


class RollingRecorder:
    """Record a channel into rolling segments while it is live."""

    def __init__(
        self,
        channel: str,
        segments_dir: str | Path,
        logs_dir: str | Path,
        *,
        segment_sec: int = 10,
        keep_sec: int = 300,
        check_interval: int = 60,
        platform: str = "twitch",
        index: SegmentIndex | None = None,
    ) -> None:
        self.channel = channel.lstrip("#")
        self.platform = platform.strip().lower()
        self.url = (
            f"kick.com/{self.channel}"
            if self.platform == "kick"
            else f"twitch.tv/{self.channel}"
        )
        self.segments_dir = Path(segments_dir)
        self.logs_dir = Path(logs_dir)
        self.segment_sec = int(segment_sec)
        self.keep_sec = int(keep_sec)
        self.check_interval = int(check_interval)
        self.index = index or SegmentIndex(
            self.segments_dir, self.keep_sec, segment_sec=self.segment_sec
        )

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._processes: list[subprocess.Popen[bytes]] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.index.start()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"roller-{self.channel}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                self._signal_group(process, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
        with self._lock:
            self._processes = []
        self.index.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        try:
            streamlink = resolve_streamlink()
            ffmpeg = get_ffmpeg()
        except (RecorderError, ClipperError) as exc:
            LOGGER.error("%s", exc)
            return
        LOGGER.info("Rolling recorder started for #%s", self.channel)
        while not self._stop.is_set():
            if not self._is_live(streamlink):
                self._stop.wait(self.check_interval)
                continue
            self._record_once(streamlink, ffmpeg)
            if self._stop.wait(self.check_interval):
                break

    def _is_live(self, streamlink: str) -> bool:
        command = [streamlink, "--stream-url", self.url, "best"]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOGGER.warning("Live check for #%s failed: %s", self.channel, exc)
            return False
        if result.returncode != 0:
            return False
        return (result.stdout or "").strip().startswith(("http://", "https://"))

    def _record_once(self, streamlink: str, ffmpeg: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pattern = self.segments_dir / f"{self.channel}_{stamp}_%06d.ts"
        log_path = self.logs_dir / f"streamlink_{self.channel}.log"
        try:
            log_file = log_path.open("a", encoding="utf-8", buffering=1)
        except OSError:
            log_file = subprocess.DEVNULL

        producer = subprocess.Popen(
            [streamlink, self.url, "best", "-O"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=log_file,
            start_new_session=True,
        )
        consumer = subprocess.Popen(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                "pipe:0",
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                str(self.segment_sec),
                "-segment_start_number",
                "0",
                "-segment_format",
                "mpegts",
                str(pattern),
            ],
            stdin=producer.stdout,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
        )
        if producer.stdout is not None:
            producer.stdout.close()
        with self._lock:
            self._processes = [producer, consumer]
        LOGGER.info("Recording #%s into rolling segments at %s", self.channel, self.segments_dir)
        consumer.wait()
        if producer.poll() is None:
            self._signal_group(producer, signal.SIGTERM)
            try:
                producer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._signal_group(producer, signal.SIGKILL)
        with self._lock:
            self._processes = []
        if log_file is not subprocess.DEVNULL:
            try:
                log_file.close()
            except OSError:
                pass

    @staticmethod
    def _signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass
