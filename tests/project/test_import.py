from functools import partial
from math import ceil
import random
from typing import Type, Optional
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode

from factory.django import mute_signals
import requests_mock

from django.core.management import call_command
from django.db.models.signals import post_save, pre_delete, m2m_changed
from django.test import TestCase

from django_ctct.models import (
  CTCTModel, ContactList, CustomField, Contact, ContactCustomField,
  EmailCampaign, CampaignActivity, CampaignSummary,
)
from tests.factories import get_factory, TokenFactory


class TestImportCommand(TestCase):
  """Test the `import_ctct` management command.

  Notes
  -----
  Since the order of import is crucial (e.g., ContactLists have to exist
  before Contacts can be created), we must cover the entire command in a
  single test.

  """

  models = [
    ContactList, CustomField, Contact,
    EmailCampaign, CampaignActivity, CampaignSummary,
  ]
  num_objs = {
    ContactList: 50,
    CustomField: 2,
    Contact: 50,
    EmailCampaign: 50,
    CampaignActivity: 50,
    CampaignSummary: 50,
  }
  per_request = {
    ContactList: 50,         # limit in [1, 1000]
    CustomField: 50,         # limit in [1, 100]
    Contact: 50,             # limit in [1, 500]
    EmailCampaign: 50,       # limit in [1, 500]
    CampaignActivity: 1,     # No bulk get
    CampaignSummary: 50,     # Same as EmailCampaign
  }

  def setUp(self):
    # Set up mock API
    self.mock_api = requests_mock.Mocker()
    self.mock_api.start()

    self.create_responses()

    # Set up mock Token
    TokenFactory.create()

  def tearDown(self):
    self.mock_api.stop()

  def create_responses(self) -> None:
    self.objects = {}
    self.data = {}

    with mute_signals(post_save, pre_delete, m2m_changed):

      # Create ContactLists and CustomFields first
      lists = get_factory(ContactList).create_batch(
        self.num_objs[ContactList]
      )
      custom_fields = get_factory(CustomField).create_batch(
        self.num_objs[CustomField]
      )
      contacts = get_factory(Contact, include_related=True).create_batch(
        self.num_objs[Contact]
      )

      # Set ManyToMany relationships
      for contact in contacts:
        count = random.randint(1, 10)
        contact.list_memberships.set(random.sample(lists, count))

        factory = get_factory(ContactCustomField)
        for custom_field in custom_fields:
          factory(contact=contact, custom_field=custom_field)

      # Create EmailCampaigns and CampaignActivities
      campaigns = get_factory(
        EmailCampaign,
        include_related=True
      ).create_batch(
        self.num_objs[EmailCampaign]
      )
      activities = CampaignActivity.objects.filter(role='primary_email')
      summaries = CampaignSummary.objects.all()

      # Serialize instances
      # Must do EmailCampaigns before CampaignActivity.contact_lists can be set
      self.objects = {
        ContactList: lists,
        CustomField: custom_fields,
        Contact: contacts,
        EmailCampaign: campaigns,
        CampaignActivity: activities,
        CampaignSummary: summaries,
      }
      for model, objects in self.objects.items():
        if model is CampaignActivity:
          # Since EmailCampaign has been stored, we can now set contact_lists
          for obj in objects:
            count = random.randint(1, 10)
            obj.contact_lists.set(random.sample(lists, count))

        # Serialize and store
        serializer = partial(model.remote.serialize, field_types='all')
        self.data[model] = list(map(serializer, objects))

      # Delete objects
      for model in self.models:
        model.objects.all().delete()

  def get_api_url(
    self,
    model: Type[CTCTModel],
    api_id: Optional[str] = None,
  ) -> str:
    url = model.remote.get_url(api_id=api_id)
    if params := model.remote.API_GET_QUERIES:
      # Important: do not include a forward slash before the params
      url = f'{url}?{urlencode(params)}'
    return url

  def get_api_response(
    self,
    model: Type[CTCTModel],
  ) -> dict:
    data = {
      '_links': {},
      # This key is e.g. 'lists' or 'contacts', but we don't call it directly
      'data': self.data[model],
    }
    # TODO: GH #3
    # NOTE: Infinite loop if we're calling get_api_url()
    # if 'cursor' not in (url := self.get_api_url()):
    #   next_endpoint = url.split('v3')[-1] + '&cursor=cursor'
    #   data['_links'] = {'next': {'href': next_endpoint}}
    return data

  @patch('django_ctct.models.Token.decode')
  def test_import(self, token_decode: MagicMock) -> None:

    # Set up MagicMocks
    token_decode.return_value = True

    # Set up API mocker
    for model in self.models:
      if model is EmailCampaign:
        # Set up single, detailed endpoint first
        id_label = EmailCampaign.remote.API_ID_LABEL
        for datum in self.data[EmailCampaign]:
          self.mock_api.get(
            url=self.get_api_url(EmailCampaign, api_id=datum[id_label]),
            status_code=200,
            json=datum.copy(),
          )

          # Bulk endpoint does not contain CampaignActivity info
          datum.pop('campaign_activities')

        # Now set up bulk endpoint (without CampaignActivity info)
        self.mock_api.get(
          url=self.get_api_url(EmailCampaign),
          status_code=200,
          json=self.get_api_response(model),
        )
      elif model is CampaignActivity:
        # No bulk GET, must request each CampaignActivity individually
        id_label = CampaignActivity.remote.API_ID_LABEL
        for datum in self.data[CampaignActivity]:
          self.mock_api.get(
            url=self.get_api_url(CampaignActivity, api_id=datum[id_label]),
            status_code=200,
            json=datum,
          )
      else:
        # Mock the bulk GET request
        self.mock_api.get(
          url=self.get_api_url(model),
          status_code=200,
          json=self.get_api_response(model),
        )

      # Verify factory objects have been deleted
      self.assertEqual(model.objects.count(), 0)

    call_command('import_ctct', '--noinput')

    # Verify the number of requests that were made
    num_requests = sum([
      ceil(self.num_objs[model] / self.per_request[model])
      for model in self.models
    ]) + self.num_objs[EmailCampaign]
    self.assertEqual(self.mock_api.call_count, num_requests)

    # Verify that objects have been created
    for model in self.models:
      self.assertEqual(model.objects.count(), self.num_objs[model])
      if model is Contact:
        # Verify related object creation
        RELATED_OBJ_COUNT = 2
        for field in Contact._meta.get_fields():
          if field.many_to_many:
            through_model = field.remote_field.through
            self.assertTrue(through_model.objects.exists())
          elif field.related_model is not None:
            self.assertEqual(
              field.related_model.objects.count(),
              self.num_objs[Contact] * RELATED_OBJ_COUNT
            )
