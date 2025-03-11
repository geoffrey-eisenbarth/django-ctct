from typing import Literal
from unittest.mock import patch, MagicMock
import uuid

import factory
from parameterized import parameterized, parameterized_class
import requests_mock

from django.db import models
from django.contrib import admin
from django.contrib.auth.models import User
from django.forms import model_to_dict
from django.http import HttpRequest
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from django_ctct.models import (
  CTCTModel, Token, CustomField, ContactList, Contact, ContactNote,
)
from django_ctct.admin import *

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
    include_related = (self.model in [Contact, EmailCampaign])
    self.factory = get_factory(self.model, include_related=include_related)
    with factory.django.mute_signals(models.signals.post_save):
      self.existing_obj = self.factory.create(api_id=uuid.uuid4())

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
    if obj.api_id is None:
      api_id = str(uuid.uuid4())
    else:
      api_id = str(obj.api_id)
    data[self.model.remote.API_ID_LABEL] = api_id

    return data

  def get_form_data(self, obj: CTCTModel) -> (dict, dict):
    obj_data = model_to_dict(obj, fields=self.model.remote.API_EDITABLE_FIELDS)
    obj_data = {k:v for k, v in obj_data.items() if v}

    inline_data = {}
    inline_admins = {
      Contact: [
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
        related_objs = [
          related_obj_factory.build(),
          related_obj_factory.build(),
        ]
        for num, related_obj in enumerate(related_objs):
          data = inline_admin.model.remote.serialize(related_obj)
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
    obj = self.factory.build()

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
    if inline_data:
      breakpoint()
    #for inline_admin in self.get_inline_admins():
    #  related_model = inline_admin.model
    #  for data in self.get_object_data(method='POST', model=related_model, get_multiple=True):
    #    self.assertTrue(
    #      related_model.objects.filter(**data).exists()
    #    )

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, 1)

  # TODO: How to check updating inlines
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
    other_obj_data = self.model.remote.serialize(other_obj, field_types='editable')
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
    # TODO: Is this considered "testing the API" and we should assume that it got deleted?
    #self.mock_api.mock(
    #  model=self.model,
    #  method='GET',
    #  status_code=404,
    #  api_id=self.existing_obj.api_id,
    #)
    #remote_obj, _ = self.model.remote.get(self.existing_obj.api_id)
    #self.assertIsNone(remote_obj)

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
    admin_changelist_path = reverse(f'admin:django_ctct_{self.model.__name__.lower()}_changelist')
    self.response = self.client.get(admin_changelist_path)
    request = self.response.wsgi_request

    model_admin = admin.site._registry[self.model]
    self.assertFalse(model_admin.has_add_permission(request, obj=None))
    self.assertFalse(model_admin.has_change_permission(request, obj=None))
    self.assertFalse(model_admin.has_delete_permission(request, obj=None))
