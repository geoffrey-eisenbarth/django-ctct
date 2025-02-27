from typing import Literal

from parameterized import parameterized, parameterized_class

from django.db.models import Model
from django.contrib import admin
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from django_ctct.models import (
  Token, CustomField, ContactList, Contact, ContactNote,
)
from django_ctct.admin import *


# TODO:
# 1) Decide if "honorific" and "suffix" fields are staying in the lib
# 2) How to do inline customfield checking?
# 3) How do set Token/mock responses for ctct_sync_admin?
# 4) Need to test with ctct_sync_signals?
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

  def get_object_data(
    self,
    verb: Literal['exists', 'create', 'update'],
    model: Optional[Model] = None,
  ) -> dict:
    # TODO PUSH: Can we use CTCTModel.API_EDITABLE_FIELDS here?
    data = {
      ContactList: {
        'name': f'Test {verb} name',
        'description': f'Test {verb} description',
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
      ContactPhoneNumber: [{
        'kind': ContactPhoneNumber.KINDS[0][0],
        'phone_number': '800-843-2827',
      },],
      ContactStreetAddress: [{
        'kind': ContactStreetAddress.KINDS[0][0],
        'street': '1060 West Addison St',
        'city': 'Chicago',
        'state': 'IL',
        'postal_code': '60613',
        'country': 'United States',
      },],
      ContactNote: [{
        'author': self.superuser,
        'content': 'Test note',
      },],
    }[model or self.model]
    return data

  def get_inline_admins(self) -> list[admin.options.InlineModelAdmin]:
    """Return related InlineAdmins."""
    inlines = {
      Contact: [
        ContactPhoneNumberInline,
        ContactStreetAddressInline,
        ContactNoteInline,
      ],
    }.get(self.model, [])
    return inlines

  def get_inline_data(
    self,
    request: HttpRequest,
    create_inlines: bool = False,
  ) -> dict:
    """Must include management data for inlines."""

    inline_data = {}

    for inline_admin in self.get_inline_admins():
      # Include data for the management form
      formset = inline_admin(self.model, admin.site).get_formset(request)()
      for key, value in formset.management_form.initial.items():
        inline_data[f'{formset.prefix}-{key}'] = value

      # Include data for actual related model formsets
      if create_inlines:
        object_data = self.get_object_data(verb='create', model=inline_admin.model)
        for num, data in enumerate(object_data):
          for key, value in data.items():
            inline_data[f'{formset.prefix}-{num}-{key}'] = value
        inline_data[f'{formset.prefix}-TOTAL_FORMS'] = len(object_data)

    return inline_data

  @parameterized.expand([
    (False, False),
    (False, True),
    (True, False),
    (True, True),
  ])
  def test_create(self, ctct_sync_admin: bool, create_inlines: bool):
    with self.settings(CTCT_SYNC_ADMIN=ctct_sync_admin):
      path = reverse(f'admin:django_ctct_{self.model.__name__.lower()}_add')
      response = self.client.get(path)

      data = self.get_object_data(verb='create')
      inline_data = self.get_inline_data(
        request=response.wsgi_request,
        create_inlines=create_inlines,
      )
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

      # Verify inline objects were created
      if create_inlines:
        for inline_admin in self.get_inline_admins():
          related_model = inline_admin.model
          for data in self.get_object_data(verb='create', model=related_model):
            self.assertTrue(
              related_model.objects.filter(**data).exists()
            )

      # Verify remote creation
      if ctct_sync_admin:
        pass

  # TODO: How to check updating inlines
  @parameterized.expand([False, True])
  def test_update(self, ctct_sync_admin: bool):
    with self.settings(CTCT_SYNC_ADMIN=ctct_sync_admin):
      path = reverse(
        f'admin:django_ctct_{self.model.__name__.lower()}_change',
        args=(self.existing_obj.pk, ),
      )
      response = self.client.get(path)

      data = self.get_object_data(verb='create')
      inline_data = self.get_inline_data(response.wsgi_request)

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

  @parameterized.expand([False, True])
  def test_delete(self, ctct_sync_admin: bool):
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
    self.user = User.objects.create_user(
      'user', 'user@example.com', 'password',
    )
    self.client.force_login(self.user)

  def test_permissions(self):
    path = reverse(f'admin:django_ctct_{self.model.__name__.lower()}_changelist')
    response = self.client.get(path)
    request = response.wsgi_request

    model_admin = admin.site._registry[self.model]
    self.assertFalse(model_admin.has_add_permission(request, obj=None))
    self.assertFalse(model_admin.has_change_permission(request, obj=None))
    self.assertFalse(model_admin.has_delete_permission(request, obj=None))
