from unittest.mock import patch, MagicMock
from uuid import uuid4

from factory.django import mute_signals
from parameterized import parameterized_class
import requests_mock

from django.db.models.signals import post_save, m2m_changed, pre_delete
from django.test import TestCase
from django.utils import timezone

from django_ctct.models import (
  CTCTModel, CustomField, ContactList, Contact,
  EmailCampaign,
)

from django_ctct.signals import (
  remote_save, remote_delete, remote_update_m2m
)
from tests.factories import get_factory, TokenFactory


@parameterized_class(
  ('model', ),
  [(ContactList, ), (CustomField, ), (Contact, )],
)
class ModelTest(TestCase):

  def setUp(self):
    # Set up mock API
    self.mock_api = requests_mock.Mocker()
    self.mock_api.start()

    # Set up mock Token
    TokenFactory.create()

    # Create an existing object
    include_related = (self.model in [Contact, EmailCampaign])
    self.factory = get_factory(self.model, include_related=include_related)
    with mute_signals(post_save, m2m_changed):
      kwargs = {}
      if self.model is Contact:
        m2m_factory = get_factory(ContactList)
        kwargs['list_memberships'] = [m2m_factory(), m2m_factory()]
      elif self.model is EmailCampaign:
        raise NotImplementedError('How to specify M2M on CampaignActivity?')
      self.existing_obj = self.factory.create(**kwargs)

    # Connect signals
    post_save.connect(remote_save)
    pre_delete.connect(remote_delete)
    m2m_changed.connect(remote_update_m2m)

  def tearDown(self):
    self.mock_api.stop()

  def get_api_response(self, obj: CTCTModel) -> dict:
    # Serialize factory obj
    data = self.model.remote.serialize(obj, field_types='all')

    # Set timestamps
    ts_now = timezone.now().strftime(self.model.remote.TS_FORMAT)
    for field in ['created_at', 'updated_at']:
      if data[field] is None:
        data[field] = ts_now

    # Set API ID
    data[self.model.remote.API_ID_LABEL] = str(obj.api_id or uuid4())

    return data

  @patch('django_ctct.models.Token.decode')
  def test_create(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Build the object using factory-boy
    obj = self.factory.build(api_id=None)

    # Set up API mocker
    self.mock_api.post(
      url=self.model.remote.get_url(),
      status_code=201,
      json=self.get_api_response(obj),
    )

    # Save the object, triggering post_save signal
    try:
      obj.save()
    except:
      breakpoint()

    # Verify API fields were added
    obj.refresh_from_db()
    self.assertIsNotNone(obj.api_id)

  @patch('django_ctct.models.Token.decode')
  def test_update(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Set up API mocker
    response = self.get_api_response(self.existing_obj)
    self.mock_api.put(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=200,
      json=response,
    )

    # Save the object, triggering post_save signal
    # TODO: Wait...is anything being updated?
    self.existing_obj.save()

    # Verify object was updated
    self.existing_obj.refresh_from_db()
    for field_name, response_value in response.items():
      if field_name.endswith('_at'):
        # Defer to CTCT timestamps
        continue
      elif field_name.endswith('_id'):
        # Verify API id was correctly stored
        self.assertEqual(str(self.existing_obj.api_id), response_value)
      elif isinstance(response_value, list):
        # TODO: Verify ManyToMany (update the code below)
        continue

        # Update variable name
        response_values = response_value.copy()
        del response_value

        # Verify extra related objects weren't created
        related_objs = list(getattr(self.existing_obj, field_name).all())
        self.assertEqual(len(related_objs), len(response_values))

        # Verify related objects were updated
        for related_obj in related_objs:
          api_id_label = type(related_objs[0]).remote.API_ID_LABEL
          response_value = next(filter(
            lambda x: x[api_id_label] == str(related_obj.api_id),
            response_values,
          ))
          for field_name in response_value.keys():
            if not (field_name.endswith('_id') or field_name.endswith('_at')):
              self.assertEqual(
                getattr(related_obj, field_name),
                response_value[field_name]
              )

      elif obj_value := getattr(self.existing_obj, field_name, None):
        self.assertEqual(obj_value, response_value)

  @patch('django_ctct.models.Token.decode')
  def test_delete(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Set up API mocker
    self.mock_api.delete(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=204,
      text='',
    )

    # Delete the object
    self.existing_obj.delete()

    # Verify object was deleted
    self.assertFalse(
      self.model.objects.filter(pk=self.existing_obj.pk).exists()
    )
