#!/usr/bin/env python3
"""Detect spikes in a Twitch chat log.

The log format is one message per line::

    <unix timestamp>\\t<username>\\t<message>

A spike is a bucket whose message count is greater than ``multiplier`` times
the rolling average of the previous baseline window.
"""

from __future__ import annotations

import argparse
import math
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple, TextIO


class Spike(NamedTuple):
    start_time: float
    end_time: float
    message_count: int


# Do not flag the first few buckets while the rolling average is still warming
# up. This is capped at the baseline window for tests/short logs.
_MIN_WARMUP_BUCKETS = 6


def _parse_timestamp(line: str) -> float | None:
    first = line.split("\t", 1)[0]
    try:
        timestamp = float(first)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return timestamp


def _add_line(line: str, counts: dict[int, int], window_sec: float) -> int | None:
    timestamp = _parse_timestamp(line)
    if timestamp is None:
        return None
    index = int(timestamp // window_sec)
    counts[index] = counts.get(index, 0) + 1
    return index


def _read_counts(
    chat_log_path: str | Path, window_sec: float
) -> tuple[dict[int, int], int | None, int | None]:
    counts: dict[int, int] = {}
    first_index: int | None = None
    last_index: int | None = None

    with Path(chat_log_path).open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            index = _add_line(line, counts, window_sec)
            if index is None:
                continue
            if first_index is None or index < first_index:
                first_index = index
            if last_index is None or index > last_index:
                last_index = index

    return counts, first_index, last_index


def _evaluate_bucket(
    index: int,
    counts: dict[int, int],
    first_index: int | None,
    window_sec: float,
    baseline_sec: float,
    multiplier: float,
    min_messages: int,
) -> Spike | None:
    """Evaluate one bucket against the preceding baseline window."""
    count = counts.get(index, 0)
    if count < min_messages or first_index is None:
        return None

    baseline_buckets = max(1, round(baseline_sec / window_sec))
    prior_available = index - first_index
    if prior_available < 1:
        return None

    warmup_buckets = min(_MIN_WARMUP_BUCKETS, baseline_buckets)
    if prior_available < warmup_buckets:
        return None

    buckets_to_average = min(prior_available, baseline_buckets)
    total = sum(
        counts.get(prior_index, 0)
        for prior_index in range(index - buckets_to_average, index)
    )
    rolling_average = total / buckets_to_average

    # A completely idle baseline makes any real burst a spike. min_messages
    # above prevents a single stray message from being treated as one.
    if rolling_average <= 0:
        is_spike = True
    else:
        is_spike = count > multiplier * rolling_average

    if not is_spike:
        return None

    start = index * window_sec
    return Spike(start, start + window_sec, count)


def find_spikes(
    chat_log_path: str | Path,
    window_sec: float = 10,
    multiplier: float = 3.0,
    *,
    baseline_sec: float = 300,
    min_messages: int = 3,
) -> list[Spike]:
    """Return all spike buckets found in a chat log.

    ``window_sec`` is the bucket size, ``baseline_sec`` is the rolling average
    window, and ``multiplier`` is the threshold applied to that average.
    """
    if window_sec <= 0:
        raise ValueError("window_sec must be greater than zero")
    if baseline_sec <= 0:
        raise ValueError("baseline_sec must be greater than zero")
    if multiplier <= 0:
        raise ValueError("multiplier must be greater than zero")
    if min_messages < 1:
        raise ValueError("min_messages must be at least one")

    path = Path(chat_log_path)
    if not path.exists():
        return []

    counts, first_index, last_index = _read_counts(path, window_sec)
    if first_index is None or last_index is None:
        return []

    spikes: list[Spike] = []
    baseline_buckets = max(1, round(baseline_sec / window_sec))
    warmup_buckets = min(_MIN_WARMUP_BUCKETS, baseline_buckets)

    # Only buckets with enough history can be spikes.
    for index in range(first_index + warmup_buckets, last_index + 1):
        if counts.get(index, 0) < min_messages:
            continue
        spike = _evaluate_bucket(
            index,
            counts,
            first_index,
            window_sec,
            baseline_sec,
            multiplier,
            min_messages,
        )
        if spike is not None:
            spikes.append(spike)

    return spikes


def _sleep_or_stop(stop_event, seconds: float) -> bool:
    """Sleep, returning True when a stop was requested."""
    if stop_event is not None:
        return stop_event.wait(seconds)
    time.sleep(seconds)
    return False


def watch_spikes(
    chat_log_path: str | Path,
    window_sec: float = 10,
    multiplier: float = 3.0,
    *,
    baseline_sec: float = 300,
    min_messages: int = 3,
    poll_sec: float = 2.0,
    settle_sec: float = 0.0,
    stop_event=None,
) -> Iterator[Spike]:
    """Tail a chat log and yield spikes as buckets close.

    Existing lines are used to seed the rolling average, but spikes that
    happened before the watcher started are not emitted. ``settle_sec`` delays
    evaluation so a clip can include video recorded after the spike bucket.
    """
    if window_sec <= 0:
        raise ValueError("window_sec must be greater than zero")
    if baseline_sec <= 0:
        raise ValueError("baseline_sec must be greater than zero")
    if multiplier <= 0:
        raise ValueError("multiplier must be greater than zero")
    if min_messages < 1:
        raise ValueError("min_messages must be at least one")
    if poll_sec <= 0:
        raise ValueError("poll_sec must be greater than zero")
    if settle_sec < 0:
        raise ValueError("settle_sec cannot be negative")

    path = Path(chat_log_path)
    counts: dict[int, int] = {}
    buffer = ""
    first_index: int | None = None
    next_eval_index: int | None = None
    baseline_buckets = max(1, round(baseline_sec / window_sec))
    handle: TextIO | None = None

    def ingest(chunk: str) -> None:
        nonlocal buffer, first_index
        buffer += chunk
        if "\n" not in buffer:
            return
        parts = buffer.split("\n")
        buffer = parts.pop()
        for line in parts:
            index = _add_line(line, counts, window_sec)
            if index is None:
                continue
            if first_index is None or index < first_index:
                first_index = index

    try:
        while stop_event is None or not stop_event.is_set():
            if handle is None:
                try:
                    handle = path.open("r", encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    if _sleep_or_stop(stop_event, poll_sec):
                        break
                    continue
                except OSError:
                    if _sleep_or_stop(stop_event, poll_sec):
                        break
                    continue

                # Seed from history so the first new spike has a baseline.
                ingest(handle.read())
                now = time.time()
                next_eval_index = int(now // window_sec)

                # Only recent history matters for the rolling average.
                cutoff = next_eval_index - baseline_buckets - 2
                for old_index in [key for key in counts if key < cutoff]:
                    del counts[old_index]
                if counts:
                    first_index = max(first_index or cutoff, cutoff)
                continue

            chunk = handle.read()
            if chunk:
                ingest(chunk)

            now = time.time()
            assert next_eval_index is not None
            while next_eval_index * window_sec + window_sec + settle_sec <= now:
                spike = _evaluate_bucket(
                    next_eval_index,
                    counts,
                    first_index,
                    window_sec,
                    baseline_sec,
                    multiplier,
                    min_messages,
                )
                next_eval_index += 1
                if spike is not None:
                    yield spike

            # Keep memory bounded during long-running monitoring.
            cutoff = next_eval_index - baseline_buckets - 5
            stale = [key for key in counts if key < cutoff]
            if stale:
                for old_index in stale:
                    del counts[old_index]
                if first_index is not None:
                    first_index = max(first_index, cutoff)

            if _sleep_or_stop(stop_event, poll_sec):
                break
    finally:
        if handle is not None:
            handle.close()


def _format_spike(spike: Spike) -> str:
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(spike.start_time))
    end = time.strftime("%H:%M:%S", time.localtime(spike.end_time))
    return f"{spike.start_time:.3f}\t{spike.end_time:.3f}\t{spike.message_count}\t{start} - {end}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Find chat-activity spikes.")
    parser.add_argument("chat_log", nargs="?", default="logs/chat.log")
    parser.add_argument("--watch", action="store_true", help="tail the log")
    parser.add_argument("--window-sec", type=float, default=10)
    parser.add_argument("--multiplier", type=float, default=3.0)
    parser.add_argument("--baseline-sec", type=float, default=300)
    parser.add_argument("--min-messages", type=int, default=3)
    parser.add_argument("--poll-sec", type=float, default=2.0)
    parser.add_argument(
        "--settle-sec",
        type=float,
        default=0.0,
        help="wait this long after a bucket closes before reporting it",
    )
    args = parser.parse_args()

    if args.watch:
        print(f"Watching {args.chat_log} for spikes...", flush=True)
        try:
            for spike in watch_spikes(
                args.chat_log,
                args.window_sec,
                args.multiplier,
                baseline_sec=args.baseline_sec,
                min_messages=args.min_messages,
                poll_sec=args.poll_sec,
                settle_sec=args.settle_sec,
            ):
                print(_format_spike(spike), flush=True)
        except KeyboardInterrupt:
            pass
        return 0

    spikes = find_spikes(
        args.chat_log,
        args.window_sec,
        args.multiplier,
        baseline_sec=args.baseline_sec,
        min_messages=args.min_messages,
    )
    for spike in spikes:
        print(_format_spike(spike))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
