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
from typing import Any, Callable

import yaml

from . import scripts
from .providers import Provider


def refresh_models(providers: dict[str, Provider], target: Path, *,
                   executor_for: Callable[[str], Any] | None = None,
                   config_dir: Path | None = None,
                   project_config: Path | None = None) -> dict[str, Any]:
    """Ask every enabled provider for its models and write `target`.

    In order: a static `models:` list, then `models_cmd`, then — for a
    provider with neither — its `models` action (CX-C4), run through
    `scripts.run_action` so it gets the provider's environment and is found
    where its other actions are. `executor_for(name)` gives that action its
    executor; local when not given.
    """
    from .executor.local import LocalExecutor
    from .paths import global_config_dir

    models: dict[str, list[dict[str, str]]] = {}
    problems: dict[str, str] = {}

    for name, provider in providers.items():
        if not provider.enabled:
            problems[name] = "disabled in providers.yaml"
            continue

        # A static list is the answer for a CLI with no way to enumerate its
        # models — claude has no `models` subcommand. Previously such providers
        # were skipped silently, before the PATH check, so claude never appeared
        # in models.yaml and nothing ever said why.
        if provider.models_static:
            models[name] = [
                m for m in provider.models_static
                if isinstance(m, dict) and provider.allows_model(m.get("id", ""))
            ]
            continue

        config = config_dir or global_config_dir()
        if not provider.models_cmd and scripts.resolve(
                name, provider, config, project_config) is None:
            problems[name] = "no models_cmd and no static models: list"
            continue
        if not provider.available():
            problems[name] = f"{provider.bin} not on PATH"
            continue
        if not provider.models_cmd:
            executor = executor_for(name) if executor_for else LocalExecutor()
            code, out, err = scripts.run_action(name, provider, executor, "models",
                                                config, project_config, timeout=120)
            if code == scripts.UNIMPLEMENTED:
                continue                  # says it has no `models`: nothing to say
            if code != 0:
                problems[name] = (err or out).strip()[:200] or f"`models` exited {code}"
                continue
            models[name] = provider.parse_models(out)
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
