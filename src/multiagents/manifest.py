"""D3: the per-provider CLI dependency manifest — lint, probe and digests.

Everything a provider integration relies on in its NATIVE CLI is declared in
a ``<provider>.dependencies.yaml`` beside its adapter and scripts. This
module checks those declarations two ways:

:func:`lint`
    Static, over the SHIPPED tree only. Every ``used_by`` reference must
    resolve to executable code or config, and every leaf of the provider's
    shipped ``providers.yaml`` entry that the selectors in ``_SELECTORS``
    name must be pointed at by some declared dependency. Syntactic evidence
    only: the lint proves neither completeness nor behaviour, and does not
    claim to resist deliberate gaming.

:func:`probe`
    Dynamic, per execution context. The real binary is asked its version and
    compared with the versions recorded as verified, following the state
    precedence of DM-R6. No state here ever refuses a launch.

The digests (:func:`integration_digest`) tie a ``verified`` entry to the
exact integration it was measured against — the adapter/script files and the
provider's ``providers.yaml`` entry, by CONTENT, so a byte-identical override
keeps the digest while any real edit voids entries recorded before it.
Shared integration code outside the provider's own files (``docker.py``,
which sets ``ANTHROPIC_BASE_URL``) is deliberately outside both digests.

Contract: ``context/specs/d3-cli-dependency-manifest.md`` (DM-R1..R7, plus
the amendments of 2026-09-30 that fix the interface names used here).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from . import config as config_module
from . import scripts as scripts_module
from .paths import ProjectPaths, global_config_dir, shipped_defaults_dir
from .providers import Provider, load_providers, resolve_inheritance

MANIFEST_SUFFIX = ".dependencies.yaml"
SCHEMA = 1

# DM-R2: the dependency kinds and the reference/target vocabularies.
KINDS = ("subcommand", "flag", "env", "state_path", "stream_field",
         "text_match", "exit_code", "endpoint", "layout")
STREAMS = ("stdout", "stderr", "either")
MATCHES = ("exact", "fragment")

# DM-R6: the probe's timing. The overall deadline of a probe, cleanup
# included, is the timeout plus PROBE_CLEANUP seconds.
PROBE_TIMEOUT = 10
PROBE_CLEANUP = 5

# `exec_in_running` marks a timeout with this return code (DockerExecutor).
TIMEOUT_RC = 124

DEFAULTS_REL = Path("src/multiagents/defaults")

_PLACEHOLDER = re.compile(r"^\{[^{}]+\}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_ARM_PATTERN = re.compile(r"^[A-Za-z0-9_*?.@\[\]!-]+(\|[A-Za-z0-9_*?.@\[\]!-]+)*$")
_ARM_LINE = re.compile(r"^([ \t]*)([^()#\n]*?)[ \t]*\)(.*)$")
_FN_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{(.*)$")
_FN_KEYWORD = re.compile(r"^function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{?(.*)$")
# A shell word boundary for `exact` matching: the characters that may sit
# against a whole literal. `.` and `-` are deliberately NOT boundaries —
# `.part.state.input` inside `.part.state.input.x` is a fragment, not a
# whole word.
_SHELL_BOUNDARY = set(" \t\n'\"`()<>;|&=[]{}")


@dataclass
class LintFinding:
    """One lint result. `dependency_id` is None for document-level problems."""

    provider: str
    dependency_id: str | None
    code: str
    message: str


@dataclass
class ProbeResult:
    """What `probe` found for one provider in one execution context."""

    state: str
    version: str | None = None
    detail: str = ""
    manifest_source: Path | None = None
    integration_sources: dict[str, str | None] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)


# ------------------------------------------------------------------ schema --


def _kind_of(dep_id: str) -> str | None:
    kind, sep, _slug = dep_id.partition(".")
    return kind if sep else None


def _validate(doc: Any, provider: str) -> list[str]:
    """DM-R2. Every way a document can be malformed, as message strings."""
    if not isinstance(doc, dict):
        return ["the document is not a mapping"]
    errors: list[str] = []
    if "schema" not in doc:
        errors.append("schema: is required")
    elif isinstance(doc["schema"], bool) or doc["schema"] != SCHEMA:
        errors.append(f"schema: must be {SCHEMA}")
    if "provider" not in doc:
        errors.append("provider: is required")
    elif doc["provider"] != provider:
        errors.append(f"provider: must equal the file-name prefix {provider!r}")
    binary = doc.get("binary")
    if "binary" not in doc:
        errors.append("binary: is required")
    elif not isinstance(binary, dict):
        errors.append("binary: must be a mapping")
    else:
        if "name" not in binary:
            errors.append("binary.name: is required")
        command = binary.get("version_command")
        if "version_command" not in binary:
            errors.append("binary.version_command: is required")
        elif (not isinstance(command, list) or not command
              or not all(isinstance(part, str) for part in command)):
            errors.append("binary.version_command: must be a list of strings")
        regex = binary.get("version_regex")
        if "version_regex" not in binary:
            errors.append("binary.version_regex: is required")
        elif not isinstance(regex, str):
            errors.append("binary.version_regex: must be a string")
        else:
            try:
                re.compile(regex)
            except re.error as exc:
                errors.append(f"binary.version_regex: does not compile ({exc})")
        stream = binary.get("version_stream", "stdout")
        if stream not in STREAMS:
            errors.append(f"binary.version_stream: must be one of {STREAMS}")
    if "dependencies" not in doc:
        errors.append("dependencies: is required")
    elif not isinstance(doc["dependencies"], list):
        errors.append("dependencies: must be a list")
    else:
        for index, dep in enumerate(doc["dependencies"]):
            where = f"dependencies[{index}]"
            if not isinstance(dep, dict):
                errors.append(f"{where}: must be a mapping")
                continue
            if "id" not in dep:
                errors.append(f"{where}.id: is required")
            elif not isinstance(dep["id"], str) or _kind_of(dep["id"]) is None:
                errors.append(f"{where}.id: must have the form <kind>.<slug>")
            kind = dep.get("kind")
            if "kind" not in dep:
                errors.append(f"{where}.kind: is required")
            elif kind not in KINDS:
                errors.append(f"{where}.kind: must be one of {list(KINDS)}")
            if isinstance(dep.get("id"), str) and isinstance(kind, str):
                prefix = _kind_of(dep["id"])
                if prefix is not None and prefix != kind:
                    errors.append(
                        f"{where}.id: kind prefix {prefix!r} disagrees with kind {kind!r}")
            if "value" not in dep:
                errors.append(f"{where}.value: is required")
            elif not isinstance(dep["value"], str):
                errors.append(f"{where}.value: must be a string")
            if "used_by" not in dep:
                errors.append(f"{where}.used_by: is required")
            elif (not isinstance(dep["used_by"], list) or not dep["used_by"]
                  or not all(isinstance(ref, str) for ref in dep["used_by"])):
                errors.append(f"{where}.used_by: must be a non-empty list of strings")
            if dep.get("match", "exact") not in MATCHES:
                errors.append(f"{where}.match: must be one of {list(MATCHES)}")
    verified = doc.get("verified", [])
    if not isinstance(verified, list):
        errors.append("verified: must be a list")
    not_deps = doc.get("not_dependencies", [])
    if not isinstance(not_deps, list):
        errors.append("not_dependencies: must be a list")
    else:
        for index, entry in enumerate(not_deps):
            if not isinstance(entry, dict) or "selector" not in entry:
                errors.append(f"not_dependencies[{index}]: needs a selector")
            elif not str(entry.get("reason") or "").strip():
                errors.append(f"not_dependencies[{index}].reason: is required")
    return errors


def _read_manifest(path: Path) -> tuple[dict | None, str | None]:
    """`(document, error)`. Reading or parsing failures are errors, never raises."""
    try:
        text = path.read_text()
    except (OSError, ValueError) as exc:
        return None, f"cannot read {path}: {type(exc).__name__}: {exc}"
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"not valid YAML: {str(exc).splitlines()[0]}"
    return doc, None


def _manifest_layers(paths: ProjectPaths | None) -> list[Path]:
    layers = []
    if paths is not None:
        layers.append(paths.config / "providers")
    layers.append(global_config_dir() / "providers")
    layers.append(shipped_defaults_dir() / "providers")
    return layers


def _find_manifest(provider: str, paths: ProjectPaths | None) -> Path | None:
    """DM-R1: project, then global, then shipped. The first file found wins,
    and wins whole — layers are never merged, so a malformed project manifest
    does not fall through to a good global one."""
    for directory in _manifest_layers(paths):
        candidate = directory / f"{provider}{MANIFEST_SUFFIX}"
        if candidate.exists():
            return candidate
    return None


# ------------------------------------------------------- shell comment-free --


def _strip_line(line: str) -> tuple[str, tuple[str, bool] | None]:
    """One line with comments blanked, plus a pending here-document.

    Respects single and double quotes and ``${…#…}`` parameter expansions, so
    a ``#`` inside any of them is not a comment. The previous significant
    character decides whether ``#`` starts a comment at all: POSIX only
    starts one at the beginning of a word.
    """
    out = list(line)
    pending: tuple[str, bool] | None = None
    i, n = 0, len(line)
    prev = ""
    while i < n:
        ch = line[i]
        if ch == "'":
            end = line.find("'", i + 1)
            i = n if end == -1 else end + 1
            prev = "'"
            continue
        if ch == '"':
            i += 1
            while i < n:
                if line[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if line[i] == '"':
                    i += 1
                    break
                i += 1
            prev = '"'
            continue
        if ch == "\\" and i + 1 < n:
            prev = line[i + 1]
            i += 2
            continue
        if ch == "$" and i + 1 < n and line[i + 1] == "{":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if line[i] == "{":
                    depth += 1
                elif line[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            prev = "}"
            continue
        if ch == "<" and line.startswith("<<", i) and not line.startswith("<<<", i):
            j = i + 2
            strip_tabs = False
            if j < n and line[j] == "-":
                strip_tabs = True
                j += 1
            if j < n and line[j] in "'\"":
                end = line.find(line[j], j + 1)
                if end != -1:
                    pending = (line[j + 1:end], strip_tabs)
                    i = end + 1
                    prev = ">"
                    continue
            else:
                match = re.match(r"[A-Za-z0-9_]+", line[j:])
                if match:
                    pending = (match.group(0), strip_tabs)
                    i = j + match.end()
                    prev = ">"
                    continue
        if ch == "#" and prev in ("", " ", "\t"):
            for k in range(i, n):
                out[k] = " "
            return "".join(out), pending
        prev = ch
        i += 1
    return "".join(out), pending


def _strip_shell_comments(text: str) -> str:
    """The text with comments removed. Here-document bodies are literal."""
    lines = text.split("\n")
    out: list[str] = []
    heredoc: tuple[str, bool] | None = None
    for line in lines:
        if heredoc is not None:
            out.append(line)
            compared = line.lstrip("\t") if heredoc[1] else line
            if compared.strip() == heredoc[0]:
                heredoc = None
            continue
        stripped, pending = _strip_line(line)
        out.append(stripped)
        if pending is not None:
            heredoc = pending
    return "\n".join(out)


# ---------------------------------------------------------- shell structure --


def _shell_index(text: str) -> dict[str, Any]:
    """Top-level functions and top-level ``case`` arms of a shell script.

    Structure is read from the comment-stripped text, line-oriented the way
    the shipped scripts are laid out: functions and ``case`` statements start
    at column 0, an arm ends at its own ``;;`` line or the next arm at the
    same indent, and anything deeper (a nested ``case`` inside an arm) is
    body text of that arm. A one-line function — ``f() { …; }`` — is its own
    body, or its closing brace would swallow every function after it. Shell
    references assume case arms and functions start at column 0, and ``esac`` is
    on its own line.
    """
    stripped = _strip_shell_comments(text)
    lines = stripped.split("\n")
    functions: dict[str, str] = {}
    arms: dict[str, list[str]] = {}
    errors: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _FN_LINE.match(line) or _FN_KEYWORD.match(line)
        if match:
            name = match.group(1)
            rest = match.group(2)
            if "}" in rest:
                functions[name] = line
                i += 1
                continue
            body = [line]
            i += 1
            while i < len(lines) and lines[i].strip() != "}":
                body.append(lines[i])
                i += 1
            if i < len(lines):
                body.append(lines[i])
                i += 1
            else:
                errors.append(f"unclosed function {name}")
            functions.setdefault(name, "\n".join(body))
            continue
        if line.startswith("case "):
            block: list[str] = []
            arm_indent: str | None = None
            current: str | None = None
            i += 1
            while i < len(lines) and not lines[i].startswith("esac"):
                block_line = lines[i]
                header = _ARM_LINE.match(block_line)
                is_header = False
                if header and _ARM_PATTERN.match(header.group(2)):
                    if arm_indent is None:
                        arm_indent = header.group(1)
                    is_header = header.group(1) == arm_indent
                if is_header and header:
                    current = header.group(2)
                    arms.setdefault(current, [])
                    arms[current].append(header.group(3))
                elif current is not None:
                    arms[current].append(block_line)
                if block_line.strip() == ";;":
                    current = None
                block.append(block_line)
                i += 1
            if i < len(lines):
                i += 1                                   # esac
            else:
                errors.append("unclosed case block")
            continue
        i += 1
    return {
        "stripped": stripped,
        "functions": functions,
        "arms": {alternative: "\n".join(chunks)
                 for label, chunks in arms.items()
                 for alternative in label.split("|")},
        "errors": errors,
    }


# -------------------------------------------------------- python definitions --


class _Literals(ast.NodeVisitor):
    """String constants of a definition's body. Docstrings do not count; an
    f-string's constant parts count only for `fragment` matching."""

    def __init__(self) -> None:
        self.full: set[str] = set()
        self.parts: set[str] = set()
        self._skip: set[int] = set()
        self._joined = 0

    @staticmethod
    def _mark_docstring(node: ast.AST, skip: set[int]) -> None:
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            skip.add(id(body[0].value))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._mark_docstring(node, self._skip)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._mark_docstring(node, self._skip)
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._joined += 1
        self.generic_visit(node)
        self._joined -= 1

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self._skip:
            (self.parts if self._joined else self.full).add(node.value)


def _resolve_definition(tree: ast.Module, qualname: str) -> ast.AST | None:
    """A top-level definition, descending through class bodies only: a nested
    function belongs to its enclosing definition and is not addressable."""
    node: ast.AST | None = None
    for part in qualname.split("."):
        body = tree.body if node is None else (
            node.body if isinstance(node, ast.ClassDef) else [])
        found = next(
            (item for item in body
             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)) and item.name == part),
            None,
        )
        if found is None:
            return None
        node = found
    return node


# ------------------------------------------------------------- ref checking --


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _walk_pointer(base: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        return None
    cursor = base
    for raw in pointer.split("/")[1:]:
        token = _unescape(raw)
        if isinstance(cursor, dict):
            if token not in cursor:
                return None
            cursor = cursor[token]
        elif isinstance(cursor, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                return None
            index = int(token)
            if index >= len(cursor):
                return None
            cursor = cursor[index]
        else:
            return None
    return cursor


def _yaml_literals(node: Any) -> list[str]:
    """The scalar at the pointer, or any scalar beneath it. A mapping also
    counts its keys — at any depth, so native dependencies expressed as
    mapping keys (`part.reason`, or a provider env var) are covered."""
    if isinstance(node, dict):
        out: list[str] = []
        for key, value in node.items():
            out.append(str(key))
            out.extend(_yaml_literals(value))
        return out
    if isinstance(node, list):
        out = []
        for item in node:
            out.extend(_yaml_literals(item))
        return out
    return [str(node)]


def _occurs(value: str, literals: Any, match: str) -> bool:
    for literal in literals:
        text = str(literal)
        if text == value if match == "exact" else value in text:
            return True
    return False


def _occurs_in_text(value: str, text: str, match: str) -> bool:
    if match == "fragment":
        return value in text
    i = text.find(value)
    while i != -1:
        before = text[i - 1] if i > 0 else ""
        after = text[i + len(value)] if i + len(value) < len(text) else ""
        if (before == "" or before in _SHELL_BOUNDARY) and (
                after == "" or after in _SHELL_BOUNDARY):
            return True
        i = text.find(value, i + 1)
    return False


def _check_ref(root: Path, providers: Any, ref: str, value: str,
               match: str) -> str | None:
    """DM-R4: None when `ref` resolves with `value` in its executable
    literals, else the reason it does not."""
    if ref.startswith("providers.yaml#"):
        pointer = ref[len("providers.yaml#"):]
        target = _walk_pointer(providers, pointer)
        if target is None:
            return f"{ref}: pointer does not resolve"
        if not _occurs(value, _yaml_literals(target), match):
            return (f"{ref}: {value!r} does not occur in the scalars at that "
                    f"pointer")
        return None

    if "::" not in ref:
        return (f"{ref!r} is not a reference (expected providers.yaml#…, "
                "a.py::name, an .sh::case:/fn:/file)")
    relative, _, selector = ref.rpartition("::")
    if not relative or not selector:
        return f"{ref!r}: empty path or selector"
    candidate = (root / relative).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        return f"{ref!r}: the path escapes the repository"
    if not candidate.is_file():
        return f"{ref!r}: no such file"
    if relative.endswith(".py"):
        if not _QUALNAME.match(selector):
            return f"{ref!r}: {selector!r} is not a dotted qualified name"
        try:
            tree = ast.parse(candidate.read_text())
        except (OSError, SyntaxError) as exc:
            return f"{ref!r}: cannot parse ({type(exc).__name__})"
        definition = _resolve_definition(tree, selector)
        if definition is None:
            return f"{ref!r}: no definition {selector!r}"
        collector = _Literals()
        collector.visit(definition)
        literals = list(collector.full) + (list(collector.parts)
                                           if match == "fragment" else [])
        if not _occurs(value, literals, match):
            return f"{ref!r}: {value!r} is not a string literal of {selector!r}"
        return None

    if relative.endswith(".sh"):
        index = _shell_index(candidate.read_text())
        if index.get("errors"):
            return f"{ref!r}: parse error: {', '.join(index['errors'])}"
        if selector == "file":
            if index["functions"] or index["arms"]:
                return (f"{ref!r}: ::file is only for a script with no case "
                        f"arms and no functions")
            if _occurs_in_text(value, index["stripped"], match):
                return None
            return f"{ref!r}: {value!r} does not occur in the script"
        form, _, name = selector.partition(":")
        if form == "case" and name:
            chunks = index["arms"].get(name)
            if not chunks:
                return f"{ref!r}: no top-level case arm {name!r}"
            if _occurs_in_text(value, chunks, match):
                return None
            return f"{ref!r}: {value!r} does not occur in case arm {name!r}"
        if form == "fn" and name:
            body = index["functions"].get(name)
            if body is None:
                return f"{ref!r}: no function {name!r}"
            if _occurs_in_text(value, body, match):
                return None
            return f"{ref!r}: {value!r} does not occur in function {name!r}"
        return f"{ref!r}: {selector!r} is not case:/fn:/file"

    return f"{ref!r}: only .py and .sh files are referenced"


# ------------------------------------------------------- reverse coverage --

# DM-R5, confirmed against the shipped providers.yaml. These leaves of a
# provider's entry must each be pointed at by a declared dependency, or
# waived by a not_dependencies selector:
#   /spawn/args[*], /spawn/resume[*], /spawn/permission/*[*],
#   /spawn/optional/*[*], /models_cmd[*], /mcp/args[*],
#   /stream/session_id_paths[*], /stream/rules[*]/match (keys and values),
#   /stream/rules[*]/fields (values), /stream/status_map keys — directly or
#   per rule, where the CLI's own stream schema puts it —
#   /refusal_markers[*], /truncation_markers[*],
#   /transcript/limit_markers[*]/match, /home_links[*], /bin_versions_depth.
# Placeholders such as {prompt} are multiagents' own template syntax and are
# exempt. Codex's /spawn and /stream surfaces are the adapter's normalised
# interface, not the native CLI's, and are waived in its manifest.


@dataclass
class _Leaf:
    path: str            # selector path, e.g. "/spawn/args/1"
    value: str
    own: str             # pointer designating this leaf itself (or its
                         # mapping, for a mapping KEY)
    parent: str          # pointer of the immediate parent container


def _leaf(pointer_prefix: str, path: str, value: Any, *,
          own: str | None = None, parent: str | None = None) -> _Leaf:
    return _Leaf(path, str(value),
                 own if own is not None else f"{pointer_prefix}{path}",
                 parent if parent is not None else pointer_prefix)


def _list_leaves(entry: dict, pointer_prefix: str, path: str,
                 out: list[_Leaf]) -> None:
    items = entry
    for part in path.strip("/").split("/"):
        if not isinstance(items, dict) or part not in items:
            return
        items = items[part]
    if not isinstance(items, list):
        if isinstance(items, (str, int, float, bool)):
            out.append(_leaf(pointer_prefix, path, items))
        return
    for index, item in enumerate(items):
        if isinstance(item, (str, int, float, bool)):
            out.append(_leaf(pointer_prefix, f"{path}/{index}", item,
                             parent=f"{pointer_prefix}{path}"))


def _mapping_value_leaves(mapping: dict, pointer: str, path: str,
                          out: list[_Leaf]) -> None:
    for key, value in mapping.items():
        if isinstance(value, (str, int, float, bool)):
            escaped = _escape_token(str(key))
            out.append(_Leaf(f"{path}/{escaped}", str(value),
                             f"{pointer}/{escaped}", pointer))


def _rule_leaves(rules: list, pointer_prefix: str, out: list[_Leaf]) -> None:
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        base = f"{pointer_prefix}/stream/rules/{index}"
        match = rule.get("match")
        if isinstance(match, dict):
            for key, value in match.items():
                # A mapping key can only be designated by a pointer to the
                # mapping itself; its value also has its own pointer.
                out.append(_Leaf(f"/stream/rules/{index}/match", str(key),
                                 f"{base}/match", f"{base}/match"))
                if isinstance(value, (str, int, float, bool)):
                    out.append(_Leaf(f"/stream/rules/{index}/match", str(value),
                                     f"{base}/match/{_escape_token(str(key))}",
                                     f"{base}/match"))
        fields = rule.get("fields")
        if isinstance(fields, dict):
            _mapping_value_leaves(fields, f"{base}/fields",
                                  f"/stream/rules/{index}/fields", out)
        status_map = rule.get("status_map")
        if isinstance(status_map, dict):
            for key in status_map:
                out.append(_Leaf(f"/stream/rules/{index}/status_map", str(key),
                                 f"{base}/status_map", f"{base}/status_map"))


def _leaves(entry: Any, name: str) -> list[_Leaf]:
    """Every leaf the selectors demand, with the pointers that can cover it."""
    out: list[_Leaf] = []
    if not isinstance(entry, dict):
        return out
    prefix = f"/{name}"
    for path in ("/spawn/args", "/spawn/resume", "/models_cmd", "/mcp/args",
                 "/stream/session_id_paths", "/refusal_markers",
                 "/truncation_markers", "/home_links"):
        _list_leaves(entry, prefix, path, out)
    spawn = entry.get("spawn")
    if isinstance(spawn, dict):
        for group in ("permission", "optional"):
            block = spawn.get(group)
            if not isinstance(block, dict):
                continue
            for key, value in block.items():
                if isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, (str, int, float, bool)):
                            escaped = _escape_token(str(key))
                            out.append(_leaf(
                                prefix, f"/spawn/{group}/{escaped}/{index}",
                                item))
    stream = entry.get("stream")
    if isinstance(stream, dict):
        rules = stream.get("rules")
        if isinstance(rules, list):
            _rule_leaves(rules, prefix, out)
        status_map = stream.get("status_map")
        if isinstance(status_map, dict):
            for key in status_map:
                out.append(_Leaf("/stream/status_map", str(key),
                                 f"{prefix}/stream/status_map",
                                 f"{prefix}/stream/status_map"))
    transcript = entry.get("transcript")
    if isinstance(transcript, dict):
        markers = transcript.get("limit_markers")
        if isinstance(markers, list):
            for index, marker in enumerate(markers):
                if isinstance(marker, dict):
                    value = marker.get("match")
                    if isinstance(value, (str, int, float, bool)):
                        item = f"/transcript/limit_markers/{index}"
                        out.append(_Leaf(item, str(value),
                                         f"{prefix}{item}/match",
                                         f"{prefix}{item}"))
    if isinstance(entry.get("bin_versions_depth"), (str, int, float, bool)):
        _list_leaves(entry, prefix, "/bin_versions_depth", out)
    return out


def _value_matches(leaf_value: str, dep_value: str, match: str) -> bool:
    return leaf_value == dep_value if match == "exact" else dep_value in leaf_value


def _covered(leaf: _Leaf, deps: list[dict]) -> bool:
    for dep in deps:
        if not _value_matches(leaf.value, str(dep.get("value", "")),
                              dep.get("match", "exact")):
            continue
        for ref in dep.get("used_by") or []:
            if not ref.startswith("providers.yaml#"):
                continue
            pointer = ref[len("providers.yaml#"):]
            if pointer in (leaf.own, leaf.parent):
                return True
    return False


def _waived(leaf: _Leaf, selectors: list[str]) -> bool:
    return any(leaf.path == selector or leaf.path.startswith(selector + "/")
               for selector in selectors)


# ------------------------------------------------------------------- lint --


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def lint(root: Path | None = None) -> list[LintFinding]:
    """DM-R5, over the SHIPPED tree only: user overrides never affect it.

    Forward: every used_by reference resolves (DM-R4). Reverse: every leaf
    the selectors demand is pointed at, or waived. The output says declared
    dependencies resolve; it never claims all dependencies are covered.
    """
    root = (root or _repo_root()).resolve()
    defaults = root / DEFAULTS_REL
    providers_file = defaults / "providers.yaml"
    try:
        document = yaml.safe_load(providers_file.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return [LintFinding("", None, "malformed",
                            f"{providers_file} cannot be read")]
    providers = document.get("providers") or {}
    resolved = resolve_inheritance(providers)
    findings: list[LintFinding] = []
    manifests = sorted((defaults / "providers").glob(f"*{MANIFEST_SUFFIX}"))
    for path in manifests:
        name = path.name[: -len(MANIFEST_SUFFIX)]
        doc, error = _read_manifest(path)
        if error is not None:
            findings.append(LintFinding(name, None, "malformed", error))
            continue
        errors = _validate(doc, name)
        if errors:
            findings.append(LintFinding(name, None, "malformed",
                                        "; ".join(errors)))
            continue
        dependencies = doc.get("dependencies") or []
        seen: set[str] = set()
        for dep in dependencies:
            dep_id = dep["id"]
            if dep_id in seen:
                findings.append(LintFinding(
                    name, dep_id, "duplicate_id",
                    f"{dep_id!r} is declared more than once"))
            seen.add(dep_id)
            if "MULTIAGENTS_" in str(dep.get("value", "")):
                findings.append(LintFinding(
                    name, dep_id, "internal_protocol",
                    f"{dep_id!r} names a MULTIAGENTS_* variable, which is "
                    f"multiagents' own protocol, not the CLI's surface"))
            for ref in dep.get("used_by") or []:
                reason = _check_ref(root, resolved, ref, dep["value"],
                                    dep.get("match", "exact"))
                if reason is not None:
                    findings.append(LintFinding(name, dep_id,
                                                "unresolved_ref", reason))
        not_deps = doc.get("not_dependencies") or []
        selectors = [str(entry["selector"]) for entry in not_deps]
        entry = resolved.get(name) or {}
        leaves = _leaves(entry, name)
        for selector in selectors:
            if not any(_waived(leaf, [selector]) for leaf in leaves):
                findings.append(LintFinding(
                    name, None, "stale_not_dependency",
                    f"{selector} matches no leaf of {name}'s entry"))
        for leaf in leaves:
            if _PLACEHOLDER.match(leaf.value):
                continue
            if _waived(leaf, selectors):
                continue
            if not _covered(leaf, dependencies):
                findings.append(LintFinding(
                    name, None, "uncovered_leaf",
                    f"{leaf.path} ({leaf.value!r}) is not covered by any "
                    f"used_by pointer"))
    return findings


# ----------------------------------------------------------------- digests --


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=False)


def _runtime_lookup(paths: ProjectPaths | None) -> Callable[[str], Path | None]:
    def find(name: str) -> Path | None:
        return scripts_module.find_script(
            name, global_config_dir(), paths.config if paths else None)
    return find


def _shipped_lookup(name: str) -> Path | None:
    candidate = shipped_defaults_dir() / "providers" / name
    return candidate if candidate.is_file() else None


def _integration(name: str, entry: Any,
                 lookup: Callable[[str], Path | None]) -> tuple[str, dict[str, str | None]]:
    """DM-R3a: the sha256 of a framed sequence of
    ``(role, identifier, sha256(content))`` records — the adapter, the script
    action file and the provider's ``providers.yaml`` entry after `extends`
    is resolved — in that order. Files are identified by NAME and hashed by
    CONTENT, so a byte-identical override in another layer keeps the digest;
    which layer each part came from is reported separately. A missing file is
    framed as a `missing` record, never skipped."""
    entry = entry or {}
    frames: list[str] = []
    sources: dict[str, str | None] = {}
    adapter = str(entry.get("adapter") or "")
    script = str(entry.get("script")
                 or (entry.get("auth") or {}).get("script") or "")
    if not script and not adapter:
        script = f"{name}.sh"
    roles: list[tuple[str, str]] = []
    if adapter:
        roles.append(("adapter", adapter))
    if script and script != adapter:
        roles.append(("script", script))
    for role, filename in roles:
        path = lookup(filename)
        sources[role] = str(path) if path is not None else None
        if path is None or not path.is_file():
            frames.append(f"{role}\x1f{filename}\x1fmissing")
        else:
            frames.append(f"{role}\x1f{filename}\x1f{_sha(path.read_bytes())}")
    entry_json = _canonical(entry)
    frames.append(f"providers_entry\x1f{name}\x1f{_sha(entry_json.encode())}")
    digest = _sha("\n".join(frames).encode())
    return digest, sources


def _shipped_providers() -> dict:
    try:
        document = yaml.safe_load(
            (shipped_defaults_dir() / "providers.yaml").read_text()) or {}
    except OSError:
        return {}
    return document.get("providers") or {}


def integration_digest(provider_name: str, paths: ProjectPaths | None) -> str:
    """The sha256 of the provider's integration, as at runtime."""
    cfg = config_module.load(paths, seed=False)
    entry = resolve_inheritance(cfg.providers or {}).get(provider_name)
    return _integration(provider_name, entry, _runtime_lookup(paths))[0]


def _dependencies_digest(doc: dict) -> str:
    payload = {"binary": doc.get("binary"), "dependencies": doc.get("dependencies")}
    return _sha(_canonical(payload).encode())


# ------------------------------------------------------------------ probe --


def host_platform() -> str:
    """`linux-x86_64` — the platform a `verified` entry is pinned to."""
    return f"{platform.system().lower()}-{platform.machine()}"


def _probe_contexts(config: Any, name: str) -> list[str]:
    """DM-R6: the executors agents actually use — the project executor plus
    every per-agent executor override for THIS provider, enumerated from the
    config itself rather than `executor_for()`'s first match."""
    kinds = {getattr(config, "executor", "local") or "local"}
    for spec in (getattr(config, "agents", None) or {}).values():
        if (getattr(spec, "provider", "") == name
                and getattr(spec, "executor", "")):
            kinds.add(spec.executor)
    out = []
    for kind in ("local", "docker"):
        if kind in kinds:
            out.append("host" if kind == "local" else "docker")
    return out or ["host"]


def _kill_process_group(child: subprocess.Popen) -> None:
    """Kill and reap a timed-out probe and everything it started, bounded."""
    import contextlib
    import signal

    if child.returncode is None:
        with contextlib.suppress(OSError):
            os.killpg(child.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        child.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
        child.communicate(timeout=PROBE_CLEANUP)
    for stream in (child.stdout, child.stderr):
        with contextlib.suppress(OSError, AttributeError):
            if stream is not None:
                stream.close()


def _text(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _evaluate_probe(rc: int, out: str, err: str, binary: dict, context: str) -> tuple[str, str | None, str]:
    """DM-R6: evaluate common probe states from command output."""
    if rc == TIMEOUT_RC:
        detail = err.strip() or f"{'docker exec' if context == 'docker' else 'probe'} timed out"
        return "timeout", None, detail
    if rc == 127:
        detail = err.strip() or f"binary not found{' in the container' if context == 'docker' else ''}"
        return "missing", None, detail
    if rc != 0:
        tail = (err or out).strip().splitlines()
        return "probe_failed", None, f"exit {rc}" + (f": {tail[-1][:160]}" if tail else "")
    
    stream = binary.get("version_stream") or "stdout"
    text = {"stdout": out, "stderr": err}.get(stream)
    if text is None:
        text = out + "\n" + err
    match = re.search(binary["version_regex"], text)
    if match is None:
        return "probe_failed", None, "the version regex matched nothing the CLI printed"
    
    version = match.group(1) if match.groups() else match.group(0)
    return "ok", str(version), ""


def _run_host_probe(argv: list[str], env: dict[str, str],
                    timeout: int) -> tuple[int, str, str]:
    """Non-interactive: stdin is /dev/null and there is no TTY. Never raises."""
    try:
        child = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, start_new_session=True, text=True)
    except FileNotFoundError:
        return 127, "", ""
    except OSError as exc:
        return 126, "", f"{type(exc).__name__}: {exc}"
    try:
        out, err = child.communicate(timeout=timeout)
        return child.returncode, out, err
    except subprocess.TimeoutExpired:
        _kill_process_group(child)
        return TIMEOUT_RC, "", f"probe timed out after {timeout}s"


def _docker_executor(paths: ProjectPaths | None, cfg: Any):
    from .executor import get_executor

    return get_executor(
        "docker", (cfg.project or {}).get("executor", {}).get("docker", {}),
        paths=paths, providers=load_providers(cfg.providers or {}),
        config_dir=global_config_dir())


def _verified_versions(doc: dict, context: str) -> list[str]:
    executor = "docker" if context == "docker" else "local"
    out = []
    for entry in doc.get("verified") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("executor", "")) == executor and entry.get("version"):
            out.append(str(entry["version"]))
    return out


def _probe(name: str, paths: ProjectPaths | None, context: str,
           cfg: Any) -> ProbeResult:
    entry = resolve_inheritance(cfg.providers or {}).get(name) or {}
    provider = Provider.from_dict(name, entry)

    def done(state: str, version: str | None = None, detail: str = "",
             manifest: Path | None = None,
             digests: dict[str, str] | None = None) -> ProbeResult:
        digest, sources = _integration(name, entry, _runtime_lookup(paths))
        return ProbeResult(state, version, detail, manifest, sources,
                           {"integration_digest": digest,
                            "dependencies_digest": (digests or {}).get(
                                "dependencies_digest", "")})

    # State 1: disabled. Nothing is run at all.
    if not provider.enabled:
        return done("disabled",
                    detail="provider is disabled in providers.yaml",
                    manifest=_find_manifest(name, paths))

    manifest = _find_manifest(name, paths)
    digests: dict[str, str] = {}
    doc: dict = {}
    if manifest is not None:
        raw, error = _read_manifest(manifest)
        if error is not None:
            return done("malformed", detail=error, manifest=manifest)
        errors = _validate(raw, name)
        if errors:
            return done("malformed", detail="; ".join(errors), manifest=manifest)
        doc = raw
        digests["dependencies_digest"] = _dependencies_digest(doc)

    result_extra = {"dependencies_digest": digests.get("dependencies_digest", "")}

    def state(state_name: str, version: str | None = None,
              detail: str = "") -> ProbeResult:
        digest, sources = _integration(name, entry, _runtime_lookup(paths))
        return ProbeResult(state_name, version, detail, manifest, sources,
                           {"integration_digest": digest, **result_extra})

    # State 3: no manifest in any layer. Information only.
    if manifest is None:
        return state("no manifest",
                     detail="no manifest in the project, global or shipped layer")

    binary = doc["binary"]
    command = list(binary["version_command"])
    timeout = PROBE_TIMEOUT

    version: str | None = None
    if context == "docker":
        executor = _docker_executor(paths, cfg)
        probed = executor.exec_in_running([provider.bin, *command], timeout)
        if not isinstance(probed, tuple):
            return state("container not running",
                         detail=f"{probed!r}")
        rc, out, err = probed
        outcome, version, detail = _evaluate_probe(rc, out, err, binary, context)
        if outcome != "ok":
            return state(outcome, detail=detail)
    else:
        from .executor import get_executor

        env = scripts_module.build_env(
            name, provider, get_executor("local", {}, providers=None))
        resolved = provider.resolve_bin(env=env)
        # State 4: missing — resolved the way H7 resolves it.
        if resolved.launcher is None:
            return state("missing", detail=provider.bin_error(resolved))
        rc, out, err = _run_host_probe(
            [str(resolved.launcher), *command], env, timeout)
        outcome, version, detail = _evaluate_probe(rc, out, err, binary, context)
        if outcome != "ok":
            return state(outcome, detail=detail)

    # States 8 and 9, and `overridden` between them.
    executor_name = "docker" if context == "docker" else "local"
    integration, sources = _integration(name, entry, _runtime_lookup(paths))
    dependencies = _dependencies_digest(doc)
    applicable = []
    for record in doc.get("verified") or []:
        if not isinstance(record, dict):
            continue
        if (str(record.get("version", "")) == version
                and str(record.get("platform", "")) == host_platform()
                and str(record.get("executor", "")) == executor_name
                and str(record.get("integration_digest", "")) == integration
                and str(record.get("dependencies_digest", "")) == dependencies):
            applicable.append(record)
    verified_note = ("; verified versions recorded for this context: "
                     + ", ".join(_verified_versions(doc, context) or ["none"]))

    if applicable:
        scope = applicable[0].get("scope", "all")
        if scope == "all":
            return ProbeResult("verified", version, verified_note, manifest,
                               sources, {"integration_digest": integration,
                                         "dependencies_digest": dependencies})
        count = len(scope) if isinstance(scope, list) else 0
        return ProbeResult(
            f"verified (partial: {count} of {len(doc.get('dependencies') or [])} "
            f"dependencies)", version, verified_note, manifest, sources,
            {"integration_digest": integration,
             "dependencies_digest": dependencies})

    shipped_entry = resolve_inheritance(_shipped_providers()).get(name)
    if shipped_entry is not None:
        shipped_digest = _integration(name, shipped_entry, _shipped_lookup)[0]
        if shipped_digest != integration:
            return ProbeResult(
                "overridden", version,
                "the integration differs from the shipped one"
                + verified_note, manifest, sources,
                {"integration_digest": integration,
                 "dependencies_digest": dependencies})
    return ProbeResult("unverified", version, verified_note, manifest, sources,
                       {"integration_digest": integration,
                        "dependencies_digest": dependencies})


def probe(provider_name: str, paths: ProjectPaths | None = None,
          context: str = "host") -> ProbeResult:
    """DM-R6: probe the real binary in one execution context.

    `context` is ``host`` or ``docker``. The docker probe runs only inside an
    already-running container, through ``DockerExecutor.exec_in_running``;
    it never starts, creates or seeds one. No state returned here ever
    refuses a launch.
    """
    cfg = config_module.load(paths, seed=False)
    return _probe(provider_name, paths, context, cfg)


# ------------------------------------------------------------------ doctor --


def cli_dependencies_section(paths: ProjectPaths | None, config: Any,
                             providers: dict[str, Provider],
                             out: Callable[[str], Any] = print) -> int:
    """DM-R6: the `cli dependencies` doctor section, after `providers`.

    Returns the problems it found: `malformed` counts once per provider, and
    `missing` counts only in a context the providers section does not already
    cover — a binary absent only in the container. Every other state is
    information or a warning.
    """
    problems = 0
    for name in sorted(providers):
        counted_malformed = False
        for context in _probe_contexts(config, name):
            try:
                result = _probe(name, paths, context, config)
            except Exception as exc:                  # a section, never a crash
                out(f"  {name:12} {context:7} probe error   {type(exc).__name__}: {exc}")
                continue
            row = f"  {name:12} {context:7} {result.state}"
            if result.version:
                row += f"  {result.version}"
            out(row)
            if result.manifest_source is not None:
                out(f"    manifest  {result.manifest_source}")
            for role, source in (result.integration_sources or {}).items():
                out(f"    {role:9} {source or '(missing)'}")
            digests = result.digests or {}
            out(f"    integration digest  {digests.get('integration_digest', '')}")
            out(f"    dependencies digest {digests.get('dependencies_digest', '')}")
            if result.detail:
                out(f"    {result.detail}")
            if result.state == "malformed" and not counted_malformed:
                counted_malformed = True
                problems += 1
            if result.state == "missing" and context == "docker":
                problems += 1
    return problems
