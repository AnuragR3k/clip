#!/usr/bin/env python3
"""Split-screen vertical renderer and facecam crop helpers.

The original ``clipper.to_vertical()`` (blurred background, centered 16:9) is
left untouched. This module renders a *hard split*: the facecam crop fills the
top of a 1080x1920 frame and the gameplay crop fills the bottom, both with
"cover" scaling (no letterboxing, no blurred padding).

A crop box may be given as absolute pixels (integers or floats > 1) or as
0..1 fractions of the source frame (floats in 0..1). Fractions are convenient
in config.yaml because different Twitch sources can have different resolution.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from clipper import ClipperError, _run_ffmpeg, get_ffmpeg

LOGGER = logging.getLogger(__name__)

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
DEFAULT_TOP_PX = 672
DEFAULT_BOTTOM_PX = 1248

# Starting points, taken from real frames of each channel on 2026-11-18.
# Fractions of the 1920x1080 source: webcam in top-right (jasontheween),
# top-left (xqc, lacy). Tune with `python layout.py preview ...`.
DEFAULT_FACECAM_BOX = {"x": 0.78, "y": 0.0, "w": 0.22, "h": 0.27}
DEFAULT_FACECAM_CHANNELS: dict[str, dict[str, float]] = {
    "jasontheween": {"x": 0.785, "y": 0.0, "w": 0.215, "h": 0.264},
    "xqc": {"x": 0.0, "y": 0.042, "w": 0.193, "h": 0.222},
    "lacy": {"x": 0.0, "y": 0.0, "w": 0.276, "h": 0.278},
}

BOX_KEYS = ("x", "y", "w", "h")
Rect = tuple[int, int, int, int]  # x, y, w, h in source pixels

ASSETS_DIR = Path(__file__).with_name("assets")
PLATFORM_BRAND = {
    "twitch": {"logo": "twitch.svg", "url": "twitch.tv"},
    "kick": {"logo": "kick.svg", "url": "kick.com"},
}


def brand_for(platform: str, channel: str) -> dict[str, str] | None:
    """Return logo path and channel handle for a platform, if known."""
    entry = PLATFORM_BRAND.get(str(platform).strip().lower())
    if entry is None:
        return None
    logo = ASSETS_DIR / entry["logo"]
    if not logo.is_file():
        LOGGER.warning("Brand logo missing: %s", logo)
        return None
    return {"logo": str(logo), "handle": f"{entry['url']}/{channel}"}


def get_dimensions(path: str | Path) -> tuple[int, int]:
    """Return (width, height) of the first video stream via ffprobe."""
    configured = os.environ.get("FFPROBE_BIN")
    probe = shutil.which(configured) if configured else shutil.which("ffprobe")
    if not probe:
        raise ClipperError(
            "ffprobe executable not found. Install ffmpeg (which provides "
            "ffprobe) or set FFPROBE_BIN."
        )
    command = [
        probe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClipperError(f"Could not probe {path}: {exc}") from exc
    if result.returncode != 0:
        raise ClipperError(f"ffprobe failed for {path}: {result.stderr.strip()[:500]}")
    match = re.search(r"(\d+)x(\d+)", result.stdout)
    if not match:
        raise ClipperError(f"ffprobe reported no video dimensions for {path}")
    return int(match.group(1)), int(match.group(2))


def _coordinate(value: Any, total: int, *, is_size: bool = False) -> int:
    """Convert one crop coordinate to pixels.

    Integers are always absolute pixels. Floats in 0..1 are fractions of the
    source dimension; other floats are absolute pixels.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Invalid crop coordinate: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid crop coordinate: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Invalid crop coordinate: {value!r}")
    if number < 0 or (is_size and number == 0):
        raise ValueError("Crop coordinates must be non-negative and sizes positive.")
    if isinstance(value, float) and 0.0 <= number <= 1.0:
        number *= total
    pixels = round(number)
    return max(0, min(total, pixels))


def normalize_box(box: Mapping[str, Any] | Sequence[Any], width: int, height: int) -> Rect:
    """Turn a config crop box into integer pixels ``(x, y, w, h)``."""
    if isinstance(box, Mapping):
        missing = [key for key in BOX_KEYS if key not in box]
        if missing:
            raise ValueError(f"Crop box is missing {', '.join(missing)}: {box!r}")
        values = [box[key] for key in BOX_KEYS]
    elif isinstance(box, Sequence) and len(box) == 4:
        values = list(box)
    else:
        raise ValueError("Crop box must be {x,y,w,h} or a 4-item sequence.")

    x = _coordinate(values[0], width)
    y = _coordinate(values[1], height)
    w = _coordinate(values[2], width, is_size=True)
    h = _coordinate(values[3], height, is_size=True)
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


def validate_box(box: Any) -> None:
    """Check a config crop box without knowing the source dimensions."""
    if isinstance(box, Mapping):
        missing = [key for key in BOX_KEYS if key not in box]
        if missing:
            raise ValueError(f"Crop box is missing {', '.join(missing)}: {box!r}")
        values = [box[key] for key in BOX_KEYS]
    elif isinstance(box, Sequence) and len(box) == 4:
        values = list(box)
    else:
        raise ValueError("Crop box must be {x,y,w,h} or a 4-item sequence.")
    for value, is_size in zip(values, (False, False, True, True)):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise TypeError(f"Invalid crop coordinate: {value!r}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid crop coordinate: {value!r}") from exc
        if not math.isfinite(number) or number < 0 or (is_size and number == 0):
            raise ValueError(f"Invalid crop coordinate: {value!r}")

    def is_fraction(value: Any, *, is_size: bool) -> bool:
        return isinstance(value, float) and 0.0 <= value <= 1.0 and not (
            is_size and value == 0.0
        )

    if (
        is_fraction(values[0], is_size=False)
        and is_fraction(values[2], is_size=True)
        and values[0] + values[2] > 1.0
    ):
        raise ValueError("Facecam crop extends past the right edge.")
    if (
        is_fraction(values[1], is_size=False)
        and is_fraction(values[3], is_size=True)
        and values[1] + values[3] > 1.0
    ):
        raise ValueError("Facecam crop extends past the bottom edge.")


def validate_layout_config(facecam: Any, layout: Any) -> None:
    """Validate the ``facecam:`` and ``layout:`` sections of config.yaml."""
    if not isinstance(facecam, Mapping):
        raise TypeError("`facecam:` in config.yaml must be a mapping.")
    if "default" in facecam:
        validate_box(facecam["default"])
    channels = facecam.get("channels") or {}
    if not isinstance(channels, Mapping):
        raise TypeError("`facecam.channels:` must be a mapping of channel names.")
    for channel, box in channels.items():
        if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", str(channel)):
            raise ValueError(f"Invalid channel name in facecam config: {channel!r}")
        validate_box(box)

    if not isinstance(layout, Mapping):
        raise TypeError("`layout:` in config.yaml must be a mapping.")
    top = int(layout.get("top_px", DEFAULT_TOP_PX))
    bottom = int(layout.get("bottom_px", DEFAULT_BOTTOM_PX))
    if top <= 0 or bottom <= 0 or top + bottom != OUTPUT_HEIGHT:
        raise ValueError(
            f"layout.top_px + layout.bottom_px must equal {OUTPUT_HEIGHT} "
            f"(got {top} + {bottom})."
        )
    if "gameplay_box" in layout and layout["gameplay_box"] is not None:
        validate_box(layout["gameplay_box"])


def facecam_box_for(facecam: Mapping[str, Any] | None, channel: str) -> dict[str, Any]:
    """Return the crop box configured for ``channel`` (or the default)."""
    facecam = facecam or {}
    channels = facecam.get("channels") or {}
    if isinstance(channels, Mapping) and channel in channels:
        return dict(channels[channel])
    return dict(facecam.get("default") or DEFAULT_FACECAM_BOX)


def _scale_branch(box: Mapping[str, Any] | Sequence[Any], size: int, label: str, source_size: tuple[int, int], input_label: str = "0:v") -> str:
    x, y, w, h = normalize_box(box, *source_size)
    return (
        f"[{input_label}]crop={w}:{h}:{x}:{y},"
        f"scale={OUTPUT_WIDTH}:{size}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{size},setsar=1[{label}]"
    )


def _escape_filter_path(path: Path) -> str:
    return re.sub(r"([\\':,\[\]])", r"\\\1", str(path))


def _ass_style(text: str, top_px: int) -> str:
    """Minimal ASS subtitle with bold white text and a black outline."""
    from textwrap import wrap

    safe = str(text).replace("\n", " ")
    lines = wrap(safe, width=34) or [safe]
    safe_lines = [
        line.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        for line in lines
    ]
    body = r"\N".join(safe_lines)
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {OUTPUT_WIDTH}\nPlayResY: {OUTPUT_HEIGHT}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Hook,DejaVu Sans,58,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&H90000000,-1,0,0,0,100,100,0,0,1,4,2,2,40,40,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,9:59:59.99,Hook,,0,0,0,,"
        f"{{\\an8\\pos({OUTPUT_WIDTH // 2},{top_px + 24})}}{body}\n"
    )


def _font_path() -> str:
    """Find a bold font for the platform handle."""
    configured = os.environ.get("DRAWTEXT_FONT")
    candidates = [
        configured,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise ClipperError(
        "No bold font found for the brand overlay; set DRAWTEXT_FONT to a .ttf file."
    )


def _drawtext_handle(handle: str) -> str:
    """Escape a handle for ffmpeg's drawtext filter."""
    escaped = (
        handle.replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
    )
    return (
        f"drawtext=fontfile='{_escape_filter_path(Path(_font_path()))}':"
        f"text='{escaped}':fontsize=30:fontcolor=0xF2F5F1:"
        "x=1080-tw-112:y=30:box=1:boxcolor=0x99000000:boxborderw=10"
    )


def build_filter(
    source_size: tuple[int, int],
    facecam_box: Mapping[str, Any] | Sequence[Any],
    gameplay_box: Mapping[str, Any] | Sequence[Any] | None,
    top_px: int,
    bottom_px: int,
    *,
    hook_text: str | None = None,
    ass_path: Path | None = None,
    caption_ass: Path | None = None,
    brand_logo: Path | None = None,
    brand_handle: str | None = None,
    logo_size: int = 64,
) -> str:
    """Build the ffmpeg filter_complex for the hard split layout.

    Optional overlays, applied in order: platform logo + handle at the top
    right, timed speech captions at the bottom, and a static hook title.
    """
    width, height = source_size
    game = gameplay_box if gameplay_box is not None else {"x": 0, "y": 0, "w": width, "h": height}
    source_input = "1:v" if brand_logo is not None else "0:v"
    parts = [
        _scale_branch(facecam_box, top_px, "cam", source_size, source_input),
        _scale_branch(game, bottom_px, "game", source_size, source_input),
        "[cam][game]vstack=inputs=2,format=yuv420p[v]",
    ]
    current = "[v]"

    if brand_logo is not None:
        parts.append(
            f"[0:v]scale={logo_size}:{logo_size}:flags=lanczos[logo]"
        )
        parts.append(
            f"{current}[logo]overlay=x={OUTPUT_WIDTH - logo_size - 32}:y=28:"
            "format=auto[brand]"
        )
        current = "[brand]"
    if brand_handle:
        parts.append(f"{current}{_drawtext_handle(brand_handle)}[txt]")
        current = "[txt]"
    if caption_ass is not None:
        parts.append(
            f"{current}ass=filename={_escape_filter_path(caption_ass)}[cap]"
        )
        current = "[cap]"
    if hook_text:
        if ass_path is None:
            raise ValueError("ass_path is required when hook_text is set.")
        parts.append(f"{current}ass=filename={_escape_filter_path(ass_path)}[out]")
        current = "[out]"
    parts.append(f"{current}null[vout]")
    return ";".join(parts)


def to_split_vertical(
    raw_path: str | Path,
    output_path: str | Path,
    facecam_box: Mapping[str, Any] | Sequence[Any],
    *,
    top_px: int = DEFAULT_TOP_PX,
    bottom_px: int = DEFAULT_BOTTOM_PX,
    gameplay_box: Mapping[str, Any] | Sequence[Any] | None = None,
    hook_text: str | None = None,
    caption_ass: str | Path | None = None,
    brand_logo: str | Path | None = None,
    brand_handle: str | None = None,
    logo_size: int = 64,
) -> Path:
    """Render a 1080x1920 hard-split clip: facecam on top, gameplay below.

    ``facecam_box`` and ``gameplay_box`` accept pixel values or 0..1 source
    fractions. Gameplay defaults to the full source frame: after "cover"
    scaling to 1080x1248 the center crop naturally excludes corner webcams, so
    the gameplay area stays clean without a second manual crop. Set
    ``gameplay_box`` explicitly if the action is off-center.

    ``caption_ass`` burns timed subtitles, and ``brand_logo``/``brand_handle``
    add the platform mark and ``twitch.tv/<channel>`` (or Kick equivalent) at
    the top right.
    """
    source = Path(raw_path)
    if not source.is_file() or source.stat().st_size == 0:
        raise ClipperError(f"Input clip does not exist or is empty: {source}")
    if top_px <= 0 or bottom_px <= 0 or top_px + bottom_px != OUTPUT_HEIGHT:
        raise ValueError(
            f"top_px + bottom_px must be {OUTPUT_HEIGHT} (got {top_px}+{bottom_px})."
        )

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    size = get_dimensions(source)
    validate_box(facecam_box)
    if gameplay_box is not None:
        validate_box(gameplay_box)

    ass_file: Path | None = None
    caption_file: Path | None = None
    try:
        if hook_text:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".ass",
                prefix=".hook_",
                dir=output.parent,
                delete=False,
                encoding="utf-8",
            ) as handle:
                ass_file = Path(handle.name)
                handle.write(_ass_style(hook_text, top_px))
        if caption_ass is not None:
            caption_file = Path(caption_ass)
            if not caption_file.is_file():
                raise ClipperError(f"Caption file is missing: {caption_file}")

        logo = Path(brand_logo) if brand_logo is not None else None
        if logo is not None and not logo.is_file():
            raise ClipperError(f"Brand logo is missing: {logo}")

        filter_complex = build_filter(
            size,
            facecam_box,
            gameplay_box,
            top_px,
            bottom_px,
            hook_text=hook_text,
            ass_path=ass_file,
            caption_ass=caption_file,
            brand_logo=logo,
            brand_handle=brand_handle,
            logo_size=logo_size,
        )
        command = [get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
        if logo is not None:
            command += ["-i", str(logo)]
        command += [
            "-i",
            str(source),
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            f"{1 if logo is not None else 0}:a?",
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
        _run_ffmpeg(command, f"rendering split layout for {source.name}")
    finally:
        if ass_file is not None:
            ass_file.unlink(missing_ok=True)

    if not output.is_file() or output.stat().st_size == 0:
        raise ClipperError(f"ffmpeg produced no split clip at {output}")
    LOGGER.info("Split vertical clip: %s", output)
    return output


def extract_preview(
    clip_path: str | Path,
    output_path: str | Path,
    *,
    at_sec: float = 2.0,
) -> Path:
    """Extract a single still frame from a clip for crop tuning."""
    source = Path(clip_path)
    if not source.is_file() or source.stat().st_size == 0:
        raise ClipperError(f"Clip does not exist or is empty: {source}")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    if at_sec > 0:
        command += ["-ss", f"{at_sec:.3f}"]
    command += ["-i", str(source), "-frames:v", "1"]
    if output.suffix.lower() in {".jpg", ".jpeg"}:
        command += ["-vf", "format=yuvj420p", "-q:v", "3"]
    command.append(str(output))
    _run_ffmpeg(command, f"extracting preview from {source.name}")
    if not output.is_file() or output.stat().st_size == 0:
        raise ClipperError(f"ffmpeg produced no preview at {output}")
    return output


def suggest_facecam_box(image_path: str | Path) -> str:
    """Optional OpenCV face-detection hint; cv2 is never a hard dependency."""
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError:
        return (
            "OpenCV is not installed; inspect the preview still manually. "
            "No dependency was added for this optional helper."
        )
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(str(cascade_path))
    image = cv2.imread(str(image_path))
    if image is None:
        return f"Could not read {image_path} with OpenCV."
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    if len(faces) == 0:
        return "No face found by OpenCV; inspect the still manually."
    x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
    height, width = gray.shape[:2]
    pad_x, pad_y = int(w * 0.25), int(h * 0.35)
    x = max(0, x - pad_x)
    y = max(0, y - pad_y)
    w = min(width - x, w + 2 * pad_x)
    h = min(height - y, h + 2 * pad_y)
    return (
        "OpenCV suggestion (fractions of source): "
        f"{{x: {x / width:.3f}, y: {y / height:.3f}, "
        f"w: {w / width:.3f}, h: {h / height:.3f}}}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview", help="extract a still frame for crop tuning")
    preview.add_argument("clip", help="saved .mp4 clip")
    preview.add_argument("--out", help="output image (.jpg or .png)")
    preview.add_argument("--at", type=float, default=2.0, help="seconds into the clip")
    preview.add_argument(
        "--suggest", action="store_true", help="optional OpenCV face-detection hint"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.command == "preview":
            out = Path(args.out) if args.out else Path(args.clip).with_suffix(".preview.jpg")
            still = extract_preview(args.clip, out, at_sec=args.at)
            print(f"Preview: {still}")
            if args.suggest:
                print(suggest_facecam_box(still))
            return 0
    except (ClipperError, ValueError, TypeError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
