#!/usr/bin/env python3
"""Offline acceptance test for the rolling -> cut -> encode -> review pipeline.

No Twitch connection, no SupoClip. Run: .venv/bin/python test_pipeline.py
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import yaml

from clipper import get_ffmpeg, to_vertical
from layout import facecam_box_for, to_split_vertical
from main import _make_synthetic_recording, _write_synthetic_chat_log, find_spikes
from review import ReviewStore, render_page
from rolling import SegmentIndex, concat_segments
from spike_detector import Spike
from supervisor import load_config


def segments_from_recording(recording: Path, segments_dir: Path, channel: str, ffmpeg: str) -> None:
    import subprocess

    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(recording),
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            "10",
            "-segment_format",
            "mpegts",
            str(segments_dir / f"{channel}_20261118_120000_%06d.ts"),
        ],
        check=False,
    )
    assert result.returncode == 0, "segmenting failed"


def main() -> int:
    ffmpeg = get_ffmpeg()
    root = Path(tempfile.mkdtemp(prefix="cliptest_", dir="/tmp/opencode"))
    try:
        (root / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "channels": ["alpha", "beta", "gamma", "delta", "epsilon"],
                    "segment_sec": 10,
                    "keep_sec": 300,
                    "pre_sec": 30,
                    "post_sec": 15,
                    "publish": {"enabled": False, "platforms": ["youtube", "instagram"]},
                    "facecam": {
                        "default": {"x": 0.0, "y": 0.0, "w": 0.3, "h": 0.4},
                        "channels": {
                            "alpha": {"x": 0.7, "y": 0.0, "w": 0.3, "h": 0.4}
                        },
                    },
                    "layout": {"top_px": 672, "bottom_px": 1248},
                }
            ),
            encoding="utf-8",
        )
        config = load_config(root / "config.yaml")
        assert len(config["channels"]) == 5
        assert facecam_box_for(config["facecam"], "alpha") == {
            "x": 0.7,
            "y": 0.0,
            "w": 0.3,
            "h": 0.4,
        }
        assert facecam_box_for(config["facecam"], "unknown") == {
            "x": 0.0,
            "y": 0.0,
            "w": 0.3,
            "h": 0.4,
        }

        recording = root / "source.ts"
        start = time.time() - 180
        _make_synthetic_recording(recording, 180, ffmpeg, record_start=start)
        chat_log = root / "chat.log"
        spike_expected = _write_synthetic_chat_log(
            chat_log, start, 180, 10, 3.0, 3, 15
        )
        spikes = find_spikes(chat_log, 10, 3.0, baseline_sec=300, min_messages=3)
        assert spikes, "no spike detected in synthetic chat"
        spike: Spike = min(spikes, key=lambda s: abs(s.start_time - spike_expected))

        segments_dir = root / "segments"
        segments_dir.mkdir()
        segments_from_recording(recording, segments_dir, "alpha", ffmpeg)
        index = SegmentIndex(segments_dir, keep_sec=300, segment_sec=10)
        files = sorted(segments_dir.glob("*.ts"))
        assert len(files) >= 2, "expected multiple segments"
        for offset, path in enumerate(files):
            index._entries[path.name] = start + offset * 10
        index._save()

        window = index.segments_for_window(spike.start_time - 34, spike.start_time + 19)
        assert window, "no segments selected for spike window"
        staged = root / "staged.ts"
        concat_segments(window, staged, ffmpeg=ffmpeg)
        assert staged.stat().st_size > 0

        from clipper import cut_clip

        raw = root / "raw.mp4"
        cut_clip(
            staged,
            spike.start_time,
            raw,
            pre_sec=30,
            post_sec=15,
            recording_start=index.first_seen(window[0]),
        )
        vertical = root / "vertical.mp4"
        to_vertical(raw, vertical)
        assert vertical.stat().st_size > 0

        edited = root / "edited.mp4"
        to_split_vertical(
            raw,
            edited,
            facecam_box_for(config["facecam"], "alpha"),
            top_px=config["layout"]["top_px"],
            bottom_px=config["layout"]["bottom_px"],
        )
        assert edited.stat().st_size > 0

        store = ReviewStore(root / "review")
        clip_id = store.add(
            "alpha", spike.start_time, raw, vertical, chat_excerpt="POG POG POG"
        )
        assert store.get(clip_id)["status"] == "pending"
        assert store.decide(clip_id, "approved")["status"] == "approved"
        store.update(
            clip_id, edited_path=str(edited.resolve()), edit_status="done"
        )
        assert any(item["id"] == clip_id for item in store.list())
        page = render_page(store.list())
        assert "Approve" in page or "approved" in page
        assert "Preview" in page and "edited.mp4" in page

        print("PIPELINE TEST PASSED")
        print(f"  spike      : {spike.start_time:.3f} ({spike.message_count} msgs)")
        print(f"  segments   : {len(files)} -> {len(window)} in window")
        print(f"  vertical   : {vertical}")
        print(f"  split      : {edited}")
        print(f"  review id  : {clip_id}")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
