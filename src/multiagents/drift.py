"""The config-drift warning (H11, CD-R1..CD-R5).

A layer value can silently freeze the shipped default it shadows: list
values replace wholesale when layers merge, so a global ``providers.yaml``
copied whole from the shipped file pinned the old turn rules for 13 days
with nothing saying so. This module only *reports* such shadowing —
loading and merging are untouched (CD-R5), and nothing here ever writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .paths import ProjectPaths, global_config_dir, shipped_defaults_dir

#: Compared key-by-key against the shipped copies (CD-R1). ``models.yaml`` is
#: deliberately absent: `refresh-models` rewrites it by design, so a diff
#: there is a refresh, not drift.
LAYER_FILES = ("providers.yaml", "agents.yaml", "project.yaml")

#: A layer file's top-level list of deliberate overrides (CD-R3).
ACK_KEY = "drift_acknowledged"

LIST_SHADOW = "list_shadow"
FILE_SHADOW = "file_shadow"
STALE_ACK = "stale_acknowledgement"


@dataclass
class Shadow:
    """One way a layer file shadows the shipped defaults."""

    file: Path      # the layer file the drift — or acknowledgement — is in
    key_path: str   # dotted key path, or the file's path relative to the layer
    kind: str       # list_shadow | file_shadow | stale_acknowledgement
    detail: str


def find_shadowing(paths: ProjectPaths | None = None) -> list[Shadow]:
    """Drift between the config layers and the shipped defaults (CD-R1..CD-R3).

    The global layer always; the project's ``.multiagents/config/`` too when
    `paths` is given. Per layer, without de-duplication: the same shadow in
    both layers is two items, because each is fixed by editing its own file.
    """
    layers = [global_config_dir()]
    if paths is not None:
        layers.append(Path(paths.config))
    out: list[Shadow] = []
    for layer in layers:
        out.extend(_drift_in_layer(layer))
    return out


def drift_summary(paths: ProjectPaths | None = None) -> str:
    """One line naming the count and `multiagents doctor` (CD-R4).

    For the server's startup log, where nobody is reading a list — the count
    is what makes a stale copied layer visible at all. Never raises: a
    warning that took the server down would be worse than the drift.
    """
    try:
        items = find_shadowing(paths)
    except Exception as exc:                       # detection, not a gate
        return f"config drift: unreadable ({type(exc).__name__})"
    if not items:
        return "config drift: none"
    return (f"config drift: {len(items)} item(s) shadow the shipped "
            f"defaults — `multiagents doctor` lists them")


def _read_yaml(path: Path):
    """The parsed file, or None when it is absent or not valid YAML.

    An unreadable layer is a job for doctor's other sections, not a drift
    item: this warns about what a file *says*, never about its shape.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, ValueError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _walk(layer: dict, shipped: dict, prefix: tuple[str, ...],
          layer_file: Path, out: list[Shadow]) -> None:
    """Collect list values that shadow a different shipped list (CD-R1).

    Scalars are deliberate overrides by nature and mappings merge, so only
    lists are reported — and only when both sides are lists, since a list
    where the shipped file has a scalar (or the reverse) is a reshape, not
    a frozen copy. Keys the shipped file does not have cannot shadow it.
    """
    for key, value in layer.items():
        if not isinstance(key, str):
            continue
        if not prefix and key == ACK_KEY:
            continue          # never drift itself, whatever the shipped file says
        if key not in shipped:
            continue
        theirs = shipped[key]
        if isinstance(value, list) and isinstance(theirs, list):
            if value != theirs:
                out.append(Shadow(
                    layer_file, ".".join(prefix + (key,)), LIST_SHADOW,
                    f"list of {len(value)} items shadows the shipped "
                    f"list of {len(theirs)} — list values replace, not merge"))
        elif isinstance(value, dict) and isinstance(theirs, dict):
            _walk(value, theirs, prefix + (key,), layer_file, out)


def _compare_yaml(layer_file: Path) -> list[Shadow]:
    shipped_file = shipped_defaults_dir() / layer_file.name
    layer = _read_yaml(layer_file)
    shipped = _read_yaml(shipped_file)
    if layer is None or shipped is None:
        return []
    out: list[Shadow] = []
    _walk(layer, shipped, (), layer_file, out)
    return out


def _compare_instructions(layer: Path) -> list[Shadow]:
    """Copied instruction briefs that differ from the shipped ones (CD-R2).

    Same relative path under ``agents/`` is the copy signature; a file the
    shipped tree does not have is the user's own agent, not a copy.
    """
    shipped_root = shipped_defaults_dir() / "agents"
    layer_root = layer / "agents"
    if not shipped_root.is_dir() or not layer_root.is_dir():
        return []
    out: list[Shadow] = []
    for shipped_file in sorted(shipped_root.rglob("*")):
        if not shipped_file.is_file():
            continue
        rel = shipped_file.relative_to(shipped_root)
        layer_file = layer_root / rel
        try:
            if not layer_file.is_file():
                continue
            if layer_file.read_bytes() == shipped_file.read_bytes():
                continue
            older = layer_file.stat().st_mtime < shipped_file.stat().st_mtime
        except OSError:
            continue
        # Only a copy older than the shipped file can be a frozen one; a
        # newer edit is at least a deliberate fork, so it is reported
        # without claiming the shipped version is newer.
        detail = ("differs from the shipped copy"
                  + (" — the shipped version is newer" if older else ""))
        out.append(Shadow(layer_file, f"agents/{rel.as_posix()}", FILE_SHADOW,
                          detail))
    return out


def _ack_scope(item: Shadow, ack_file: str) -> bool:
    """Can `ack_file`'s acknowledgement list silence this item?

    Key paths are acknowledged in the file that carries them. An ``.md``
    copy has no file of its own to carry one, so its acknowledgement lives
    in the same layer's ``agents.yaml`` (CD-R3) — and in no other file.
    """
    if item.kind == FILE_SHADOW:
        return ack_file == "agents.yaml"
    return item.kind == LIST_SHADOW and item.file.name == ack_file


def _apply_acknowledgements(layer: Path, items: list[Shadow]) -> list[Shadow]:
    """Drop acknowledged items, report acknowledgements that match nothing.

    An entry silences exactly the item it names in the file that carries
    it; one that names nothing — the drift was fixed, or never existed —
    is itself reported so the list cannot rot into folklore.
    """
    out: list[Shadow] = []
    for name in LAYER_FILES:
        data = _read_yaml(layer / name)
        entries = data.get(ACK_KEY) if data else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, str):
                continue
            matched = [i for i in items
                       if i.key_path == entry and _ack_scope(i, name)]
            if matched:
                items = [i for i in items if i not in matched]
            else:
                out.append(Shadow(layer / name, entry, STALE_ACK,
                                  "acknowledges no drift in this layer"))
    return items + out


def _drift_in_layer(layer: Path) -> list[Shadow]:
    items: list[Shadow] = []
    for name in LAYER_FILES:
        layer_file = layer / name
        if layer_file.is_file():
            items.extend(_compare_yaml(layer_file))
    items.extend(_compare_instructions(layer))
    return _apply_acknowledgements(layer, items)
