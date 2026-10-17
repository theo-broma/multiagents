"""Shared fixed-upstream protocol and provider/account routing for auth listeners.

The routing pool is independent of ingress authority. Isolated attempts supply
host-recorded routing identities; legacy callers supply verified signed claims.
Credential projection for isolated providers belongs to slice 4C.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

UPSTREAM = "https://api.anthropic.com"
MAX_BODY_SIZE = 64 * 1024 * 1024
BODY_IDLE_TIMEOUT = 60
HOP_HEADERS = {"host", "authorization", "content-length", "connection",
               "accept-encoding", "proxy-connection", "keep-alive",
               "transfer-encoding", "upgrade", "te", "trailer"}


def routing_identity(agent: str, provider: str) -> str:
    return f"{agent}\x00{provider}" if provider else agent


def routing_pool(accounts, provider):
    accounts.reload_pins()
    accounts.labels()
    return accounts.eligible(provider), accounts.provider_pins.get(provider, "")


def request_headers(headers, *, isolated=False):
    hop = set(HOP_HEADERS)
    if isolated:
        hop.update({"x-api-key", "proxy-authorization", "cookie"})
    if isolated and hasattr(headers, "get_all"):
        for value in headers.get_all("connection", []):
            hop.update(name.strip().lower() for name in value.split(","))
    return {key: value for key, value in headers.items()
            if key.lower() not in hop and not (isolated and key.lower().startswith("proxy-"))}


def validate_label(label: str) -> str:
    if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9_-]+", label):
        raise ValueError("account label must contain lowercase letters, digits, '-' or '_'")
    return label


class Accounts:
    """The pool, its limits, and the pinning that keeps caches warm."""

    def __init__(self, vault: Path, pins: dict[str, str] | None = None,
                 pins_path: Path | None = None) -> None:
        self.vault = vault
        self.lock = threading.Lock()
        self.pinned: dict[str, str] = {}          # agent id -> account label
        self.limited: dict[str, float] = {}       # account label -> until
        self._cache: dict[str, tuple[str, str]] = {}
        self.rejected: dict[str, str] = {}       # label -> rejected content hash
        self.pins_path = pins_path
        self.provider_pins = self._validate_pins(pins or {})
        self.reload_pins()
        self.labels()                           # fail before admitting requests

    @staticmethod
    def _validate_pins(pins: dict[str, str]) -> dict[str, str]:
        if not isinstance(pins, dict):
            raise ValueError("proxy pins must be a provider-to-account mapping")
        return {provider: validate_label(label) for provider, label in pins.items()}

    def reload_pins(self) -> None:
        if self.pins_path is not None:
            self.provider_pins = self._validate_pins(json.loads(self.pins_path.read_text()))

    def labels(self) -> list[str]:
        """Every account with a credential, in label order.

        Read from disk every time rather than held: a login can add one while
        this is running, and an operator who has just added an account and
        watched nothing happen is owed a better answer than "restart it".
        """
        root = self.vault / "accounts"
        labels = ["default"] if (self.vault / ".credentials.json").is_file() else []
        if root.is_dir():
            if (root / "default").exists():
                raise ValueError("accounts/default is reserved; rename it to another account label")
            for path in root.iterdir():
                if not path.is_dir():
                    continue
                validate_label(path.name)
                if (path / ".credentials.json").is_file():
                    labels.append(path.name)
        return sorted(labels)

    def path(self, label: str) -> Path:
        validate_label(label)
        if label == "default":
            return self.vault / ".credentials.json"
        return self.vault / "accounts" / label / ".credentials.json"

    def token(self, label: str) -> str:
        """That account's current access token, re-read when the file changes."""
        path = self.path(label)
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        stamp = hashlib.sha256(raw).hexdigest()
        cached = self._cache.get(label)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            data = json.loads(raw)
        except (OSError, ValueError):
            return ""
        for block in data.values() if isinstance(data, dict) else []:
            if isinstance(block, dict) and isinstance(block.get("accessToken"), str) and block["accessToken"]:
                self._cache[label] = (stamp, block["accessToken"])
                return block["accessToken"]
        return ""

    def for_agent(self, agent_id: str, provider: str = "",
                  tried: list[str] | None = None, *,
                  eligible: list[str] | None = None, fixed: str | None = None) -> str:
        """Which account this agent uses. Decided once, then kept.

        Pinned for the agent's whole life even when a rested account appears,
        because the cache it has built on this one is worth more than the
        headroom on that one. See the module docstring.
        """
        with self.lock:
            return self._select(agent_id, provider, tried, eligible=eligible, fixed=fixed)

    def admit(self, agent_id: str, provider: str, tried: list[str], *,
              eligible: list[str], fixed: str) -> tuple[str, int | None, bool]:
        """Select and snapshot exhaustion under the cooldown mark's lock."""
        with self.lock:
            label = self._select(agent_id, provider, tried, eligible=eligible, fixed=fixed)
            rejected = {l for l in eligible if self.is_rejected(l)}
            now = time.monotonic()
            ends = [self.limited[l] for l in eligible if l not in rejected
                    and self.limited.get(l, 0) > now]
            retry = max(1, math.ceil(min(ends) - now)) if ends else None
            return label, retry, bool(rejected)

    def _select(self, agent_id: str, provider: str, tried: list[str] | None, *,
                eligible: list[str] | None, fixed: str | None) -> str:
        eligible = self.eligible(provider) if eligible is None else eligible
        free = [label for label in eligible if label not in (tried or [])
                and self.usable(label)]
        fixed = self.provider_pins.get(provider) if fixed is None else fixed
        if fixed:
            return fixed if fixed in free else ""
        label = self.pinned.get(agent_id)
        if label and label in free:
            return label
        if not free:
            return ""
        # Fewest agents first, so a second account is actually used rather
        # than sitting idle until the first one is exhausted.
        counts = {l: 0 for l in free}
        for pinned in self.pinned.values():
            if pinned in counts:
                counts[pinned] += 1
        label = min(free, key=lambda l: (counts[l], l))
        self.pinned[agent_id] = label
        return label

    def eligible(self, provider: str = "") -> list[str]:
        fixed = self.provider_pins.get(provider)
        return [fixed] if fixed else [label for label in self.labels()
                                     if label not in self.provider_pins.values()]

    def is_rejected(self, label: str) -> bool:
        self.token(label)                        # refresh the content fingerprint
        stamp = self._cache.get(label, ("", ""))[0]
        return label in self.rejected and self.rejected[label] == stamp

    def usable(self, label: str) -> bool:
        if self.is_rejected(label):
            return False
        return self.limited.get(label, 0) <= time.monotonic()

    def mark_unauthenticated(self, label: str, token: str) -> None:
        with self.lock:
            # The file may have changed while this request was upstream. Do
            # not quarantine a new login because an older token was refused.
            self.token(label)
            stamp, current = self._cache.get(label, ("", ""))
            if current == token:
                self.rejected[label] = stamp
            for agent, pinned in list(self.pinned.items()):
                if pinned == label:
                    del self.pinned[agent]

    def mark_limited(self, label: str, seconds: float) -> None:
        with self.lock:
            self.limited[label] = max(self.limited.get(label, 0),
                                      time.monotonic() + max(60.0, seconds))
            # Unpin everyone on it. They will land somewhere else NEXT request,
            # which is the only moment a switch is safe: mid-stream it is not
            # possible and between streams it costs only the cache.
            for agent, pinned in list(self.pinned.items()):
                if pinned == label:
                    del self.pinned[agent]

    def release(self, agent_id: str) -> None:
        with self.lock:
            self.pinned.pop(agent_id, None)


def upstream_url(path: str, upstream: str = UPSTREAM) -> str:
    """The URL a request target would be forwarded to, or "" if it leaves UPSTREAM.

    The target is appended to `UPSTREAM`, so one that does not start with `/`
    (`.attacker.invalid/x`, `@attacker.invalid/x`, `:8443/x`) moves the host,
    and with it the account token. The check is on the URL as built, both as
    `urlsplit` reads it and as urllib will dial it, so it holds for every
    spelling rather than for the ones someone thought of.
    """
    url = upstream + path
    want = urllib.parse.urlsplit(upstream)
    try:
        got = urllib.parse.urlsplit(url)
        port = got.port
        request = urllib.request.Request(url)
    except ValueError:
        return ""
    if (got.scheme, got.hostname, port, got.username, got.password) != \
            (want.scheme, want.hostname, want.port, None, None):
        return ""
    if request.type != want.scheme or request.host != want.netloc:
        return ""
    return url
