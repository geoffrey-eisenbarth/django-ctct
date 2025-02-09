from typing import Type, Literal

import requests_mock

from django.test import TestCase

from django_ctct.utils import to_dt
from django_ctct.models import (
  CTCTModel,
  Token,
  ContactList, CustomField,
  Contact, ContactNote, ContactPhoneNumber, ContactStreetAddress, ContactCustomField,
  EmailCampaign, CampaignActivity,
)
from tests.responses import MockConstantContactAPI


class ContactListTestCase(TestCase):
  def setUp(self):
    self.mock_api = MockConstantContactAPI()
    self.mock_api.start()

  def tearDown(self):
    self.mock_api.stop()

  def get_request_body(
    self,
    cls: Type[CTCTModel]
    endpoint: str,
    method: Literal['GET', 'POST', 'PUT', 'DELETE'],
  ) -> dict:
    response = MockConstantContactAPI.RESPONSES[endpoint][method][201]
    body = {
      k: v
      for k, v in response.items()
      if k in cls.API_BODY_FIELDS
    }
    return body


  def test_api_create_success(self, m):
    # Set up the mock response
    self.mock_api.mock(endpoint='/contact_lists', method='POST', status_code=201)

    # Call the code that creates the object
    body_fields = ['name', 'description', 'favorite']
    obj = ContactList.objects.create(**{
      k: v
      for k, v in api_response.items()
      if k in body_fields
    })

    # Assertations
    for field_name, value in api_response.items():
      if field_name.endswith('_id'):
        lhs = getattr(obj, 'id')
      else:
        lhs = getattr(obj, field_name)

      if field_name.endswith('_at'):
        rhs = to_dt(value)
      else:
        rhs = value

      self.assertEqual(lhs, rhs)


  @requests_mock.Mocker()
  def test_api_create_error(self, m):
    m.post(url, status_code=400, json={'error_message': 'Invalid name'})

    with self.assertRaises(MyExpectedException):
      obj = ContactList.objects.create(name='<Invalid name>')
