from __future__ import annotations

import datetime as dt
from functools import partial
from typing import TYPE_CHECKING, Literal, Optional, NoReturn
from urllib.parse import urlencode
from uuid import UUID

from jwt import ExpiredSignatureError
from ratelimit import limits, sleep_and_retry
import requests
from requests.exceptions import HTTPError
from requests.models import Response

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models import Model
from django.db.models import signals
from django.db.models.query import QuerySet
from django.http import HttpRequest, Http404
from django.middleware.csrf import get_token as get_csrf_token
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from django_ctct.utils import get_related_fields
from django_ctct.vendor import mute_signals


if TYPE_CHECKING:
  from django_ctct.models import (
    Token, Contact, ContactList,
    EmailCampaign, CampaignActivity,
  )


class BaseRemoteManager(models.Manager):
  """Base manager for utilizing an API."""

  API_URL = 'https://api.cc.email'
  API_VERSION = '/v3'

  @classmethod
  def get_url(
    cls,
    api_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    endpoint_suffix: Optional[str] = None,
  ) -> str:
    endpoint = endpoint or cls.API_ENDPOINT
    if not endpoint.startswith(cls.API_VERSION):
      endpoint = f'{cls.API_VERSION}{endpoint}'

    url = f'{cls.API_URL}{endpoint}'

    if api_id:
      url += f'/{api_id}'

    if endpoint_suffix:
      url += f'{endpoint_suffix}'

    return url

  def raise_or_json(self, response: Response) -> Optional[dict]:
    if response.status_code == 204:
      data = None
    elif response.status_code == 404:
      # Allow catching 404 separately from HTTPError
      raise Http404
    else:
      data = response.json()

    try:
      response.raise_for_status()
    except HTTPError:
      if isinstance(data, list):
        data = data[0]
      # Models use 'error_message', Tokens use 'error_description'
      error_message = data.get('error_message', data.get('error_description'))
      message = _(
        f"[{response.status_code}] {error_message}"
      )
      raise HTTPError(message, response=response)

    return data

  def _improperly_configured(self):
    message = _(
      "You must define this method on a child class."
    )
    raise ImproperlyConfigured(message)

  def get_queryset(self):
    """Prevent access to the db from within RemoteManager."""
    return super().get_queryset().none()

  def create(self):
    return self._improperly_configured()

  def get(self):
    return self._improperly_configured()

  def all(self):
    return self._improperly_configured()

  def update(self):
    return self._improperly_configured()

  def delete(self):
    return self._improperly_configured()


class TokenRemoteManager(BaseRemoteManager):
  """Manager for utilizing CTCT's Auth Token API."""

  API_URL = 'https://authz.constantcontact.com/oauth2/default'
  API_VERSION = '/v1'
  API_SCOPE = '+'.join([
    'account_read',
    'account_update',
    'contact_data',
    'campaign_data',
    'offline_access',
  ])

  def get_auth_url(self, request: HttpRequest) -> str:
    """Returns a URL for logging into CTCT.com to grant permissions."""
    endpoint = self.get_url(endpoint='/authorize')
    data = {
      'client_id': settings.CTCT_PUBLIC_KEY,
      'redirect_uri': settings.CTCT_REDIRECT_URI,
      'response_type': 'code',
      'state': get_csrf_token(request),
      'scope': self.API_SCOPE,
    }
    url = f"{endpoint}?{urlencode(data, safe='+')}"
    return url

  def connect(self) -> None:
    self.session = requests.Session()
    self.session.auth = (settings.CTCT_PUBLIC_KEY, settings.CTCT_SECRET_KEY)

  def create(self, auth_code: str) -> Token:
    """Creates the initial Token using an `auth_code` from CTCT.

    Notes
    -----
    The value of CTCT_REDIRECT_URI must exactly match the value
    specified in the developer's page on constantcontact.com.

    """

    response = self.session.post(
      url=self.get_url(endpoint='/token'),
      data={
        'code': auth_code,
        'redirect_uri': settings.CTCT_REDIRECT_URI,
        'grant_type': 'authorization_code',
      },
    )
    data = self.raise_or_json(response)
    token = self.model.objects.create(**data)
    return token

  def get(self) -> Token:
    """Fetches most recent token, refreshing if necessary."""

    token = self.model.objects.first()
    if not token:
      message = _(
        "No tokens in the database yet. "
        f"Go to {reverse('ctct:auth')} and sign into ConstantContact."
      )
      raise ValueError(message)

    try:
      token.decode()
    except ExpiredSignatureError:
      self.connect()
      token = self.update(token)

    return token

  def update(self, token: Token) -> Token:
    """Obtain a new Token from CTCT using the refresh code."""

    response = self.session.post(
      url=self.get_url(endpoint='/token'),
      data={
        'refresh_token': token.refresh_token,
        'grant_type': 'refresh_token',
      },
    )
    data = self.raise_or_json(response)
    token = self.model.objects.create(**data)
    return token


class RemoteManager(BaseRemoteManager):
  """Manager for utilizing the CTCT API."""

  API_LIMIT_CALLS = 4   # four calls
  API_LIMIT_PERIOD = 1  # per second

  API_GET_QUERIES = {}
  API_EDITABLE_FIELDS = tuple()
  API_READONLY_FIELDS = (
    'api_id',
  )

  API_MAX_LENGTH = dict()

  TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

  def get_queryset(self):
    """Prevent access to the db from within RemoteManager."""
    return super().get_queryset().none()

  def connect(self) -> None:
    from django_ctct.models import Token

    token = Token.remote.get()
    self.session = requests.Session()
    self.session.headers.update({
      'Authorization': f"{token.token_type} {token.access_token}"
    })

  def serialize(
    self,
    obj: Model,
    field_types: Literal['editable', 'readonly', 'all'] = 'editable',
  ) -> dict:
    """Convert from Django object to API request body."""

    data = {}

    field_names = {
      'editable': self.API_EDITABLE_FIELDS,
      'readonly': self.API_READONLY_FIELDS,
      'all': self.API_EDITABLE_FIELDS + self.API_READONLY_FIELDS,
    }[field_types]

    for field_name in field_names:
      try:
        value = getattr(obj, field_name, None)
      except ValueError as e:
        if obj._meta.get_field(field_name).many_to_many and (obj.pk is None):
          # Can't access related field when obj.pk is None
          continue
        else:
          raise e

      if value is None:
        # Don't include null values
        continue
      elif isinstance(value, UUID):
        # Convert UUID to string
        value = str(value)
      elif isinstance(value, dt.datetime):
        # Convert datetime to string
        value = value.strftime(self.TS_FORMAT)

      # The field determines how the value is serialized
      try:
        field = self.model._meta.get_field(field_name)
      except FieldDoesNotExist:
        # The API field was defined as a @property
        data[field_name] = value
        continue

      if field_name == 'api_id':
        field_name = self.model.remote.API_ID_LABEL
      elif field_name.endswith('_id'):
        api_id = getattr(obj, field_name[:-3]).api_id
        value = str(api_id)
      elif field.many_to_many:
        if obj.pk:
          qs = getattr(obj, field_name).values_list('api_id', flat=True)
          value = list(map(str, qs))
        else:
          value = []
      elif field.one_to_many:
        if obj.pk:
          if not hasattr(field.related_model, 'remote'):
            continue
          serialize = partial(
            field.related_model.remote.serialize,
            field_types=field_types,
          )
          value = [serialize(_) for _ in getattr(obj, field_name).all()]
        else:
          value = []
      elif field.one_to_one or field.many_to_one:
        if not hasattr(field.related_model, 'remote'):
          continue
        serialize = partial(
          field.related_model.remote.serialize,
          field_types=field_types,
        )
        value = serialize(getattr(obj, field_name))
      data[field_name] = value

    # Allow models to override manager serialization
    if hasattr(obj, 'serialize'):
      data = obj.serialize(data)

    return data

  def deserialize(
    self,
    data: dict,
    pk: Optional[int] = None,
  ) -> (Model, dict):
    """Convert from API response body to Django object."""

    if not isinstance(data, dict):
      message = _(
        f"Expected a {type({})}, got {type(data)}."
      )
      raise ValueError(message)
    else:
      data = data.copy()

    if hasattr(self, 'API_ID_LABEL'):
      data['api_id'] = data.pop(self.API_ID_LABEL)

    # Clean field values, must be done before field restriction
    model_fields = self.model._meta.get_fields()
    for field in model_fields:
      if clean := getattr(self.model, f'clean_remote_{field.name}', None):
        if (value := clean(data)) is not None:
          data[field.name] = value

    # Set related objects
    data = self.deserialize_related_obj_fields(data, parent_pk=pk)
    data, related_objs = self.deserialize_related_objs_fields(data, parent_pk=pk)  # noqa: E501

    # Restrict to the fields defined in the Django object
    # NOTE: We prefer `field.attname` over `field.name` in order to pick up
    # ForeignKeys and OneToOneFields
    data = {
      k: v for k, v in data.items()
      if k in [getattr(f, 'attname', f.name) for f in model_fields]
    }

    if pk:
      # Preserve unrelated fields (e.g. EmailCampaign.send_preview)
      obj = self.model.objects.get(pk=pk)
      for field_attname, value in data.items():
        setattr(obj, field_attname, value)
    else:
      # Instatiate new object
      obj = self.model(**data)

    return obj, related_objs

  def deserialize_related_obj_fields(
    self,
    data: dict,
    parent_pk: Optional[int] = None
  ) -> dict:
    """Deserialize ForeignKeys and OneToOneFields.

    Notes
    -----
    These fields can be set using `field.attname`, so we don't need to return a
    `related_objs` dictionary like we do with ManyToManyFields and
    ReverseForeignKeys.

    """

    if parent_pk:
      otos, _, fks, _ = get_related_fields(self.model)
      for field in filter(lambda f: f.attname in data, otos + fks):
        data[field.attname] = parent_pk
    return data

  def deserialize_related_objs_fields(
    self,
    data: dict,
    parent_pk: Optional[int] = None,
  ) -> (dict, dict):
    """Deserialize ManyToManyFields and ReverseForeignKeys."""

    related_objs = {}

    _, mtms, _, rfks = get_related_fields(self.model)
    for field in filter(lambda f: f.name in data, mtms + rfks):
      if related_data := data.pop(field.name):
        if all(isinstance(_, dict) for _ in related_data):
          # Add in the parent object's pk
          parent = {f'{field.remote_field.name}_id': parent_pk}
          deserialize = field.related_model.remote.deserialize
          objs = [deserialize(datum | parent)[0] for datum in related_data]
          related_objs[field.related_model] = objs
        elif all(isinstance(_, str) for _ in related_data):
          # ManyToManyField, make a list of "through model" instances
          # TODO: GH #12
          # NOTE: This relies on old code where API ids were used
          ThroughModel = getattr(self.model, field.name).through
          model_attname = f'{field.model._meta.model_name}_id'
          other_attname = f'{field.related_model._meta.model_name}_id'
          objs = [
            ThroughModel(**{
              model_attname: data['api_id'],
              other_attname: related_obj_api_id,
            })
            for related_obj_api_id in related_data
          ]
          related_objs[ThroughModel] = objs

    return data, related_objs

  @sleep_and_retry
  @limits(calls=API_LIMIT_CALLS, period=API_LIMIT_PERIOD)
  def check_api_limit(self) -> None:
    """Honor the API's rate limit."""
    pass

  # @task(queue_name='ctct')
  def create(self, obj: Model) -> Model:
    """Creates an existing Django object on the remote server.

    Notes
    -----
    This method saves the API's response to the local database in order to
    preserve values calculated by the API (e.g. API_READONLY_FIELDS).

    """

    if not (pk := obj.pk):
      raise ValueError('Must create object locally first.')

    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data, pk=pk)

    # Overwrite local obj with CTCT's response
    # NOTE: We don't need to do anything with `related_objs` since they were
    # set locally before the API request
    with mute_signals(signals.post_save):
      obj.save()

    return obj

  def get(self, api_id: str) -> (Model, dict):
    """Gets an existing object from the remote server.

    Notes
    -----
    This method will not save the object to the local database. We return the
    object as well as a dictionary of the form {field_name: [RelatedModel()]}.

    """

    self.check_api_limit()

    response = self.session.get(
      url=self.get_url(api_id),
      params=self.API_GET_QUERIES,
    )

    try:
      data = self.raise_or_json(response)
    except Http404:
      raise self.model.DoesNotExist(api_id)
    else:
      obj, related_objs = self.deserialize(data)

    return obj, related_objs

  def all(self, endpoint: Optional[str] = None) -> list[(Model, dict)]:
    """Gets all existing objects from the remote server.

    Notes
    -----
    This method will not save the object to the local database. We return
    a list of (obj, {field_name: [RelatedModel()]}) tuples.

    """

    objs = []

    paginated = True
    while paginated:
      self.check_api_limit()

      response = self.session.get(
        url=self.get_url(endpoint=endpoint),
        params=self.API_GET_QUERIES,
      )
      data = self.raise_or_json(response)

      # Data only contains two keys: '_links' and e.g. 'lists' or 'contacts'
      links = data.pop('_links', None)
      data = next(iter(data.values()))
      objs.extend(map(self.deserialize, data))

      try:
        endpoint = links.get('next').get('href')
      except AttributeError:
        paginated = False

    return objs

  # @task(queue_name='ctct')
  def update(self, obj: Model) -> Model:
    """Updates an existing Django object on the remote server.

    Notes
    -----
    This method saves the API's response to the local database in order to
    preserve values calculated by the API.

    """

    if not (pk := obj.pk):
      raise ValueError('Must create object locally first.')
    elif obj.api_id is None:
      raise ValueError('Must create object remotely first.')

    self.check_api_limit()
    response = self.session.put(
      url=self.get_url(obj.api_id),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data, pk=pk)

    # Overwrite local obj with CTCT's response
    # NOTE: We don't need to do anything with `related_objs` since they were
    # set locally before the API request
    with mute_signals(signals.post_save):
      obj.save()

    return obj

  # @task(queue_name='ctct')
  def delete(
    self,
    obj: Model,
    endpoint_suffix: Optional[str] = None,
  ) -> None:
    """Deletes existing Django object(s) on the remote server.

    Notes
    -----
    This method can be used to delete sub-resources of an object (such as a
    scheduled EmailCampaign) via the optional `endpoint_suffix` param.

    We ignore 404 responses in the situation that the remote object has already
    been deleted.

    """

    url = self.get_url(obj.api_id, endpoint_suffix=endpoint_suffix)
    self.check_api_limit()
    response = self.session.delete(url)

    if response.status_code != 404:
      # Allow 404
      self.raise_or_json(response)

  def bulk_delete(self, objs: list[Model]) -> None:
    """Deletes multiple objects from remote server in batches."""

    try:
      api_max_ids = self.API_ENDPOINT_BULK_LIMIT
      endpoint = self.API_ENDPOINT_BULK_DELETE
    except AttributeError:
      name = self.model.__name__
      message = _(
        f"ConstantContact does not support bulk deletion of {name}."
      )
      raise NotImplementedError(message)

    # Prepare connection and payloads
    self.connect()
    api_id_label = self.model.remote.API_ID_LABEL + 's'
    api_ids = [str(o.api_id) for o in objs]

    # Remote delete in batches
    for i in range(0, len(api_ids), api_max_ids):
      self.check_api_limit()
      response = self.session.post(
        url=self.get_url(endpoint=endpoint),
        json={api_id_label: api_ids[i:i + api_max_ids]},
      )
      self.raise_or_json(response)


class ContactListRemoteManager(RemoteManager):
  """Extend RemoteManager to handle adding multiple Contacts."""

  API_ENDPOINT = '/contact_lists'
  API_ENDPOINT_BULK_DELETE = '/activities/list_delete'
  API_ENDPOINT_BULK_LIMIT = 100
  API_ID_LABEL = 'list_id'
  API_EDITABLE_FIELDS = (
    'name',
    'description',
    'favorite',
  )
  API_READONLY_FIELDS = (
    'api_id',
    'created_at',
    'updated_at',
  )
  API_MAX_LENGTH = {
    'name': 255,
  }

  # @task(queue_name='ctct')
  def add_list_memberships(
    self,
    contact_list: Optional[ContactList] = None,
    contact_lists: Optional[QuerySet[ContactList]] = None,
    contacts: Optional[QuerySet[Contact]] = None,
  ) -> None:
    """Adds multiple Contacts to (multiple) ContactLists."""

    API_MAX_CONTACTS = 500

    if contact_list is not None:
      list_ids = [contact_list.api_id]
    else:
      list_ids = list(map(str, contact_lists.values_list('api_id', flat=True)))

    if contacts is not None:
      contact_ids = list(map(str, contacts.values_list('api_id', flat=True)))
    else:
      message = _(
        "Must pass a QuerySet of Contacts."
      )
      raise ValueError(message)

    for i in range(0, len(contact_ids), API_MAX_CONTACTS):
      self.check_api_limit()
      response = self.session.post(
        url=self.get_url(endpoint='/activities/add_list_memberships'),
        json={
          'source': {'contact_ids': contact_ids[i:i + API_MAX_CONTACTS]},
          'list_ids': list_ids,
        },
      )
      self.raise_or_json(response)


class CustomFieldRemoteManager(RemoteManager):
  """Extend RemoteManager to handle CustomFields."""

  API_ENDPOINT = '/contact_custom_fields'
  API_ENDPOINT_BULK_DELETE = '/activities/custom_fields_delete'
  API_ENDPOINT_BULK_LIMIT = 100
  API_ID_LABEL = 'custom_field_id'
  API_EDITABLE_FIELDS = (
    'label',
    'type',
  )
  API_READONLY_FIELDS = (
    'api_id',
    'name',
    'created_at',
    'updated_at',
  )
  API_MAX_LENGTH = {
    'label': 50,
    'name': 50,
  }


class ContactRemoteManager(RemoteManager):
  """Extend RemoteManager to handle Contacts."""

  API_ENDPOINT = '/contacts'
  API_ENDPOINT_BULK_DELETE = '/activities/contact_delete'
  API_ENDPOINT_BULK_LIMIT = 500
  API_ID_LABEL = 'contact_id'

  API_EDITABLE_FIELDS = (
    'email',
    'first_name',
    'last_name',
    'job_title',
    'company_name',
    'phone_numbers',
    'street_addresses',
    'custom_fields',
    'list_memberships',
    'notes',
  )
  API_READONLY_FIELDS = (
    'api_id',
    'created_at',
    'updated_at',
    'opt_out_source',
    'opt_out_date',
    'opt_out_reason',
  )
  API_GET_QUERIES = {
    'include': ','.join([
      'custom_fields',
      'list_memberships',
      'notes',
      'phone_numbers',
      'street_addresses',
    ]),
  }

  API_MAX_LENGTH = {
    'first_name': 50,
    'last_name': 50,
    'job_title': 50,
    'company_name': 50,
    'opt_out_reason': 255,
  }
  API_MAX_NOTES = 150
  API_MAX_PHONE_NUMBERS = 3
  API_MAX_STREET_ADDRESSES = 3
  API_MAX_CUSTOM_FIELDS = 25
  API_MAX_LIST_MEMBERSHIPS = 50

  def create(self, obj: Contact) -> Contact:
    try:
      response = super().create(obj)
    except HTTPError as e:
      if e.response.status_code == 409:
        # Locate the resource via email address and update
        response = self.update_or_create(obj)
      else:
        raise e
    return response

  # @task(queue_name='ctct')
  def update(self, obj: Contact) -> Optional[Contact]:
    """Update Contact and ContactList membership on CTCT servers.

    Notes
    -----
    The PUT call will overwrite all properties not included in the request
    body with NULL, so we need to make sure the `serialize()` method
    includes all important fields. While the `create_or_update()` method
    supports partial updates, it won't allow us to remove a ContactList.

    """
    return super().update(obj)

  def update_or_create(self, obj: Contact) -> Contact:
    """Updates or creates the Contact based on `email`.

    Notes
    -----

    The '/sign_up_form' endpoint will allow us to do a "update or create"
    request, based on the email address of the Contact. This can be useful
    when creating Contacts that may already exist in ConstantContact's
    database, even if they've been "deleted" before.

    Updates to existing contacts are partial updates. This endpoint only
    updates the fields that are included in the request body. Updates append
    new contact lists or custom fields to the existing `list_memberships` or
    `custom_fields` arrays.

    """

    if not obj.pk:
      raise ValueError('Must create object locally first.')

    # This endpoint expects a slightly different serialization
    data = self.serialize(obj)
    data['email_address'] = data.pop('email_address')['address']

    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(endpoint_suffix='/sign_up_form'),
      json=data,
    )
    data = self.raise_or_json(response)

    # CTCT doesn't return a full object at this endpoint
    _, api_id = data.pop('action'), data.pop('contact_id')
    if data:
      raise ValueError(f'Unexpected response data: {data}.')

    # Save the API id
    with mute_signals(signals.post_save):
      obj.api_id = api_id
      obj.save(update_fields=['api_id'])


class ContactNoteRemoteManager(RemoteManager):
  """Extend RemoteManager to handle ContactNotes."""

  API_ID_LABEL = 'note_id'
  API_EDITABLE_FIELDS = (
    'content',
  )
  API_MAX_LENGTH = {
    'content': 2000,
  }


class ContactPhoneNumberRemoteManager(RemoteManager):
  """Extend RemoteManager to handle ContactPhoneNumbers."""

  API_ID_LABEL = 'phone_number_id'
  API_EDITABLE_FIELDS = (
    'kind',
    'phone_number',
  )


class ContactStreetAddressRemoteManager(RemoteManager):
  """Extend RemoteManager to handle ContactStreetAddresses."""

  API_ID_LABEL = 'street_address_id'
  API_EDITABLE_FIELDS = (
    'kind',
    'street',
    'city',
    'state',
    'postal_code',
    'country',
  )
  API_MAX_LENGTH = {
    'street': 255,
    'city': 50,
    'state': 50,
    'postal_code': 50,
    'country': 50,
  }


class ContactCustomFieldRemoteManager(RemoteManager):
  """Extend RemoteManager to handle ContactCustomFields."""

  API_EDITABLE_FIELDS = (
    'custom_field_id',
    'value',
  )
  API_MAX_LENGTH = {
    'value': 255,
  }


class EmailCampaignRemoteManager(RemoteManager):
  """Extend RemoteManager to handle creating EmailCampaigns."""

  API_ENDPOINT = '/emails'
  API_ID_LABEL = 'campaign_id'
  API_EDITABLE_FIELDS = (
    'name',
    'scheduled_datetime',
  )
  API_READONLY_FIELDS = (
    'api_id',
    'current_status',
    'campaign_activities',
    'created_at',
    'updated_at',
  )
  API_MAX_LENGTH = {
    'name': 80,  # TODO: GH #2
  }

  def serialize(
    self,
    obj: Model,
    field_types: Literal['editable', 'readonly', 'all'] = 'editable',
  ) -> dict:
    if obj.api_id and (field_types == 'editable'):
      # The only field that the API will update
      data = {'name': obj.name}
    else:
      data = super().serialize(obj, field_types)
    return data

  # @task(queue_name='ctct')
  def create(self, obj: EmailCampaign) -> EmailCampaign:
    """Creates a local EmailCampaign on the remote servers.

    Notes
    -----
    This method will also create the new `primary_email` and `permalink`
    CampaignActivities on CTCT and associate the `primary_email` one
    with the new EmailCampaign in the database.

    """

    from django_ctct.models import CampaignActivity

    # Validate
    if not (pk := obj.pk):
      raise ValueError('Must create object locally first.')
    try:
      activity = obj.campaign_activities.get(role='primary_email')
    except CampaignActivity.DoesNotExist:
      # The request body must contain the following fields:
      #   'format_type', 'from_name', 'from_email', 'reply_to_email',
      #   'subject', and 'html_content'.
      #
      # Our CampaignActivity model provides default values for these fields,
      # so we can simply serialize an "empty" object
      activity = CampaignActivity()

    # Create EmailCampaign and CampaignActivity remotely
    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(),
      json={
        'name': obj.name,
        'email_campaign_activities': [
          CampaignActivity.remote.serialize(activity),
        ],
      },
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data, pk=pk)

    # Get the activity's api_id that CTCT assigned
    for related_obj in related_objs[CampaignActivity]:
      if related_obj.role == 'primary_email':
        activity.api_id = related_obj.api_id
        break

    # Overwrite local obj with CTCT's response
    with mute_signals(signals.post_save):
      obj.save()
      if activity.pk is None:
        activity.campaign = obj
        activity.save()
      else:
        activity.save(update_fields=['api_id'])

    # Send preview and/or schedule the campaign
    if obj.send_preview or (obj.scheduled_datetime is not None):
      CampaignActivity.remote.connect()
      CampaignActivity.remote.update(activity)

    return obj

  # @task(queue_name='ctct')
  def update(self, obj: EmailCampaign) -> EmailCampaign:
    """Update EmailCampaign on remote servers.

    Notes
    -----
    The only field that can be (remotely) updated this way is the `name` field.
    In order to change when the campaign is scheduled to be sent or send a
    preview, the `primary_email` CampaignActivity must be updated remotely.

    """
    if not (pk := obj.pk):
      raise ValueError('Must create object locally first.')
    elif obj.api_id is None:
      raise ValueError('Must create object remotely first.')

    self.check_api_limit()
    response = self.session.patch(
      url=self.get_url(obj.api_id),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data, pk=pk)

    # Overwrite local obj with CTCT's response
    # NOTE: We don't need to do anything with `related_objs` since they were
    # set locally before the API request
    with mute_signals(signals.post_save):
      obj.save()

    return obj


class CampaignActivityRemoteManager(RemoteManager):
  """Extend RemoteManager to handle scheduling."""

  API_ENDPOINT = '/emails/activities'
  API_ID_LABEL = 'campaign_activity_id'
  API_EDITABLE_FIELDS = (
    'from_name',
    'from_email',
    'reply_to_email',
    'subject',
    'preheader',
    'html_content',
    'contact_lists',
    'format_type',                  # Must include in request
    'physical_address_in_footer',   # Must include in request
  )
  API_READONLY_FIELDS = (
    'api_id',
    'role',
    'current_status',
  )
  API_MAX_LENGTH = {
    'from_name': 100,
    'from_email': 80,
    'reply_to_email': 80,
    'subject': 200,
    'preheader': 250,
    'html_content': int(15e4),
  }
  API_GET_QUERIES = {
    'include': ','.join([
      # 'physical_address_in_footer',
      # 'permalink_url',
      'html_content',
      # 'document_properties',
    ]),
  }

  # @task(queue_name='ctct')
  def create(self, obj: CampaignActivity) -> NoReturn:
    message = _(
      "ConstantContact API does not support creating CampaignActivities. "
      "They are created during the creation of an EmailCampaign."
    )
    raise NotImplementedError(message)

  # @task(queue_name='ctct')
  def update(self, obj: Model) -> Model:
    """Update CampaignActivity on remote servers.

    Notes
    -----
    CampaignActivities can only be updated if their associated EmailCampaign
    is in DRAFT or SENT status. If the EmailCampaign is already scheduled,
    we make an API call to unschedule it and then re-schedule it after
    updates were made. If you wish to send a new preview out after the activity
    has been updated, you can set `send_preview = True`.

    """

    if obj.role != 'primary_email':
      message = _(
        f"CampaignActivity with role `{obj.role}` not supported yet."
      )
      raise NotImplementedError(message)

    if was_scheduled := (obj.campaign.current_status == 'SCHEDULED'):
      self.unschedule(obj)

    obj = super().update(obj)

    if obj.campaign.send_preview:
      self.send_preview(obj)

    if was_scheduled or (obj.campaign.scheduled_datetime is not None):
      self.schedule(obj)

    return obj

  # @task(queue_name='ctct')
  def send_preview(
    self,
    obj: CampaignActivity,
    recipients: Optional[list[str]] = None,
    message: Optional[str] = None,
  ) -> None:
    """Sends a preview of the EmailCampaign."""

    if recipients is None:
      recipients = getattr(settings, 'CTCT_PREVIEW_RECIPIENTS', settings.MANAGERS)  # noqa: 501
      recipients = [email for (name, email) in recipients]

    if message is None:
      message = getattr(settings, 'CTCT_PREVIEW_MESSAGE', '')

    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(obj.api_id, endpoint_suffix='/tests'),
      json={
        'email_addresses': recipients,
        'personal_message': message,
      },
    )
    self.raise_or_json(response)

  # @task(queue_name='ctct')
  def schedule(self, obj: CampaignActivity) -> None:
    """Schedules the `primary_email` CampaignActivity.

    Notes
    -----
    Recipients must be set before scheduling; if recipients have already been
    set, this can be skipped by setting `update_first=False`.

    """

    # Validate role, scheduled_datetime, and contact_lists
    if obj.role != 'primary_email':
      message = _(
        f"Cannot schedule CampaignActivities with role '{obj.role}'."
      )
      raise ValueError(message)

    if obj.campaign.scheduled_datetime is None:
      message = _(
        "Must specify `scheduled_datetime`."
      )
      raise ValueError(message)

    if not obj.contact_lists.exists():
      message = _(
        "Must specify `contact_lists`."
      )
      raise ValueError(message)

    # Schedule the CampaignActivity
    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(obj.api_id, endpoint_suffix='/schedules'),
      json={'scheduled_date': obj.campaign.scheduled_datetime.isoformat()},
    )
    self.raise_or_json(response)

  # @task(queue_name='ctct')
  def unschedule(self, obj: CampaignActivity) -> None:
    """Unschedules the `primary_email` CampaignActivity."""
    if obj.role == 'primary_email':
      self.delete(obj, endpoint_suffix='/schedules')
    else:
      message = _(
        f"Cannot unschedule CampaignActivities with role '{obj.role}'."
      )
      raise ValueError(message)


class CampaignSummaryRemoteManager(RemoteManager):
  """Extend RemoteManager to handle creating EmailCampaignSummarys."""

  API_ENDPOINT = '/reports/summary_reports/email_campaign_summaries'
  API_READONLY_FIELDS = (
    'campaign_id',
    'sends',
    'opens',
    'clicks',
    'forwards',
    'optouts',
    'abuse',
    'bounces',
    'not_opened',
  )

  def serialize(
    self,
    obj: Model,
    field_types: Literal['editable', 'readonly', 'all'] = 'editable',
  ) -> dict:
    data = super().serialize(obj, field_types)
    data['unique_counts'] = {
      stat_field: data.pop(stat_field)
      for stat_field in self.API_READONLY_FIELDS[1:]
    }
    return data
