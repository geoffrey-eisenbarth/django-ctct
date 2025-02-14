import datetime as dt

from django.db.models import Model
from django.utils import timezone


def to_dt(s: str) -> dt.datetime:
  TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
  return timezone.make_aware(dt.datetime.strptime(s, TS_FORMAT))

def get_related_fields(model: Model) -> (list, list, list, list):
  one_to_ones = []
  many_to_manys = []
  foreign_keys = []
  reverse_fks = []

  for field in model._meta.get_fields():
    if field.one_to_one:
      one_to_ones.append(field)
    elif field.many_to_many:
      many_to_manys.append(field)
    elif field.many_to_one:
      if field.concrete:
        foreign_keys.append(field)
      else:
        reverse_fks.append(field)
    elif field.one_to_many:
      reverse_fks.append(field)

  return one_to_ones, many_to_manys, foreign_keys, reverse_fks
