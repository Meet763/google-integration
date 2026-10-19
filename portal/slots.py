from datetime import datetime, timedelta


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def free_gaps(
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    if window_end <= window_start:
        return []
    gaps: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for start, end in merge_intervals(busy):
        block_start = max(start, window_start)
        block_end = min(end, window_end)
        if block_end <= window_start or block_start >= window_end:
            continue
        if block_start > cursor:
            gaps.append((cursor, block_start))
        cursor = max(cursor, block_end)
    if cursor < window_end:
        gaps.append((cursor, window_end))
    return [(start, end) for start, end in gaps if end > start]


def bookable_starts(
    gaps: list[tuple[datetime, datetime]],
    duration: timedelta,
    step: timedelta,
) -> list[tuple[datetime, datetime, datetime]]:
    """Return (slot_start, slot_end, gap_end) for each start that fits."""
    if duration <= timedelta(0) or step <= timedelta(0):
        return []
    slots: list[tuple[datetime, datetime, datetime]] = []
    for gap_start, gap_end in gaps:
        cursor = gap_start
        while cursor + duration <= gap_end:
            slots.append((cursor, cursor + duration, gap_end))
            cursor += step
    return slots


def overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    for busy_start, busy_end in busy:
        if busy_start < end and busy_end > start:
            return True
    return False
