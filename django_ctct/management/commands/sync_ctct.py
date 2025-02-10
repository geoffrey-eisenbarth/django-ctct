from argparse import ArgumentParser
from datetime import date, timedelta
import dateutil.parser as dparser
from dateutil.parser import ParserError

from ratelimit import limits, sleep_and_retry
import requests
from requests.exceptions import HTTPError

from django.core.management.base import BaseCommand
from django.db.models import Q

from django_ctct.models import (
  Token, CTCTModel, Contact, ContactList, EmailCampaign, CampaignActivity,
)

from www.posts.models import Post  # TODO


# TODO: Some of this is TPG specific
class Command(BaseCommand):
  """Imports CTCT objects from CTCT servers.

  Notes
  -----
  The `json` kwarg does not work for GET requests, and the `data`
  kwarg appends a slash, which CTCT refuses. Because of this, extra
  URL parameters are appened to the initial value of `endpoint`.

  """

  help = 'Syncs database CTCT objects with CTCT servers.'

  CTCT_MODELS = [
    ContactList,
    Contact,
    EmailCampaign,
    CampaignActivity,
  ]
  URL_CHECKS = {
    ContactList: '?include_membership_count=active',
    Contact: '?include=custom_fields,list_memberships',
  }
  LIST_KEYS = {
    ContactList: 'lists',
    Contact: 'contacts',
    EmailCampaign: 'campaigns',
    CampaignActivity: 'campaign_activities',
  }

  # TODO: TPG
  DUPLICATED_EMAILS = [
    # For some reason, these email addresses are duplicated in CTCT
    'rudnickbk@aol.com',
    'rreesed@msn.com',
  ]

  @sleep_and_retry
  @limits(calls=4, period=1)
  def ctct_api_get(self, url) -> dict:
    """Extends `response.raise_for_status` to provide a better error report.

    Notes
    -----
    We also utilize the `ratelimit` library to prevent more than four calls
    per second, the limit imposed by CTCT API.

    """

    response = requests.get(
      url=url,
      headers={'Authorization': f"{self.token.type} {self.token.access_code}"},
    )

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

  def _get_post_from_name(self, name: str) -> Post:
    """Fetches database Post based on EmailCampaign name."""

    # Fetch known Post from "trouble-maker" EmailCampaigns
    KNOWN_ISSUES = {
      'COLUMN 2016-10-19': Post.objects.get(
        title="Going once...",
        publish_date=date(2020, 10, 19),
      ),
      'COLUMN 2019-01-02': None,
      'COLUMN 2020-10-19': None,
      'COLUMN 2016-05-04': None,
    }
    if name in KNOWN_ISSUES:
      return KNOWN_ISSUES[name]

    # Otherwise, we built a query based on the passed EmailCampaign name
    query = Q()

    category = name.split(' ')[0]
    if category in dict(Post.CATEGORIES):
      query &= Q(category=category)
    else:
      return None

    publish_date = name.split(' ')[1]
    try:
      publish_date = dparser.parse(publish_date)
    except ParserError:
      return None
    else:
      # Take into account old Campaign names might have wrong dates
      dt = timedelta(days=3)
      query &= Q(publish_date__range=(publish_date - dt, publish_date + dt))

    title = ' '.join(name.split(' ')[2:])
    if 'COMP' in title:
      # Newsletter COMP stats are not saved
      return None
    elif 'PAID' in title:
      # Category and date are enough to find the Post
      pass
    elif title:
      # Take into account Campaign title truncation
      query &= Q(title__startswith=title[:20], title__endswith=title[-20:])

    try:
      post = Post.objects.get(query)
    except Post.DoesNotExist:
      # Post doesn't exist in database, return None
      post = None
    except Post.MultipleObjectsReturned:
      # Possible duplicate Post or filtering issue
      message = self.style.ERROR(
        f'Multiple Post candidates for Campaign: {query}'
      )
      self.stdout.write(message)
      post = None

    return post

  def _sync_model(self, Model: CTCTModel):
    """Fetches and syncs Contacts from CTCT with database."""

    if Model is Contact:
      contact_lists = {}
    elif Model is CampaignActivity:
      return self._sync_campaign_activities()

    create_objs, update_objs = [], []

    endpoint = Model.API_ENDPOINT
    if check := self.URL_CHECKS.get(Model):
      if check not in endpoint:
        endpoint += check

    paginated = True
    while paginated:

      response = self.ctct_api_get(f'{Model.BASE_URL}{endpoint}')
      list_key = self.LIST_KEYS[Model]
      for ctct_obj in response[list_key]:

        if Model is EmailCampaign:
          if ctct_obj['current_status'] == 'Removed':
            continue
          if post := self._get_post_from_name(ctct_obj['name']):
            ctct_obj['post_id'] = post.id
          else:
            message = self.style.ERROR(
              f"Could not find Post for {ctct_obj['name']} EmailCampaign."
            )
            self.stdout.write(message)
            continue

        obj = Model.from_ctct(ctct_obj, save=False)
        if (Model is Contact) and (obj.email in self.DUPLICATED_EMAILS):
          pass
        elif obj._id:
          update_objs.append(obj)
        else:
          create_objs.append(obj)

        if Model is Contact:
          # Grab ContactList information for later
          contact_lists[obj.id] = ctct_obj['list_memberships']

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
      fields=[f.name for f in Model._meta.fields if f.name != '_id'],
      batch_size=500,
    )
    if update_count:
      message = self.style.SUCCESS(
        f'Synced {update_count} {Model.__name__} instances.'
      )
      self.stdout.write(message)

    if Model is Contact:
      # Set ContactLists via ThroughModel
      ThroughModel = Contact.lists.through
      for contact in create_objs + update_objs:
        contactlists = ContactList.objects.filter(
          id__in=contact_lists[contact.id],
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
      url = f'{Model.BASE_URL}{EmailCampaign.API_ENDPOINT}/{campaign.id}'
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
            obj = Model.from_ctct(ctct_obj, save=False)
            obj.campaign = campaign
            if obj._id:
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
      fields=[f.name for f in Model._meta.fields if f.name != '_id'],
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

    endpoint = '/v3/reports/summary_reports/email_campaign_summaries'
    paginated = True
    while paginated:

      response = self.ctct_api_get(f'{Model.BASE_URL}{endpoint}')
      for ctct_obj in response['bulk_email_campaign_summaries']:
        obj = Model.from_ctct(ctct_obj, save=False)
        if obj._id:
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
      fields=[f.name for f in Model._meta.fields if f.name != '_id'],
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

    endpoint = '/v3/contacts?status=unsubscribed'
    paginated = True
    while paginated:

      response = self.ctct_api_get(f'{Model.BASE_URL}{endpoint}')
      for ctct_obj in response['contacts']:
        obj = Model.from_ctct(ctct_obj, save=False)
        if obj.email in self.DUPLICATED_EMAILS:
          pass
        elif obj._id:
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
