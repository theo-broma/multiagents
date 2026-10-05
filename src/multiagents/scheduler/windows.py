"""Local wall-time membership and inherited windows (NC-R38/NC-R88)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ZONES = {}


def prepare(default_zone, records=()):
    """Load zone data before taking a plan transaction; evaluation is pure."""
    names, pending = {default_zone}, [records]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if isinstance(value.get("timezone"), str):
                names.add(value["timezone"])
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    for name in names:
        if name not in ZONES:
            try:
                ZONES[name] = ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                # Retain negative lookups too: schema validation inside the
                # transaction must not search zone files a second time.
                ZONES[name] = None


def contains(spec, zone, instant):
    if zone is None:
        raise ValueError("window: invalid timezone")
    ranges = [tuple(int(h) * 60 + int(m) for h, m in
                    (part.split(":") for part in value.split("-"))) for value in spec["ranges"]]
    if any(start == end for start, end in ranges):
        return False
    local = datetime.fromtimestamp(instant, zone)
    minute = local.hour * 60 + local.minute + local.second / 60 + local.microsecond / 60000000
    today, yesterday = DAYS[local.weekday()], DAYS[(local.weekday() - 1) % 7]
    for start, end in ranges:
        if end > start:
            if today in spec["days"] and start <= minute < end:
                return True
        elif (today in spec["days"] and minute >= start
              or yesterday in spec["days"] and minute < end):
            return True
    return False


def boundaries(spec, zone, start, end):
    if zone is None:
        raise ValueError("window: invalid timezone")
    # Both folds are candidates; membership below discards imaginary gap
    # endpoints. Offset changes are boundaries too (a gap can skip an endpoint).
    day = datetime.fromtimestamp(start, zone).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, zone).date() + timedelta(days=1)
    while day <= last:
        for value in spec["ranges"]:
            for part in value.split("-"):
                h, m = map(int, part.split(":"))
                wall = datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=m)
                for fold in (0, 1):
                    stamp = wall.replace(tzinfo=zone, fold=fold).timestamp()
                    if start < stamp < end:
                        yield stamp
        day += timedelta(days=1)
    left = start
    offset = datetime.fromtimestamp(left, zone).utcoffset()
    while left < end:
        right = min(left + 3600, end)
        next_offset = datetime.fromtimestamp(right, zone).utcoffset()
        if next_offset != offset:
            lo, hi = int(left), int(right) + 1
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if datetime.fromtimestamp(mid, zone).utcoffset() == offset:
                    lo = mid
                else:
                    hi = mid
            if start < hi < end:
                yield hi
        left, offset = right, next_offset


@lru_cache(maxsize=2048)
def evaluate(encoded, default_zone, instant):
    specs = json.loads(encoded)
    if not specs:
        return {"open": True, "empty": False, "next_open": None, "next_close": None}
    windows = []
    for spec in specs:
        name = spec.get("timezone", default_zone)
        zone = ZONES.get(name)
        if zone is None:
            raise ValueError(f"window: invalid timezone {name!r}")
        windows.append((spec, zone))
    end = instant + 14 * 86400
    points = {instant, end}
    for spec, zone in windows:
        points.update(boundaries(spec, zone, instant, end))
    points = sorted(points)
    opened = all(contains(spec, zone, instant) for spec, zone in windows)
    previous, nonempty = opened, False
    next_open = next_close = None
    for left, right in zip(points, points[1:]):
        member = all(contains(spec, zone, (left + right) / 2) for spec, zone in windows)
        nonempty |= member
        if member != previous:
            stamp = datetime.fromtimestamp(left, timezone.utc).isoformat()
            if member and next_open is None:
                next_open = stamp
            elif not member and next_close is None:
                next_close = stamp
        previous = member
    return {"open": opened, "empty": not nonempty, "next_open": next_open, "next_close": next_close}


def effective(node, nodes, default_zone, instant):
    specs = []
    while node:
        if node.get("window"):
            specs.append(node["window"])
        node = nodes.get(node.get("parent"))
    return evaluate(json.dumps(specs, sort_keys=True), default_zone, instant)
