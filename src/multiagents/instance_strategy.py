"""Names shared by family routing and configuration validation (IS-R1/R2)."""

STRATEGIES = (
    "soonest_reset",
    "shortest_window_least_remaining",
    "shortest_window_most_remaining",
    "longest_window_least_remaining",
    "longest_window_most_remaining",
    "least_loaded",
)


class InstanceStrategyError(ValueError):
    """An invalid strategy, which doctor can report while checking the rest."""


def validate_strategy(value, *, nullable=False, source=""):
    if nullable and value is None:
        return
    if not isinstance(value, str) or value not in STRATEGIES:
        raise InstanceStrategyError(
            f"instance_strategy: unknown strategy {value!r}"
            + (f" ({source})" if source else ""))
