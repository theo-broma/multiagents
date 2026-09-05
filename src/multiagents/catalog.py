"""Local snapshot of the public model catalog, and what changed since.

``models.opencode.ai/api.json`` is a public, unauthenticated catalog carrying
per-model pricing, context limits and capability flags. We keep the slice we
care about on disk so that each new orchestrator session can ask a useful
question: *has anything changed underneath the roster since last time?*

The answer matters because `agents.yaml` pins specific model ids. A model that
disappears, loses `tool_call`, or gets its context halved breaks an agent
silently — the run just fails oddly. A price change matters too, if less
urgently.

This module only reports and, when told to, writes. It never edits
``agents.yaml``: deciding what to do about a change is the orchestrator's job,
and by design it consults the critic before acting.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CATALOG_URL = "https://models.opencode.ai/api.json"
FETCH_TIMEOUT = 45

# Fields whose change we actually care about. The catalog carries plenty of
# metadata that churns without consequence (descriptions, release notes); diffing
# everything would bury the signal.
WATCHED = ("cost", "limit", "tool_call", "reasoning", "structured_output", "modalities")


def catalog_dir(config_dir: Path) -> Path:
    return config_dir / "catalog"


def local_path(config_dir: Path, provider: str) -> Path:
    return catalog_dir(config_dir) / f"{provider}.json"


# --------------------------------------------------------------------------


def fetch_remote(provider: str, url: str = CATALOG_URL) -> dict[str, Any]:
    """Download the catalog and return one provider's slice.

    Public and unauthenticated — no credential is sent. Raises on network or
    parse failure so the caller can report it rather than silently treating an
    outage as "nothing changed".
    """
    request = urllib.request.Request(url, headers={"User-Agent": "multiagents/0.1"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        payload = json.load(response)
    if provider not in payload:
        raise KeyError(
            f"provider {provider!r} is not in the catalog at {url} "
            f"(available: {', '.join(sorted(payload)[:8])}…)"
        )
    return payload[provider]


def load_local(config_dir: Path, provider: str) -> dict[str, Any] | None:
    path = local_path(config_dir, provider)
    if not path.is_file():
        return None
    try:
        with path.open() as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def save_local(config_dir: Path, provider: str, data: dict[str, Any],
               url: str = CATALOG_URL) -> Path:
    path = local_path(config_dir, provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "provider": provider,
        "source": url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "data": data,
    }
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as handle:
        json.dump(snapshot, handle, indent=2, sort_keys=True)
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------


@dataclass
class Change:
    model: str
    kind: str                      # added | removed | changed
    fields: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        if self.kind in ("added", "removed"):
            return f"{self.kind}: {self.model}"
        bits = []
        for name, (before, after) in sorted(self.fields.items()):
            bits.append(f"{name} {_render(before)} -> {_render(after)}")
        return f"changed: {self.model} ({'; '.join(bits)})"


def _render(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={v}" for k, v in sorted(value.items())) + "}"
    return str(value)


def diff(local: dict[str, Any] | None, remote: dict[str, Any]) -> list[Change]:
    """Compare a stored snapshot against a freshly fetched provider slice."""
    if local is None:
        return []                                   # nothing to compare against yet
    old_models = (local.get("data") or {}).get("models") or {}
    new_models = remote.get("models") or {}

    changes: list[Change] = []
    for model in sorted(set(new_models) - set(old_models)):
        changes.append(Change(model, "added"))
    for model in sorted(set(old_models) - set(new_models)):
        changes.append(Change(model, "removed"))

    for model in sorted(set(old_models) & set(new_models)):
        before, after = old_models[model], new_models[model]
        fields = {
            key: (before.get(key), after.get(key))
            for key in WATCHED
            if before.get(key) != after.get(key)
        }
        if fields:
            changes.append(Change(model, "changed", fields))
    return changes


def assess(changes: list[Change], provider_prefix: str, agents: dict) -> dict[str, Any]:
    """Work out whether any change touches a model the roster actually pins.

    This is the signal that decides whether the orchestrator needs to think
    about `agents.yaml` at all. Most catalog churn is irrelevant: new models
    appearing, prices moving on models nobody uses.
    """
    in_use: dict[str, list[str]] = {}
    for name, spec in agents.items():
        model = getattr(spec, "model", "") or ""
        if model.startswith(provider_prefix + "/"):
            in_use.setdefault(model.split("/", 1)[1], []).append(name)

    affecting: list[dict[str, Any]] = []
    severity = "none"
    for change in changes:
        users = in_use.get(change.model)
        if not users:
            continue
        level = "info"
        if change.kind == "removed":
            level = "critical"
        elif change.kind == "changed":
            # Losing tool calling makes a model unusable as an agent; the run
            # will fail in a confusing way rather than an obvious one.
            if "tool_call" in change.fields and not change.fields["tool_call"][1]:
                level = "critical"
            elif "cost" in change.fields or "limit" in change.fields:
                level = "warning"
        affecting.append({
            "model": change.model, "kind": change.kind,
            "used_by": users, "severity": level, "detail": change.describe(),
        })
        if level == "critical" or (level == "warning" and severity != "critical"):
            severity = level
        elif severity == "none":
            severity = "info"

    return {
        "severity": severity,
        "affecting_roster": affecting,
        "unrelated_changes": len(changes) - len(affecting),
    }


def check(config_dir: Path, provider: str, agents: dict,
          url: str = CATALOG_URL) -> dict[str, Any]:
    """Fetch, compare, and report. Writes nothing — see :func:`apply`."""
    local = load_local(config_dir, provider)
    try:
        remote = fetch_remote(provider, url)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError,
            json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "provider": provider,
            "error": f"{type(exc).__name__}: {exc}",
            "note": "catalog unreachable; the local snapshot is unchanged",
            "have_local": local is not None,
        }

    if local is None:
        return {
            "ok": True,
            "provider": provider,
            "first_run": True,
            "models": len(remote.get("models") or {}),
            "changes": [],
            "assessment": {"severity": "none", "affecting_roster": [], "unrelated_changes": 0},
            "note": "no local snapshot yet; call update_model_catalog to record a baseline",
        }

    changes = diff(local, remote)
    return {
        "ok": True,
        "provider": provider,
        "fetched_at_local": local.get("fetched_at"),
        "models": len(remote.get("models") or {}),
        "changed": len(changes),
        "changes": [c.describe() for c in changes[:40]],
        "assessment": assess(changes, provider, agents),
    }


def apply(config_dir: Path, provider: str, url: str = CATALOG_URL) -> dict[str, Any]:
    """Record the current remote catalog as the new local baseline."""
    try:
        remote = fetch_remote(provider, url)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError,
            json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    path = save_local(config_dir, provider, remote, url)
    return {
        "ok": True,
        "provider": provider,
        "written": str(path),
        "models": len(remote.get("models") or {}),
    }
