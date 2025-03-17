from argparse import ArgumentParser
from typing import Optional, Type

from tqdm import tqdm

import django
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
    silent: bool = False,
  ) -> list[CTCTModel]:

    verb = 'Imported' if (update_fields is None) else 'Updated'

    # Perform upsert using `bulk_create()`
    if model._meta.auto_created or (model is ContactCustomField):
      # TODO: Should we delete existing through model instances?
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
    """Sets pk values for OneToOne and ForeignKeys defined on `model`."""
    for field in model._meta.get_fields():
      if field.one_to_one or field.many_to_one:
        # Convert API id to Django pk (hits db)
        id_to_pk = self.get_id_to_pk(field.remote_field.model)
        converter = lambda o: id_to_pk[getattr(o, field.attname)]

        [setattr(o, field.attname, converter(o)) for o in instances]

  def set_related_object_pks(
    self,
    model: Type[CTCTModel],
    obj_w_pk: CTCTModel,
    related_model: Type[CTCTModel],
    instances: list[CTCTModel],
  ) -> None:
    """Sets pk values for various related object."""
    for field in related_model._meta.get_fields():
      if field.remote_field:
        if field.name == 'author':
          # CTCT doesn't store Author info
          continue
        if field.many_to_many and (instances[0].pk is None):
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
        [setattr(o, field.attname, converter(o)) for o in instances]

  def import_model(self, model: CTCTModel) -> None:
    """Imports objects from CTCT into Django's database."""

    if model is CampaignActivity:
      # CampaignActivities do not have a bulk API endpoint
      return self.import_campaign_activities()

    model.remote.connect()
    try:
      objs, related_objs = zip(*model.remote.all())
    except ValueError:
      # No values returned
      return

    if model is CampaignSummary:
      # Convert API id to pk for the OneToOneField with EmailCampaign
      self.set_direct_object_pks(model, objs)

    # Upsert models to get Django pks
    objs_w_pks = self.upsert(model, objs)

    for obj_w_pk, related_objs in zip(objs_w_pks, related_objs):
      for related_model, instances in related_objs.items():
        self.set_related_object_pks(model, obj_w_pk, related_model, instances)

        # Upsert now that pks have been set
        self.upsert(related_model, instances, silent=True)

  def import_campaign_activities(self) -> None:
    """CampaignActivities must be imported one at a time."""

    model = CampaignActivity

    objs_and_related_objs = []

    model.remote.connect()
    for activity in tqdm(model.objects.filter(role='primary_email')):
      obj, related_objs = model.remote.get(activity.api_id)
      obj.pk = activity.pk
      obj.campaign_id = activity.campaign_id

      objs_and_related_objs.append((obj, related_objs))

    # Upsert objects to update fields
    self.upsert(
      model=model,
      objs=[_[0] for _ in objs_and_related_objs],
      unique_fields=['campaign_id', 'role'],
      update_fields=['role', 'subject', 'preheader', 'html_content']
    )

    for obj_w_pk, related_objs in objs_and_related_objs:
      for related_model, instances in related_objs.items():
        self.set_related_object_pks(model, obj_w_pk, related_model, instances)

        # Upsert now that pks have been set
        self.upsert(related_model, instances, silent=True)

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
      question = f'Import {model.__name__}? (y/n): '
      if self.noinput or (input(question).lower()[0] == 'y'):
        self.import_model(model)
      else:
        message = f'Skipping {model.__name__}'
        self.stdout.write(self.style.NOTICE(message))
