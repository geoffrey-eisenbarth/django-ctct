from typing import Literal

import pytest

from parameterized import parameterized, parameterized_class

from django.test import TestCase, Client, override_settings
from django.contrib import admin
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.urls import reverse

from django_ctct.models import (
  Token, CustomField, ContactList, Contact, ContactNote,
)
from django_ctct.admin import *


@parameterized_class(
  ('model', ),
  [(ContactList, ), (CustomField, ), (Contact, )],
)
class ModelAdminTest(TestCase):

  def setUp(self):
    self.client = Client()
    self.superuser = User.objects.create_superuser(
      'admin', 'admin@example.com', 'password',
    )
    self.client.force_login(self.superuser)

    data = self.get_object_data('exists')
    self.existing_obj = self.model.objects.create(**data)

  def get_object_data(self, verb: Literal['exists', 'create', 'update']) -> dict:
    data = {
      ContactList: {
        'name': f'Test {verb} name',
        'description': 'Test {verb} description',
        'favorite': False,
      },
      CustomField: {
        'label': f'Test {verb} label',
        'type': CustomField.TYPES[0][0],
      },
      Contact: {
        'email': f'{verb}@example.com',
        'first_name': verb.title(),
        'last_name': 'Name',
        'job_title': verb.title(),
        'company_name': 'Company',
      },
    }[self.model]
    return data

  def get_inline_management_data(self, request: HttpRequest) -> dict:
    """Must include management data for inlines."""
    inlines = {
      Contact: [
        ContactPhoneNumberInline,
        ContactStreetAddressInline,
        ContactNoteInline,
      ],
    }.get(self.model, [])

    inline_data = {}
    for inline in inlines:
      formset = inline(self.model, admin.site).get_formset(request)()
      for key, value in formset.management_form.initial.items():
        inline_data[f'{formset.prefix}-{key}'] = value

    return inline_data

  #@parameterized.expand([False, True])
  def test_create(self, ctct_sync_admin = False):
    with self.settings(CTCT_SYNC_ADMIN=ctct_sync_admin):
      path = reverse(f'admin:django_ctct_{self.model.__name__.lower()}_add')
      response = self.client.get(path)

      data = self.get_object_data(verb='create')
      inline_data = self.get_inline_management_data(response.wsgi_request)

      response = self.client.post(path, data | inline_data)

      # Verify it redirected (a 200 response would show form errors)
      self.assertEqual(response.status_code, 302)

      # Verify object was created
      self.assertTrue(
        self.model.objects.filter(**data).exists()
      )
      self.assertEqual(
        self.model.objects.get(**data).exists_remotely,
        ctct_sync_admin,
      )

      # Verify remote creation
      if ctct_sync_admin:
        pass

  #@parameterized.expand([False, True])
  def test_update(self, ctct_sync_admin = False):
    with self.settings(CTCT_SYNC_ADMIN=ctct_sync_admin):
      path = reverse(
        f'admin:django_ctct_{self.model.__name__.lower()}_change',
        args=(self.existing_obj.pk, ),
      )
      response = self.client.get(path)

      data = self.get_object_data(verb='create')
      inline_data = self.get_inline_management_data(response.wsgi_request)

      response = self.client.post(path, data | inline_data)

      # Verify it redirected (a 200 response would show form errors)
      self.assertEqual(response.status_code, 302)

      # Verify object was updated
      obj = self.model.objects.get(pk=self.existing_obj.pk)
      for field, value in data.items():
        self.assertEqual(getattr(obj, field), value)

      # Verify remote update
      if ctct_sync_admin:
        pass

  #@parameterized.expand([False, True])
  def test_delete(self, ctct_sync_admin = False):
    with self.settings(CTCT_SYNC_ADMIN=ctct_sync_admin):
      path = reverse(
        f'admin:django_ctct_{self.model.__name__.lower()}_delete',
        args=(self.existing_obj.pk, ),
      )
      data = {'post': 'yes'}  # Click the confirm delete button
      response = self.client.post(path, data)

      # Verify it redirected (a 200 response would show form errors)
      self.assertEqual(response.status_code, 302)

      # Verify object was deleted
      self.assertFalse(
        self.model.objects.filter(pk=self.existing_obj.pk).exists()
      )

      # Verify remote deletion
      if ctct_sync_admin:
        pass


@parameterized_class(
  ('model', ),
  [(Token, ), (ContactNote, )],
)
class ViewModelAdminTest(TestCase):

  def setUp(self):
    self.client = Client()
    self.superuser = User.objects.create_superuser(
      'admin', 'admin@example.com', 'password',
    )
    self.client.force_login(self.superuser)

  def test_permissions(self):
    path = reverse(f'admin:django_ctct_{self.model.__name__.lower()}_changelist')
    response = self.client.get(path)
    request = response.wsgi_request

    model_admin = admin.site._registry[self.model]
    self.assertFalse(model_admin.has_add_permission(request, obj=None))
    self.assertFalse(model_admin.has_change_permission(request, obj=None))
    self.assertFalse(model_admin.has_delete_permission(request, obj=None))
