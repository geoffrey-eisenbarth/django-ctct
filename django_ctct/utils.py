from __future__ import annotations

import datetime as dt

from django.db.models import (
  ForeignKey,
  ManyToManyField,
  ManyToOneRel,
  Model,
  OneToOneField,
)
from django.utils import timezone

type RelatedFields = tuple[
  list[OneToOneField[Model]],
  list[ManyToManyField[Model, Model]],
  list[ForeignKey[Model]],
  list[ManyToOneRel],
]


def to_dt(s: str) -> dt.datetime:
  """Parse an ISO 8601 timestamp from the API into an aware datetime.

  Notes
  -----
  The API returns UTC timestamps (with or without milliseconds), so naive
  values are assumed to be UTC.

  """
  parsed = dt.datetime.fromisoformat(s)
  if timezone.is_naive(parsed):
    parsed = timezone.make_aware(parsed, dt.UTC)
  return parsed


def get_related_fields(model: type[Model]) -> RelatedFields:
  one_to_ones: list[OneToOneField[Model]] = []
  many_to_manys: list[ManyToManyField[Model, Model]] = []
  foreign_keys: list[ForeignKey[Model]] = []
  reverse_fks: list[ManyToOneRel] = []

  for field in model._meta.get_fields():
    if isinstance(field, OneToOneField):
      one_to_ones.append(field)
    elif isinstance(field, ManyToManyField):
      many_to_manys.append(field)
    elif isinstance(field, ForeignKey):
      foreign_keys.append(field)
    elif isinstance(field, ManyToOneRel):
      reverse_fks.append(field)

  return one_to_ones, many_to_manys, foreign_keys, reverse_fks
