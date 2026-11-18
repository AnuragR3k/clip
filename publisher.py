#!/usr/bin/env python3
"""Publish approved spike clips through SupoClip's own API.

Flow per approved clip:
  1. upload the rendered MP4 with POST /media/upload -> ``upload://...``
  2. POST /tasks/ingest-rendered -> task_id + clip_id (no re-render)
  3. POST /social/posts once per target platform (YouTube, Instagram)

Authentication is either a SupoClip API key (``SUPOCLIP_API_KEY``) or the
self-host signed-header scheme (``SUPOCLIP_USER_ID`` + ``SUPOCLIP_AUTH_SECRET``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class PublisherError(RuntimeError):
    """Raised when SupoClip refuses an ingest or a publish."""


class Publisher:
    def __init__(self, config: dict) -> None:
        self.base_url = str(config.get("api_url") or "http://localhost:8000").rstrip("/")
        key_env = str(config.get("api_key_env") or "SUPOCLIP_API_KEY")
        self.api_key = os.environ.get(key_env, "").strip()
        self.user_id = os.environ.get("SUPOCLIP_USER_ID", "").strip()
        self.secret = os.environ.get("SUPOCLIP_AUTH_SECRET", "").strip()
        if not self.api_key and not (self.user_id and self.secret):
            raise PublisherError(
                f"Set {key_env} (create a key in SupoClip /settings/api-keys) or "
                "SUPOCLIP_USER_ID + SUPOCLIP_AUTH_SECRET."
            )

        platforms = config.get("platforms") or ["youtube", "instagram"]
        self.platforms = [str(p).strip().lower() for p in platforms if str(p).strip()]
        self.account_ids = {
            str(k).lower(): str(v)
            for k, v in (config.get("social_account_ids") or {}).items()
            if v
        }
        self.hashtags = [str(tag).lstrip("#") for tag in (config.get("hashtags") or [])]
        self.caption_template = str(
            config.get("caption_template") or "{title} #shorts"
        )
        self.privacy_level = str(config.get("privacy_level") or "public")

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": content_type}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            timestamp = str(int(time.time()))
            signature = hmac.new(
                self.secret.encode("utf-8"),
                f"{self.user_id}:{timestamp}".encode(),
                hashlib.sha256,
            ).hexdigest()
            headers.update(
                {
                    "x-supoclip-user-id": self.user_id,
                    "x-supoclip-ts": timestamp,
                    "x-supoclip-signature": signature,
                }
            )
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> dict:
        data = body
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=self._headers(content_type),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise PublisherError(f"{method} {path} -> {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise PublisherError(f"{method} {path} failed: {exc.reason}") from None
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _upload(self, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise PublisherError(f"Clip file is missing: {path}")
        boundary = f"----spike{uuid.uuid4().hex}"
        parts: list[bytes] = []
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="video"; filename="{path.name}"\r\n'
            "Content-Type: video/mp4\r\n\r\n".encode()
        )
        parts.append(path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        result = self._request(
            "POST",
            "/media/upload",
            body=b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        video_path = result.get("video_path")
        if not isinstance(video_path, str) or not video_path.startswith("upload://"):
            raise PublisherError(f"Unexpected upload response: {result}")
        return video_path

    def _resolve_accounts(self) -> dict[str, str]:
        result = self._request("GET", "/social/connections")
        connections = result.get("connections") or []
        found: dict[str, str] = {}
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            provider = str(connection.get("provider") or "").lower()
            if connection.get("status") != "active":
                continue
            if connection.get("id"):
                found.setdefault(provider, str(connection["id"]))
        chosen = dict(found)
        chosen.update(self.account_ids)
        missing = [p for p in self.platforms if not chosen.get(p)]
        if missing:
            raise PublisherError(
                "No active SupoClip social account for: "
                + ", ".join(missing)
                + ". Connect them in the SupoClip UI first."
            )
        return chosen

    @staticmethod
    def _video_file(record: dict) -> Path:
        """Prefer the post-approval split render; fall back to the vertical."""
        for key in ("edited_path", "vertical_path"):
            candidate = record.get(key)
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        raise PublisherError(
            "No rendered clip file found (checked edited_path and vertical_path)."
        )

    def publish(self, record: dict) -> dict[str, Any]:
        video_file = self._video_file(record)
        LOGGER.info("Publishing %s", video_file)
        video_path = self._upload(video_file)
        title = str(record.get("title") or "Spike clip")
        ingest = self._request(
            "POST",
            "/tasks/ingest-rendered",
            payload={
                "file_path": video_path,
                "title": title[:200],
                "filename": video_file.name,
                "text": str(record.get("chat_excerpt") or "")[:2000],
            },
        )
        task_id, clip_id = ingest.get("task_id"), ingest.get("clip_id")
        if not task_id or not clip_id:
            raise PublisherError(f"Unexpected ingest response: {ingest}")

        accounts = self._resolve_accounts()
        caption = self.caption_template.format(
            channel=record.get("channel", ""),
            title=title,
            spike_time=record.get("spike_time", ""),
        )[:2200]
        hashtags = list(self.hashtags)

        posts: dict[str, Any] = {}
        for platform in self.platforms:
            payload: dict[str, Any] = {
                "task_id": task_id,
                "clip_id": clip_id,
                "social_account_id": accounts[platform],
                "title": title[:100],
                "caption": caption,
                "hashtags": hashtags,
            }
            if platform != "instagram":
                payload["privacy_level"] = self.privacy_level
            result = self._request("POST", "/social/posts", payload=payload)
            post = result.get("post") or result
            posts[platform] = {
                "id": post.get("id"),
                "status": post.get("status"),
                "url": post.get("public_url") or post.get("platform_url"),
            }
            LOGGER.info("Queued %s post %s", platform, posts[platform])
        return {"task_id": task_id, "clip_id": clip_id, "posts": posts}
