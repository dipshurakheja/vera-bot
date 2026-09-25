"""Time parsing and human date formatting (India-centric)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

IST = timezone(timedelta(hours=5, minutes=30))
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_INDEX = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> Optional[datetime]:
    """Parse ISO date/datetime strings. Naive values are treated as UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.strptime(s[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_date_only(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) == 10


def local(dt: datetime) -> datetime:
    """Keep an explicit offset if the source had one; otherwise show in IST."""
    if dt.utcoffset() == timedelta(0):
        return dt.astimezone(IST)
    return dt


def fmt_date(value: Any, weekday: bool = False, year: bool = False) -> str:
    dt = parse_iso(value)
    if dt is None:
        return str(value or "")
    d = dt if is_date_only(value) else local(dt)
    out = f"{d.day} {_MONTHS[d.month - 1]}"
    if year:
        out += f" {d.year}"
    if weekday:
        out = f"{_DAYS[d.weekday()]} {out}"
    return out


def fmt_time(value: Any) -> str:
    dt = parse_iso(value)
    if dt is None:
        return ""
    d = local(dt)
    hour12 = d.hour % 12 or 12
    suffix = "am" if d.hour < 12 else "pm"
    return f"{hour12}{suffix}" if d.minute == 0 else f"{hour12}:{d.minute:02d}{suffix}"


def weekday_name(value: Any, long: bool = False) -> str:
    dt = parse_iso(value)
    if dt is None:
        return ""
    d = dt if is_date_only(value) else local(dt)
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return names[d.weekday()] if long else _DAYS[d.weekday()]


def days_until(target: Any, now: Optional[datetime]) -> Optional[int]:
    t = parse_iso(target)
    if t is None or now is None:
        return None
    return (t.astimezone(IST).date() - now.astimezone(IST).date()).days


def month_of(now: Optional[datetime]) -> Optional[int]:
    return now.astimezone(IST).month if now else None


def month_range_contains(month_range: str, month: int) -> bool:
    """'Nov-Feb' contains 12; 'Jan' contains 1; 'Feb 14' contains 2."""
    parts = [p.strip().lower()[:3] for p in str(month_range).replace("–", "-").split("-")]
    idx = [MONTH_INDEX.get(p) for p in parts if MONTH_INDEX.get(p)]
    if not idx:
        return False
    if len(idx) == 1:
        return month == idx[0]
    start, end = idx[0], idx[-1]
    if start <= end:
        return start <= month <= end
    return month >= start or month <= end


def month_range_start(month_range: str) -> Optional[int]:
    part = str(month_range).replace("–", "-").split("-")[0].strip().lower()[:3]
    return MONTH_INDEX.get(part)
