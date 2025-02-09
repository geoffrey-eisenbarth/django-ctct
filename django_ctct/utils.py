import datetime as dt

from django.utils import timezone


def to_dt(s: str) -> dt.datetime:
  TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
  return timezone.make_aware(dt.datetime.strptime(s, TS_FORMAT))
