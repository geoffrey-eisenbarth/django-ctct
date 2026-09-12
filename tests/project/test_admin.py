from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, TypeVar
from unittest import SkipTest
from unittest.mock import MagicMock, patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import Model, QuerySet
from django.forms import model_to_dict
from django.http import HttpRequest
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from parameterized import parameterized_class
from requests.exceptions import HTTPError

from django_ctct.admin import CampaignActivityInline
from django_ctct.models import (
  CampaignActivity,
  CampaignSummary,
  Contact,
  ContactCustomField,
  ContactList,
  ContactNote,
  CTCTEndpointModel,
  CustomField,
  EmailCampaign,
  JsonDict,
)
from tests.factories import get_factory
from tests.project.test_models import TestCRUD

if TYPE_CHECKING:
  from django.test.client import _MonkeyPatchedWSGIResponse as TestHttpResponse


E = TypeVar("E", bound=CTCTEndpointModel)


@parameterized_class(
  ("model",),
  [
    (ContactList,),
    (CustomField,),
    (Contact,),
    (EmailCampaign,),
  ],
)
@override_settings(CTCT_SYNC_ADMIN=True, CTCT_RAISE_FOR_API=True)
class ModelAdminTest(TestCRUD[E], TestCase):
  model: type[E]

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
      "admin",
      "admin@example.com",
      "password",
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

    # Primary form data
    obj_data = model_to_dict(obj, fields=self.model.API_EDITABLE_FIELDS)
    obj_data = {k: v for k, v in obj_data.items() if v}

    # Convert non-inline ManyToMany objects to pks
    if obj.pk:
      for field_name, value in obj_data.items():
        if obj._meta.get_field(field_name).many_to_many:
          obj_data[field_name] = [_.pk for _ in value]

    # Inline form data (including the ContactCustomField M2M)
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
        inline_data[f"{formset.prefix}-{key}"] = value

      if obj.pk:
        # Include initial data and pks for existing related objects
        for i, form in enumerate(formset.initial_forms):
          inline_data[f"{formset.prefix}-{i}-id"] = form.instance.pk
          for field_name, initial_value in form.initial.items():
            if isinstance(initial_value, (list, QuerySet)):
              # For ManyToMany, we need a list of PKs
              value = [o.pk for o in initial_value]

            if update_related:
              # TODO: GH #13
              raise NotImplementedError
            else:
              value = initial_value

            inline_data[f"{formset.prefix}-{i}-{field_name}"] = value
            inline_data[f"initial-{formset.prefix}-{i}-{field_name}"] = initial_value  # noqa: E501

        for i, form in enumerate(formset.initial_forms):
          inline_data[f"{formset.prefix}-{i}-id"] = form.instance.pk
          for field in inline_admin.model._meta.get_fields():
            if field.name in form.initial:
              value = form.initial[field.name]
              if isinstance(value, (list, QuerySet)):
                # For ManyToMany, we need a list of PKs
                value = [o.pk for o in value]
              inline_data[f"{formset.prefix}-{i}-{field_name}"] = value
            elif field.default is not models.NOT_PROVIDED:
              # Include related object defaults
              default = "" if field.default is None else field.get_default()
              inline_data[f"initial-{formset.prefix}-{i}-{field.name}"] = default  # noqa: E501

      else:
        # Include new data for related object
        related_obj_factory = get_factory(inline_admin.model)
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
          data = inline_admin.model.serializer.serialize(related_obj)
          if inline_admin.model is CampaignActivity:
            # Factory can't specify ManyToManyField during build()
            data["contact_lists"] = [cl.pk for cl in self.existing_lists]
          elif inline_admin.model is ContactCustomField:
            # Make sure to use pk here
            data["custom_field"] = related_obj.custom_field.pk

          for field_name, value in data.items():
            inline_data[f"{formset.prefix}-{i}-{field_name}"] = value

          # Include defaults for new related objects
          for field in filter(
            lambda f: f.default is not models.NOT_PROVIDED,
            inline_admin.model._meta.get_fields(),
          ):
            default = "" if field.default is None else field.get_default()
            inline_data[f"initial-{formset.prefix}-{i}-{field.name}"] = default

        inline_data[f"{formset.prefix}-TOTAL_FORMS"] = len(related_objs)

    return obj_data, inline_data

  def assert_redirect(self, response: TestHttpResponse) -> None:
    """Verify response was a redirect (a 200 response implies form errors)."""

    if (response.status_code == 200) and (response.context is not None):
      # Check for form errors in a way that will display them to the dev
      form = response.context["adminform"]
      self.assertFalse(form.errors or form.non_field_errors())

      for formset in response.context["inline_admin_formsets"]:
        self.assertFalse(formset.non_form_errors())
        for form in formset.forms:
          self.assertFalse(form.errors)
    else:
      self.assertEqual(response.status_code, 302)

  def create_obj(self, obj: E) -> E:
    """Create object using Django admin."""

    # Make a GET to the add object admin view
    admin_add_path = reverse(f"admin:django_ctct_{self.model.__name__.lower()}_add")
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
        obj_data[f"{key}__in"] = obj_data.pop(key)
    return self.model.objects.filter(**obj_data).distinct().get()

  def update_obj(self, obj: E) -> E:
    """Update object using Django admin."""

    # Make a GET to the change object admin view
    admin_change_path = reverse(
      f"admin:django_ctct_{self.model.__name__.lower()}_change",
      args=(obj.pk,),
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
        obj_data[f"{key}__in"] = obj_data.pop(key)

    return self.model.objects.filter(**obj_data).distinct().get()

  def delete_obj(self, obj: E) -> None:
    """Delete object using Django admin."""

    # Make a POST to the delete object admin confirm view.
    admin_confirm_delete_path = reverse(
      f"admin:django_ctct_{self.model.__name__.lower()}_delete",
      args=(obj.pk,),
    )
    data = {"post": "yes"}  # Click the confirm delete button
    response = self.client.post(admin_confirm_delete_path, data)

    # Verify it redirected (form errors would result in a 200 response)
    self.assert_redirect(response)

  @patch("django_ctct.models.Token.decode")
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
      url=self.model.remote.get_url(endpoint=self.model.API_ENDPOINT_BULK_DELETE),
      status_code=201,
      json={},  # Response is not used by django_ctct
    )

    # Create objects
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

  @patch("django_ctct.models.Token.decode")
  def test_catch_api_errors(self, token_decode: MagicMock) -> None:
    """HTTPErrors are re-raised or reported via message_user(); others propagate."""

    token_decode.return_value = True
    model_admin = admin.site._registry[self.model]

    if self.model.API_ENDPOINT_BULK_DELETE is None:
      # bulk_delete() raises NotImplementedError, which is not an HTTPError,
      # so catch_api_errors() never catches it and it always propagates.
      with self.assertRaises(NotImplementedError):
        model_admin.delete_queryset(HttpRequest(), self.model.objects.none())
      return

    # bulk_delete() raises ImproperlyConfigured if no limit is specified; like
    # NotImplementedError, this is not an HTTPError so it always propagates.
    with patch.object(self.model, "API_ENDPOINT_BULK_LIMIT", None):
      with self.assertRaises(ImproperlyConfigured):
        model_admin.delete_queryset(HttpRequest(), self.model.objects.none())

    # Set up API mocker to fail
    self.mock_api.post(
      url=self.model.remote.get_url(endpoint=self.model.API_ENDPOINT_BULK_DELETE),
      status_code=400,
      json={},
    )

    objs = self.factory.create_batch(size=2)
    queryset = self.model.objects.filter(pk__in=[o.pk for o in objs])

    # CTCT_RAISE_FOR_API=True (class default): HTTPErrors propagate to the caller
    with self.assertRaises(HTTPError):
      model_admin.delete_queryset(HttpRequest(), queryset)

    # CTCT_RAISE_FOR_API=False: HTTPErrors are caught and reported via message_user()
    request = self.client.get(
      reverse(f"admin:django_ctct_{self.model.__name__.lower()}_changelist")
    ).wsgi_request
    with self.settings(CTCT_RAISE_FOR_API=False):
      model_admin.delete_queryset(request, queryset)
      model_admin.delete_queryset(request=request, queryset=queryset)
    messages = [str(m) for m in get_messages(request)]
    self.assertEqual(sum("ConstantContact" in m for m in messages), 2)

    # Objects still exist since the queryset delete never ran
    self.assertTrue(queryset.exists())

  def test_get_readonly_fields(self) -> None:
    """get_readonly_fields() adapts based on object state and permissions."""

    model_admin = admin.site._registry[self.model]
    request = HttpRequest()
    request.user = self.superuser

    if isinstance(self.existing_obj, Contact):
      readonly_fields = model_admin.get_readonly_fields(request, self.existing_obj)
      self.assertNotIn("list_memberships", readonly_fields)

      self.existing_obj.opt_out_source = "ACTION_BY_CONTACT"
      request.user = User.objects.create_user(
        username="staff",
        email="staff@example.com",
        password="pw",
      )
      readonly_fields = model_admin.get_readonly_fields(request, self.existing_obj)
      self.assertIn("list_memberships", readonly_fields)

    elif isinstance(self.existing_obj, EmailCampaign):
      readonly_fields = model_admin.get_readonly_fields(request, self.existing_obj)
      self.assertNotIn("scheduled_datetime", readonly_fields)

      inline_admin = CampaignActivityInline(self.existing_obj.__class__, admin.site)
      inline_readonly_fields = inline_admin.get_readonly_fields(
        request, self.existing_obj
      )
      self.assertNotIn("subject", inline_readonly_fields)

      self.existing_obj.current_status = "DONE"
      readonly_fields = model_admin.get_readonly_fields(request, self.existing_obj)
      self.assertIn("scheduled_datetime", readonly_fields)

      inline_readonly_fields = inline_admin.get_readonly_fields(
        request, self.existing_obj
      )
      self.assertIn("subject", inline_readonly_fields)

  @patch("django_ctct.models.Token.decode")
  def test_ctct_message_user(self, token_decode: MagicMock) -> None:
    """Updating schedule/preview state produces the expected user-facing message."""

    if not isinstance(self.existing_obj, EmailCampaign):
      return

    token_decode.return_value = True

    admin_change_path = reverse(
      f"admin:django_ctct_{self.model.__name__.lower()}_change",
      args=(self.existing_obj.pk,),
    )

    def do_update(
      email_campaign: E,
      scheduled_datetime: dt.datetime | None,
      send_preview: bool,
    ) -> list[str]:
      """Submit a change form and return the messages produced."""
      assert isinstance(email_campaign, EmailCampaign)

      response = self.client.get(admin_change_path)
      obj_data, inline_data = self.get_form_data(
        obj=email_campaign,
        request=response.wsgi_request,
      )

      # AdminSplitDateTime widget requires separate date/time keys
      obj_data.pop("scheduled_datetime", None)
      if scheduled_datetime is not None:
        obj_data["scheduled_datetime_0"] = scheduled_datetime.strftime("%Y-%m-%d")
        obj_data["scheduled_datetime_1"] = scheduled_datetime.strftime("%H:%M:%S")
      else:
        obj_data["scheduled_datetime_0"] = ""
        obj_data["scheduled_datetime_1"] = ""
      if send_preview:
        obj_data["send_preview"] = "on"

      # Mock the CampaignActivity update that always follows a message
      activity = email_campaign.campaign_activities.get(role="primary_email")
      api_response = self.get_api_response(email_campaign)
      self.mock_api.put(
        url=CampaignActivity.remote.get_url(api_id=activity.api_id),
        status_code=200,
        json=api_response["campaign_activities"][0],
      )
      if scheduled_datetime is not None:
        self.mock_api.post(
          url=CampaignActivity.remote.get_url(
            api_id=activity.api_id,
            endpoint_suffix="/schedules",
          ),
          status_code=201,
          json={},
        )
      if send_preview:
        self.mock_api.post(
          url=CampaignActivity.remote.get_url(
            api_id=activity.api_id,
            endpoint_suffix="/tests",
          ),
          status_code=201,
          json={},
        )

      response = self.client.post(admin_change_path, data=obj_data | inline_data)
      self.assert_redirect(response)
      email_campaign.refresh_from_db()
      return [str(m) for m in get_messages(response.wsgi_request)]

    # Scheduling triggers "scheduled to be sent"
    future = timezone.now() + dt.timedelta(days=1)
    messages = do_update(self.existing_obj, future, send_preview=False)
    self.assertTrue(any("scheduled to be sent" in m for m in messages))

    # Unscheduling triggers "unscheduled remotely"
    messages = do_update(self.existing_obj, None, send_preview=False)
    self.assertTrue(any("unscheduled remotely" in m for m in messages))

    # Sending a preview (with no schedule change) triggers "updated remotely"
    messages = do_update(self.existing_obj, None, send_preview=True)
    self.assertTrue(any("updated remotely" in m for m in messages))
    self.assertTrue(any("preview has been sent out" in m for m in messages))

  def test_changelist_view(self) -> None:
    """Visiting the changelist renders list_display callables."""

    admin_changelist_path = reverse(
      f"admin:django_ctct_{self.model.__name__.lower()}_changelist"
    )
    response = self.client.get(admin_changelist_path)
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.context["cl"].result_count, 1)

    if self.model is Contact:
      for value in ("sync", "not_synced", "optout"):
        response = self.client.get(admin_changelist_path, {"ctct": value})
        self.assertEqual(response.status_code, 200)


@parameterized_class(
  ("model",),
  [
    (ContactNote,),
    (CampaignSummary,),
  ],
)
class ViewModelAdminTest(TestCase):
  model: type[Model]

  def setUp(self) -> None:
    self.client = Client()
    self.user = User.objects.create_user(
      "user",
      "user@example.com",
      "password",
    )
    self.client.force_login(self.user)

    # A couple of objects so the changelist actually renders rows,
    # exercising get_queryset()/list_display callables/list_filter lookups.
    get_factory(self.model).create_batch(2)

  def test_permissions(self) -> None:
    admin_changelist_path = reverse(
      f"admin:django_ctct_{self.model.__name__.lower()}_changelist"
    )
    response = self.client.get(admin_changelist_path)
    request = response.wsgi_request

    model_admin = admin.site._registry[self.model]
    self.assertFalse(model_admin.has_add_permission(request))
    self.assertFalse(model_admin.has_change_permission(request, obj=None))
    self.assertFalse(model_admin.has_delete_permission(request, obj=None))

  def test_get_readonly_fields(self) -> None:
    """get_readonly_fields() locks all fields for existing objects only."""

    model_admin = admin.site._registry[self.model]
    request = HttpRequest()
    obj = self.model._default_manager.first()

    assert obj is not None
    self.assertEqual(model_admin.get_readonly_fields(request, obj=None), ())

    readonly_fields = model_admin.get_readonly_fields(request, obj)
    expected = tuple(f.name for f in obj._meta.fields if f.name != "active")
    self.assertEqual(readonly_fields, expected)

  def test_changelist_view(self) -> None:
    """Superusers can view (read-only) the changelist, rendering all rows."""

    superuser = User.objects.create_superuser(
      "admin",
      "admin@example.com",
      "password",
    )
    client = Client()
    client.force_login(superuser)

    admin_changelist_path = reverse(
      f"admin:django_ctct_{self.model.__name__.lower()}_changelist"
    )
    response = client.get(admin_changelist_path)

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.context["cl"].result_count, 2)

    # Superusers are still allowed to delete
    model_admin = admin.site._registry[self.model]
    self.assertTrue(model_admin.has_delete_permission(response.wsgi_request))
