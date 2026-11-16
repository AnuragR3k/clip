#!/usr/bin/env python3
"""Record a single Twitch channel with Streamlink.

The recorder runs in a background thread. It asks Streamlink whether the
channel is live, then launches a normal ``streamlink`` recording process and
waits for it to end before checking again.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"_(\d{8}_\d{6})\.ts$")


class RecorderError(RuntimeError):
    """Raised when recording cannot be started."""


def resolve_streamlink() -> str:
    """Return the Streamlink executable, or raise a useful error."""
    binary = os.environ.get("STREAMLINK_BIN") or shutil.which("streamlink")
    if not binary:
        raise RecorderError(
            "streamlink executable not found. Install it with "
            "`pip install streamlink` and make sure it is on PATH."
        )
    return binary


def recording_start_epoch(path: str | Path) -> float | None:
    """Read the recording start time from ``<channel>_<YYYYmmdd_HHMMSS>.ts``.

    Returns a Unix timestamp, or ``None`` when the filename does not follow
    the expected convention.
    """
    match = _FILENAME_RE.search(Path(path).name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return None


def latest_recording(recordings_dir: str | Path) -> Path | None:
    """Return the most recently written ``.ts`` recording, if any."""
    directory = Path(recordings_dir)
    if not directory.is_dir():
        return None

    files: list[tuple[float, Path]] = []
    for path in directory.glob("*.ts"):
        if not path.is_file():
            continue
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not files:
        return None
    return max(files, key=lambda item: item[0])[1]


def find_recording_for_spike(
    recordings_dir: str | Path, spike_timestamp: float
) -> Path | None:
    """Pick the newest recording that contains a wall-clock spike time.

    Normally this is the current file. If the stream restarted between the
    spike and clipping, fall back to the previous recording that still covers
    the spike. ``latest_recording`` is the final fallback.
    """
    directory = Path(recordings_dir)
    if not directory.is_dir():
        return None

    entries: list[tuple[float, float, Path]] = []
    for path in directory.glob("*.ts"):
        if not path.is_file():
            continue
        start = recording_start_epoch(path)
        if start is None:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        entries.append((start, modified, path))

    if not entries:
        return latest_recording(directory)

    covering = [
        entry
        for entry in entries
        if entry[0] <= spike_timestamp <= entry[1]
    ]
    if covering:
        return max(covering, key=lambda entry: entry[0])[2]

    earlier = [entry for entry in entries if entry[0] <= spike_timestamp]
    if earlier:
        return max(earlier, key=lambda entry: entry[0])[2]

    return latest_recording(directory)


class StreamRecorder:
    """Background Streamlink recorder for one channel."""

    def __init__(
        self,
        channel: str,
        recordings_dir: str | Path,
        logs_dir: str | Path,
        check_interval: int = 60,
    ) -> None:
        self.channel = channel.lstrip("#")
        self.recordings_dir = Path(recordings_dir)
        self.logs_dir = Path(logs_dir)
        self.check_interval = check_interval

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_file = None
        self._active_path: Path | None = None
        self._last_live_state: bool | None = None
        self._lock = threading.Lock()

    @property
    def active_path(self) -> Path | None:
        """The file currently being written, if recording is active."""
        with self._lock:
            return self._active_path

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, name="stream-recorder", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the polling loop and terminate Streamlink gracefully."""
        self._stop.set()
        with self._lock:
            process = self._process

        if process is not None and process.poll() is None:
            LOGGER.info("Stopping Streamlink recorder...")
            self._signal_process_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                LOGGER.warning("Streamlink did not stop in time; killing it.")
                self._signal_process_group(process, signal.SIGKILL)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

        self._reap_process()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        try:
            binary = resolve_streamlink()
        except RecorderError as exc:
            LOGGER.error("%s", exc)
            return

        LOGGER.info(
            "Recorder started for %s (polling every %ss).",
            self.channel,
            self.check_interval,
        )

        while not self._stop.is_set():
            with self._lock:
                process = self._process

            if process is not None and process.poll() is None:
                self._stop.wait(2)
                continue

            if process is not None:
                code = process.returncode
                LOGGER.info("Recording process exited (code %s).", code)
                self._reap_process()
                if self._stop.wait(self.check_interval):
                    break
                continue

            live = self._is_live(binary)
            if self._stop.is_set():
                break

            if not live:
                if self._last_live_state is not False:
                    LOGGER.info(
                        "Channel %s is offline; checking again every %ss.",
                        self.channel,
                        self.check_interval,
                    )
                self._last_live_state = False
                self._stop.wait(self.check_interval)
                continue

            self._last_live_state = True
            if self._stop.is_set():
                break
            self._start_streamlink(binary)

        self._reap_process()

    def _is_live(self, binary: str) -> bool:
        """Ask Streamlink for a stream URL without downloading the stream."""
        command = [
            binary,
            "--stream-url",
            f"twitch.tv/{self.channel}",
            "best",
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except subprocess.TimeoutExpired:
            LOGGER.warning("Live check timed out; treating channel as offline.")
            return False
        except OSError as exc:
            LOGGER.warning("Could not run streamlink live check: %s", exc)
            return False

        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            if detail:
                LOGGER.debug("Streamlink live check: %s", detail[-1])
            return False

        url = (result.stdout or "").strip()
        return url.startswith(("http://", "https://"))

    def _start_streamlink(self, binary: str) -> None:
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = self.recordings_dir / f"{self.channel}_{stamp}.ts"
        streamlink_log = self.logs_dir / "streamlink.log"

        try:
            stderr_file = streamlink_log.open("a", encoding="utf-8", buffering=1)
        except OSError as exc:
            LOGGER.warning("Cannot open %s: %s", streamlink_log, exc)
            stderr_file = None

        command = [
            binary,
            f"twitch.tv/{self.channel}",
            "best",
            "-o",
            str(output_path),
        ]

        LOGGER.info("Channel is live; recording to %s", output_path)
        if stderr_file is not None:
            stderr_file.write(
                f"\n=== {datetime.now().isoformat(timespec='seconds')} "
                f"starting {self.channel} ===\n"
            )

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file or subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            LOGGER.error("Failed to start streamlink: %s", exc)
            if stderr_file is not None:
                stderr_file.close()
            self._last_live_state = False
            return

        with self._lock:
            self._process = process
            self._stderr_file = stderr_file
            self._active_path = output_path

    def _reap_process(self) -> None:
        with self._lock:
            process = self._process
            stderr_file = self._stderr_file
            self._process = None
            self._stderr_file = None
            self._active_path = None

        if process is not None and process.poll() is None:
            # This should only happen when stop() did not own the process.
            self._signal_process_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_process_group(process, signal.SIGKILL)

        if stderr_file is not None:
            try:
                stderr_file.close()
            except OSError:
                pass

    @staticmethod
    def _signal_process_group(
        process: subprocess.Popen[bytes], sig: signal.Signals
    ) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass
