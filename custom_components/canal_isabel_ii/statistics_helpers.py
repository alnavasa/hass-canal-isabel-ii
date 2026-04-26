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


def is_cost_stream_regression(
    latest: float,
    restored: float | None,
    threshold: float = 0.01,
) -> bool:
    """Return True if ``latest`` cumulative cost has regressed below ``restored``.

    Used by both ``CanalCumulativeCostSensor.native_value`` (entity
    state guard) and ``CanalCumulativeCostSensor._push_cost_statistics_locked``
    (recorder push guard). Sharing the predicate keeps the two paths
    in lockstep — if the entity state holds at the old value, the push
    must skip too, otherwise the recorder ends up with a series whose
    sum drops below the entity state's sum and the Energy panel
    renders the seam as a large negative bar (the v0.5.20 regression
    that recurred even after ``clear_cost_stats`` because nothing in
    the push path mirrored the state-side guard).

    ## Why a threshold

    Floating-point noise in :func:`compute_hourly_cost_stream` can
    produce cum_eur values that differ by sub-cent amounts between
    runs (e.g. tariff segment splits at vigencia boundaries, or
    division-by-period_m3 in :func:`per_m3_with_iva` rounding
    differently when the sum of liters changes by 1 mL). A bare
    ``latest < restored`` would flag those as regressions and freeze
    the sensor on noise. The default 1-cent threshold absorbs that —
    real regressions are at minimum one bimonth's cuota fija (≈ 18 €
    for diametro=15, n_viv=1), nowhere near the threshold.

    ## Edge cases

    - ``restored is None`` (fresh install, no prior state to compare
      against): never a regression, always returns False so the
      cold-start push runs.
    - ``latest == restored`` (idempotent recompute): not a regression,
      returns False so the push proceeds and re-asserts the same
      values to the recorder (cheap upsert, keeps the seam-detection
      logic in :func:`merge_forward_and_backfill` calibrated).
    - ``latest`` slightly below ``restored`` but within threshold:
      not a regression — covers the floating-point noise case above.
    """
    if restored is None:
        return False
    return latest < restored - threshold


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


def cumulative_to_deltas(
    items: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Invert a monotonic cumulative series into per-hour deltas.

    The cost stream produced by ``compute_hourly_cost_stream`` is
    *already cumulative* (``cumulative_eur`` per hour). To feed it
    into :func:`merge_forward_and_backfill` — which operates on
    deltas and replays from zero — we need the inverse transform:
    ``delta[i] = cumulative[i] - cumulative[i-1]``, with the first
    row's delta being its own absolute value (treats the series as
    starting from zero, matching the merge function's semantics).

    Defensive: a non-monotonic input row (``cum[i] < cum[i-1]``)
    emits a 0.0 delta rather than a negative one. The cost stream is
    monotonic by construction (cuota fija + non-negative variable),
    so this clamp never fires in production — but it keeps the
    helper resilient against future callers and against any rounding
    artefact that might produce a tiny negative delta.

    Output order matches input order. Sorting is the caller's job
    (or the merge function's, since it sorts internally).
    """
    out: list[tuple[datetime, float]] = []
    prev = 0.0
    for ts, cum in items:
        d = cum - prev
        if d < 0:
            d = 0.0
        out.append((ts, d))
        prev = cum
    return out


def needs_backfill(
    items: list[tuple[datetime, float]],
    last_start: datetime | None,
) -> bool:
    """Return True if the push includes a timestamp at or before ``last_start``.

    ``continuation_stats`` blocks every such row (it treats them as
    "already in HA"). That's the right default for rolling-forward
    pushes — but a user who explicitly filtered the portal to a past
    month will trip this guard on every single row. Detecting that
    case here is what lets the caller branch into the
    ``merge_forward_and_backfill`` recomputation path.

    ``last_start is None`` means "no previous stats" → nothing to
    backfill, let the cold-start path in ``continuation_stats`` run.
    """
    if last_start is None or not items:
        return False
    return any(ts <= last_start for ts, _ in items)


def merge_forward_and_backfill(
    new_items: list[tuple[datetime, float]],
    existing_rows: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """Merge a backfill push with existing statistics into a full replay.

    ## The problem

    ``continuation_stats`` is purpose-built for "add hours after
    ``last_start``", and it refuses to touch anything older. That's
    correct for the default rolling-forward flow, where a
    past-timestamp row would almost always be a rogue duplicate from
    a stale cache. But it breaks the user-intentional backfill case:
    the user filters the portal to January, pulls the CSV, and the
    algorithm silently drops every row because January lies entirely
    before ``last_start`` (which is "this week").

    ## The invariant this function exploits

    The Energy dashboard computes each hour's bar as
    ``sum[n] - sum[n-1]`` — it does NOT read the absolute ``sum``.
    So if we shift the entire running-sum series by a constant, every
    bar stays the same. In particular, recomputing from zero over
    the merged series produces the *same bars* as the old series for
    every hour that was already stored, plus correct bars for the
    newly-inserted hours. The end-of-series ``sum`` will differ by
    a constant offset, but only the last row of the OLD series was
    ever visible to anyone as "sum at last_start" — every older row's
    role is to render its one bar, which is invariant.

    ## What we do

    1. Convert ``existing_rows`` (``(start, sum)`` from HA's recorder)
       back into deltas via ``delta[i] = sum[i] - sum[i-1]`` (first
       row's delta is its ``sum`` — equivalent to assuming running
       started at zero).
    2. Merge those deltas with ``new_items`` deltas. On timestamp
       collision, **new wins** (a re-downloaded CSV for the same hour
       is authoritative; the user is explicitly rewriting history).
    3. Sort by timestamp, recompute a monotonic running sum from
       zero, return ``(start, running)`` pairs for every slot.

    The caller then upserts the whole list via
    ``async_add_external_statistics``. HA's ``(statistic_id, start)``
    upsert semantics mean the overlap rows get overwritten with
    their (unchanged, modulo the offset) values, and the new rows
    get inserted in their chronological place.

    ## Edge cases

    - Empty ``existing_rows``: degenerates to ``continuation_stats``
      cold start (sum from zero, no filter).
    - Empty ``new_items``: returns the existing series re-rendered
      from zero — a no-op for anyone reading bar heights. Harmless
      to upsert but the caller should skip for efficiency.
    - Timestamp collisions: the item in ``new_items`` replaces the
      one in ``existing_rows``. This is how a user who re-downloads
      the portal for the same range gets their latest values.
    """
    # Invert existing running-sum into deltas.
    existing_sorted = sorted(existing_rows, key=lambda x: x[0])
    existing_deltas: dict[datetime, float] = {}
    prev_sum = 0.0
    for ts, running in existing_sorted:
        delta = running - prev_sum
        # Defensive: a negative delta would mean the stored series
        # already has a regression. Pin to 0 so the merged replay
        # stays monotonic. This should never happen with our own
        # writes but protects against manual recorder edits.
        if delta < 0:
            delta = 0.0
        existing_deltas[ts] = delta
        prev_sum = running

    # Merge — new items take precedence on collision.
    merged: dict[datetime, float] = dict(existing_deltas)
    for ts, liters in new_items:
        merged[ts] = liters

    # Replay from zero.
    if not merged:
        return []
    rows = sorted(merged.items(), key=lambda x: x[0])
    out: list[tuple[datetime, float]] = []
    running = 0.0
    for ts, liters in rows:
        running += liters
        out.append((ts, running))
    return out
