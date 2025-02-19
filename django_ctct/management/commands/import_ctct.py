from argparse import ArgumentParser
from ratelimit import limits, sleep_and_retry
import requests
from requests.exceptions import HTTPError

from django.core.management.base import BaseCommand
from django.db.models import Q

from django_ctct.models import (
  Token, CTCTModel, ContactList, CustomField, Contact, EmailCampaign, CampaignActivity,
)


class Command(BaseCommand):
  """Imports CTCT objects from CTCT servers.

  Notes
  -----
  The `json` kwarg does not work for GET requests, and the `data`
  kwarg appends a slash, which CTCT refuses. Because of this, extra
  URL parameters are appened to the initial value of `endpoint`.

  """

  help = 'Imports data from ConstantContact'

  CTCT_MODELS = [
    ContactList,
    CustomField,
    Contact,
    EmailCampaign,
    CampaignActivity,
  ]

  def upsert(self, Model: CTCTModel, objs: list[CTCTModel]) -> list[CTCTModel]:

    if getattr(Model, 'is_through_model', False):
      # TODO: Should we delete all ThroughModel instances each time?
      objs = Model.objects.bulk_create(
        objs=objs,
        update_conflicts=False,
      )
    else:
      objs = Model.objects.bulk_create(
        objs=objs,
        update_conflicts=True,
        unique_fields=['api_id'],
        update_fields=[
          f.name
          for f in Model._meta.fields
          if not f.primary_key and (f.name != 'api_id')
        ],
      )

    message = self.style.SUCCESS(
      f'Imported {len(objs)} {Model.__name__} instances.'
    )
    self.stdout.write(message)

    return objs

  # TODO: This fails for CampaignActivity?
  def import_model(self, Model: CTCTModel):
    """Imports objects from CTCT into Django's database."""

    Model.remote.setup()
    objs, related_objs = Model.remote.all()
    objs = self.upsert(Model, objs)

    for RelatedModel, objs in related_objs.items():
      objs = self.upsert(RelatedModel, objs)

  # TODO:
  def import_campaign_stats(self):
    """"Imports EmailCampaign stats from CTCT into Django's database."""

    Model = EmailCampaign
    update_objs = []

    endpoint = '/reports/summary_reports/email_campaign_summaries'
    paginated = True
    while paginated:

      response = self.ctct_api_get(
        url=Model().get_api_url(method='GET', endpoint=endpoint)
      )
      for ctct_obj in response['bulk_email_campaign_summaries']:
        obj = Model.from_api(ctct_obj, save=False)
        if obj.pk:
          update_objs.append(obj)
        else:
          continue

      try:
        endpoint = response.get('_links').get('next').get('href')
      except AttributeError:
        paginated = False

    # Bulk update
    update_count = Model.objects.bulk_update(
      update_objs,
      fields=[f.name for f in Model._meta.fields if not f.primary_key],
      batch_size=500,
    )
    message = self.style.SUCCESS(
      f'Imported stats for {update_count} EmailCampaign instances.'
    )
    self.stdout.write(message)

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

    question = 'Import EmailCampaign statistics? (y/n): '
    if self.noinput or (input(question).lower()[0] == 'y'):
      self.import_campaign_stats()
