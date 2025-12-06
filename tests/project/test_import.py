from functools import partial
from math import ceil
import random
from typing import Type, TypeVar, Iterable, cast
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode

import requests_mock

from django.core.management import call_command
from django.db.models import Model
from django.test import TestCase

from django_ctct.models import (
  JsonDict,
  CTCTModel, CTCTEndpointModel, ContactList, CustomField, Contact,
  ContactNote, ContactPhoneNumber, ContactStreetAddress, ContactCustomField,
  EmailCampaign, CampaignActivity, CampaignSummary,
)
from tests.factories import get_factory, TokenFactory, NUM_RELATED_OBJS


E = TypeVar('E', bound=CTCTEndpointModel)


class TestImportCommand(TestCase):
  """Test the `import_ctct` management command.

  Notes
  -----
  Since the order of import is crucial (e.g., ContactLists have to exist
  before Contacts can be created), we must cover the entire command in a
  single test.

  """

  models: list[Type[CTCTEndpointModel]] = [
    ContactList, CustomField, Contact,
    EmailCampaign, CampaignActivity, CampaignSummary,
  ]
  num_objs: dict[Type[CTCTEndpointModel], int] = {
    ContactList: 50,
    CustomField: 2,
    Contact: 50,
    EmailCampaign: 50,
    CampaignActivity: 50,
    CampaignSummary: 50,
  }
  per_request: dict[Type[CTCTEndpointModel], int] = {
    ContactList: 50,         # limit in [1, 1000]
    CustomField: 50,         # limit in [1, 100]
    Contact: 50,             # limit in [1, 500]
    EmailCampaign: 50,       # limit in [1, 500]
    CampaignActivity: 1,     # No bulk get
    CampaignSummary: 50,     # Same as EmailCampaign
  }

  def setUp(self) -> None:
    # Set up mock API
    self.mock_api = requests_mock.Mocker()
    self.mock_api.start()

    self.create_responses()

    # Set up mock Token
    TokenFactory.create()

  def tearDown(self) -> None:
    self.mock_api.stop()

  def create_responses(self) -> None:
    self.data: dict[Type[CTCTEndpointModel], list[JsonDict]] = {}
    instances: dict[Type[CTCTEndpointModel], list[CTCTModel]] = {}
    objs: Iterable[Model]

    for model, num_objs in self.num_objs.items():
      if model is CampaignActivity:
        # These were already created with EmailCampaignFactory
        objs = CampaignActivity.objects.filter(role='primary_email')
      elif model is CampaignSummary:
        # These were already created with EmailCampaignFactory
        objs = CampaignSummary.objects.all()
      else:
        # Create new instances
        objs = get_factory(model).create_batch(num_objs)

      instances[model] = cast(list[CTCTModel], objs)

      if model is Contact:
        # Set ManyToMany relationships
        for contact in objs:
          memberships = cast(
            list[ContactList],
            random.sample(instances[ContactList], random.randint(1, 10))
          )
          cast(Contact, contact).list_memberships.set(memberships)

          for custom_field in instances[CustomField]:
            get_factory(ContactCustomField).create(
              contact=contact,
              custom_field=custom_field,
            )

    # Serialize instances
    for model, objs in instances.items():
      if model is CampaignActivity:
        # Since EmailCampaign has been stored, we can now set contact_lists
        for obj in objs:
          recipients = cast(
            list[ContactList],
            random.sample(instances[ContactList], random.randint(1, 10))
          )
          cast(CampaignActivity, obj).contact_lists.set(recipients)

      # Serialize and store
      serializer = partial(model.serializer.serialize, field_types='all')
      self.data[model] = list(map(serializer, objs))

    # Delete objects
    for model in self.models:
      model.objects.all().delete()

  def get_api_url(
    self,
    model: Type[E],
    api_id: str | None = None,
  ) -> str:
    url = model.remote.get_url(api_id=api_id)
    if params := model.API_GET_QUERIES:
      # Important: do not include a forward slash before the params
      url = f'{url}?{urlencode(params)}'
    return url

  def get_api_response(
    self,
    model: Type[E],
  ) -> dict[str, list[JsonDict]]:
    data = {
      # '_links': {},  # TODO: GH #3
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
        id_label = EmailCampaign.API_ID_LABEL
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
        id_label = CampaignActivity.API_ID_LABEL
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
        # Verify though model object creation
        self.assertTrue(
          Contact.list_memberships.through.objects.exists()
        )

        # Verify related object creation
        related_models: list[Type[CTCTModel]] = [
          ContactNote,
          ContactPhoneNumber,
          ContactStreetAddress,
        ]
        for related_model in related_models:
          self.assertEqual(
            related_model.objects.count(),
            self.num_objs[Contact] * NUM_RELATED_OBJS[Contact]
          )
