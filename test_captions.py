#!/usr/bin/env python3
"""Offline checks for caption timing and platform branding helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

from captions import Cue, write_ass
from layout import brand_for


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="captiontest_") as tmp:
        root = Path(tmp)
        ass = write_ass(
            [Cue(0.5, 3.0, "nice one try again"), Cue(65.25, 70.0, "lets go")],
            root / "caps.ass",
        )
        text = ass.read_text(encoding="utf-8")
        assert "0:00:00.50,0:00:03.00" in text, text
        assert "0:01:05.25,0:01:10.00" in text, text
        assert "Caption,,0,0,696,," in text, text

        twitch = brand_for("twitch", "lacy")
        kick = brand_for("kick", "n3on")
        assert twitch and twitch["handle"] == "twitch.tv/lacy", twitch
        assert kick and kick["handle"] == "kick.com/n3on", kick
        assert Path(twitch["logo"]).is_file()
        assert Path(kick["logo"]).is_file()
        assert brand_for("youtube", "someone") is None

    print("CAPTION/BRAND TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
