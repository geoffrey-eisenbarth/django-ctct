from argparse import ArgumentParser
from collections import defaultdict
from typing import Optional, Type

from tqdm import tqdm

import django
from django.utils.translation import gettext_lazy as _
from django.core.management.base import BaseCommand

from django_ctct.models import (
  CTCTModel, ContactList, CustomField,
  Contact, ContactCustomField,
  EmailCampaign, CampaignActivity, CampaignSummary,
)


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

  CTCT_MODELS = [
    ContactList,
    CustomField,
    Contact,
    EmailCampaign,
    CampaignActivity,
    CampaignSummary,
  ]

  def get_id_to_pk(self, model: Type[CTCTModel]) -> dict:
    """Returns a dictionary to convert CTCT API ids to Django pks."""
    id_to_pk = {
      str(api_id): int(pk)
      for (api_id, pk) in model.objects.values_list('api_id', 'pk')
    }
    return id_to_pk

  def upsert(
    self,
    model: CTCTModel,
    objs: list[CTCTModel],
    update_conflicts: bool = True,
    unique_fields: list[str] = ['api_id'],
    update_fields: Optional[list[str]] = None,
    silent: Optional[bool] = None,
  ) -> list[CTCTModel]:

    verb = 'Imported' if (update_fields is None) else 'Updated'
    if silent is None:
      silent = self.noinput

    # Perform upsert using `bulk_create()`
    if model._meta.auto_created or (model is ContactCustomField):
      if not issubclass(model, CTCTModel):
        # Delete ManyToMany objects
        model.objects.all().delete()
      update_conflicts = False
      unique_fields = update_fields = None
    elif model is CampaignSummary:
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
    id_field = None
    if issubclass(model, CTCTModel):
      id_field = 'api_id'
    elif model is CampaignSummary:
      id_field = 'campaign_id'

    if id_field is not None:
      seen = set()
      objs = [
        seen.add(getattr(o, id_field)) or o
        for o in objs
        if getattr(o, id_field) not in seen
      ]

    # Perform the upsert
    objs_w_pks = model.objects.bulk_create(
      objs=objs,
      update_conflicts=update_conflicts,
      unique_fields=unique_fields,
      update_fields=update_fields,
    )
    if update_conflicts and (django.get_version() < '5.0'):
      # In older versions, enabling the update_conflicts parameter prevented
      # setting the primary key on each model instance.
      if model is not CampaignSummary:
        # CampaignSummary doesn't have `api_id` field (or related_objs)
        # so it's okay to skip this part
        id_to_pk = self.get_id_to_pk(model)
        [setattr(o, 'pk', id_to_pk[o.api_id]) for o in objs_w_pks]

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
    for field in model._meta.get_fields():
      if field.one_to_one or field.many_to_one:
        # Convert API id to Django pk (hits db)
        id_to_pk = self.get_id_to_pk(field.remote_field.model)
        converter = lambda o: id_to_pk[getattr(o, field.attname)]

        [setattr(o, field.attname, converter(o)) for o in instances]

  def set_related_object_pks(
    self,
    model: CTCTModel,
    objs_w_pks: list[CTCTModel],
    related_fields: list[dict],
  ) -> None:
    """Sets Django pk values for ManyToMany and ReverseForeignKey objects."""
    for obj_w_pk, related_fields in zip(objs_w_pks, related_fields):
      for related_model, related_objs in related_fields.items():
        for field in related_model._meta.get_fields():
          if field.remote_field:
            if field.name == 'author':
              # CTCT doesn't store Author info
              continue
            if field.many_to_many and (related_objs[0].pk is None):
              # Can't save ManyToMany until parent object has pk
              continue
            elif field.remote_field.model is model:
              # No need to hit the db, we know the pk is obj_w_pk.pk
              converter = lambda _: obj_w_pk.pk
            else:
              # Convert API id to Django pk (hits db)
              id_to_pk = self.get_id_to_pk(field.remote_field.model)
              converter = lambda o: id_to_pk[getattr(o, field.attname)]

            # Set pks on related objects
            [setattr(o, field.attname, converter(o)) for o in related_objs]

  def import_model(self, model: CTCTModel) -> None:
    """Imports objects from CTCT into Django's database."""

    if model is CampaignActivity:
      # CampaignActivities do not have a bulk API endpoint
      return self.import_campaign_activities()

    model.remote.connect()
    try:
      objs, related_fields = zip(*model.remote.all())
    except ValueError:
      # No values returned
      return

    if model is CampaignSummary:
      # Convert API id to Django pk for the OneToOneField with EmailCampaign
      self.set_direct_object_pks(model, objs)

    # Upsert models to get Django pks
    objs_w_pks = self.upsert(model, objs)

    # Convert API id to Django pk for related objects
    self.set_related_object_pks(model, objs_w_pks, related_fields)

    # Reshape related_fields for efficiency
    rows, related_fields = related_fields, defaultdict(list)
    for row in rows:
      for related_model, related_objs in row.items():
        related_fields[related_model].extend(related_objs)

    # Upsert related_objs
    for related_model, related_objs in related_fields.items():
      self.upsert(related_model, related_objs)

  def import_campaign_activities(self) -> None:
    """CampaignActivities must be imported one at a time."""

    # First make sure CampaignActivity api id's are stored locally
    EmailCampaign.remote.connect()
    for campaign in EmailCampaign.objects.prefetch_related('campaign_activities'):  # noqa: E501
      try:
        activity = campaign.campaign_activities.get(role='primary_email')
      except CampaignActivity.DoesNotExist:
        # Must fetch from API
        try:
          _, related_fields = EmailCampaign.remote.get(campaign.api_id)
        except EmailCampaign.DoesNotExist:
          # Available in bulk endpoint but not detail endpoint
          continue
        else:
          # Set related object pk and store in db
          activity = next(filter(
            lambda ca: ca.role == 'primary_email',
            related_fields[CampaignActivity],
          ))
          activity.campaign_id = campaign.pk
          activity.save()

    # Now fetch CampaignActivity details
    CampaignActivity.remote.connect()
    objs_and_related_fields = []
    activities = CampaignActivity.objects.filter(role='primary_email')
    for activity in tqdm(activities, disable=self.noinput):
      try:
        obj, related_fields = CampaignActivity.remote.get(activity.api_id)
      except CampaignActivity.DoesNotExist:
        # Came from EmailCampaign detail endpoint but doesn't exist elsewhere
        continue
      else:
        obj.pk = activity.pk
        obj.campaign_id = activity.campaign_id
        objs_and_related_fields.append((obj, related_fields))

    # Upsert objects to update fields
    self.upsert(
      model=CampaignActivity,
      objs=[o for (o, _) in objs_and_related_fields],
      unique_fields=['campaign_id', 'role'],
      update_fields=['role', 'subject', 'preheader', 'html_content']
    )

    # Convert API id to Django pk for related objects
    objs_w_pks, related_fields = zip(*objs_and_related_fields)
    self.set_related_object_pks(CampaignActivity, objs_w_pks, related_fields)

    # Reshape related_fields for efficiency
    rows, related_fields = related_fields, defaultdict(list)
    for row in rows:
      for related_model, related_objs in row.items():
        related_fields[related_model].extend(related_objs)

    # Upsert related_objs
    for related_model, related_objs in related_fields.items():
      self.upsert(related_model, related_objs)

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

  def handle(self, *args, **kwargs):
    """Primary access point for Django management command."""

    self.noinput = kwargs['noinput']
    self.stats_only = kwargs['stats_only']

    if self.stats_only:
      self.CTCT_MODELS = [CampaignSummary]

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
