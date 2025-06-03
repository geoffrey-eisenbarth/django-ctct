from __future__ import annotations

import datetime as dt
from typing import Type

from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models
from django.utils import timezone


OneToOneFields = list[models.OneToOneField[models.Model]]
ManyToManyFields = list[models.ManyToManyField[models.Model, models.Model]]
ForeignKeys = list[models.ForeignKey[models.Model]]
ManyToOneRels = list[models.ManyToOneRel]

RelatedFields = tuple[
  OneToOneFields,
  ManyToManyFields,
  ForeignKeys,
  ManyToOneRels,
]


def to_dt(s: str, ts_format: str = '%Y-%m-%dT%H:%M:%SZ') -> dt.datetime:
  return timezone.make_aware(dt.datetime.strptime(s, ts_format))


def get_related_fields(model: Type[models.Model]) -> RelatedFields:
  one_to_ones: OneToOneFields = []
  many_to_manys: ManyToManyFields = []
  foreign_keys: ForeignKeys = []
  reverse_fks: ManyToOneRels = []

  for field in model._meta.get_fields():
    if isinstance(field, GenericForeignKey):
      # Unused by the app, so we exclude them here to simplify type checking
      continue
    elif isinstance(field, models.OneToOneField):
      one_to_ones.append(field)
    elif isinstance(field, models.ManyToManyField):
      many_to_manys.append(field)
    elif isinstance(field, models.ForeignKey):
      foreign_keys.append(field)
    elif isinstance(field, models.ManyToOneRel):
      reverse_fks.append(field)

  return one_to_ones, many_to_manys, foreign_keys, reverse_fks
