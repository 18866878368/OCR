"""Date-only evidence stays a date; midnight is never invented."""
from datetime import date, datetime
import re

DATE = re.compile(r'(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})\s*日?')
CLOCK = re.compile(r'(?<!\d)([01]?\d|2[0-3])\s*[:：时]\s*([0-5]?\d)(?:\s*[:：分]\s*([0-5]?\d))?(?!\d)')


def date_values(text):
    result = set()
    for m in DATE.finditer(str(text)):
        try:
            result.add(date(*map(int,m.groups())).isoformat())
        except ValueError:
            pass
    return result


def canonical_date(value):
    values = date_values(value)
    return next(iter(values)) if len(values)==1 else None


def composed_times(text):
    days = date_values(text)
    if len(days)!=1:
        return set()
    day = next(iter(days))
    return {day+f'T{int(m[1]):02}:{int(m[2]):02}:{int(m[3] or 0):02}' for m in CLOCK.finditer(text)}
