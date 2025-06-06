from typing import TYPE_CHECKING, Type, Generic
from unittest import SkipTest
from unittest.mock import patch, MagicMock

from parameterized import parameterized_class

from django.db import models
from django.db.models import QuerySet
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.forms import model_to_dict
from django.http import HttpRequest
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils.translation import gettext as _

from django_ctct.vendor import mute_signals
from django_ctct.models import (
  JsonDict, C, E,
  CustomField, ContactList, Contact,
  ContactCustomField, ContactNote,
  EmailCampaign, CampaignActivity, CampaignSummary,
)

from tests.factories import get_factory
from tests.project.test_models import TestCRUD


if TYPE_CHECKING:
  from django.test.client import _MonkeyPatchedWSGIResponse as TestHttpResponse


@parameterized_class(
  ('model', ),
  [(ContactList, ), (CustomField, ), (Contact, ), (EmailCampaign, ),],
)
@override_settings(CTCT_SYNC_ADMIN=True, CTCT_RAISE_FOR_API=True)
class ModelAdminTest(TestCRUD[E], TestCase):

  model: Type[E]

  @classmethod
  def setUpClass(cls) -> None:
    super().setUpClass()
    if cls is ModelAdminTest:
      message = _("This is the unparameterized base class.")
      raise SkipTest(message)

  def setUp(self) -> None:
    super().setUp()

    # Set up client to access admin page
    self.client = Client()
    self.superuser = User.objects.create_superuser(
      'admin', 'admin@example.com', 'password',
    )
    self.client.force_login(self.superuser)

  def get_form_data(
    self,
    obj: E,
    request: HttpRequest,
    update_related: bool = False,
  ) -> tuple[JsonDict, JsonDict]:
    """Return data necessary to submit a ModelAdmin form (plus inlines)."""

    obj_data: JsonDict
    inline_data: JsonDict = {}

    if update_related:
      # TODO: GH #13
      raise NotImplementedError

    # Primary form data
    obj_data = model_to_dict(obj, fields=self.model.API_EDITABLE_FIELDS)
    obj_data = {k: v for k, v in obj_data.items() if v}

    # Convert ManyToMany objects to pks
    if obj.pk:
      for field_name, value in obj_data.items():
        if obj._meta.get_field(field_name).many_to_many:
          obj_data[field_name] = [_.pk for _ in value]

    # Inline form data data
    model_admin = admin.site._registry[self.model]
    inline_admins = model_admin.get_inlines(request, obj)
    for inline_admin in inline_admins:
      # Properly initialize the inline admin formset
      FormSet = inline_admin(self.model, admin.site).get_formset(
        request=request,
        obj=obj if obj.pk else None,
      )
      formset = FormSet(instance=obj if obj.pk else None)

      # Include data for the management form
      for key, value in formset.management_form.initial.items():
        inline_data[f'{formset.prefix}-{key}'] = value

      if obj.pk:
        # Include initial data and pks for existing related objects
        for i, form in enumerate(formset.initial_forms):
          for field_name, value in form.initial.items():
            if isinstance(value, (list, QuerySet)):
              # For ManyToMany, we need a list of PKs
              value = [o.pk for o in value]
            inline_data[f'{formset.prefix}-{i}-{field_name}'] = value
          inline_data[f'{formset.prefix}-{i}-id'] = form.instance.pk
      else:
        # Include new data for related object
        related_obj_factory = get_factory(inline_admin.model)
        # TODO: inline.admin is of type CTCTModel, can't be ContactCustomField
        # TODO: how does this play into serializer?
        if inline_admin.model is ContactCustomField:
          # We want to re-use existing CustomFields
          related_objs = [
            related_obj_factory.build(custom_field=self.custom_fields[0]),
            related_obj_factory.build(custom_field=self.custom_fields[1]),
          ]
        elif inline_admin.model is CampaignActivity:
          # Set parent EmailCampaign and re-using existing ContactLists
          related_objs = [related_obj_factory.build(campaign=obj)]
        else:
          related_objs = [
            related_obj_factory.build(),
            related_obj_factory.build(),
          ]

        for i, related_obj in enumerate(related_objs):
          if inline_admin.model is ContactCustomField:
            # TODO: ContactCustomField has no serializer (no api_id)
            # Use Django PKs not API ids
            data = {
              'custom_field': related_obj.custom_field.pk,
              'value': related_obj.value,
            }
          else:
            data = inline_admin.model.serializer.serialize(related_obj)
            if inline_admin.model is CampaignActivity:
              # Factory can't specify ManyToManyField during build()
              data['contact_lists'] = [cl.pk for cl in self.existing_lists]

          for field_name, value in data.items():
            inline_data[f'{formset.prefix}-{i}-{field_name}'] = value
        inline_data[f'{formset.prefix}-TOTAL_FORMS'] = len(related_objs)

    return obj_data, inline_data

  def assert_redirect(self, response: 'TestHttpResponse') -> None:
    """Verify response was a redirect (a 200 response implies form errors)."""

    if (response.status_code == 200) and (response.context is not None):
      breakpoint()  # check response.context_data
      # Check for form errors in a way that will display them to the dev
      form = response.context['adminform']
      self.assertFalse(form.errors or form.non_field_errors())

      for formset in response.context['inline_admin_formsets']:
        self.assertFalse(formset.non_form_errors())
        for form in formset.forms:
          self.assertFalse(form.errors)
    else:
      self.assertEqual(response.status_code, 302)

  def create_obj(self, obj: E) -> E:
    """Create object using Django admin."""

    # Make a GET to the add object admin view
    admin_add_path = reverse(
      f'admin:django_ctct_{self.model.__name__.lower()}_add'
    )
    response = self.client.get(admin_add_path)

    # Make a POST to create the new object
    obj_data, inline_data = self.get_form_data(
      obj=obj,
      request=response.wsgi_request,
    )
    response = self.client.post(
      path=admin_add_path,
      data=obj_data | inline_data,
    )

    # Verify it redirected (form errors would result in a 200 response)
    self.assert_redirect(response)

    # Refresh from db and return
    for key, value in obj_data.copy().items():
      if isinstance(value, list):
        # Must use .distinct() with a list
        obj_data[f'{key}__in'] = obj_data.pop(key)
    obj = self.model.objects.filter(**obj_data).distinct().get()
    return obj

  def update_obj(self, obj: E) -> E:
    """Update object using Django admin."""

    # Make a GET to the change object admin view
    admin_change_path = reverse(
      f'admin:django_ctct_{self.model.__name__.lower()}_change',
      args=(obj.pk, ),
    )
    response = self.client.get(admin_change_path)

    # Make a POST to update the existing object
    obj_data, inline_data = self.get_form_data(
      obj=obj,
      request=response.wsgi_request,
    )
    response = self.client.post(
      path=admin_change_path,
      data=obj_data | inline_data,
    )

    # Verify it redirected (form errors would result in a 200 response)
    self.assert_redirect(response)

    # Refresh from db and return
    for key, value in obj_data.copy().items():
      if isinstance(value, list):
        # Must use .distinct() with a list
        obj_data[f'{key}__in'] = obj_data.pop(key)
    obj = self.model.objects.filter(**obj_data).distinct().get()
    return obj

  def delete_obj(self, obj: E) -> None:
    """Delete object using Django admin."""

    # Make a POST to the delete object admin confirm view.
    admin_confirm_delete_path = reverse(
      f'admin:django_ctct_{self.model.__name__.lower()}_delete',
      args=(obj.pk, ),
    )
    data = {'post': 'yes'}  # Click the confirm delete button
    response = self.client.post(admin_confirm_delete_path, data)

    # Verify it redirected (form errors would result in a 200 response)
    self.assert_redirect(response)

  @patch('django_ctct.models.Token.decode')
  def test_bulk_delete(self, token_decode: MagicMock) -> None:
    """Test bulk deletion in Django admin."""

    if self.model.API_ENDPOINT_BULK_DELETE is None:
      # CTCT does not provide a bulk delete endpoint
      return
    elif self.model.API_ENDPOINT_BULK_LIMIT is None:
      message = _("Must specify API_ENDPOINT_BULK_LIMIT.")
      raise ImproperlyConfigured(message)

    token_decode.return_value = True

    # Set up API mocker
    self.mock_api.post(
      url=self.model.remote.get_url(
        endpoint=self.model.API_ENDPOINT_BULK_DELETE
      ),
      status_code=201,
      json={},  # Response is not used by django_ctct
    )

    # Create objects
    with mute_signals(models.signals.post_save):
      num_calls = 2
      size = self.model.API_ENDPOINT_BULK_LIMIT * num_calls
      objs = self.factory.create_batch(size=size)
      pks = [o.pk for o in objs]

    # Use ModelAdmin to perform bulk delete
    model_admin = admin.site._registry[self.model]
    model_admin.delete_queryset(
      request=HttpRequest(),
      queryset=self.model.objects.filter(pk__in=pks),
    )

    # Verify objects were deleted
    self.assertFalse(self.model.objects.filter(pk__in=pks).exists())

    # Verify the number of requests that were made
    self.assertEqual(self.mock_api.call_count, num_calls)


@parameterized_class(
  ('model', ),
  [(ContactNote, CampaignSummary, )],
)
class ViewModelAdminTest(TestCase, Generic[C]):

  model: Type[C]

  def setUp(self) -> None:
    self.client = Client()
    self.user = User.objects.create_user(
      'user', 'user@example.com', 'password',
    )
    self.client.force_login(self.user)

  def test_permissions(self) -> None:
    admin_changelist_path = reverse(
      f'admin:django_ctct_{self.model.__name__.lower()}_changelist'
    )
    response = self.client.get(admin_changelist_path)
    request = response.wsgi_request

    model_admin = admin.site._registry[self.model]
    self.assertFalse(model_admin.has_add_permission(request))
    self.assertFalse(model_admin.has_change_permission(request, obj=None))
    self.assertFalse(model_admin.has_delete_permission(request, obj=None))
