"""The config, described well enough to edit safely — and written back in place.

Two problems, one module.

**Describing.** A settings screen needs to know that `executor.kind` is one of
two words, that `limits.max_concurrent` is a number, that an agent's model
belongs to its provider's namespace, and that `push_agent_branches` is a switch.
None of that is in the YAML. Rather than hand-maintaining a schema beside the
config — two lists to keep in step, one of which will rot — the schema is
*derived*: types come from the values already there, choices from the config
itself (providers from `providers.yaml`, models from the catalog), and the help
text from the comment above each key. Those comments are the project's real
documentation, and this is what makes them show up where the decision is made.

**Writing.** The shipped YAML is mostly comments, and they explain what every
setting costs. Loading with `yaml.safe_load` and dumping the result back would
delete all of it — the config would still work and would stop teaching anybody
anything. So writes are surgical: find the line that defines the key, replace
the value on it, leave every other byte alone. A key that does not exist yet is
inserted under its parent block at the right indent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

# Files a user may edit from the monitor. `models.yaml` is deliberately absent:
# it says at the top that it is generated, and `refresh-models` rewrites it.
EDITABLE = ("project.yaml", "agents.yaml", "providers.yaml")

# Choices that cannot be derived from the data, because they are enumerated in
# code rather than in config. Keyed by the tail of the dotted path.
FIXED_CHOICES = {
    "executor.kind": ["local", "docker"],
    "permission": ["full", "sandbox", "readonly"],
    "home_policy": ["per-agent", "shared", "host"],
    "role": ["", "orchestrator", "initializer"],
    "effort": ["", "low", "medium", "high"],
}

# Keys whose value is a free-text secret-ish or path-ish string that a dropdown
# would only get in the way of.
FREE_TEXT = ("bin", "remote", "base_branch", "branch_prefix", "instructions",
             "description", "script_name", "container", "image")


# --------------------------------------------------------------------------
# reading the file as lines, keeping everything


class YamlFile:
    """A YAML file addressed by dotted path, edited by line.

    Deliberately not a YAML round-tripper. Round-trippers exist, they are a
    dependency, and they still reformat more than they promise. Everything the
    monitor writes is one scalar on one line, and for that a line is the right
    unit: the bytes around it are guaranteed untouched because they are never
    read into a model and written back out.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lines = path.read_text().splitlines() if path.is_file() else []
        self.data = yaml.safe_load("\n".join(self.lines)) or {} if self.lines else {}

    # -- addressing ------------------------------------------------------

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    BLOCK = re.compile(r":\s*[|>][+-]?\d*\s*(#.*)?$")

    def _key_lines(self) -> list[tuple[int, int, str]]:
        """``(line number, indent, key)`` for every mapping key in the file.

        Block scalars are skipped wholesale. `description: >-` is followed by
        prose, and prose contains lines like "Use it when: ..." — which look
        exactly like keys and are not. Indexing one would offer it as a setting
        and, worse, let an edit write a value into the middle of somebody's
        agent brief.
        """
        out = []
        skip_below: int | None = None
        for number, line in enumerate(self.lines):
            stripped = line.strip()
            indent = self._indent(line)
            if skip_below is not None:
                if not stripped or indent > skip_below:
                    continue                  # still inside the block scalar
                skip_below = None
            if not stripped or stripped.startswith("#") or stripped.startswith("- "):
                continue
            match = re.match(r'^(["\']?)([\w.\-/]+)\1\s*:(\s|$)', stripped)
            if not match:
                continue
            out.append((number, indent, match.group(2)))
            if self.BLOCK.search(line):
                skip_below = indent
        return out

    def indent_step(self) -> int:
        """How far this file indents a child. Measured, not assumed.

        Two spaces is the convention and not the rule; a file written with four
        would get a new key at the wrong depth, which is a different key.
        """
        keys = self._key_lines()
        for (_, outer, _), (_, inner, _) in zip(keys, keys[1:]):
            if inner > outer:
                return inner - outer
        return 2

    def find(self, path: list[str]) -> int | None:
        """The line defining ``path``, or None.

        Indentation is the only guide, which is all YAML gives you: keep a
        stack of the keys currently open, pop whatever this line's indent has
        closed, and the stack IS the dotted path at every line.
        """
        stack: list[tuple[int, str]] = []
        for number, indent, key in self._key_lines():
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key))
            if [k for _, k in stack] == path:
                return number
        return None

    # -- comments as documentation ---------------------------------------

    def comment_above(self, line_number: int) -> str:
        """The contiguous comment block immediately above a line."""
        out = []
        index = line_number - 1
        while index >= 0:
            stripped = self.lines[index].strip()
            if not stripped.startswith("#"):
                break
            out.append(stripped.lstrip("#").strip())
            index -= 1
        # Rule-off lines separate sections in these files; as help text they are
        # noise with the width of a paragraph.
        text = [line for line in reversed(out) if set(line) - set("-=_ ")]
        return "\n".join(text).strip()

    def inline_comment(self, line_number: int) -> str:
        _, _, tail = self.lines[line_number].partition(":")
        return self._split_comment(tail)[1].lstrip("# ").strip()

    def help_for(self, path: list[str]) -> str:
        """What this file says about a setting, wherever it says it.

        Three places, in the order a reader would look: the block above the key,
        the note at the end of its line, and — for a key whose explanation was
        written once for the whole group, which is how most of this config is
        commented — the block above its parent.
        """
        line = self.find(path)
        if line is None:
            return ""
        text = self.comment_above(line) or self.inline_comment(line)
        if text or len(path) < 2:
            return text
        parent = self.find(path[:-1])
        return self.comment_above(parent) if parent is not None else ""

    # -- writing ----------------------------------------------------------

    @staticmethod
    def render(value: Any) -> str:
        """One scalar, as YAML, on one line."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(YamlFile.render(v) for v in value) + "]"
        text = str(value)
        dumped = yaml.safe_dump(text, default_flow_style=True).strip()
        return dumped.removesuffix("...").strip() or '""'

    @staticmethod
    def _split_comment(tail: str) -> tuple[str, str]:
        """Split `value  # note` into value and note, respecting quotes.

        Scanned rather than matched. The first version used a regex with a
        guard for a quoted value, and the guard was wrong in the ordinary case:
        `key: "value"  # note` starts with a quote and contains a hash, so the
        guard fired and the note was deleted. A `#` is a comment only outside
        quotes and only after whitespace; that is three rules, and three rules
        are a loop, not a pattern.
        """
        quote = ""
        for index, char in enumerate(tail):
            if quote:
                if char == quote and tail[index - 1: index] != "\\":
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char == "#" and index and tail[index - 1] in " \t":
                return tail[:index].rstrip(), tail[index:]
        return tail.rstrip(), ""

    def _replace_value(self, line: str, rendered: str) -> str:
        """Swap the value on a `key: value` line, keeping its inline comment.

        Kept in its COLUMN, not merely kept. These files align their trailing
        comments, and a value that changes width would otherwise leave one
        comment out of line with the block it belongs to — a diff about
        whitespace, in a file whose comments are the documentation.
        """
        head, _, tail = line.partition(":")
        _, comment = self._split_comment(tail)
        if not comment:
            return f"{head}: {rendered}"
        column = line.index(comment, len(head))
        rebuilt = f"{head}: {rendered}"
        return rebuilt + " " * max(2, column - len(rebuilt)) + comment

    def set(self, path: list[str], value: Any) -> str:
        """Set ``path``, and return a unified-diff-ish description of the edit."""
        line_number = self.find(path)
        if line_number is not None:
            before = self.lines[line_number]
            if re.match(r"^\s*[\w.\-\"']+\s*:\s*$", before):
                raise ValueError(
                    f"{'.'.join(path)} is a block, not a value; edit the file "
                    f"by hand for structural changes")
            after = self._replace_value(before, self.render(value))
            self.lines[line_number] = after
            return f"-{before}\n+{after}"
        return self._insert(path, value)

    def _insert(self, path: list[str], value: Any) -> str:
        """Add a key the file does not have, under the deepest parent it does."""
        for depth in range(len(path) - 1, 0, -1):
            parent = self.find(path[:depth])
            if parent is None:
                continue
            indent = self._indent(self.lines[parent])
            step = self.indent_step()
            child_indent = indent + step
            # After the block's last real line, not before the next block's
            # header. The difference is the comment paragraph that introduces
            # the NEXT section: inserting above it is valid YAML that reads as
            # if the key belonged to something else.
            index = parent + 1
            for cursor in range(parent + 1, len(self.lines)):
                stripped = self.lines[cursor].strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if self._indent(self.lines[cursor]) <= indent:
                    break
                index = cursor + 1
            block = []
            for extra, key in enumerate(path[depth:-1]):
                block.append(" " * (child_indent + step * extra) + f"{key}:")
            block.append(" " * (child_indent + step * len(path[depth:-1]))
                         + f"{path[-1]}: {self.render(value)}")
            self.lines[index:index] = block
            return "\n".join(f"+{line}" for line in block)

        step = self.indent_step()
        block = [f"{path[0]}:"] if len(path) > 1 else []
        for depth, key in enumerate(path[1:-1], start=1):
            block.append(" " * step * depth + f"{key}:")
        block.append(" " * step * (len(path) - 1)
                     + f"{path[-1]}: {self.render(value)}")
        self.lines.extend(block)
        return "\n".join(f"+{line}" for line in block)

    def save(self) -> None:
        text = "\n".join(self.lines) + "\n"
        yaml.safe_load(text)                  # never write a file we cannot read
        self.path.write_text(text)


# --------------------------------------------------------------------------
# describing what may be edited


def _model_choices(config, provider: str) -> list[str]:
    """Every model id this provider is known to serve, best-effort and additive.

    Three sources, because each is incomplete on its own: `models.yaml` is
    empty until `refresh-models` has run, the catalog only covers providers
    that publish one, and neither knows about a model somebody has already
    pinned by hand in `agents.yaml`. A union of the three plus free text is
    strictly more useful than any of them alone, and no entry is ever removed
    from the list just because one source forgot it.
    """
    from ..catalog import local_path
    from ..paths import global_config_dir

    out: set[str] = set()
    listed = (config.models or {}).get(provider)
    if isinstance(listed, list):
        out.update(str(m) for m in listed)
    elif isinstance(listed, dict):
        out.update(str(m) for m in listed)

    for name in (provider, f"{provider}-go"):
        try:
            data = json.loads(local_path(global_config_dir(), name).read_text())
            models = (data.get("data") or {}).get("models") or {}
            out.update(f"{name}/{model}" if "/" not in str(model) else str(model)
                       for model in models)
        except (OSError, ValueError, AttributeError):
            continue

    out.update(spec.model for spec in config.agents.values()
               if spec.provider == provider and spec.model)
    return sorted(out)


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    return "text"


def _choices_for(config, path: list[str], value: Any) -> list[str]:
    dotted = ".".join(path)
    tail = path[-1]
    if tail in FREE_TEXT:
        return []
    for key, choices in FIXED_CHOICES.items():
        if dotted.endswith(key):
            return choices
    if path[:1] == ["agents"] and len(path) == 3:
        if tail == "provider":
            return sorted(config.providers)
        if tail == "model":
            agent = config.agents.get(path[1])
            return _model_choices(config, agent.provider if agent else "")
        if tail == "executor":
            return ["", "local", "docker"]
    if tail == "executor" and isinstance(value, str):
        return ["", "local", "docker"]
    return []


def _walk(node: Any, path: list[str], out: list[dict], config,
          source: YamlFile | None, file: str) -> None:
    for key, value in (node or {}).items():
        here = path + [str(key)]
        if isinstance(value, dict):
            _walk(value, here, out, config, source, file)
            continue
        if isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value):
            continue                          # structural; not a settings widget
        line = source.find(here) if source else None
        out.append({
            "file": file,
            "path": here,
            "key": ".".join(here),
            "label": here[-1].replace("_", " "),
            "group": ".".join(here[:-1]) or file.removesuffix(".yaml"),
            "value": value,
            "kind": _kind(value),
            "choices": _choices_for(config, here, value),
            "help": source.help_for(here) if source else "",
            "line": line,
        })


def describe(paths, config) -> list[dict]:
    """Every editable setting in the merged config, with how to show it.

    The *values* come from the merged view, because that is what actually
    applies. The *help* comes from the shipped defaults, because that is where
    the reasoning is written down and a project copy may have been trimmed.
    """
    from ..paths import shipped_defaults_dir

    merged = {
        "project.yaml": config.project,
        "agents.yaml": {"agents": {name: _agent_dict(spec)
                                   for name, spec in sorted(config.agents.items())}},
        "providers.yaml": {"providers": config.providers},
    }
    out: list[dict] = []
    for file in EDITABLE:
        shipped = shipped_defaults_dir() / file
        source = YamlFile(shipped) if shipped.is_file() else None
        _walk(merged[file], [], out, config, source, file)
    return out


def _agent_dict(spec) -> dict:
    """An AgentSpec back to the shape agents.yaml holds."""
    data = {
        "provider": spec.provider, "model": spec.model,
        "instructions": spec.instructions, "description": spec.description,
        "permission": spec.permission, "can_spawn": spec.can_spawn,
        "max_children": spec.max_children, "timeout": spec.timeout,
        "silence_timeout": spec.silence_timeout, "max_steps": spec.max_steps,
        "writes": spec.writes, "conversational": spec.conversational,
        "executor": spec.executor, "launch": spec.launch, "role": spec.role,
    }
    if spec.effort is not None:
        data["effort"] = spec.effort
    return data


# --------------------------------------------------------------------------
# writing


def coerce(value: Any, kind: str) -> Any:
    """Bring a value in from a form field, where everything is a string."""
    if kind == "bool":
        return value if isinstance(value, bool) else str(value).lower() in (
            "1", "true", "yes", "on")
    if kind == "int":
        return int(str(value).strip())
    if kind == "float":
        return float(str(value).strip())
    if kind == "list":
        if isinstance(value, list):
            return value
        return [part.strip() for part in str(value).split(",") if part.strip()]
    return value


def write(paths, file: str, path: list[str], value: Any) -> dict:
    """Set one setting in the PROJECT layer, and say what changed.

    Always the project layer: it is the one that belongs to this project, it
    already wins over the global and shipped ones, and writing to either of
    those would silently change every other project on the machine.
    """
    if file not in EDITABLE:
        raise ValueError(f"{file} is not editable from here")
    if not path:
        raise ValueError("no setting named")

    target = paths.config / file
    if not target.is_file():
        # A project seeded before this file existed, or one deliberately
        # trimmed. An empty file is a legitimate layer: the merge fills it in.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {file} — project overrides\n")

    document = YamlFile(target)
    diff = document.set(path, value)
    document.save()
    return {"ok": True, "file": str(target), "key": ".".join(path),
            "value": value, "diff": diff}
