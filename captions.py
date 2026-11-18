#!/usr/bin/env python3
"""Burned-in speech subtitles using a local faster-whisper model.

No API key or network service is needed after the model has been downloaded
once. ``tiny`` is the default: it is the cheapest model that still produces
usable word boundaries on short gameplay clips.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from textwrap import wrap

from clipper import ClipperError, get_ffmpeg

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def _decode_audio(clip_path: Path) -> object:
    """Decode mono 16 kHz PCM with ffmpeg into a numpy float array.

    Passing an array to faster-whisper avoids its PyAV decoder, which is
    incompatible with some newer PyAV releases.
    """
    import numpy as np

    command = [
        get_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(clip_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "f32le",
        "pipe:1",
    ]
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise ClipperError(f"Audio decode failed: {result.stderr.decode('utf-8', 'replace')[-1000:]}")
    return np.frombuffer(result.stdout, dtype="<f4").copy()


def transcribe(
    clip_path: str | Path,
    *,
    model_size: str = "tiny",
    language: str | None = None,
    device: str = "cpu",
) -> list[Cue]:
    """Return timed transcript cues for a clip's audio."""
    source = Path(clip_path)
    if not source.is_file() or source.stat().st_size == 0:
        raise ClipperError(f"Clip does not exist or is empty: {source}")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ClipperError(
            "faster-whisper is not installed. Run "
            "`.venv/bin/pip install -r requirements.txt` in the clip project."
        ) from None

    LOGGER.info("Transcribing %s with faster-whisper/%s...", source.name, model_size)
    try:
        model = WhisperModel(model_size, device=device, compute_type="int8")
        audio = _decode_audio(source)
        segments, _info = model.transcribe(
            audio,
            language=language,
            vad_filter=True,
            beam_size=1,
        )
        cues: list[Cue] = []
        for segment in segments:
            text = " ".join(str(segment.text).split())
            if not text:
                continue
            cues.append(Cue(float(segment.start), float(segment.end), text))
    except Exception as exc:
        raise ClipperError(f"Transcription failed: {exc}") from exc
    LOGGER.info("Transcript: %s cue(s) from %s", len(cues), source.name)
    return cues


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    # ASS uses centiseconds (H:MM:SS.cc), not milliseconds.
    return f"{int(hours):01d}:{int(minutes):02d}:{secs:05.2f}"


def _escape(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", " ")
    )


def write_ass(
    cues: list[Cue],
    output_path: str | Path,
    *,
    width: int = 1080,
    height: int = 1920,
    bottom_px: int = 1248,
    font_size: int = 50,
    wrap_chars: int = 26,
) -> Path:
    """Write an ASS subtitle file with subtitles at the bottom of the frame."""
    if width <= 0 or height <= 0 or bottom_px <= 0 or bottom_px > height:
        raise ValueError("Invalid caption frame dimensions.")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    margin_v = max(24, height - bottom_px + 24)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"),
        (f"Style: Caption,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,"
        f"&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,{margin_v},1"),
        "",
        "[Events]",
        ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text"),
    ]
    for cue in cues:
        if cue.end <= cue.start:
            continue
        chunks = wrap(" ".join(cue.text.split()), width=wrap_chars) or [cue.text]
        body = r"\N".join(_escape(chunk) for chunk in chunks[:3])
        lines.append(
            f"Dialogue: 0,{_timestamp(cue.start)},{_timestamp(cue.end)},"
            f"Caption,,0,0,{margin_v},,{body}"
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output
