"""Provider adapters — declarative, so adding a CLI means editing YAML.

A provider block says three things: how to build the command line, how to read
the event stream it prints, and where its credentials live. No Python is
required to add an integration whose output is line-delimited JSON, which covers
both CLIs shipped here and most others.

The stream rules are ordered and first-match-wins. Each rule has a ``match``
(dotted paths that must all equal the given values) and ``fields`` (dotted paths
lifted into a normalised event). Anything unmatched is preserved as a ``raw``
event rather than dropped, so ``multiagents probe`` can show you exactly which
lines a new provider's rules are failing to classify.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
from dataclasses import dataclass, field
from typing import Any, Iterable

# Normalised event kinds the rest of the system understands.
TEXT, TOOL, STEP, RESULT, RAW, ERROR = "text", "tool", "step", "result", "raw", "error"


def get_path(obj: Any, path: str) -> Any:
    """Look up ``a.b.c`` in nested dicts. Missing anywhere yields ``None``."""
    cursor = obj
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


@dataclass
class Event:
    """One normalised stream event."""

    kind: str
    name: str = ""                       # tool name
    args: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    state: str = ""                      # tool/step outcome, provider-specific
    status: str = ""                     # final run status
    tokens: dict[str, Any] = field(default_factory=dict)
    step: int | None = None
    session_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def loop_signature(self) -> str | None:
        """Stable hash input for doom-loop detection: what was called, with what."""
        if self.kind != TOOL or not self.name:
            return None
        try:
            args = json.dumps(self.args, sort_keys=True, default=str)[:2000]
        except (TypeError, ValueError):
            args = str(self.args)[:2000]
        return f"{self.name}:{args}"


@dataclass
class Provider:
    name: str
    bin: str
    spawn: dict[str, Any]
    stream: dict[str, Any]
    models_cmd: list[str] = field(default_factory=list)
    models_parse: str = "lines"
    models_include: list[str] = field(default_factory=list)
    models_exclude: list[str] = field(default_factory=list)
    home_links: list[str] = field(default_factory=list)
    docker: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> Provider:
        return cls(
            name=name,
            bin=data.get("bin", name),
            spawn=data.get("spawn", {}) or {},
            stream=data.get("stream", {}) or {},
            models_cmd=list(data.get("models_cmd", []) or []),
            models_parse=data.get("models_parse", "lines"),
            models_include=list(data.get("models_include", []) or []),
            models_exclude=list(data.get("models_exclude", []) or []),
            home_links=list(data.get("home_links", []) or []),
            docker=data.get("docker", {}) or {},
            notes=data.get("notes", ""),
        )

    # ------------------------------------------------------------- command --

    def available(self) -> str | None:
        """Absolute path to the binary, or None if it is not on PATH."""
        return shutil.which(self.bin)

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        workdir: str,
        permission: str = "full",
        session_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> list[str]:
        """Render the argv for one run.

        Placeholders are substituted whole-token, never string-formatted into
        arbitrary text, so a prompt containing braces cannot corrupt the command.
        """
        values = {
            "prompt": prompt,
            "model": model,
            "workdir": workdir,
            "session_id": session_id or "",
            **{k: str(v) for k, v in (options or {}).items() if v is not None},
        }

        def render(tokens: Iterable[str]) -> list[str]:
            out = []
            for token in tokens:
                if token.startswith("{") and token.endswith("}") and token[1:-1] in values:
                    out.append(str(values[token[1:-1]]))
                else:
                    out.append(token)
            return out

        argv = [self.bin, *render(self.spawn.get("args", []))]

        if session_id and self.spawn.get("resume"):
            argv += render(self.spawn["resume"])

        perms = (self.spawn.get("permission") or {}).get(permission)
        if perms:
            argv += render(perms)

        for key, template in (self.spawn.get("optional") or {}).items():
            if (options or {}).get(key) is not None:
                argv += render(template)

        return argv

    # -------------------------------------------------------------- stream --

    @property
    def stream_format(self) -> str:
        return self.stream.get("format", "ndjson")

    def _session_id(self, payload: dict) -> str:
        for path in self.stream.get("session_id_paths", []):
            value = get_path(payload, path)
            if isinstance(value, str) and value:
                return value
        return ""

    def parse_line(self, line: str) -> Event | None:
        """Turn one line of the CLI's stdout into a normalised event."""
        line = line.strip()
        if not line:
            return None

        if self.stream_format == "text":
            return Event(kind=TEXT, text=line, raw={"line": line})

        if not line.startswith("{"):
            # CLIs interleave human-readable notices with their JSON stream;
            # keep them as raw events so nothing is silently lost.
            return Event(kind=RAW, text=line, raw={"line": line})

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return Event(kind=RAW, text=line, raw={"line": line})
        if not isinstance(payload, dict):
            return Event(kind=RAW, raw={"value": payload})

        session_id = self._session_id(payload)

        for rule in self.stream.get("rules", []):
            match = rule.get("match", {}) or {}
            if all(get_path(payload, key) == value for key, value in match.items()):
                fields = rule.get("fields", {}) or {}
                extracted = {k: get_path(payload, p) for k, p in fields.items()}
                tokens = extracted.get("tokens")
                args = extracted.get("args")
                step = extracted.get("step")
                return Event(
                    kind=rule.get("as", RAW),
                    name=str(extracted.get("name") or ""),
                    args=args if isinstance(args, dict) else ({} if args is None else {"_": args}),
                    text=str(extracted.get("text") or ""),
                    state=str(extracted.get("state") or ""),
                    status=str(extracted.get("status") or ""),
                    tokens=tokens if isinstance(tokens, dict) else {},
                    step=step if isinstance(step, int) else None,
                    session_id=session_id,
                    raw=payload,
                )

        return Event(kind=RAW, session_id=session_id, raw=payload)

    # -------------------------------------------------------------- models --

    def allows_model(self, model_id: str) -> bool:
        """Is this model id one we want recorded as available?

        A CLI may offer models that bill against a different account than the
        one you intend agents to use — opencode lists ``deepinfra/*`` alongside
        the subscription's own models. ``models_include`` keeps the generated
        list to what the subscription actually covers.
        """
        if self.models_exclude and any(
            fnmatch.fnmatch(model_id, pattern) for pattern in self.models_exclude
        ):
            return False
        if not self.models_include:
            return True
        return any(fnmatch.fnmatch(model_id, pattern) for pattern in self.models_include)

    def parse_models(self, output: str) -> list[dict[str, str]]:
        models: list[dict[str, str]] = []
        for line in output.splitlines():
            line = line.rstrip()
            if not line or line.startswith(("Fetching", "  ", "\t")) and self.models_parse == "lines":
                continue
            if self.models_parse == "tsv" and "\t" in line:
                ident, _, label = line.partition("\t")
                if self.allows_model(ident.strip()):
                    models.append({"id": ident.strip(), "label": label.strip()})
            elif self.models_parse == "lines":
                if line.startswith("Fetching") or " " in line.strip():
                    continue
                if self.allows_model(line.strip()):
                    models.append({"id": line.strip(), "label": ""})
        return models


def load_providers(raw: dict[str, Any]) -> dict[str, Provider]:
    return {name: Provider.from_dict(name, data or {}) for name, data in raw.items()}
