from argparse import ArgumentParser
from collections import defaultdict
from operator import attrgetter
from typing import Optional

from tqdm import tqdm

from django.core.management.base import BaseCommand

from django_ctct.models import (
  CTCTModel, CustomField,
  ContactList, Contact,
  EmailCampaign, CampaignActivity,
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
  ]

  def upsert(
    self,
    Model: CTCTModel,
    objs: list[CTCTModel],
    update_fields: Optional[list[str]] = None,
  ) -> list[CTCTModel]:

    verb = 'Imported' if (update_fields is None) else 'Updated'

    if getattr(Model, 'is_through_model', False):
      breakpoint()
      Model.objects.all().delete()
      objs = Model.objects.bulk_create(objs)
    else:
      # Perform upsert using `bulk_create()`
      if update_fields is None:
        update_fields = [
          f.name
          for f in Model._meta.fields
          if not f.primary_key and (f.name != 'api_id')
        ]

      objs = Model.objects.bulk_create(
        objs=objs,
        update_conflicts=True,
        unique_fields=['api_id'],
        update_fields=update_fields,
      )

    # Inform the user
    message = self.style.SUCCESS(
      f'{verb} {len(objs)} {Model.__name__} instances.'
    )
    self.stdout.write(message)

    return objs

  def import_model(self, Model: CTCTModel) -> None:
    """Imports objects from CTCT into Django's database."""

    if Model is CampaignActivity:
      return self.import_campaign_activities()

    Model.remote.connect()
    objs, related_objs = Model.remote.all()
    objs = self.upsert(Model, objs)

    for RelatedModel, objs in related_objs.items():
      objs = self.upsert(RelatedModel, objs)

  def import_campaign_activities(self) -> None:
    """Imports CampaignActivities from CTCT into Django's database."""

    objs, related_objs = [], defaultdict(list)

    EmailCampaign.remote.connect()
    CampaignActivity.remote.connect()

    # Use the EmailCampaign detail endpoint to get CampaignActivity api_ids
    for api_id in tqdm(EmailCampaign.objects.values_list('api_id', flat=True)):
      _, _related_objs = EmailCampaign.remote.get(api_id)

      # Now we use the CampaignActivity detail endpoint to get remaining fields
      primary_emails = filter(
        lambda obj: obj.role == 'primary_email',
        _related_objs.get(CampaignActivity, [])
      )
      for api_id in map(attrgetter('api_id'), primary_emails):
        obj, _related_objs = CampaignActivity.remote.get(api_id)

        # Store objects for later
        # NOTE We updated `related_obj` with the values from `_related_objs`
        for RelatedModel, instances in _related_objs.items():
          related_objs[RelatedModel].extend(instances)
        if obj is not None:
          objs.append(obj)

    # Upsert objects and related objects
    update_fields = ['role', 'subject', 'preheader', 'html_content']
    objs = self.upsert(CampaignActivity, objs, update_fields=update_fields)

    for RelatedModel, objs in related_objs.items():
      objs = self.upsert(RelatedModel, objs)

  def import_campaign_stats(self):
    """"Imports EmailCampaign stats from CTCT into Django's database."""

    endpoint = '/reports/summary_reports/email_campaign_summaries'
    update_fields = [
      'current_status', 'created_at', 'updated_at', 'scheduled_datetime',
      'sends', 'opens', 'clicks', 'forwards',
      'optouts', 'abuse', 'bounces', 'not_opened',
    ]

    EmailCampaign.remote.connect()
    objs, _ = EmailCampaign.remote.all(endpoint=endpoint)
    objs = self.upsert(EmailCampaign, objs, update_fields=update_fields)

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
      self.CTCT_MODELS = []

    for Model in self.CTCT_MODELS:
      question = f'Import {Model.__name__}? (y/n): '
      if self.noinput or (input(question).lower()[0] == 'y'):
        self.import_model(Model)
      else:
        message = f'Skipping {Model.__name__}'
        self.stdout.write(self.style.NOTICE(message))

    question = 'Update EmailCampaign statistics? (y/n): '
    if self.noinput or (input(question).lower()[0] == 'y'):
      self.import_campaign_stats()
