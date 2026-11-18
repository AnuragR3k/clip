#!/usr/bin/env python3
"""Offline test for the publisher's SupoClip HTTP flow (mock server)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from publisher import Publisher

seen: dict[str, list[dict]] = {"posts": [], "auth": [], "uploads": []}


class Handler(BaseHTTPRequestHandler):
    def _json(self, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        seen["auth"].append({"path": self.path, "key": self.headers.get("Authorization")})
        if self.path == "/social/connections":
            self._json(
                {
                    "connections": [
                        {"id": "acc_y", "provider": "youtube", "status": "active"},
                        {"id": "acc_i", "provider": "instagram", "status": "active"},
                    ]
                }
            )
        else:
            self._json({})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        seen["auth"].append({"path": self.path, "key": self.headers.get("Authorization")})
        if self.path == "/media/upload":
            assert b"video" in body
            seen["uploads"].append(body.split(b"\r\n", 2)[1].decode("utf-8", "replace"))
            self._json({"video_path": "upload://test.mp4"})
        elif self.path == "/tasks/ingest-rendered":
            data = json.loads(body)
            assert data["file_path"] == "upload://test.mp4"
            self._json({"task_id": "t1", "clip_id": "c1"})
        elif self.path == "/social/posts":
            seen["posts"].append(json.loads(body))
            self._json({"post": {"id": "p1", "status": "queued"}})
        else:
            self._json({})

    def log_message(self, *args) -> None:
        pass


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    root = Path(tempfile.mkdtemp(prefix="pubtest_", dir="/tmp/opencode"))
    clip_path = root / "vertical.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 256)
    edited_path = root / "edited.mp4"
    edited_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"e" * 256)
    os.environ["SUPOCLIP_API_KEY"] = "sk_test"
    try:
        publisher = Publisher(
            {
                "api_url": f"http://127.0.0.1:{port}",
                "api_key_env": "SUPOCLIP_API_KEY",
                "platforms": ["youtube", "instagram"],
                "hashtags": ["shorts", "gta6"],
                "caption_template": "{title} #shorts",
            }
        )
        result = publisher.publish(
            {
                "channel": "alpha",
                "spike_time": 1794997690.0,
                "title": "alpha - 16:28:10",
                "vertical_path": str(clip_path),
                "edited_path": str(edited_path),
                "chat_excerpt": "POG",
            }
        )
        assert result["task_id"] == "t1" and result["clip_id"] == "c1"
        assert set(result["posts"]) == {"youtube", "instagram"}
        assert len(seen["posts"]) == 2
        youtube = next(p for p in seen["posts"] if p.get("social_account_id") == "acc_y")
        instagram = next(p for p in seen["posts"] if p.get("social_account_id") == "acc_i")
        assert youtube["privacy_level"] == "public"
        assert "privacy_level" not in instagram
        assert youtube["hashtags"] == ["shorts", "gta6"]
        assert all(item["key"] == "Bearer sk_test" for item in seen["auth"])
        assert len(seen["uploads"]) == 1
        assert "edited.mp4" in seen["uploads"][0], "publisher must upload the edited file"
        print("PUBLISHER TEST PASSED")
        print(f"  calls: {[item['path'] for item in seen['auth']]}")
        print(f"  posts: {result['posts']}")
        return 0
    finally:
        server.shutdown()
        import shutil

        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
