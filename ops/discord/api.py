"""Discord REST client for the project server. Standard library only: runs on a bare CI runner."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://discord.com/api/v10"
_UA = "DiscordBot (https://github.com/nikita-ivlev/sessionmate, 1.0)"


class DiscordError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {body[:400]}")
        self.status = status


def token_file() -> Path:
    if os.environ.get("DISCORD_BOT_TOKEN_FILE"):
        return Path(os.environ["DISCORD_BOT_TOKEN_FILE"])
    return Path(os.environ.get("APPDATA", Path.home())) / "Sessionmate-dev" / "discord-bot-token"


def load_token() -> str:
    """CI passes DISCORD_BOT_TOKEN; on the dev PC the token sits in a file outside the repo."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    path = token_file()
    if not path.is_file():
        raise SystemExit(f"No bot token: set DISCORD_BOT_TOKEN or put the token in {path}")
    return path.read_text(encoding="utf-8-sig").strip()


class Discord:
    """Thin REST wrapper. Never logs the token; waits out rate limits."""

    def __init__(self, token: str | None = None, reason: str = "Sessionmate ops") -> None:
        self._token = token or load_token()
        self._reason = reason

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bot {self._token}",
            "User-Agent": _UA,
            "Content-Type": "application/json",
            "X-Audit-Log-Reason": self._reason,
        }
        for attempt in range(6):
            req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode(errors="replace")
                if exc.code == 429:
                    time.sleep(float(json.loads(raw).get("retry_after", 2)) + 0.3)
                    continue
                if exc.code >= 500 and attempt < 2:
                    time.sleep(2)
                    continue
                raise DiscordError(method, path, exc.code, raw) from None
        raise DiscordError(method, path, 429, "rate limited too long")

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body=None):
        return self.call("POST", path, body if body is not None else {})

    def patch(self, path: str, body):
        return self.call("PATCH", path, body)

    def put(self, path: str, body=None):
        return self.call("PUT", path, body)

    def delete(self, path: str):
        return self.call("DELETE", path)
