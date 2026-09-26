"""Pure planning calculations; no browser or staging traffic is performed."""

import math


def theoretical_throughput(concurrent_contexts: int, average_duration_seconds: float) -> float:
    """Return the ideal operations/s if every context remains busy."""

    if concurrent_contexts < 1:
        raise ValueError("concurrent_contexts must be positive")
    if not math.isfinite(average_duration_seconds) or average_duration_seconds <= 0:
        raise ValueError("average_duration_seconds must be finite and positive")
    return concurrent_contexts / average_duration_seconds


def projected_duration_seconds(
    work_items: int,
    observed_throughput: float,
    *,
    utilization: float = 1.0,
) -> float:
    """Project wall time at an observed rate and an explicit planning utilization.

    The caller is responsible for applying the rate only to comparable work. A
    utilization below one is a planning assumption, not a measured safety margin.
    """

    if work_items < 0:
        raise ValueError("work_items cannot be negative")
    if not math.isfinite(observed_throughput) or observed_throughput <= 0:
        raise ValueError("observed_throughput must be finite and positive")
    if not math.isfinite(utilization) or not 0 < utilization <= 1:
        raise ValueError("utilization must be finite and within (0, 1]")
    return work_items / (observed_throughput * utilization)
