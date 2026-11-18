#!/usr/bin/env python3
"""Approve-first review queue for spike clips.

Clips produced by the pipeline land here with status ``pending``. A human
approves or rejects them through the CLI or the tiny built-in web page; only
approved clips are handed to the publisher.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import random
import re
import string
import time
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

LOGGER = logging.getLogger(__name__)

VALID_STATUSES = {"pending", "approved", "rejected", "published", "error"}


class ReviewStore:
    """JSON-file backed review records (one file per clip)."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, clip_id: str) -> Path:
        if not clip_id or any(char in clip_id for char in "/\\.."):
            raise ValueError("Invalid clip id.")
        return self.directory / f"{clip_id}.json"

    def add(
        self,
        channel: str,
        spike_time: float,
        raw_path: str | Path,
        vertical_path: str | Path,
        *,
        chat_excerpt: str = "",
        title: str = "",
        platform: str = "twitch",
        platforms: list[str] | None = None,
    ) -> str:
        stamp = datetime.fromtimestamp(spike_time).strftime("%Y%m%d_%H%M%S")
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        clip_id = f"{stamp}_{channel}_{suffix}"
        record = {
            "id": clip_id,
            "channel": channel,
            "platform": str(platform).strip().lower(),
            "spike_time": spike_time,
            "created": time.time(),
            "status": "pending",
            "raw_path": str(Path(raw_path).resolve()),
            "vertical_path": str(Path(vertical_path).resolve()),
            "chat_excerpt": chat_excerpt[:2000],
            "title": title or f"{channel} - {datetime.fromtimestamp(spike_time):%H:%M:%S}",
            "platforms": platforms or [],
            "posts": {},
            "error": "",
        }
        self._path(clip_id).write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )
        return clip_id

    def get(self, clip_id: str) -> dict | None:
        path = self._path(clip_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def list(self) -> list[dict]:
        records: list[dict] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                records.append(data)
        records.sort(key=lambda item: item.get("created", 0), reverse=True)
        return records

    def update(self, clip_id: str, **changes) -> dict | None:
        record = self.get(clip_id)
        if record is None:
            return None
        record.update(changes)
        self._path(clip_id).write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )
        return record

    def decide(self, clip_id: str, status: str) -> dict | None:
        if status not in {"approved", "rejected"}:
            raise ValueError(f"Cannot set status to {status!r}.")
        return self.update(clip_id, status=status, error="")


def render_page(records: list[dict]) -> str:
    rows = []
    for record in records:
        clip_id = html.escape(str(record.get("id", "")))
        status = html.escape(str(record.get("status", "")))
        moment = datetime.fromtimestamp(float(record.get("spike_time", 0))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        channel = html.escape(str(record.get("channel", "")))
        title = html.escape(str(record.get("title", "")))
        vertical = html.escape(str(record.get("vertical_path", "")))
        edited = str(record.get("edited_path") or "")
        edit_status = html.escape(str(record.get("edit_status") or ""))
        preview = (
            f'<a href="/preview/{clip_id}">Preview</a>'
            if (edited or record.get("vertical_path"))
            else ""
        )
        render_info = (
            f"Edited: {html.escape(edited)}"
            if edited
            else "Edited: <em>pending / using vertical fallback</em>"
        )
        excerpt = html.escape(str(record.get("chat_excerpt", ""))[:300])
        buttons = ""
        if record.get("status") in {"pending", "rejected"}:
            buttons = (
                f'<form method="post" action="/api/review/{clip_id}/approve">'
                '<button class="approve">Approve &amp; publish</button></form>'
                f'<form method="post" action="/api/review/{clip_id}/reject">'
                '<button class="reject">Reject</button></form>'
            )
        rows.append(
            "<tr>"
            f"<td><strong>{channel}</strong><br><small>{moment}</small></td>"
            f"<td>{title}<br><small>{vertical}</small><br><small>{render_info}</small>"
            f"<br><small>{edit_status} {preview}</small></td>"
            f"<td><span class='badge {status}'>{status}</span></td>"
            f"<td><small>{excerpt}</small></td>"
            f"<td class='actions'>{buttons}</td>"
            "</tr>"
        )
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5"><title>Spike clip review</title>
<style>
body{font:14px system-ui,sans-serif;margin:2rem;background:#12141a;color:#e8e8ef}
table{border-collapse:collapse;width:100%} th,td{padding:.6rem;border-bottom:1px solid #2a2d38;text-align:left;vertical-align:top}
small{color:#9298aa} .badge{padding:.2rem .5rem;border-radius:6px;background:#2a2d38}
.badge.approved,.badge.published{background:#1b5e3a} .badge.rejected{background:#6b2b2b}
button{padding:.4rem .7rem;border:0;border-radius:6px;cursor:pointer}
.approve{background:#2e9d63;color:white} .reject{background:#9d3b3b;color:white}
.refresh{background:#3a7bd5;color:white}
form{display:inline;margin-right:.3rem}
</style></head><body>
<h1>Spike clip review</h1>
<p>Approved clips are queued for passthrough publishing to YouTube Shorts and Instagram Reels. The page also refreshes itself every 5 seconds.</p>
<p><form method="get" action="/"><button class="refresh" type="submit">Refresh</button></form></p>
<table><thead><tr><th>Channel / moment</th><th>Clip</th><th>Status</th><th>Chat</th><th>Action</th></tr></thead>
<tbody>""" + "".join(rows) + "</tbody></table></body></html>"


def build_handler(
    store: ReviewStore,
    on_approve: Callable[[dict], None] | None = None,
    allowed_roots: list[str | Path] | None = None,
):
    roots = [Path(path).resolve() for path in (allowed_roots or [])]
    content_types = {
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webm": "video/webm",
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "SpikeReview/1.0"

        def _send(self, status: int, body: str, content_type: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(self, file_path: str | Path) -> None:
            path = Path(file_path).resolve()
            if not path.is_file():
                self._send(404, "File not found", "text/plain")
                return
            if roots and not any(path.is_relative_to(root) for root in roots):
                self._send(403, "Path is outside the clip directories", "text/plain")
                return
            size = path.stat().st_size
            start, end = 0, size - 1
            status = 200
            header = self.headers.get("Range", "")
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", header.strip())
            if match:
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), end)
                if start <= end:
                    status = 206
            payload = path.read_bytes()[start : end + 1]
            self.send_response(status)
            self.send_header("Content-Type", content_types.get(path.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/review"):
                self._send(200, json.dumps(store.list(), indent=2), "application/json")
                return
            if path.startswith("/preview/"):
                clip_id = unquote(path[len("/preview/") :])
                if not clip_id or any(char in clip_id for char in "/\\.."):
                    self._send(400, "Invalid clip id", "text/plain")
                    return
                record = store.get(clip_id)
                target = (record or {}).get("edited_path") or (record or {}).get(
                    "vertical_path"
                )
                if not target:
                    self._send(404, "Clip not found", "text/plain")
                    return
                self._send_file(target)
                return
            if path.startswith("/clips/"):
                self._send_file(unquote(path[len("/clips/") :]))
                return
            self._send(200, render_page(store.list()), "text/html")

        def do_POST(self) -> None:
            parts = self.path.strip("/").split("/")
            # /api/review/<id>/approve|reject
            if len(parts) != 4 or parts[0] != "api" or parts[1] != "review":
                self._send(404, "Not found", "text/plain")
                return
            clip_id, action = parts[2], parts[3]
            record = store.get(clip_id)
            if record is None:
                self._send(404, "Clip not found", "text/plain")
                return
            if action == "approve":
                record = store.decide(clip_id, "approved")
                if on_approve is not None and record is not None:
                    on_approve(record)
            elif action == "reject":
                store.decide(clip_id, "rejected")
            else:
                self._send(400, "Unknown action", "text/plain")
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, fmt: str, *args) -> None:
            LOGGER.debug("review http: " + fmt, *args)

    return Handler


def serve(
    store: ReviewStore,
    port: int = 8765,
    on_approve=None,
    allowed_roots: list[str | Path] | None = None,
) -> None:
    server = ThreadingHTTPServer(
        ("0.0.0.0", port), build_handler(store, on_approve, allowed_roots)
    )
    LOGGER.info("Review UI at http://localhost:%s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Review spike clips before publishing.")
    parser.add_argument("--dir", default="./review", help="review store directory")
    parser.add_argument("action", nargs="?", default="list", choices=["list", "approve", "reject", "serve"])
    parser.add_argument("clip_id", nargs="?")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    store = ReviewStore(args.dir)
    if args.action == "list":
        for record in store.list():
            print(
                f"{record['id']}  {record['status']:<10}  "
                f"{record.get('channel', '')}  {record.get('title', '')}"
            )
        return 0
    if args.action in {"approve", "reject"}:
        if not args.clip_id:
            parser.error("approve/reject needs a clip id")
        record = store.decide(args.clip_id, args.action)
        if record is None:
            print("Clip not found.")
            return 1
        print(f"{record['id']} -> {record['status']}")
        return 0
    serve(store, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
