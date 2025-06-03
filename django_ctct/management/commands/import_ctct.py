from argparse import ArgumentParser
from collections import defaultdict
from typing import Type, Optional, Any, TypeVar, Collection, TypeAlias
from uuid import UUID

from tqdm import tqdm

import django
from django.db import models
from django.utils.translation import gettext as _
from django.core.management.base import BaseCommand

from django_ctct.models import (
  CTCTModel, ContactList, CustomField,
  Contact, ContactCustomField,
  EmailCampaign, CampaignActivity, CampaignSummary,
)
from django_ctct.utils import get_related_fields


ModelType = TypeVar('ModelType', bound=models.Model)
RelatedType = TypeVar('RelatedType', bound='CTCTModel')
RelatedObjects: TypeAlias = dict[Type[RelatedType], list[RelatedType]]


class Command(BaseCommand):
  """Imports django-ctct model instances from CTCT servers.

  Notes
  -----
  CTCT does not provide an endpoint for fetching bulk CampaignActivities.
  As a result, we must loop through the EmailCampaigns, make a request to get
  the associated CampaignActivities, and then make a second request to get the
  details of the CampaignActivity.

  As a result, importing CampaignActivities will be slow, and running it
  multiple times may result in exceeding CTCT's 10,000 requests per day
  limit.

  """

  help = 'Imports data from ConstantContact'

  CTCT_MODELS: list[Type[CTCTModel]] = [
    ContactList,
    CustomField,
    Contact,
    EmailCampaign,
    CampaignActivity,
    # CampaignSummary,  # TODO: Implement CampaignSummary
  ]

  def get_id_to_pk(self, model: Type[CTCTModel]) -> dict[str, int]:
    """Returns a dictionary to convert CTCT API ids to Django pks."""
    id_to_pk = {
      str(api_id): int(pk)
      for api_id, pk in model.objects.values_list('api_id', 'pk')
      if api_id is not None
    }
    return id_to_pk

  def upsert(
    self,
    model: Type[CTCTModel],
    objs: list[CTCTModel],
    update_conflicts: bool = True,
    unique_fields: Optional[Collection[str]] = ['api_id'],
    update_fields: Optional[Collection[str]] = None,
    silent: Optional[bool] = None,
  ) -> list[CTCTModel]:

    verb = 'Imported' if (update_fields is None) else 'Updated'
    if silent is None:
      silent = self.noinput

    # Perform upsert using `bulk_create()`
    if model._meta.auto_created or (model is ContactCustomField):  # type: ignore[comparison-overlap]  # noqa: E501
      # TODO: Implement ContactCustomField
      if not issubclass(model, CTCTModel):
        # Delete ManyToMany objects
        model.objects.all().delete()
      update_conflicts = False
      unique_fields = update_fields = None
    elif model is CampaignSummary:  # type: ignore[comparison-overlap]
      # TODO: Implement CampaignSummary
      assert hasattr(model, 'remote')
      update_conflicts = True
      unique_fields = ['campaign_id']
      update_fields = model.remote.API_READONLY_FIELDS[1:]
    elif update_fields is None:
      update_fields = [
        f.name
        for f in model._meta.fields
        if not f.primary_key and (f.name != 'api_id')
      ]

    # Remove possible duplicates (CTCT API can't be trusted)
    if issubclass(model, CTCTModel):
      id_field = 'api_id'
    elif model is CampaignSummary:
      id_field = 'campaign_id'
    else:
      id_field = None

    if id_field is not None:
      seen, unique_objs = set(), []
      for obj in objs:
        if getattr(obj, id_field) not in seen:
          seen.add(getattr(obj, id_field))
          unique_objs.append(obj)
    else:
      unique_objs = objs

    # Perform the upsert
    objs_w_pks = model.objects.bulk_create(
      objs=unique_objs,
      update_conflicts=update_conflicts,
      unique_fields=unique_fields,
      update_fields=update_fields,
    )
    if update_conflicts and (django.get_version() < '5.0'):
      # In older versions, enabling the update_conflicts parameter prevented
      # setting the primary key on each model instance.
      if issubclass(model, CTCTModel) and hasattr(model, 'api_id'):
        # CampaignSummary doesn't have `api_id` field (or related_objs)
        id_to_pk = self.get_id_to_pk(model)
        for o in objs_w_pks:
          if o.api_id is not None:
            setattr(o, 'pk', id_to_pk[str(o.api_id)])

    # Inform the user
    if not silent:
      message = self.style.SUCCESS(
        f'{verb} {len(objs)} {model.__name__} instances.'
      )
      self.stdout.write(message)

    return objs_w_pks

  def set_direct_object_pks(
    self,
    model: Type[CTCTModel],
    instances: list[CTCTModel],
  ) -> None:
    """Sets Django pk values for OneToOne and ForeignKeys objects."""

    otos, _, fks, _ = get_related_fields(model)
    fields = otos + fks
    for field in fields:
      related_model = field.related_model
      if (related_model != 'self') and issubclass(related_model, CTCTModel):
        id_to_pk = self.get_id_to_pk(related_model)
        for o in instances:
          setattr(o, field.attname, id_to_pk[str(getattr(o, field.attname))])

  def set_related_object_pks(
    self,
    model: Type[CTCTModel],
    objs_w_pks: list[CTCTModel],
    related_objs: dict[Type[RelatedType], list[RelatedType]]
  ) -> None:
    """Sets Django pk values for ManyToMany and ReverseForeignKey objects."""

    if not any(related_objs):
      return

    _, m2ms, _, rfks = get_related_fields(model)

    m2m_attnames = {
      field.remote_field.through: (
        field.m2m_column_name(), field.m2m_reverse_name()
      )
      for field in m2ms
    }
    if m2ms:
      id_to_pk = {
        m2m.remote_field.through: self.get_id_to_pk(m2m.related_model)
        for m2m in m2ms
      }

    if model is Contact:
      m2m_attnames[ContactCustomField] = ('contact_id', 'custom_field_id')
      id_to_pk[ContactCustomField] = self.get_id_to_pk(CustomField)

    rfk_attnames = {
      rel.related_model: rel.field.attname
      for rel in rfks
    }

    for obj_w_pk, related_objs in zip(objs_w_pks, related_objs):
      for related_model, related_obj_list in related_objs.items():
        if m2m_attname := m2m_attnames.get(related_model):
          column_name, reverse_name = m2m_attname
          for o in related_obj_list:
            setattr(o, column_name, obj_w_pk.pk)
            api_id = str(getattr(o, reverse_name))
            # TODO: use through_model instead of related_model?
            setattr(o, reverse_name, id_to_pk[related_model][api_id])

        elif rfk_attname := rfk_attnames.get(related_model):
          for o in related_obj_list:
            setattr(o, rfk_attname, obj_w_pk.pk)

  def import_model(self, model: Type[CTCTModel]) -> None:
    """Imports objects from CTCT into Django's database."""

    objs: list[CTCTModel]
    related_objs: RelatedObjects[RelatedType]

    if model is CampaignActivity:
      # CampaignActivities do not have a bulk API endpoint
      return self.import_campaign_activities()

    model.remote.connect()
    try:
      # Split apart so we can save objs to db and get pks
      objs, related_objs = zip(*model.remote.all())
    except ValueError:
      # No values returned
      return

    # TODO: Implement CampaignSummary
    if model is CampaignSummary:  # type: ignore[comparison-overlap]
      # Convert API id to Django pk for the OneToOneField with EmailCampaign
      self.set_direct_object_pks(model, objs)

    # Upsert models to get Django pks
    objs_w_pks = self.upsert(model, objs)

    # Convert API ids to Django pks for related objects
    self.set_related_object_pks(model, objs_w_pks, related_objs)

    # Reshape related_objs for efficiency
    rows, related_objs = related_objs, defaultdict(list)
    for row in rows:
      for related_model, related_obj_list in row.items():
        related_objs[related_model].extend(related_obj_list)

    # Upsert related_obj_list
    for related_model, related_obj_list in related_objs.items():
      self.upsert(related_model, related_obj_list)

  def import_campaign_activities(self) -> None:
    """CampaignActivities must be imported one at a time."""

    # First make sure CampaignActivity API id's are stored locally
    EmailCampaign.remote.connect()
    for campaign in EmailCampaign.objects.exclude(
      api_id__isnull=True,
      campaign_activities__role='primary_email',
      campaign_activities__api_id__isnull=False,
    ):
      # Fetch from API
      assert isinstance(campaign.api_id, UUID)
      try:
        _, related_objs = EmailCampaign.remote.get(campaign.api_id)
      except EmailCampaign.DoesNotExist:
        # Available in bulk endpoint but not detail endpoint
        continue
      else:
        # Set related object pk and store in db
        activity = next(filter(
          lambda ca: ca.role == 'primary_email',
          related_objs[CampaignActivity],
        ))
        activity.campaign_id = campaign.pk
        activity.save()

    # Now fetch CampaignActivity details
    CampaignActivity.remote.connect()

    objs_and_related_objs = []
    activities = CampaignActivity.objects.filter(
      role='primary_email',
      api_id__isnull=False,
    )

    for activity in tqdm(activities, disable=self.noinput):
      assert isinstance(activity.api_id, UUID)
      try:
        obj, related_objs = CampaignActivity.remote.get(activity.api_id)
      except CampaignActivity.DoesNotExist:
        # Came from EmailCampaign detail endpoint but doesn't exist elsewhere
        continue
      else:
        obj.pk = activity.pk
        obj.campaign_id = activity.campaign_id
        objs_and_related_objs.append((obj, related_objs))

    # Upsert objects to update fields
    self.upsert(
      model=CampaignActivity,
      objs=[o for (o, _) in objs_and_related_objs],
      unique_fields=['campaign_id', 'role'],
      update_fields=['role', 'subject', 'preheader', 'html_content']
    )

    # Convert API id to Django pk for related objects
    objs_w_pks, related_objs = zip(*objs_and_related_objs)
    self.set_related_object_pks(CampaignActivity, objs_w_pks, related_objs)

    # Reshape related_objs for efficiency
    rows, related_objs = related_objs, defaultdict(list)
    for row in rows:
      for related_model, related_obj_list in row.items():
        related_objs[related_model].extend(related_obj_list)

    # Upsert related_obj_list
    for related_model, related_obj_list in related_objs.items():
      self.upsert(related_model, related_obj_list)

  def add_arguments(self, parser: ArgumentParser) -> None:
    """Allow optional keyword arguments."""

    parser.add_argument(
      '--noinput',
      action='store_true',
      default=False,
      help='Automatic yes to prompts',
    )
    parser.add_argument(
      '--stats_only',
      action='store_true',
      default=False,
      help='Only fetch EmailCampaign statistics',
    )

  def handle(self, *args: Any, **kwargs: Any) -> None:
    """Primary access point for Django management command."""

    self.noinput = kwargs['noinput']
    self.stats_only = kwargs['stats_only']

    if self.stats_only:
      # TODO: Implement CampaignSummary
      # self.CTCT_MODELS = [CampaignSummary]
      raise NotImplementedError

    for model in self.CTCT_MODELS:
      if model is CampaignActivity:
        note = "Note: This will result in 1 API request per EmailCampaign! "
      else:
        note = ""
      question = _(f'Import {model.__name__}? {note}(y/n): ')

      if self.noinput or (input(question).lower()[0] == 'y'):
        self.import_model(model)
      else:
        message = _(f'Skipping {model.__name__}')
        self.stdout.write(self.style.NOTICE(message))
