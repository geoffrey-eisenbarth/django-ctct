from unittest import SkipTest
from unittest.mock import patch, MagicMock
from uuid import uuid4

from factory.django import mute_signals
from parameterized import parameterized_class
import requests_mock

from django.db import models
from django.db.models import Model
from django.test import TestCase
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from django_ctct.models import (
  CTCTModel, CustomField,
  ContactList, Contact, ContactCustomField, ContactNote,
  EmailCampaign, CampaignActivity,
)
from django_ctct.signals import remote_save, remote_delete, remote_update_m2m

from tests.factories import get_factory, TokenFactory


class RequestsMockMixin:

  model: CTCTModel

  def setUp(self):
    # Set up mock API
    self.mock_api = requests_mock.Mocker()
    self.mock_api.start()

    # Set up mock Token
    TokenFactory.create()

    # Create an existing object
    include_related = (self.model in [Contact, ContactNote, EmailCampaign])
    self.factory = get_factory(self.model, include_related=include_related)

    # Handle ManyToMany instances
    if self.model is Contact:
      self.custom_fields = get_factory(CustomField).create_batch(2)
    if self.model in [Contact, EmailCampaign, CampaignActivity]:
      self.existing_lists = get_factory(ContactList).create_batch(2)
      self.contact_lists = get_factory(ContactList).create_batch(2)

    with mute_signals(
      models.signals.post_save,
      models.signals.m2m_changed,  # TODO: Do we need to mute this?
    ):
      self.existing_obj = self.factory.create()

      if self.model is Contact:
        self.existing_obj.list_memberships.set(self.existing_lists)

        for custom_field in self.custom_fields:
          get_factory(ContactCustomField).create(
            contact=self.existing_obj,
            custom_field=custom_field,
          )

      if self.model is EmailCampaign:
        primary_email = self.existing_obj.campaign_activities.get()
        primary_email.contact_lists.set(self.existing_lists)
      elif self.model is CampaignActivity:
        self.existing_obj.contact_lists.set(self.existing_lists)

  def tearDown(self):
    self.mock_api.stop()

  def get_api_response(self, obj: CTCTModel) -> dict:
    """Mock the API response dict."""

    # Serialize factory obj
    data = self.model.remote.serialize(obj, field_types='all')

    # Set timestamps
    ts_now = timezone.now().strftime(self.model.remote.TS_FORMAT)
    for field in ['created_at', 'updated_at']:
      if data.get(field, False) is None:
        data[field] = ts_now

    # Set API ID
    data[self.model.remote.API_ID_LABEL] = str(obj.api_id or uuid4())

    # Mock CTCT's creation of the primary email CampaignActivity
    if isinstance(obj, EmailCampaign) and obj.pk is None:
      campaign_activity = {
        'campaign_activity_id': str(uuid4()),
        'role': 'primary_email',
      }
      data['campaign_activities'] = [campaign_activity]

    return data


class TestCRUDMixin:

  @patch('django_ctct.models.Token.decode')
  def test_create(self, token_decode: MagicMock):
    """Test object creation in Django."""

    token_decode.return_value = True

    # Build the object using factory-boy
    obj = self.factory.build(api_id=None)

    # Set up API mocker
    api_response = self.get_api_response(obj)
    self.mock_api.post(
      url=self.model.remote.get_url(),
      status_code=201,
      json=api_response,
    )
    if isinstance(obj, EmailCampaign):
      # Set up the mock request for updating the CampaignActivity
      # (may or may not be used depending on the situation)
      # TODO: GH #13
      api_response = api_response['campaign_activities'][0]
      self.mock_api.put(
        url=CampaignActivity.remote.get_url(
          api_id=api_response['campaign_activity_id'],
        ),
        status_code=200,
        json=api_response,
      )

    obj = self.create_obj(obj)

    # Verify API fields were added
    self.assertIsNotNone(obj.api_id)

    # Verify the number of requests that were made
    if (self.model is EmailCampaign) and obj.campaign_activities.filter(
      role='primary_email',
      contact_lists__isnull=False,
    ).exists():
      # CampaignActivity had to be saved remotely to set contact_lists
      num_requests = 2
    else:
      num_requests = 1

    self.assertEqual(self.mock_api.call_count, num_requests)

  @patch('django_ctct.models.Token.decode')
  def test_update(self, token_decode: MagicMock):
    """Test object update in Django."""

    token_decode.return_value = True

    # Update some values (must be done before getting api_response)
    update_related = False  # TODO: GH #13
    other_obj = self.factory.build()
    other_obj_data = self.model.remote.serialize(
      obj=other_obj,
      field_types='editable',
    )
    for field_name, value in other_obj_data.copy().items():
      if value and hasattr(self.existing_obj, field_name):
        setattr(self.existing_obj, field_name, value)
      else:
        other_obj_data.pop(field_name)

    # Set up API mocker
    api_response = self.get_api_response(self.existing_obj)
    self.mock_api.register_uri(
      'PATCH' if (self.model is EmailCampaign) else 'PUT',
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=200,
      json=api_response,
    )
    if isinstance(self.existing_obj, EmailCampaign) and update_related:
      # Set up the mock request for updating the CampaignActivity
      # TODO: GH #13
      campaign_activity = self.existing_obj.campaign_activities.get(role='primary_email')  # noqa: E501
      self.mock_api.put(
        url=CampaignActivity.remote.get_url(api_id=campaign_activity.api_id),
        status_code=200,
        json={},
      )
      raise NotImplementedError
      num_requests = 2
    else:
      num_requests = 1

    # Save updated existing_obj
    obj = self.update_obj(self.existing_obj)

    # Verify object was updated
    for field, value in other_obj_data.items():
      self.assertEqual(getattr(obj, field), value)

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, num_requests)

  @patch('django_ctct.models.Token.decode')
  def test_delete(self, token_decode: MagicMock):
    """Test object deletion in Django."""

    token_decode.return_value = True

    # Set up API mocker
    self.mock_api.delete(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=204,
      text='',
    )

    self.delete_obj(self.existing_obj)

    # Verify object was deleted
    with self.assertRaises(self.model.DoesNotExist):
      self.existing_obj.refresh_from_db()

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, 1)


# TODO: Finish this (without signals) and close #10
# TODO: test_models, test_admin, utils are for GH #4 #10
# TODO: managers.py and signals.py seem to be GH #5?
@parameterized_class(
  ('model', ),
  [(ContactList, ), (CustomField, ), (Contact, ), (EmailCampaign, )],
)
class ModelTest(RequestsMockMixin, TestCRUDMixin, TestCase):

  model: CTCTModel

  @classmethod
  def setUpClass(cls) -> None:
    super().setUpClass()
    if cls is ModelTest:
      message = _("This is the unparameterized base class.")
      raise SkipTest(message)

  def create_obj(self, obj: Model) -> Model:
    """Create the object locally and remotely."""
    obj.save()
    remote_save(self.model, obj)
    obj.refresh_from_db()
    return obj

  def update_obj(self, obj: Model) -> Model:
    """Update the object locally and remotely."""
    obj.save()
    remote_save(self.model, obj)
    obj.refresh_from_db()
    return obj

  def delete_obj(self, obj: Model) -> None:
    """Delete the object locally and remotely."""
    obj.delete()
    remote_delete(self.model, obj)


@parameterized_class(
  ('model', ),
  [(Contact, ), (CampaignActivity, )],
)
class ManyToManyContactListTest(RequestsMockMixin, TestCase):

  model: CTCTModel

  def setUp(self):
    super().setUp()

    self.m2m_field_name = {
      Contact: 'list_memberships',
      CampaignActivity: 'contact_lists',
    }[self.model]

    # Connect signals
    # models.signals.post_save.connect(remote_save)     # TODO: GH #11?
    # models.signals.pre_delete.connect(remote_delete)  # TODO: GH #11?
    models.signals.m2m_changed.connect(remote_update_m2m)

  @patch('django_ctct.models.Token.decode')
  def test_add_manytomany(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Set up API mocker
    api_response = self.get_api_response(self.existing_obj)
    self.mock_api.put(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=200,
      json=api_response,
    )

    # Add a related object, triggering m2m_changed signal
    getattr(self.existing_obj, self.m2m_field_name).add(*self.contact_lists)

    # Verify only request was made
    self.assertEqual(self.mock_api.call_count, 1)

  @patch('django_ctct.models.Token.decode')
  def test_remove_manytomany(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Set up API mocker
    api_response = self.get_api_response(self.existing_obj)
    self.mock_api.put(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=200,
      json=api_response,
    )

    # Remove one of two related objects, triggering m2m_changed signal
    m2m_obj = getattr(self.existing_obj, self.m2m_field_name).first()
    getattr(self.existing_obj, self.m2m_field_name).remove(m2m_obj)

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, 1)

  @patch('django_ctct.models.Token.decode')
  def test_clear_manytomany(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Set up API mocker
    api_response = self.get_api_response(self.existing_obj)
    self.mock_api.put(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=200,
      json=api_response,
    )

    # Clear all related objects, triggering m2m_changed signal
    getattr(self.existing_obj, self.m2m_field_name).clear()

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, 1)
