from __future__ import annotations

import datetime
from dataclasses import dataclass

# Kurzformen wie bei cron selbst.
ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

# (Name, kleinster, groesster Wert) je Feld, in cron-Reihenfolge.
FIELDS = [
    ("Minute", 0, 59),
    ("Stunde", 0, 23),
    ("Tag", 1, 31),
    ("Monat", 1, 12),
    ("Wochentag", 0, 7),
]


def _parse_field(text: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        range_part, _, step_text = part.partition("/")
        try:
            step = int(step_text) if step_text else 1
            if range_part == "*":
                start, end = low, high
            elif "-" in range_part:
                start_text, end_text = range_part.split("-", 1)
                start, end = int(start_text), int(end_text)
            else:
                start = int(range_part)
                # "5/15" heisst bei cron: ab 5 alle 15.
                end = high if step_text else start
        except ValueError as exc:
            raise ValueError(f"{name}: ungültiger Wert {part!r}") from exc
        if step < 1 or not low <= start <= end <= high:
            raise ValueError(f"{name}: {part!r} liegt nicht in {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


@dataclass(frozen=True)
class CronSchedule:
    """A standard 5-field cron expression (minute hour day month weekday),
    evaluated in the server's local time. Supports `*`, lists, ranges,
    steps (`*/15`, `1-5`, `0,30`, `8-18/2`) and the usual `@daily` style
    aliases. No month/weekday names. Like cron, if both day-of-month and
    weekday are restricted, a time matches if EITHER matches."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sonntag (7 wird auf 0 abgebildet)
    days_restricted: bool
    weekdays_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        text = ALIASES.get(str(expression).strip(), str(expression).strip())
        parts = text.split()
        if len(parts) != 5:
            raise ValueError(
                f"Cron-Ausdruck {expression!r} braucht 5 Felder "
                "(Minute Stunde Tag Monat Wochentag), z.B. '0 3 * * *'"
            )
        try:
            parsed = [
                _parse_field(part, name, low, high)
                for part, (name, low, high) in zip(parts, FIELDS)
            ]
        except ValueError as exc:
            raise ValueError(f"Cron-Ausdruck {expression!r}: {exc}") from exc
        minutes, hours, days, months, weekdays = parsed
        return cls(
            expression=str(expression),
            minutes=frozenset(minutes),
            hours=frozenset(hours),
            days=frozenset(days),
            months=frozenset(months),
            weekdays=frozenset(day % 7 for day in weekdays),
            days_restricted=parts[2] != "*",
            weekdays_restricted=parts[4] != "*",
        )

    def matches(self, moment: datetime.datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        day_ok = moment.day in self.days
        # Python: Montag=0 .. Sonntag=6; cron: Sonntag=0 .. Samstag=6.
        weekday_ok = (moment.weekday() + 1) % 7 in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok
