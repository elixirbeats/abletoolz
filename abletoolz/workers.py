"""Choose a bounded worker count from the CPUs available to this process."""

from __future__ import annotations

import os


def available_cpu_count() -> int:
    """Respect process CPU limits, including affinity on supported Python versions."""
    process_count = getattr(os, "process_cpu_count", None)
    affinity = getattr(os, "sched_getaffinity", None)
    return first_known_count(
        process_count() if callable(process_count) else None,
        len(affinity(0)) if callable(affinity) else None,
        os.cpu_count(),
    )


def first_known_count(*counts: object) -> int:
    """The first of ``counts`` that is a positive int, most specific first; 1 when none is."""
    return next((count for count in counts if isinstance(count, int) and count > 0), 1)


def worker_count(requested: int | None, *, tasks: int | None = None, limit: int | None = None) -> int:
    """Use available CPUs for auto; preserve positive overrides within executor limits."""
    if requested is not None and requested < 1:
        raise ValueError("Worker count must be positive or auto")
    if tasks is not None and tasks < 0:
        raise ValueError("Task count cannot be negative")
    if limit is not None and limit < 1:
        raise ValueError("Worker limit must be positive")
    selected = available_cpu_count() if requested is None else requested
    if tasks is not None:
        selected = min(selected, max(1, tasks))
    if limit is not None:
        if requested is not None and selected > limit:
            raise ValueError(f"This executor supports at most {limit} workers")
        selected = min(selected, limit)
    return selected
