"""Minimal 5-field cron: minute hour day-of-month month day-of-week (+ @daily style macros)."""
from datetime import date, datetime, time as dtime, timedelta
from typing import NamedTuple

MONTHS = {n: i for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
DOWS = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}
MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
          "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}


class Cron(NamedTuple):
    minutes: set
    hours: set
    doms: set
    months: set
    dows: set
    dom_star: bool
    dow_star: bool


def _val(tok: str, names):
    t = tok.strip().lower()
    return names[t] if names and t in names else int(t)


def _field(spec: str, lo: int, hi: int, names=None) -> set:
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ValueError(f"bad step in '{spec}'")
        if part in ("*", "?"):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _val(x, names), _val(y, names)
        else:
            a = _val(part, names)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise ValueError(f"value out of range {lo}-{hi} in '{spec}'")
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> Cron:
    expr = MACROS.get(expr.strip().lower(), expr.strip())
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week")
    try:
        return Cron(_field(parts[0], 0, 59), _field(parts[1], 0, 23), _field(parts[2], 1, 31),
                    _field(parts[3], 1, 12, MONTHS), {d % 7 for d in _field(parts[4], 0, 7, DOWS)},
                    parts[2].startswith("*"), parts[4].startswith("*"))
    except ValueError as exc:
        raise ValueError(f"invalid cron '{expr}': {exc}") from None


def _day_ok(c: Cron, d: date) -> bool:
    dom_ok = d.day in c.doms
    dow_ok = (d.weekday() + 1) % 7 in c.dows
    if c.dom_star and c.dow_star:
        return True
    if c.dom_star:
        return dow_ok
    if c.dow_star:
        return dom_ok
    return dom_ok or dow_ok  # standard cron: either matches when both are restricted


def next_run(expr: str, after: datetime) -> datetime:
    """First matching local time strictly after `after`."""
    c = parse_cron(expr)
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for offset in range(366 * 8):
        d = start.date() + timedelta(days=offset)
        if d.month in c.months and _day_ok(c, d):
            for h in sorted(c.hours):
                for m in sorted(c.minutes):
                    cand = datetime.combine(d, dtime(h, m))
                    if cand >= start:
                        return cand
    raise ValueError("this cron expression never fires")


def preview(expr: str, count=3, after: datetime | None = None) -> list[datetime]:
    t, out = after or datetime.now(), []
    for _ in range(count):
        t = next_run(expr, t)
        out.append(t)
    return out
