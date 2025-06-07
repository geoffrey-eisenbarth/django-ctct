from __future__ import annotations

import datetime as dt
from typing import Type, TypeAlias

from django.db.models import (
  Model, OneToOneField, ManyToManyField, ForeignKey, ManyToOneRel,
)
from django.utils import timezone


RelatedFields: TypeAlias = tuple[
  list[OneToOneField[Model]],
  list[ManyToManyField[Model, Model]],
  list[ForeignKey[Model]],
  list[ManyToOneRel],
]


def to_dt(s: str, ts_format: str = '%Y-%m-%dT%H:%M:%SZ') -> dt.datetime:
  return timezone.make_aware(dt.datetime.strptime(s, ts_format))


def get_related_fields(model: Type[Model]) -> RelatedFields:
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
