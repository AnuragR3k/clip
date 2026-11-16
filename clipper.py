#!/usr/bin/env python3
"""Cut clips from a recording and format them vertically with ffmpeg."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"_(\d{8}_\d{6})\.ts$")

VERTICAL_FILTER = (
    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,boxblur=40:4,setsar=1[bg];"
    "[0:v]scale=1080:-2[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p[v]"
)


class ClipperError(RuntimeError):
    """Raised when ffmpeg cannot produce a clip."""


def get_ffmpeg() -> str:
    """Locate the ffmpeg executable."""
    configured = os.environ.get("FFMPEG_BIN")
    if configured:
        found = shutil.which(configured)
        if found:
            return found
        raise ClipperError(f"FFMPEG_BIN points to a missing executable: {configured}")

    found = shutil.which("ffmpeg")
    if found:
        return found

    # Optional convenience for environments where the static ffmpeg bundled
    # by imageio-ffmpeg is installed. The normal requirement is system ffmpeg.
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable:
            return executable
    except Exception:
        LOGGER.debug("Optional imageio-ffmpeg fallback is unavailable.", exc_info=True)

    raise ClipperError(
        "ffmpeg executable not found. Install ffmpeg (for example "
        "`sudo apt install ffmpeg`) or set FFMPEG_BIN."
    )


def recording_start_epoch(path: str | Path) -> float | None:
    """Infer a recording's start time from its filename, if possible."""
    match = _FILENAME_RE.search(Path(path).name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").timestamp()
    except ValueError:
        return None


def _run_ffmpeg(command: list[str], description: str, timeout: int = 900) -> None:
    LOGGER.debug("Running ffmpeg (%s): %s", description, " ".join(command))
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClipperError(f"ffmpeg timed out while {description}") from exc
    except OSError as exc:
        raise ClipperError(f"Could not run ffmpeg: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        raise ClipperError(f"ffmpeg failed while {description}:\n{detail}")


def cut_clip(
    recording_path: str | Path,
    spike_timestamp: float,
    output_path: str | Path,
    pre_sec: int = 30,
    post_sec: int = 15,
    *,
    recording_start: float | None = None,
) -> Path:
    """Cut ``pre_sec`` before a spike through ``post_sec`` after it.

    ``spike_timestamp`` may be a Unix timestamp (the normal output of the
    chat monitor) or an offset in seconds from the start of the recording.
    For Unix timestamps the recording start is read from the filename, or can
    be supplied explicitly with ``recording_start``.
    """
    if pre_sec < 0 or post_sec < 0:
        raise ValueError("pre_sec and post_sec must not be negative")

    recording = Path(recording_path)
    if not recording.is_file() or recording.stat().st_size == 0:
        raise ClipperError(f"Recording does not exist or is empty: {recording}")

    if spike_timestamp >= 1_000_000_000:
        start_epoch = (
            recording_start
            if recording_start is not None
            else recording_start_epoch(recording)
        )
        if start_epoch is None:
            raise ClipperError(
                "Cannot map the spike time into the recording: the filename has "
                "no start timestamp. Pass recording_start explicitly."
            )
        spike_offset = spike_timestamp - start_epoch
    else:
        spike_offset = spike_timestamp

    if spike_offset < 0:
        raise ClipperError(
            f"Spike occurs {abs(spike_offset):.1f}s before the recording starts."
        )

    cut_start = max(0.0, spike_offset - pre_sec)
    cut_end = spike_offset + post_sec
    duration = cut_end - cut_start
    if duration <= 0:
        raise ClipperError("Clip duration is zero; check pre_sec/post_sec.")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        get_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{cut_start:.3f}",
        "-i",
        str(recording),
        "-t",
        f"{duration:.3f}",
        "-c",
        "copy",
        str(output),
    ]
    _run_ffmpeg(command, f"cutting {recording.name}")

    if not output.is_file() or output.stat().st_size == 0:
        raise ClipperError(f"ffmpeg produced no clip at {output}")

    LOGGER.info(
        "Raw clip: %s (%.1fs from recording offset %.1fs)",
        output,
        duration,
        cut_start,
    )
    return output


def to_vertical(input_path: str | Path, output_path: str | Path) -> Path:
    """Scale a clip into a 1080x1920 vertical frame with a blurred fill."""
    source = Path(input_path)
    if not source.is_file() or source.stat().st_size == 0:
        raise ClipperError(f"Input clip does not exist or is empty: {source}")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        get_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        VERTICAL_FILTER,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ffmpeg(command, f"formatting {source.name} vertically")

    if not output.is_file() or output.stat().st_size == 0:
        raise ClipperError(f"ffmpeg produced no vertical clip at {output}")

    LOGGER.info("Vertical clip: %s", output)
    return output
