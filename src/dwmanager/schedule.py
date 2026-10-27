"""Cron expressions for scheduled tasks: minute hour day-of-month month day-of-week.

Supports *, numbers, lists (1,15), ranges (1-5), steps (*/15, 0-30/10) and day names (mon-fri).
Day of week: 0 or 7 = Sunday. Like classic cron, when both day fields are restricted a day
matches if either matches.
"""
from datetime import datetime, timedelta

ACTIONS = ("backup", "restart", "update", "start", "stop")
_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_FIELDS = ((0, 59, {}), (0, 23, {}), (1, 31, {}), (1, 12, _MON), (0, 7, _DOW))


class CronError(ValueError):
    pass


def _value(text, names):
    text = text.lower()
    if text in names:
        return names[text]
    if not text.isdigit():
        raise CronError(f"'{text}' is not a number")
    return int(text)


def _field(text, lo, hi, names):
    values = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"bad step '{s}'")
            step = int(s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names), _value(b, names)
        else:
            start = _value(part, names)
            end = hi if step > 1 else start
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise CronError(f"'{part}' is outside {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expr):
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("a cron expression has 5 fields: minute hour day month weekday")
        self.expr = " ".join(parts)
        self.minute, self.hour, self.dom, self.month, dow = (
            _field(p, lo, hi, names) for p, (lo, hi, names) in zip(parts, _FIELDS))
        self.dow = {d % 7 for d in dow}
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"

    def matches(self, dt):
        if dt.minute not in self.minute or dt.hour not in self.hour or dt.month not in self.month:
            return False
        dom_ok = dt.day in self.dom
        dow_ok = (dt.isoweekday() % 7) in self.dow
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, dt, limit_days=366):
        t = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = t + timedelta(days=limit_days)
        while t < end:
            if self.matches(t):
                return t
            t += timedelta(minutes=1)
        return None


PRESETS = [
    ("0 5 * * *", "daily_5"),
    ("0 */6 * * *", "every_6h"),
    ("0 4 * * 1", "weekly_mon_4"),
    ("*/30 * * * *", "every_30m"),
]


def validate(expr, action):
    if action not in ACTIONS:
        raise CronError(f"unknown action '{action}'")
    return Cron(expr).expr


def next_run(expr, now=None):
    try:
        return Cron(expr).next_after(now or datetime.now())
    except CronError:
        return None
