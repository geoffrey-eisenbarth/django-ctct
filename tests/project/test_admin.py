import random
from typing import Literal
from unittest.mock import patch, MagicMock
from uuid import uuid4

from factory.django import mute_signals
from parameterized import parameterized_class
import requests_mock

from django.db import models
from django.contrib import admin
from django.contrib.auth.models import User
from django.forms import model_to_dict
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from django_ctct.models import (
  CTCTModel, Token, CustomField,
  ContactList, Contact, ContactCustomField, ContactNote,
  EmailCampaign,
)
from django_ctct.admin import (
  ContactCustomFieldInline,
  ContactPhoneNumberInline,
  ContactStreetAddressInline,
  ContactNoteInline,
)


from tests.factories import get_factory, TokenFactory


HttpMethod = Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE']


# TODO: How to do inline customfield checking?
@parameterized_class(
  ('model', ),
  [(ContactList, ), (CustomField, ), (Contact, )],
)
@override_settings(CTCT_SYNC_ADMIN=True, CTCT_RAISE_FOR_API=True)
class ModelAdminTest(TestCase):

  def setUp(self):
    # Set up mock API
    self.mock_api = requests_mock.Mocker()
    self.mock_api.start()

    # Set up mock Token
    TokenFactory.create()

    # Set up client to access admin page
    self.client = Client()
    self.superuser = User.objects.create_superuser(
      'admin', 'admin@example.com', 'password',
    )
    self.client.force_login(self.superuser)

    # Create an existing object
    include_related = (self.model in [Contact, ContactNote, EmailCampaign])
    self.factory = get_factory(self.model, include_related=include_related)

    if self.model is Contact:
      # Create ContactLists and CustomFields first
      self.lists = get_factory(ContactList).create_batch(10)
      self.custom_fields = get_factory(CustomField).create_batch(2)

    with mute_signals(models.signals.post_save):
      self.existing_obj = self.factory.create()
      if self.model is Contact:
        self.existing_obj.list_memberships.set(random.sample(self.lists, 2))

        factory = get_factory(ContactCustomField)
        for custom_field in self.custom_fields:
          factory(contact=self.existing_obj, custom_field=custom_field)

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

  def get_form_data(self, obj: CTCTModel) -> (dict, dict):
    """Return data necessary to submit a ModelAdmin form (plus inlines)."""

    # Primary form data
    obj_data = model_to_dict(obj, fields=self.model.remote.API_EDITABLE_FIELDS)
    obj_data = {k: v for k, v in obj_data.items() if v}

    # Convert ManyToMany objects to pks
    if obj.pk:
      for field_name, value in obj_data.items():
        if obj._meta.get_field(field_name).many_to_many:
          obj_data[field_name] = [_.pk for _ in value]

    # Inline form data data
    inline_data = {}
    inline_admins = {
      Contact: [
        ContactCustomFieldInline,
        ContactPhoneNumberInline,
        ContactStreetAddressInline,
        ContactNoteInline,
      ],
    }.get(self.model, [])

    for inline_admin in inline_admins:
      # Include data for the management form
      request = self.response.wsgi_request
      formset = inline_admin(self.model, admin.site).get_formset(request)()
      for key, value in formset.management_form.initial.items():
        inline_data[f'{formset.prefix}-{key}'] = value

        related_obj_factory = get_factory(inline_admin.model)
        if inline_admin.model is ContactCustomField:
          # We want to re-use existing CustomFields
          kwargs_1 = {'custom_field': self.custom_fields[0]}
          kwargs_2 = {'custom_field': self.custom_fields[1]}
        else:
          kwargs_1, kwargs_2 = {}, {}

        related_objs = [
          related_obj_factory.build(**kwargs_1),
          related_obj_factory.build(**kwargs_2),
        ]

        for num, related_obj in enumerate(related_objs):
          data = inline_admin.model.remote.serialize(related_obj)
          if inline_admin.model is ContactCustomField:
            # Use Django PKs not API ids
            del data['custom_field_id']
            data['custom_field'] = self.custom_fields[num].pk

          for key, value in data.items():
            inline_data[f'{formset.prefix}-{num}-{key}'] = value
        inline_data[f'{formset.prefix}-TOTAL_FORMS'] = len(related_objs)

    return obj_data, inline_data

  @patch('django_ctct.models.Token.decode')
  def test_create(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Make a GET to the add object admin view
    admin_add_path = reverse(
      f'admin:django_ctct_{self.model.__name__.lower()}_add'
    )
    self.response = self.client.get(admin_add_path)

    # Build the object using factory-boy
    obj = self.factory.build(api_id=None)

    # Set up API mocker
    self.mock_api.post(
      url=self.model.remote.get_url(),
      status_code=201,
      json=self.get_api_response(obj),
    )

    # Make a POST to create the new object
    obj_data, inline_data = self.get_form_data(obj)
    self.response = self.client.post(
      path=admin_add_path,
      data=obj_data | inline_data,
    )

    # Verify it redirected (form errors would result in a 200 response)
    try:
      self.assertEqual(self.response.status_code, 302)
    except AssertionError as e:
      # Write form errors to file
      with open('test_admin_create.html', 'w') as f:
        f.write(self.response.rendered_content)
      raise e

    # Verify object was created
    obj = self.model.objects.filter(**obj_data).first()
    self.assertIsNotNone(obj)
    self.assertIsNotNone(obj.api_id)

    # Verify inline objects were created
    # TODO?
    if inline_data:
      pass

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, 1)

  # TODO: How to check updating inlines
  # TODO: Updating a Contact with no lists issues DELETE
  @patch('django_ctct.models.Token.decode')
  def test_update(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Make a GET to the change object admin view
    admin_change_path = reverse(
      f'admin:django_ctct_{self.model.__name__.lower()}_change',
      args=(self.existing_obj.pk, ),
    )
    self.response = self.client.get(admin_change_path)

    # Update some values
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
    self.mock_api.put(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=200,
      json=self.get_api_response(self.existing_obj),
    )

    # Make a POST to update the existing object
    obj_data, inline_data = self.get_form_data(self.existing_obj)
    self.response = self.client.post(
      path=admin_change_path,
      data=obj_data | inline_data,
    )

    # Verify it redirected (form errors would result in a 200 response)
    try:
      self.assertEqual(self.response.status_code, 302)
    except AssertionError as e:
      # Write form errors to file
      with open('test_admin_update.html', 'w') as f:
        f.write(self.response.rendered_content)
      raise e

    # Verify object was updated
    self.existing_obj.refresh_from_db()
    for field, value in other_obj_data.items():
      self.assertEqual(getattr(self.existing_obj, field), value)

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, 1)

  @patch('django_ctct.models.Token.decode')
  def test_delete(self, token_decode: MagicMock):

    token_decode.return_value = True

    # Set up API mocker
    self.mock_api.delete(
      url=self.model.remote.get_url(api_id=self.existing_obj.api_id),
      status_code=204,
      text='',
    )

    # Make a POST to the delete object admin confirm view
    admin_confirm_delete_path = reverse(
      f'admin:django_ctct_{self.model.__name__.lower()}_delete',
      args=(self.existing_obj.pk, ),
    )
    data = {'post': 'yes'}  # Click the confirm delete button
    self.response = self.client.post(admin_confirm_delete_path, data)

    # Verify it redirected (form errors would result in a 200 response)
    try:
      self.assertEqual(self.response.status_code, 302)
    except AssertionError as e:
      # Write form errors to file
      with open('test_admin_delete.html', 'w') as f:
        f.write(self.response.rendered_content)
      raise e

    # Verify object was deleted
    self.assertFalse(
      self.model.objects.filter(pk=self.existing_obj.pk).exists()
    )

    # Verify remote deletion
    # TODO: Is this considered "testing the API"?
    # self.mock_api.mock(
    #   model=self.model,
    #   method='GET',
    #   status_code=404,
    #   api_id=self.existing_obj.api_id,
    # )
    # remote_obj, _ = self.model.remote.get(self.existing_obj.api_id)
    # self.assertIsNone(remote_obj)

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, 1)


@parameterized_class(
  ('model', ),
  [(Token, ), (ContactNote, )],
)
class ViewModelAdminTest(TestCase):

  def setUp(self):
    self.client = Client()
    self.user = User.objects.create_user(
      'user', 'user@example.com', 'password',
    )
    self.client.force_login(self.user)

  def test_permissions(self):
    admin_changelist_path = reverse(
      f'admin:django_ctct_{self.model.__name__.lower()}_changelist'
    )
    self.response = self.client.get(admin_changelist_path)
    request = self.response.wsgi_request

    model_admin = admin.site._registry[self.model]
    self.assertFalse(model_admin.has_add_permission(request, obj=None))
    self.assertFalse(model_admin.has_change_permission(request, obj=None))
    self.assertFalse(model_admin.has_delete_permission(request, obj=None))
