#!/usr/bin/env python3
"""Read Twitch chat anonymously over IRC and append it to a log file.

Twitch's IRC endpoint accepts read-only anonymous connections. The nickname
``justinfan`` followed by random digits needs no OAuth token. This module does
not send chat messages.
"""

from __future__ import annotations

import logging
import random
import socket
import ssl
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697

# :username!username@username.tmi.twitch.tv PRIVMSG #channel :message
_PRIVMSG_PREFIX = " PRIVMSG #"


class ChatMonitor:
    """Background thread that logs messages for one Twitch channel."""

    def __init__(
        self,
        channel: str,
        log_path: str | Path,
        host: str = IRC_HOST,
        port: int = IRC_PORT,
        reconnect_delay: float = 5.0,
    ) -> None:
        self.channel = channel.lstrip("#").lower()
        self.log_path = Path(log_path)
        self.host = host
        self.port = port
        self.reconnect_delay = reconnect_delay

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: ssl.SSLSocket | None = None
        self._log_file = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, name="chat-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._log_file = self.log_path.open("a", encoding="utf-8", buffering=1)
        except OSError as exc:
            LOGGER.error("Cannot open chat log %s: %s", self.log_path, exc)
            return

        try:
            while not self._stop.is_set():
                try:
                    self._connect_and_read()
                except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                    if not self._stop.is_set():
                        LOGGER.warning(
                            "Chat connection lost (%s); reconnecting in %.0fs.",
                            exc,
                            self.reconnect_delay,
                        )
                if self._stop.wait(self.reconnect_delay):
                    break
        finally:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
            self._socket = None

    def _connect_and_read(self) -> None:
        context = ssl.create_default_context()
        with socket.create_connection((self.host, self.port), timeout=15) as raw:
            sock = context.wrap_socket(raw, server_hostname=self.host)
            self._socket = sock
            try:
                sock.settimeout(1.0)
                nick = f"justinfan{random.randint(10000, 99999)}"
                self._send(sock, f"NICK {nick}")
                self._send(sock, f"JOIN #{self.channel}")
                LOGGER.info(
                    "Connected to chat for #%s as %s.", self.channel, nick
                )

                buffer = b""
                while not self._stop.is_set():
                    try:
                        data = sock.recv(4096)
                    except TimeoutError:
                        continue
                    except OSError as exc:
                        raise ConnectionError(str(exc)) from exc

                    if not data:
                        raise ConnectionError("server closed the connection")

                    buffer += data
                    while b"\n" in buffer:
                        raw_line, buffer = buffer.split(b"\n", 1)
                        line = raw_line.decode("utf-8", "replace").strip("\r")
                        self._handle_line(sock, line)
            finally:
                self._socket = None
                try:
                    sock.close()
                except OSError:
                    pass

    def _handle_line(self, sock: ssl.SSLSocket, line: str) -> None:
        if not line:
            return

        if line.startswith("PING"):
            self._send(sock, "PONG :tmi.twitch.tv")
            return

        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[1] == "RECONNECT":
            raise ConnectionError("server requested a reconnect")

        if _PRIVMSG_PREFIX not in line:
            return

        prefix, _sep, remainder = line.partition(_PRIVMSG_PREFIX)
        if not prefix.startswith(":") or "!" not in prefix:
            return

        username = prefix[1:].split("!", 1)[0]
        _channel, _sep, message = remainder.partition(" :")
        if not message:
            # A message can legitimately be empty; keep the log format intact.
            message = ""

        message = message.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        if self._log_file is not None:
            self._log_file.write(f"{time.time():.3f}\t{username}\t{message}\n")

    @staticmethod
    def _send(sock: ssl.SSLSocket, line: str) -> None:
        sock.sendall(f"{line}\r\n".encode())
