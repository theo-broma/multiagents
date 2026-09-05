"""Generate ``models.yaml`` from the installed CLIs.

Hand-maintaining a list of sixty-odd model ids is a drift trap: the file
silently disagrees with reality, and you find out when a run fails on an unknown
model. Asking each CLI what it actually offers costs one subprocess call and
cannot drift.

Re-run after any subscription change — the available models move with the plan.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from .providers import Provider


def refresh_models(providers: dict[str, Provider], target: Path) -> dict[str, Any]:
    models: dict[str, list[dict[str, str]]] = {}
    problems: dict[str, str] = {}

    for name, provider in providers.items():
        if not provider.models_cmd:
            continue
        if not provider.available():
            problems[name] = f"{provider.bin} not on PATH"
            continue
        try:
            proc = subprocess.run(
                provider.models_cmd, capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            problems[name] = f"{type(exc).__name__}: {exc}"
            continue
        if proc.returncode != 0:
            problems[name] = (proc.stderr or proc.stdout).strip()[:200]
            continue
        models[name] = provider.parse_models(proc.stdout)

    target.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# GENERATED FILE — do not hand-edit.\n"
        "# Rewritten by `multiagents refresh-models`. Re-run after a subscription change.\n\n"
    )
    with target.open("w") as handle:
        handle.write(header)
        yaml.safe_dump(
            {"models": models, "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            handle, sort_keys=True, default_flow_style=False,
        )

    return {
        "written": str(target),
        "counts": {k: len(v) for k, v in models.items()},
        "problems": problems,
    }


def validate_agent_models(config: Any) -> list[str]:
    """Warn about agents naming a model no installed CLI reports.

    A warning, not an error: the list may simply be stale, and refusing to run
    on that basis would be worse than the mistake it prevents.
    """
    warnings: list[str] = []
    for name, spec in config.agents.items():
        available = config.models.get(spec.provider) or []
        if not available:
            continue
        ids = {m.get("id") for m in available if isinstance(m, dict)}
        if spec.model not in ids:
            warnings.append(
                f"agent {name!r} uses model {spec.model!r}, which {spec.provider} "
                f"does not currently list ({len(ids)} available)"
            )
    return warnings
