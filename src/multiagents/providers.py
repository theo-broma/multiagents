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
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import spendcap
from .spendcap import SpendCap

# Normalised event kinds the rest of the system understands.
TEXT, TOOL, STEP, RESULT, RAW, ERROR = "text", "tool", "step", "result", "raw", "error"

# H8: the kernel refuses any single argv element longer than MAX_ARG_STRLEN
# (131072 bytes, the trailing NUL included) and the process then fails to
# start with an opaque E2BIG. The prompt travels as one argv element, so an
# oversize prompt must be refused here, in bytes, before anything launches.
MAX_ARG_STRLEN = 131072


def check_argv_limit(provider_name: str, argv: list[str]) -> None:
    """Refuse an argv element the kernel would reject (H8).

    Counted in UTF-8 bytes plus the NUL, the way execve counts. No run-file
    prompt transport exists yet, so the only remedy is a shorter task —
    which is what the error says rather than suggesting one silently.
    """
    for element in argv:
        size = len(element.encode("utf-8")) + 1
        if size > MAX_ARG_STRLEN:
            raise RuntimeError(
                f"provider {provider_name!r}: refusing to launch — one argv "
                f"element is {size} bytes, over the kernel's 128 KiB "
                f"({MAX_ARG_STRLEN}-byte) per-argument limit (MAX_ARG_STRLEN); "
                "the process would fail to start with E2BIG. Give the agent "
                "a shorter task — no prompt-file transport exists yet."
            )


_SELECTOR = re.compile(r"^([A-Za-z0-9_-]+)\[([A-Za-z0-9_-]+)=([^\]]+)\]$")


def get_path(obj: Any, path: str) -> Any:
    """Look up a dotted path, with list support. Missing anywhere yields ``None``.

    Three segment forms, because providers nest their payloads differently:

    ``a.b.c``
        plain dict keys.
    ``a.items.0.name``
        a numeric segment indexes a list.
    ``message.content[type=text].text``
        picks the first element of a list whose field equals a value. Claude
        emits ``message.content`` as a list of typed blocks — ``thinking`` and
        ``text`` interleaved — so without this its reply cannot be addressed at
        all. First match rather than all matches: each event carries one block
        of a kind, and successive events accumulate anyway.
    """
    cursor = obj
    for part in path.split("."):
        if isinstance(cursor, list):
            if part.isdigit() and int(part) < len(cursor):
                cursor = cursor[int(part)]
                continue
            return None

        selector = _SELECTOR.match(part)
        if selector is not None:
            key, field, wanted = selector.groups()
            if not isinstance(cursor, dict):
                return None
            candidates = cursor.get(key)
            if not isinstance(candidates, list):
                return None
            cursor = next(
                (c for c in candidates
                 if isinstance(c, dict) and str(c.get(field)) == wanted),
                None,
            )
            if cursor is None:
                return None
            continue

        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _given(value: Any) -> bool:
    """Is this option actually set?

    Empty means UNSET, not "set to empty". `AgentSpec.fallback_for`'s docstring
    tells people to write `effort: ""` in a `models:` entry to stop an option
    travelling to another provider — and until this, that produced `--effort ""`
    on the command line instead of dropping the flag. The real cost was the
    other half: an agent carrying `effort: medium` failed over onto
    `gemini-3.1-pro-high`, whose NAME already states the effort, and agy refused
    the pair outright — "--model gemini-3.1-pro-high conflicts with
    --effort=medium" — nine seconds into a run that had a worktree and a branch.
    """
    return value is not None and value != ""


def option_text(value: Any) -> str:
    """An option value as it appears on the command line.

    A bool is rendered deliberately as ``true``/``false``; ``str(True)`` is
    ``"True"``, which no CLI accepts and which reached argv by accident.
    Numbers keep their own text, so a fractional ``0.5`` stays ``0.5`` rather
    than being rounded or dropped.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_value(obj: Any, values: dict[str, Any]) -> Any:
    """`obj` with every string that is exactly ``{name}`` replaced by that value.

    Whole values only, recursively through lists and mappings, so a structured
    config can name a list or a mapping as easily as a string.
    """
    if isinstance(obj, str):
        if obj.startswith("{") and obj.endswith("}") and obj[1:-1] in values:
            return values[obj[1:-1]]
        return obj
    if isinstance(obj, list):
        return [_render_value(item, values) for item in obj]
    if isinstance(obj, dict):
        return {key: _render_value(value, values) for key, value in obj.items()}
    return obj


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
    cost: float = 0.0                    # dollars for this step, if reported
    step: int | None = None
    session_id: str = ""
    # One model turn's id, when the provider's rules declare one (see
    # `fields.turn` in providers.yaml). Empty when untagged.
    turn: str = ""
    # SC-R2a: the provider's own id for the step a cost belongs to (`fields.
    # step_id`), the spend ledger's dedup key. Empty when not reported.
    step_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    startup_progress: bool = False

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
class ResolvedBin:
    path: Path | None
    launcher: Path | None
    via: str
    searched: list[str]


@dataclass
class Provider:
    name: str
    bin: str
    spawn: dict[str, Any]
    stream: dict[str, Any]
    bin_search: list[str] = field(default_factory=list)
    models_cmd: list[str] = field(default_factory=list)
    models_parse: str = "lines"
    usage_mode: str = "cumulative"       # cumulative | delta
    # Text a CLI prints when it stopped a turn early of its own accord.
    truncation_markers: list[str] = field(default_factory=list)
    refusal_markers: list[str] = field(default_factory=list)
    models_include: list[str] = field(default_factory=list)
    models_exclude: list[str] = field(default_factory=list)
    models_static: list[dict[str, str]] = field(default_factory=list)
    home_links: list[str] = field(default_factory=list)
    home_copy: list[str] = field(default_factory=list)
    container_private_home: list[str] = field(default_factory=list)
    # Host config copied into that private profile, and host-pid state removed
    # from it. See DockerExecutor.seed_private_state.
    container_private_seed: list[str] = field(default_factory=list)
    container_private_reset: list[str] = field(default_factory=list)
    # One script per provider, carrying every action this CLI needs described
    # imperatively: check, login, budget, prepare, launch. Defaults to
    # "<name>.sh". Keeping it to a single file is the point — adding a provider
    # is a config block plus one script, not a scatter of hooks.
    script: str = ""
    enabled: bool = True
    # Where this CLI records the session it is running, for the supervisor to
    # watch from outside. Optional: a provider that keeps sessions in a database
    # or an opaque directory simply omits it.
    transcript: dict = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    docker: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    # Sent to the model, verbatim, as its own prompt section — unlike `notes`,
    # which is for whoever edits this file. See compose_prompt's use of it.
    agent_guidance: str = ""
    # --- more than one account on the same CLI ---------------------------
    #
    # A second subscription is a second PROVIDER: same binary, same script,
    # same parsing, different credentials. `extends` copies the integration so
    # that is four lines rather than a duplicated block; `family` says the two
    # share a MODEL NAMESPACE, which makes failover between them free — `opus`
    # means the same thing on both accounts, so no per-agent `models:` mapping
    # is needed to move work from one to the other.
    #
    # `env` is what actually separates them: CLAUDE_CONFIG_DIR relocates
    # claude's entire state, XDG_DATA_HOME does the same for opencode. It is
    # applied to every invocation — the script actions AND the agent runs —
    # because an instance that authenticates as B but runs as A is worse than
    # no second instance at all.
    extends: str = ""
    family: str = ""
    env: dict[str, str] = field(default_factory=dict)
    # PS-R1/R5: providers that share tooling but keep their own models and
    # quota. `auth_from` names the credential OWNER — this provider logs in
    # as, and shares the container profile of, that one. One level only: an
    # owner that declares its own `auth_from` is a config error, so there are
    # no chains or cycles to walk. `budget_from` names the quota
    # source the same way, and `budget_windows` says which windows of the
    # shared payload count for this provider (None = undeclared; the payload's
    # own counted flags then stand, as they always have).
    auth_from: str = ""
    budget_from: str = ""
    budget_windows: list[str] | None = None
    # The environment variable that relocates this provider's quota/credential
    # profile to a second account, when its built-in reader takes a directory
    # rather than reading the default one. Declared here, not named in the
    # reader: an instance that `extends` this provider inherits the field, and
    # the reader resolves the instance's own value so two accounts are not
    # conflated. Empty means the reader has no profile to point at.
    budget_profile_env: str = ""
    # A relocated HOME separates accounts only under this executor; other
    # executors still use the primary account's shared credential store.
    home_account_executor: str = ""
    # Container requests may be restricted to one account in the credential
    # owner's vault. Local profiles continue to be selected by `env`.
    container_account: str = ""
    # Attached by `load_providers`, not parsed from yaml: the owner's Provider
    # object (so a lone dependent can still reach its owner's script), and the
    # effective credential environment — the owner's `env` overlaid with this
    # provider's own. None (a Provider built directly rather than loaded)
    # means "use `env` alone", which is what every provider without the key
    # has always done.
    auth_owner: "Provider | None" = None
    budget_owner: "Provider | None" = None
    credential_env: dict[str, str] | None = None
    # Attached by `load_providers`, never parsed: the provider name whose
    # built-in budget reader this provider's `extends` chain reaches (itself
    # when it has one). "" means no built-in applies. Recorded where the whole
    # map is loaded so a caller that reads one provider alone — the watchdog,
    # `refresh-quota`, a runner — gets the same answer as `read_all`.
    budget_builtin: str = ""
    # Tools whose reported arguments do not identify the call (e.g. a
    # file-viewer that never reports which range it viewed) — repeating one
    # must not trip doom_loop on its own. See Supervisor.opaque_tools.
    opaque_tools: list[str] = field(default_factory=list)
    # bug-8615db: some tools are only a poll under ONE argument value and a
    # real repeat under another — a task-status tool call reports
    # Action: status identically on every check of a task it already
    # started (legitimate, must not trip) but Action: run launching the
    # same command again is
    # exactly the loop doom_loop exists to catch. `opaque_tools` cannot make
    # that distinction (it is unconditional on the tool name alone), so this
    # is scoped: each entry is `{tool: <name>, match: {<arg key>: [<values
    # that make it a poll>]}}`. See Supervisor.opaque_tool_args.
    opaque_tool_args: list[dict[str, Any]] = field(default_factory=list)
    # SM-R1: how this CLI is handed the multiagents MCP server when the agent it
    # runs may spawn. Declared, like everything else here; see `mcp_launch` and
    # the `mcp:` blocks in providers.yaml. Empty means the CLI cannot be given
    # one, and agents on it run without the server.
    mcp: dict[str, Any] = field(default_factory=dict)
    # CX-C1: an executable exec'd as argv[0] of an agent run in place of
    # `bin`, which keeps its meaning — the native CLI, what `available()` finds
    # and the container mounts. The adapter drives it, told where it is by
    # MULTIAGENTS_BIN. Resolved like the action script (project, global,
    # shipped), and it IS the action script when `script:` is absent.
    adapter: str = ""
    # CX-C3: the resolved target of `bin` sits this many directories below the
    # one holding every installed version, and the container mounts that root
    # rather than the one file. 0 is unset: today's P0-R1 behaviour.
    bin_versions_depth: int = 0
    # CX-C5: `metered` (dollars per run) or `plan` (paid through a
    # subscription, so a cost the stream never reports is not a zero).
    billing: str = "metered"
    # RM-R5a: model ids that carry their effort as a suffix, e.g.
    # `{"-low": "low", "-medium": "medium", "-high": "high"}`. A model id
    # ending in one of these suffixes IMPLIES that effort, so an inherited
    # `effort:` that contradicts it is normalised to the model's before
    # launch, and one explicitly configured on the destination route is
    # refused — the pair would otherwise be rejected by the CLI at launch,
    # nine seconds into a run that already had a worktree and a branch.
    effort_suffixes: dict[str, str] = field(default_factory=dict)
    # PC-R1: how many runs may hold a slot on this provider at once, across
    # the whole project. None (absent or null) is no limit, the default. Set
    # by `load_providers` from the RAW block, never through `extends`: the
    # limit belongs to the instance that declares it.
    max_concurrent: int | None = None
    # SC-R1: the validated `spend_cap` block, or None (the default: no cap).
    # Like `max_concurrent`, read from the RAW block, never through `extends`.
    spend_cap: SpendCap | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict) -> Provider:
        # PS-R1a: a relative explicit `bin` (one containing "/") is refused at
        # config load, exactly as a relative `bin_search` entry is — never
        # later, as a ValueError raised from `resolve_bin` mid-operation.
        bin_name = str(data.get("bin", name))
        if "/" in bin_name and not Path(bin_name).expanduser().is_absolute():
            raise ValueError("bin: containing '/' must be an absolute path")
        return cls(
            name=name,
            bin=bin_name,
            spawn=data.get("spawn", {}) or {},
            stream=data.get("stream", {}) or {},
            bin_search=_bin_search(data.get("bin_search")),
            models_cmd=list(data.get("models_cmd", []) or []),
            truncation_markers=data.get("truncation_markers", []) or [],
            refusal_markers=data.get("refusal_markers", []) or [],
            models_parse=data.get("models_parse", "lines"),
            usage_mode=data.get("usage_mode", "cumulative"),
            models_include=list(data.get("models_include", []) or []),
            models_exclude=list(data.get("models_exclude", []) or []),
            models_static=list(data.get("models", []) or []),
            home_links=list(data.get("home_links", []) or []),
            home_copy=list(data.get("home_copy", []) or []),
            container_private_home=list(data.get("container_private_home", []) or []),
            container_private_seed=list(data.get("container_private_seed", []) or []),
            container_private_reset=list(data.get("container_private_reset", []) or []),
            script=data.get("script", "") or (data.get("auth", {}) or {}).get("script", ""),
            enabled=bool(data.get("enabled", True)),
            transcript=dict(data.get("transcript", {}) or {}),
            auth=data.get("auth", {}) or {},
            docker=data.get("docker", {}) or {},
            notes=data.get("notes", ""),
            agent_guidance=data.get("agent_guidance") or "",
            extends=data.get("extends", "") or "",
            # An instance with no family stated belongs to the one it extends,
            # and a provider that extends nothing is its own family of one.
            family=data.get("family") or data.get("extends") or name,
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            auth_from=_owner_key(name, "auth_from", data),
            budget_from=_owner_key(name, "budget_from", data),
            budget_windows=_budget_windows(name, data),
            budget_profile_env=_profile_env_key(name, data),
            home_account_executor=str(data.get("home_account_executor") or ""),
            container_account=_container_account(data),
            opaque_tools=list(data.get("opaque_tools", []) or []),
            opaque_tool_args=list(data.get("opaque_tool_args", []) or []),
            mcp=dict(data.get("mcp") or {}),
            adapter=str(data.get("adapter") or ""),
            bin_versions_depth=_versions_depth(data.get("bin_versions_depth")),
            # Anything but `plan` is metered: a misspelling must not quietly
            # hide a real dollar figure.
            billing="plan" if data.get("billing") == "plan" else "metered",
            effort_suffixes=_effort_suffixes(data.get("effort_suffixes")),
        )

    # ------------------------------------------------------------- command --

    def resolve_bin(self, env: dict[str, str] | None = None) -> ResolvedBin:
        """Resolve this operation's host CLI, preserving its launcher path."""
        searched: list[str] = []

        def check(candidate: Path, via: str) -> ResolvedBin | None:
            candidate = candidate.absolute()
            searched.append(str(candidate))
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return ResolvedBin(candidate.resolve(), candidate, via, searched)
            except (OSError, RuntimeError):
                pass
            return None

        if "/" in self.bin:
            candidate = Path(self.bin).expanduser()
            if not candidate.is_absolute():
                # PS-R1a: config load refuses a relative `bin`; a Provider
                # built around that check resolves to nothing rather than
                # raising here.
                return ResolvedBin(None, None, "bin", searched)
            return check(candidate, "bin") or ResolvedBin(None, None, "bin", searched)
        path = (os.environ if env is None else env).get("PATH", "")
        if path:
            for directory in path.split(os.pathsep):
                found = check(Path(directory) / self.bin, "PATH")
                if found:
                    return found
        for directory in self.bin_search:
            found = check(Path(directory).expanduser() / self.bin, "bin_search")
            if found:
                return found
        return ResolvedBin(None, None, "bin_search" if self.bin_search else "PATH", searched)

    def bin_error(self, resolved: ResolvedBin | None = None) -> str:
        resolved = resolved if resolved is not None else self.resolve_bin()
        places = ", ".join(resolved.searched) or "no directories (PATH is empty)"
        return (f"Provider {self.name!r}: binary {self.bin!r} not found; searched: {places}. "
                "Set bin: to an absolute path, or add its directory to bin_search: "
                "in providers.yaml.")

    def available(self) -> str | None:
        """Absolute resolved CLI path, rechecked on every operation."""
        resolved = self.resolve_bin()
        return str(resolved.path) if resolved.path is not None else None

    @property
    def script_name(self) -> str:
        """The provider's script filename: `script:`, else the adapter (CX-C1),
        else ``<name>.sh``."""
        return self.script or self.adapter or f"{self.name}.sh"

    def usable(self) -> bool:
        """Enabled by the user AND actually present on this machine."""
        return self.enabled and self.available() is not None

    def implied_effort(self, model: str) -> str | None:
        """The effort this provider's model id declares by its suffix (RM-R5a).

        The suffix is anchored at the END of the id: `gem-low-preview` does
        not imply `low`. An id matching no suffix implies nothing.
        """
        for suffix, effort in self.effort_suffixes.items():
            if suffix and model.endswith(suffix):
                return effort
        return None

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        workdir: str,
        permission: str = "full",
        session_id: str | None = None,
        options: dict[str, Any] | None = None,
        timeout: int | None = None,
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
            # This run's wall-clock budget, in the two shapes CLIs ask for.
            # A CLI with a timeout of its own MUST be told ours or it enforces
            # its own default: agy's print mode stops at 5 minutes, exits 0 and
            # returns partial output, which parses as a clean finish.
            "timeout": str(int(timeout or 0)),
            "timeout_s": f"{int(timeout or 0)}s",
            **{k: option_text(v) for k, v in (options or {}).items() if _given(v)},
        }

        def render(tokens: Iterable[str]) -> list[str]:
            out = []
            for token in tokens:
                if token.startswith("{") and token.endswith("}") and token[1:-1] in values:
                    out.append(str(values[token[1:-1]]))
                else:
                    out.append(token)
            return out

        # CX-C1: the adapter by name; the runner resolves it to its path.
        argv = [self.adapter or self.bin, *render(self.spawn.get("args", []))]

        if session_id and self.spawn.get("resume"):
            argv += render(self.spawn["resume"])

        perms = (self.spawn.get("permission") or {}).get(permission)
        if perms:
            argv += render(perms)

        for key, template in (self.spawn.get("optional") or {}).items():
            if _given((options or {}).get(key)):
                argv += render(template)

        return argv

    # ----------------------------------------------------------------- mcp --

    def mcp_launch(self, values: dict[str, Any]) -> dict[str, Any]:
        """The pieces that hand this CLI the multiagents MCP server (SM-R1).

        `values` carries the server as `mcp_command` (str), `mcp_args` (list),
        `mcp_argv` (both, as one list) and `mcp_env` (dict), plus `mcp_config`,
        the path the rendered config will be written to. Returned:

        ``config``     the file's content, placeholders rendered
        ``file``       its name under the run's own directory, or
        ``home_file``  its path inside the agent's private HOME, for a CLI that
                       reads its servers from nowhere else
        ``merge``      start from the user's own copy of ``home_file``
        ``args``/``env``  what the command line and environment gain

        Placeholders are whole values, as in `build_command`: a string that is
        exactly ``{name}`` becomes that value, list or mapping included.
        """
        block = self.mcp or {}
        return {
            "config": _render_value(block.get("config") or {}, values),
            "file": str(block.get("file") or ""),
            "home_file": str(block.get("home_file") or ""),
            "merge": bool(block.get("merge")),
            "args": [str(a) for a in _render_value(list(block.get("args") or []), values)],
            "env": {str(k): str(v) for k, v in
                    _render_value(dict(block.get("env") or {}), values).items()},
        }

    def mcp_unavailable(self, payload: dict) -> str:
        """The status a stream line reports for a server that did not start, or "".

        SM-R5. Declared as ``mcp.unavailable``: a ``match`` like a stream rule's,
        the dotted ``status`` path, and the ``values`` that mean it failed.
        """
        rule = (self.mcp or {}).get("unavailable") or {}
        if not rule.get("status"):
            return ""
        match = rule.get("match") or {}
        if not all(get_path(payload, key) == value for key, value in match.items()):
            return ""
        status = get_path(payload, str(rule["status"]))
        failed = [str(v) for v in rule.get("values") or []]
        return str(status) if status is not None and str(status) in failed else ""

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
            return Event(kind=TEXT, text=line, raw={"line": line}, startup_progress=True)

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
                signal = ""
                for path, mapping in (rule.get("status_map") or {}).items():
                    value = get_path(payload, path)
                    if value is not None and str(value) in mapping:
                        signal = str(mapping[str(value)])
                        break
                tokens = extracted.get("tokens")
                args = extracted.get("args")
                step = extracted.get("step")
                raw_cost = extracted.get("cost")
                cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else 0.0
                kind = rule.get("as", RAW)
                text = str(extracted.get("text") or "")
                status = signal or str(extracted.get("status") or "")
                # Result failures may carry error prose, not assistant output
                # (e.g. the adapter's turn.failed). Content, tools and output
                # usage are declared by each provider's rules; STEP alone is
                # still only a watchdog signal.
                assistant_text = (kind == TEXT or (kind == RESULT and
                    status.upper() in {"", "SUCCESS", "OK", "COMPLETED", "DONE"}))
                output_usage = any(
                    isinstance(value, (int, float)) and value > 0
                    for key, value in (tokens if isinstance(tokens, dict) else {}).items()
                    if key in {"output", "output_tokens", "outputTokens",
                               "candidatesTokenCount"})
                progress = (kind not in (RAW, ERROR) and
                            (kind == TOOL or (assistant_text and bool(text.strip()))
                             or output_usage))
                # PS-R4a: some lines only look like the model having run. A
                # provider declares those shapes in `stream.no_progress`, a
                # list of matches like a rule's — one CLI reports an API
                # failure as an assistant message from the model "<synthetic>"
                # inside a result whose `is_error` is true, and counting
                # either as startup progress would keep a dead provider
                # admitted and recover a probe that never ran the model.
                if progress and any(
                        all(get_path(payload, key) == value
                            for key, value in guard.items())
                        for guard in self.stream.get("no_progress") or []
                        if isinstance(guard, dict)):
                    progress = False
                return Event(
                    kind=kind,
                    name=str(extracted.get("name") or ""),
                    args=args if isinstance(args, dict) else ({} if args is None else {"_": args}),
                    text=text,
                    state=str(extracted.get("state") or ""),
                    status=status,
                    tokens=tokens if isinstance(tokens, dict) else {},
                    cost=cost,
                    step=step if isinstance(step, int) else None,
                    session_id=session_id,
                    turn=str(extracted.get("turn") or ""),
                    step_id=str(extracted.get("step_id") or ""),
                    raw=payload,
                    startup_progress=progress,
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


def _versions_depth(value: Any) -> int:
    """`bin_versions_depth` as a positive int, or 0 (unset) for anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 1 else 0


def _container_account(data: dict) -> str:
    value = data.get("container_account", "")
    if value == "":
        return ""
    from .authproxy import validate_label
    return validate_label(value)


def resolve_inheritance(raw: dict[str, Any]) -> dict[str, Any]:
    """Fold `extends:` so an instance is a few lines, not a copied integration.

    Shallow-merged one level deep, the same way the config layers are: an
    instance overrides the keys it names and inherits the rest. A missing base
    is left alone rather than raised on — a provider that names a base which is
    disabled or absent should degrade to "unavailable", not stop the CLI from
    starting.
    """
    from .config import deep_merge

    out: dict[str, Any] = {}
    for name, data in raw.items():
        data = dict(data or {})
        base_name = data.get("extends")
        seen = set()
        while base_name and base_name in raw and base_name not in seen:
            seen.add(base_name)
            base = dict(raw[base_name] or {})
            base.pop("extends", None)
            data = deep_merge(base, data)
            base_name = (raw[base_name] or {}).get("extends")
        out[name] = data
    return out


def _instance_conflicts(providers: dict[str, Provider]) -> list[str]:
    """Instances of one family that would fight over the same state.

    Two claude profiles both claiming `~/.claude` inside a container would be
    two mounts at one destination, and both would authenticate as whichever won.
    Inheriting the parent's private home is the natural way to write that by
    accident, so it is checked rather than documented.
    """
    problems = []
    claimed: dict[str, str] = {}
    for name, provider in sorted(providers.items()):
        for relative in provider.container_private_home:
            owner = claimed.get(relative)
            if owner:
                problems.append(
                    f"{name} and {owner} both claim ~/{relative} as their "
                    f"container profile; give {name} its own path and an `env:` "
                    f"entry pointing its CLI at it")
            claimed[relative] = name
    return problems


def billed_rows(rows: list[dict[str, Any]],
                providers: dict[str, Provider]) -> list[dict[str, Any]]:
    """`usage_by_model` rows, a `plan` provider's marked `"billing": "plan"`
    (CX-C5). `cost_usd` stays the number it was: it is what the tokens
    would have cost, not what was billed, and the label says which."""
    plan = {name for name, p in providers.items() if p.billing == "plan"}
    return [{**row, "billing": "plan"} if row.get("provider") in plan else row
            for row in rows]


def load_providers(raw: dict[str, Any]) -> dict[str, Provider]:
    resolved = resolve_inheritance(raw)
    providers = {name: Provider.from_dict(name, data or {})
                 for name, data in resolved.items()}
    # PS-R1/R5: the sharing keys are validated against the RAW blocks (only
    # there can an explicit `env:` entry be told from an inherited one), then
    # the owners are attached.
    _validate_sharing(providers, raw)
    for name, provider in providers.items():
        provider.max_concurrent = _max_concurrent(name, raw.get(name) or {})
        provider.spend_cap = spendcap.parse(name, raw.get(name) or {},
                                            provider.models_include)
        provider.budget_builtin = _builtin_owner(name, raw)
        _validate_budget_profile(name, provider, raw)
    return providers


def _validate_budget_profile(name: str, provider: "Provider",
                             raw: dict[str, Any]) -> None:
    """A declared `budget_profile_env` must name a reader that takes a profile.

    The variable relocates an INSTANCE's account, and only a reader that
    declares `config_dir` can be pointed at one. Declaring the variable for a
    reader that takes only the caller's spend would read the base account and
    relabel it, so it is refused here, at load, rather than mis-reported later.
    Decided by the reader's signature, never by name.

    A provider that declares its OWN script (`script:` or `auth.script:`) is
    exempt: that script may implement the `budget` action and read its own
    state, in which case the built-in reader is never reached and its
    signature says nothing. An inherited declaration is also exempt when the
    base that declares it owns a script: that script declares profile support
    for its instances. Merely inheriting a script does not exempt a NEW profile
    variable declared by the instance.
    """
    field = provider.budget_profile_env
    if not field:
        return
    current = name
    block = raw.get(current) or {}
    while "budget_profile_env" not in block and block.get("extends"):
        if block.get("script") or (block.get("auth") or {}).get("script"):
            return
        current = block["extends"]
        block = raw.get(current) or {}
    if block.get("script") or (block.get("auth") or {}).get("script"):
        return
    from .budget import _BUILTIN, reader_takes_profile

    reader = _BUILTIN.get(provider.budget_builtin)
    if reader is not None and not reader_takes_profile(reader):
        raise ValueError(
            f"provider {name!r}: budget_profile_env: {field!r} cannot apply — "
            f"the built-in reader it inherits from {provider.budget_builtin!r} "
            f"reads only the default account, so it has no profile to point at; "
            f"remove the key, or give this provider its own `script` "
            f"implementing the `budget` action, which then reads its own state")


def _builtin_owner(name: str, raw: dict[str, Any]) -> str:
    """The provider whose built-in budget reader `name` reaches through
    `extends`; itself when it has one, "" when none does.

    Resolved here because this is where the whole map is in hand. A caller that
    later reads a single provider has only its immediate `extends`, and a
    two-hop chain would otherwise resolve differently per caller. The registry
    of readers lives in `budget`, imported lazily so neither module needs the
    other at import time.
    """
    from .budget import _BUILTIN

    seen = {name}
    current = name
    while current:
        if current in _BUILTIN:
            return current
        current = (raw.get(current) or {}).get("extends") or ""
        if current in seen:
            return ""
        seen.add(current)
    return ""


def _max_concurrent(provider_name: str, data: dict) -> int | None:
    """PC-R1: `max_concurrent` is an integer of at least 1, or null/absent.

    Anything else — zero, a negative, a float (even a whole one), a bool, a
    string — is a config error at load, naming the key and the provider.
    """
    value = data.get("max_concurrent")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"provider {provider_name!r}: max_concurrent: must be "
                         f"an integer of at least 1, or null for no limit, "
                         f"not {value!r}")
    return value


def families(providers: dict[str, Provider]) -> dict[str, list[str]]:
    """{family: [provider names]} — who can take whose work without remapping."""
    out: dict[str, list[str]] = {}
    for name, provider in providers.items():
        out.setdefault(provider.family or name, []).append(name)
    return {family: sorted(names) for family, names in out.items()}


def _bin_search(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("bin_search: must be a list of absolute directories")
    for directory in value:
        if not isinstance(directory, str) or not Path(directory).expanduser().is_absolute():
            raise ValueError("bin_search: entries must be absolute directories")
    return list(value)


def _owner_key(provider_name: str, key: str, data: dict) -> str:
    """PS-R10: a sharing key must be a non-empty string, when it is present.

    A missing key is unset; anything written — empty, null, a number, a list,
    a boolean — is a config error at load, never a value that reaches an
    auth or budget read.
    """
    if key not in (data or {}):
        return ""
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"provider {provider_name!r}: {key}: must name a "
                         f"provider (a non-empty string), not {value!r}")
    return value


def _budget_windows(provider_name: str, data: dict) -> list[str] | None:
    """PS-R10: `budget_windows` is a list of glob strings, or absent.

    An empty list is a real answer — it selects nothing, so the provider's
    reading is unknown — and is kept distinct from an absent key, which leaves
    the payload's own counted flags in force. An explicitly written null is
    neither: it is a config error, not a silent return to aggregate selection
    (review ag-4cdd7b, finding 11).
    """
    if "budget_windows" not in (data or {}):
        return None
    value = data["budget_windows"]
    if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value):
        raise ValueError(f"provider {provider_name!r}: budget_windows: must be "
                         f"a list of window-name globs, not {value!r}")
    return list(value)


def _profile_env_key(provider_name: str, data: dict) -> str:
    """`budget_profile_env` is the name of an environment variable, or absent.

    A missing key is unset (the reader reads its default profile); anything
    written — null, a number, a list, an empty string — is a config error at
    load, never a value that reaches a budget read.
    """
    if "budget_profile_env" not in (data or {}):
        return ""
    value = data["budget_profile_env"]
    if not isinstance(value, str) or not value:
        raise ValueError(f"provider {provider_name!r}: budget_profile_env: must "
                         f"name an environment variable (a non-empty string), "
                         f"not {value!r}")
    return value


def expand_env_value(value: Any) -> str:
    """One `env:` value with `$VAR` and `~` resolved, the way a launch resolves it.

    The single definition of that expression: `build_env`, `_launch` and the
    built-in budget reader all call it, so a value cannot mean one directory to
    the CLI and another to whoever reports its quota.
    """
    return os.path.expanduser(os.path.expandvars(str(value)))


def resolved_profile(provider: Any) -> str:
    """The provider's account profile, resolved as every launch resolves it.

    `budget_profile_env` names the variable that relocates the CLI's account;
    this is its value after `$VAR`/`~` expansion — the same expansion
    `build_env` and `_launch` give every `env:` value. A value still relative
    after that is made absolute against the user's home: a profile is per-user
    state, and neither the launching process's cwd nor the agent's worktree —
    which differ between the launcher and the budget reader — is a meaningful
    base for it. Empty when the provider declares no variable, or sets it to
    nothing.
    """
    field = getattr(provider, "budget_profile_env", "") or ""
    if not field:
        return ""
    env = getattr(provider, "credential_env", None) \
        or getattr(provider, "env", None) or {}
    value = env.get(field)
    if not value:
        return ""
    resolved = expand_env_value(value)
    if not os.path.isabs(resolved):
        resolved = str(Path.home() / resolved)
    return resolved


def credential_owner(name: str, providers: dict[str, Any] | None) -> str:
    """The provider whose credentials `name` uses: its `auth_from` owner,
    or itself. One level, guaranteed by the load checks in `load_providers`."""
    provider = (providers or {}).get(name)
    owner = str(getattr(provider, "auth_from", "") or "")
    return owner or name


def _validate_sharing(providers: dict[str, Provider], raw: dict[str, Any]) -> None:
    """PS-R1/PS-R5 load rules, and the per-provider attachments they enable.

    Each facet is judged on its own key: `auth_from` names a provider that
    does not itself declare `auth_from`, and `budget_from` one that does not
    itself declare `budget_from` — no chains, no cycles, one level. Borrowing
    one facet from a provider that borrows the OTHER is not a chain (review
    ag-3644ef, finding 7): `budget_from: login` where `login` takes its
    credentials elsewhere still reads `login`'s own budget source.

    The env-conflict rule is checked against the RAW blocks, because
    `resolve_inheritance` has already folded `env:` dicts together and could
    no longer tell an explicitly written key from an inherited one (PS-R1a):
    only an explicit value that differs from the owner's is rejected.

    With all of that satisfied, each dependent is attached to its owners'
    Provider objects and gets its effective credential environment — the
    owner's `env` overlaid with its own, which the absence of conflicts makes
    unambiguous.
    """
    for name, provider in providers.items():
        for key, attach in (("auth_from", "auth_owner"),
                            ("budget_from", "budget_owner")):
            owner_name = getattr(provider, key)
            if not owner_name:
                continue
            if owner_name == name:
                raise ValueError(f"provider {name!r}: {key}: cannot name the "
                                 f"provider itself")
            owner = providers.get(owner_name)
            if owner is None:
                raise ValueError(f"provider {name!r}: {key}: names provider "
                                 f"{owner_name!r}, which is not declared in "
                                 f"providers.yaml")
            if getattr(owner, key):
                raise ValueError(f"provider {name!r}: {key}: names provider "
                                 f"{owner_name!r}, which declares its own "
                                 f"{key}; this is shared one level only, so "
                                 f"{owner_name!r} must own it outright")
            setattr(provider, attach, owner)
        owner = provider.auth_owner
        if owner is None:
            continue
        explicit = (raw.get(name) or {}).get("env") or {}
        clashes = sorted(
            key for key, value in explicit.items()
            if key in owner.env and str(owner.env[key]) != str(value))
        if clashes:
            raise ValueError(
                f"provider {name!r}: auth_from: {provider.auth_from!r} owns these "
                f"credentials, and its environment already sets "
                f"{', '.join(clashes)}; sharing the login means sharing those "
                f"values, so the differing entries must go")
        # PS-R2/R1a: this provider's own `env` with the owner's laid OVER it.
        # The credential keys always come from the owner: an explicit
        # conflict is rejected above, and a value merely inherited through
        # `extends` from some other base must not replace the owner's
        # profile (review ag-a50515, finding 3) — the check would read the
        # owner's login while the launch used another one.
        provider.credential_env = {**provider.env, **owner.env}


def _effort_suffixes(value: Any) -> dict[str, str]:
    """RM-R5c: `effort_suffixes` maps an anchored model-id suffix to a
    non-empty effort name, and is validated at load.

    Anything else is a config error here, never a value that reaches a
    launch: `str(v)` used to keep a YAML null (`-low:` with nothing after
    it) as the effort string "None", which RM-R5a's normalisation then
    passed to the CLI as `--effort None` — manufacturing exactly the
    launch-time rejection the suffix map exists to prevent. A non-mapping
    fails here too, rather than mid-start.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("effort_suffixes: must be a mapping of model-id "
                         "suffix to effort")
    out: dict[str, str] = {}
    for key, effort in value.items():
        if not isinstance(effort, str) or not effort:
            raise ValueError(f"effort_suffixes: {str(key)!r} maps to "
                             f"{effort!r}; the effort must be a non-empty string")
        out[str(key)] = effort
    return out
