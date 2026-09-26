"""Capacity calculations are arithmetic, not staging performance evidence."""

import math

import pytest

from queue_load_test.capacity import projected_duration_seconds, theoretical_throughput


def test_theoretical_throughput_scales_with_bounded_contexts_and_duration() -> None:
    assert theoretical_throughput(20, 0.5) == 40.0
    assert theoretical_throughput(50, 2.0) == 25.0


def test_projection_uses_observed_rate_and_explicit_utilization() -> None:
    assert projected_duration_seconds(10_000, 1_000) == 10.0
    assert projected_duration_seconds(10_000, 1_000, utilization=0.5) == 20.0
    assert projected_duration_seconds(0, 1_000) == 0.0


@pytest.mark.parametrize("contexts,duration", [(0, 1.0), (1, 0.0), (1, math.inf)])
def test_theoretical_throughput_rejects_invalid_inputs(contexts: int, duration: float) -> None:
    with pytest.raises(ValueError):
        theoretical_throughput(contexts, duration)


@pytest.mark.parametrize(
    "work_items,rate,utilization",
    [
        (-1, 1.0, 1.0),
        (1, 0.0, 1.0),
        (1, math.nan, 1.0),
        (1, 1.0, 0.0),
        (1, 1.0, 1.01),
        (1, 1.0, math.inf),
    ],
)
def test_projection_rejects_invalid_inputs(
    work_items: int, rate: float, utilization: float
) -> None:
    with pytest.raises(ValueError):
        projected_duration_seconds(work_items, rate, utilization=utilization)
