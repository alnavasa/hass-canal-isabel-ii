"""Pure helpers for the long-term-statistics push pipeline.

Extracted from ``sensor.py`` so the spike-immune algorithm can be
exercised in isolation — the production caller is buried behind
HomeAssistant's coordinator/recorder machinery, which is too heavy to
spin up for what is, in essence, a dozen-line numerical loop.

The contract of this module is therefore frozen by ``test_continuation_stats.py``:
break it and the tests fire, regardless of what happens in the wider
sensor entity.
"""

from __future__ import annotations

from datetime import datetime


def continuation_stats(
    items: list[tuple[datetime, float]],
    last_sum: float = 0.0,
    last_start: datetime | None = None,
) -> list[tuple[datetime, float]]:
    """Convert hourly liter deltas into appendable ``(start_utc, sum)`` pairs.

    The input ``items`` are ``(timestamp_utc, liters_in_that_hour)``
    pairs assumed to already be in UTC and assumed to be free of
    duplicate timestamps. Sorting is enforced here so callers can pass
    coordinator output as-is without a separate sort step (cheap on the
    sizes we deal with — at most a few hundred rows per push).

    The output is ``(start_utc, running_sum)`` for each *new* slot: any
    item with ``timestamp_utc <= last_start`` is dropped (already in
    HA's recorder from a previous push). The running sum starts from
    ``last_sum`` so the curve stays continuous across the join, which
    is what keeps the Energy dashboard's per-hour bar (``sum_n -
    sum_{n-1}``) from rendering a gigantic negative spike when the
    local cache is wiped and the next coordinator tick sees fewer
    rows than before.

    Why this matters (the bug this prevents):
    ``async_add_external_statistics`` upserts by ``(statistic_id, start)``.
    If we recomputed the running total from zero each push, the very
    first slot after a cache wipe would overwrite a previously-large
    ``sum`` with a small one. The dashboard would then render
    ``small - previous_large = a huge negative bar`` for that hour,
    and worse, every subsequent slot would be off by the wipe gap.

    By continuing from ``last_sum`` and skipping ``ts <= last_start``,
    every push is idempotent and monotonic: pushing the same items
    twice is a no-op; pushing items + new items appends only the
    delta; a shrunken cache simply contributes nothing on its own
    until truly new data arrives.

    Caveat — ``items`` must already be in UTC. Timezone normalisation
    is the caller's job because it depends on HA's runtime default
    zone, which we don't want to import here. The pure function pins
    the algorithm; the timezone gymnastics live next to the recorder
    call where they belong.
    """
    if not items:
        return []

    rows = sorted(items, key=lambda x: x[0])
    out: list[tuple[datetime, float]] = []
    running = last_sum
    for ts_utc, liters in rows:
        if last_start is not None and ts_utc <= last_start:
            continue
        running += liters
        out.append((ts_utc, running))
    return out
