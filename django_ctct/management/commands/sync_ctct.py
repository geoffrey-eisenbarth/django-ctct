arom argparse import ArgumentParser
from ratelimit import limits, sleep_and_retry
import requests
from requests.exceptions import HTTPError

from django.core.management.base import BaseCommand
from django.db.models import Q

from django_ctct.models import (
  Token, CTCTModel, Contact, ContactList, EmailCampaign, CampaignActivity,
)


# TODO: would tqdm be helpful?
class Command(BaseCommand):
  """Imports CTCT objects from CTCT servers.

  Notes
  -----
  The `json` kwarg does not work for GET requests, and the `data`
  kwarg appends a slash, which CTCT refuses. Because of this, extra
  URL parameters are appened to the initial value of `endpoint`.

  """

  help = 'Syncs database CTCT objects with CTCT servers.'

  CTCT_API_LIMIT_CALLS = 4   # Four calls in
  CTCT_API_LIMIT_PERIOD = 1  # one second

  CTCT_MODELS = [
    ContactList,
    Contact,
    EmailCampaign,
    CampaignActivity,
  ]
  LIST_KEYS = {
    ContactList: 'lists',
    Contact: 'contacts',
    EmailCampaign: 'campaigns',
    CampaignActivity: 'campaign_activities',
  }

  @sleep_and_retry
  @limits(calls=CTCT_API_LIMIT_CALLS, period=CTCT_API_LIMIT_PERIOD)
  def ctct_api_get(self, url: str) -> dict:
    """Extends `response.raise_for_status` to provide a better error report."""

    response = self.session.get(url=url)

    try:
      response.raise_for_status()
    except HTTPError as e:
      if type(e.response.json()) == list:
        error = e.response.json()[0]
      elif type(e.response.json()) == dict:
        error = e.response.json()
      message = e.args[0]
      for key, value in error.items():
        message += f'\n{key}: {value}'
      raise HTTPError(message, response=response)

    try:
      response = response.json()
    except ValueError:
      # Response is not valid JSON
      pass

    return response

  def _sync_model(self, Model: CTCTModel):
    """Fetches and syncs Contacts from CTCT with Django's db."""

    if Model is Contact:
      contact_lists = {}
    elif Model is CampaignActivity:
      return self._sync_campaign_activities()

    create_objs, update_objs = [], []

    endpoint = Model.API_ENDPOINT

    paginated = True
    while paginated:
      response = self.ctct_api_get(
        url=Model().get_api_url(method='GET', endpoint=endpoint)
      )
      list_key = self.LIST_KEYS[Model]
      for ctct_obj in response[list_key]:

        if (Model is EmailCampaign) and (ctct_obj['current_status'] == 'Removed'):
          continue

        obj = Model.from_api(ctct_obj, save=False)
        if obj.pk:
          update_objs.append(obj)
        else:
          create_objs.append(obj)

        if Model is Contact:
          # Grab ContactList information for later
          contact_lists[obj.api_id] = ctct_obj['list_memberships']
          # TODO: Add phone_numbers etc here?

      try:
        endpoint = response.get('_links').get('next').get('href')
      except AttributeError:
        paginated = False

    # Bulk create and update
    create_objs = Model.objects.bulk_create(create_objs, batch_size=500)
    if create_objs:
      message = self.style.SUCCESS(
        f'Created {len(create_objs)} {Model.__name__} instances.'
      )
      self.stdout.write(message)

    update_count = Model.objects.bulk_update(
      update_objs,
      fields=[f.name for f in Model._meta.fields if not f.primary_key]
      batch_size=500,
    )
    if update_count:
      message = self.style.SUCCESS(
a       f'Synced {update_count} {Model.__name__} instances.'
      )
      self.stdout.write(message)

    if Model is Contact:
      # Set ContactLists via ThroughModel
      ThroughModel = Contact.list_memberships.through
      for contact in create_objs + update_objs:
        contactlists = ContactList.objects.filter(
          api_id__in=contact_lists[contact.api_id],
        )
        for contactlist in contactlists:
          ThroughModel.objects.get_or_create(
            contact=contact,
            contactlist=contactlist,
          )

  def _sync_campaign_activities(self):
    """Fetches and syncs CampaignActivities from CTCT with database."""

    Model = CampaignActivity
    create_objs, update_objs = [], []

    query = Q(activities__role='primary_email')
    for campaign in EmailCampaign.objects.exclude(query):
      url = campaign.get_api_url(method='GET')
      try:
        response = self.ctct_api_get(url)
      except Exception as e:
        message = self.style.ERROR(
          f"Unable to fetch details for {campaign}: {e}."
        )
        self.stdout.write(message)

      else:
        for ctct_obj in response['campaign_activities']:
          if ctct_obj['role'] == 'primary_email':
            obj = Model.from_api(ctct_obj, save=False)
            obj.campaign = campaign
            if obj.pk:
              update_objs.append(obj)
            else:
              create_objs.append(obj)

    # Bulk create and update
    create_objs = Model.objects.bulk_create(create_objs, batch_size=500)
    if create_objs:
      message = self.style.SUCCESS(
        f'Created {len(create_objs)} {Model.__name__} instances.'
      )
      self.stdout.write(message)

    update_count = Model.objects.bulk_update(
      update_objs,
      fields=[f.name for f in Model._meta.fields if not f.primary_key],
      batch_size=500,
    )
    if update_count:
      message = self.style.SUCCESS(
        f'Synced {update_count} {Model.__name__} instances.'
      )
      self.stdout.write(message)

  def _sync_campaign_stats(self):
    """"Fetches and syncs EmailCampaign stats from CTCT with database."""

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
      f'Synced stats for {update_count} EmailCampaign instances.'
    )
    self.stdout.write(message)

  def _sync_opt_outs(self):
    """Fetches and syncs Contact opt outs."""

    Model = Contact
    create_objs, update_objs = [], []

    endpoint = '/contacts?status=unsubscribed'
    paginated = True
    while paginated:

      response = self.ctct_api_get(
        url=Model().get_api_url(method='GET', endpoint=endpoint)
      )
      for ctct_obj in response['contacts']:
        obj = Model.from_api(ctct_obj, save=False)
        if obj.pk:
          update_objs.append(obj)
        else:
          create_objs.append(obj)

      try:
        endpoint = response.get('_links').get('next').get('href')
      except AttributeError:
        paginated = False

    # Bulk create or update
    create_objs = Model.objects.bulk_create(create_objs, batch_size=500)
    if create_objs:
      message = self.style.SUCCESS(
        f'Created {len(create_objs)} {Model.__name__} instances.'
      )
      self.stdout.write(message)

    update_count = Model.objects.bulk_update(
      update_objs,
      fields=['opt_out', 'opt_out_date'],
      batch_size=500,
    )
    message = self.style.SUCCESS(
      f'Synced opt outs for {update_count} Contact instances.'
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

    self.token = Token.get()
    self.session = requests.Session()
    self.session.headers.update({
      'Authorization': f"{self.token.type} {self.token.access_code}"
    })

    if self.stats_only:
      self.CTCT_MODELS = []

    for Model in self.CTCT_MODELS:
      question = f'Sync {Model.__name__}? (y/n): '
      if self.noinput or (input(question).lower()[0] == 'y'):
        self._sync_model(Model)
      else:
        message = f'Skipping {Model.__name__}'
        self.stdout.write(self.style.NOTICE(message))

    question = 'Sync EmailCampaign statistics? (y/n): '
    if self.noinput or (input(question).lower()[0] == 'y'):
      self._sync_campaign_stats()

    question = 'Sync Contact opt-outs? (y/n): '
    if self.noinput or (input(question).lower()[0] == 'y'):
      self._sync_opt_outs()
