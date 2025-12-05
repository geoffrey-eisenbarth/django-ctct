from typing import Type, TypeVar, Generic
import unittest
from unittest.mock import patch, MagicMock
from uuid import uuid4

from parameterized import parameterized_class
import requests_mock

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase
from django.utils import timezone
from django.utils.translation import gettext as _

from django_ctct.models import (
  JsonDict,
  CTCTEndpointModel, CustomField, ContactList,
  Contact, ContactCustomField,
  EmailCampaign, CampaignActivity,
)
from django_ctct.signals import remote_save, remote_delete

from tests.factories import get_factory, TokenFactory


E = TypeVar('E', bound=CTCTEndpointModel)


class RequestsMockMixin(Generic[E]):

  model: Type[E]

  def setUp(self) -> None:
    # Set up mock API
    self.mock_api = requests_mock.Mocker()
    self.mock_api.start()

    # Set up mock Token
    TokenFactory.create()

    # Create an existing object
    self.factory = get_factory(self.model)

    # Handle ManyToMany instances
    if self.model is Contact:
      self.custom_fields = get_factory(CustomField).create_batch(2)
    if self.model in (Contact, EmailCampaign, CampaignActivity):
      self.existing_lists = get_factory(ContactList).create_batch(2)
      self.contact_lists = get_factory(ContactList).create_batch(2)

    self.existing_obj = self.factory.create()

    if isinstance(self.existing_obj, Contact):
      self.existing_obj.list_memberships.set(self.existing_lists)

      for custom_field in self.custom_fields:
        get_factory(ContactCustomField).create(
          contact=self.existing_obj,
          custom_field=custom_field,
        )

    elif isinstance(self.existing_obj, EmailCampaign):
      primary_email = self.existing_obj.campaign_activities.get()
      primary_email.contact_lists.set(self.existing_lists)
    elif isinstance(self.existing_obj, CampaignActivity):
      self.existing_obj.contact_lists.set(self.existing_lists)

  def tearDown(self) -> None:
    self.mock_api.stop()

  def get_api_response(self, obj: E) -> JsonDict:
    """Mock the API response dict."""

    # Serialize factory obj
    data = self.model.serializer.serialize(obj, field_types='all')

    # Set timestamps
    ts_now = timezone.now().strftime(self.model.serializer.TS_FORMAT)
    for field in ['created_at', 'updated_at']:
      if data.get(field, False) is None:
        data[field] = ts_now

    # Set API ID
    data[self.model.API_ID_LABEL] = str(obj.api_id or uuid4())

    # Mock CTCT's creation of the primary email CampaignActivity
    if isinstance(obj, EmailCampaign) and obj.pk is None:
      campaign_activity = {
        'campaign_activity_id': str(uuid4()),
        'role': 'primary_email',
      }
      data['campaign_activities'] = [campaign_activity]

    return data


class TestCRUD(RequestsMockMixin[E]):

  __test__ = False

  model: Type[E]

  def create_obj(self, obj: E) -> E:
    """Create the object locally and remotely."""
    message = _("Must define `create_obj` on the inheriting class.")
    raise ImproperlyConfigured(message)

  def update_obj(self, obj: E) -> E:
    """Create the object locally and remotely."""
    message = _("Must define `update_obj` on the inheriting class.")
    raise ImproperlyConfigured(message)

  def delete_obj(self, obj: E) -> None:
    """Create the object locally and remotely."""
    message = _("Must define `delete_obj` on the inheriting class.")
    raise ImproperlyConfigured(message)

  @patch('django_ctct.models.Token.decode')
  def test_create(self, token_decode: MagicMock) -> None:
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
    assert obj.api_id is not None

    # Verify the number of requests that were made
    if isinstance(obj, EmailCampaign) and obj.campaign_activities.filter(
      role='primary_email',
      contact_lists__isnull=False,
    ).exists():
      # CampaignActivity had to be saved remotely to set contact_lists
      num_requests = 2
    else:
      num_requests = 1

    assert self.mock_api.call_count == num_requests

  @patch('django_ctct.models.Token.decode')
  def test_update(self, token_decode: MagicMock) -> None:
    """Test object update in Django."""

    token_decode.return_value = True

    # Update some values (must be done before getting api_response)
    other_obj = self.factory.build()
    other_obj_data = self.model.serializer.serialize(
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

    update_related = False  # TODO: GH #13
    if isinstance(self.existing_obj, EmailCampaign) and update_related:
      # Set up the mock request for updating the CampaignActivity
      campaign_activity = self.existing_obj.campaign_activities.get(role='primary_email')  # noqa: E501
      self.mock_api.put(
        url=CampaignActivity.remote.get_url(api_id=campaign_activity.api_id),
        status_code=200,
        json={},
      )
      num_requests = 2
    else:
      num_requests = 1

    # Save updated existing_obj
    obj = self.update_obj(self.existing_obj)

    # Verify object was updated
    for field, value in other_obj_data.items():
      assert getattr(obj, field) == value

    # Verify the number of requests that were made
    assert self.mock_api.call_count == num_requests

  @patch('django_ctct.models.Token.decode')
  def test_delete(self, token_decode: MagicMock) -> None:
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
    try:
      self.existing_obj.refresh_from_db()
    except self.model.DoesNotExist:
      pass
    else:
      raise AssertionError

    # Verify the number of requests that were made
    assert self.mock_api.call_count == 1


@parameterized_class(
  ('model', ),
  [(ContactList, ), (CustomField, ), (Contact, ), (EmailCampaign, )],
)
class ModelTest(TestCRUD[E], TestCase):

  model: Type[E]

  @classmethod
  def setUpClass(cls) -> None:
    super().setUpClass()
    if cls is ModelTest:
      message = _("This is the unparameterized base class.")
      raise unittest.SkipTest(message)

  def create_obj(self, obj: E) -> E:
    """Create the object locally and remotely."""
    obj.save()
    remote_save(self.model, obj)
    obj.refresh_from_db()
    return obj

  def update_obj(self, obj: E) -> E:
    """Update the object locally and remotely."""
    obj.save()
    remote_save(self.model, obj)
    obj.refresh_from_db()
    return obj

  def delete_obj(self, obj: E) -> None:
    """Delete the object locally and remotely."""
    obj.delete()
    remote_delete(self.model, obj)


@parameterized_class(
  ('model', ),
  [(Contact, ), (CampaignActivity, )],
)
class ManyToManyContactListTest(RequestsMockMixin[E], TestCase):

  model: Type[E]

  def setUp(self) -> None:
    super().setUp()

    self.m2m_field_name = {
      Contact: 'list_memberships',
      CampaignActivity: 'contact_lists',
    }[self.model]

    # Connect signals
    # models.signals.post_save.connect(remote_save)        # TODO: GH #11?
    # models.signals.pre_delete.connect(remote_delete)     # TODO: GH #11?
    models.signals.m2m_changed.connect(remote_update_m2m)  # TODO: GH #15?

  @patch('django_ctct.models.Token.decode')
  def test_add_manytomany(self, token_decode: MagicMock) -> None:

    token_decode.return_value = True

    # Set up API mocker
    api_response = self.get_api_response(self.existing_obj)
    self.mock_api.put(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=200,
      json=api_response,
    )

    # TODO: GH #11
    # Add a related object, triggering m2m_changed signal
    getattr(self.existing_obj, self.m2m_field_name).add(*self.contact_lists)

    # Verify only request was made
    self.assertEqual(self.mock_api.call_count, 1)

  @patch('django_ctct.models.Token.decode')
  def test_remove_manytomany(self, token_decode: MagicMock) -> None:

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
  def test_clear_manytomany(self, token_decode: MagicMock) -> None:

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
