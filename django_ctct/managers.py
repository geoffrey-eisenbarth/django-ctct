from __future__ import annotations

import datetime as dt
from typing import (
  TYPE_CHECKING, TypeVar, ClassVar,
  Iterable, Literal, NoReturn, Union, cast,
)
from urllib.parse import urlencode
from uuid import UUID

from jwt import ExpiredSignatureError
from ratelimit import limits, sleep_and_retry
import requests
from requests.exceptions import HTTPError
from requests.models import Response

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models.manager import Manager
from django.db.models.query import QuerySet
from django.http import HttpRequest, Http404
from django.middleware.csrf import get_token as get_csrf_token
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from django_ctct.utils import get_related_fields


if TYPE_CHECKING:
  from django_ctct.models import (
    JsonDict, RelatedObjects,
    EndpointMixin, SerialModel, CTCTModel, CTCTEndpointModel,
    Token, ContactList, Contact,
    EmailCampaign, CampaignActivity, CampaignSummary,
  )

T = TypeVar('T', bound='EndpointMixin')
E = TypeVar('E', bound='CTCTEndpointModel')
C = TypeVar('C', bound='CTCTModel')
S = TypeVar('S', bound='SerialModel')


class ConnectionManagerMixin(Manager[T]):
  """Manager mixin for utilizing an API."""

  API_LIMIT_CALLS: int = 4   # four calls
  API_LIMIT_PERIOD: int = 1  # per second

  session: requests.Session

  def connect(self) -> None:
    if not hasattr(self, 'session'):
      from django_ctct.models import Token

      token = Token.remote.get()
      self.session = requests.Session()
      self.session.headers.update({
        'Authorization': f"{token.token_type} {token.access_token}"
      })

  @sleep_and_retry
  @limits(calls=API_LIMIT_CALLS, period=API_LIMIT_PERIOD)
  def check_api_limit(self) -> None:
    """Honor the API's rate limit."""
    pass

  def _pre_api_call(self) -> None:
    self.connect()
    self.check_api_limit()

  def get_url(
    self,
    api_id: str | UUID | None = None,
    endpoint: str | None = None,
    endpoint_suffix: str | None = None,
  ) -> str:
    endpoint = endpoint or self.model.API_ENDPOINT
    if not endpoint.startswith(self.model.API_VERSION):
      endpoint = f'{self.model.API_VERSION}{endpoint}'

    url = f'{self.model.API_URL}{endpoint}'

    if api_id:
      url += f'/{api_id}'

    if endpoint_suffix:
      url += f'{endpoint_suffix}'

    return url

  def raise_or_json(self, response: Response) -> JsonDict:
    if response.status_code == 204:
      data = {}
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


class TokenRemoteManager(ConnectionManagerMixin['Token'], Manager['Token']):
  """Manager for utilizing CTCT's Auth Token API."""

  def get_auth_url(self, request: HttpRequest) -> str:
    """Returns a URL for logging into CTCT.com to grant permissions."""
    endpoint = self.get_url(endpoint='/authorize')
    data = {
      'client_id': settings.CTCT_PUBLIC_KEY,
      'redirect_uri': settings.CTCT_REDIRECT_URI,
      'response_type': 'code',
      'state': get_csrf_token(request),
      'scope': self.model.API_SCOPE,
    }
    url = f"{endpoint}?{urlencode(data, safe='+')}"
    return url

  def connect(self) -> None:
    if not hasattr(self, 'session'):
      self.session = requests.Session()
      self.session.auth = (settings.CTCT_PUBLIC_KEY, settings.CTCT_SECRET_KEY)

  def create(self, auth_code: str) -> 'Token':  # type: ignore[override]
    """Creates the initial Token using an `auth_code` from CTCT.

    Notes
    -----
    The value of CTCT_REDIRECT_URI must exactly match the value
    specified in the developer's page on constantcontact.com.

    """

    self.connect()
    response = self.session.post(
      url=self.get_url(),
      data={
        'code': auth_code,
        'redirect_uri': settings.CTCT_REDIRECT_URI,
        'grant_type': 'authorization_code',
      },
    )
    data = self.raise_or_json(response)
    token = self.model.objects.create(**data)
    return token

  def get(self) -> 'Token':
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
      token = self.update(token)

    return token

  def update(self, token: 'Token') -> 'Token':  # type: ignore[override]
    """Obtain a new Token from CTCT using the refresh code."""

    self.connect()
    response = self.session.post(
      url=self.get_url(),
      data={
        'refresh_token': token.refresh_token,
        'grant_type': 'refresh_token',
      },
    )
    data = self.raise_or_json(response)
    token = self.model.objects.create(**data)
    return token


class Serializer(Manager[S]):

  TS_FORMAT: ClassVar[str] = '%Y-%m-%dT%H:%M:%SZ'

  def serialize(
    self,
    obj: S,
    field_types: Literal['editable', 'readonly', 'all'] = 'editable',
  ) -> JsonDict:
    """Convert from Django object to API request body."""

    data: JsonDict = {}

    field_names = {
      'editable': self.model.API_EDITABLE_FIELDS,
      'readonly': self.model.API_READONLY_FIELDS,
      'all': self.model.API_EDITABLE_FIELDS + self.model.API_READONLY_FIELDS,
    }[field_types]

    for field_name in field_names:
      try:
        value = getattr(obj, field_name, None)
      except ValueError as e:
        if obj.pk is None:
          # Object needs pk before relationship can be used
          continue
        else:
          raise e

      if value is None:
        # Don't include null values
        continue
      if field_name == 'api_id':
        # Use API_ID_LABEL and convert UUID to string
        # NOTE: For CampaignSummaries, we rely on the fact that `campaign_id`
        #       happens to be the OneToOneField's attname and a value that
        #       the CTCT API accepts (aka API_ID_LABEL).
        data[self.model.API_ID_LABEL] = str(value)
      elif isinstance(value, dt.datetime):
        # Convert datetime to string
        data[field_name] = value.strftime(self.TS_FORMAT)
      elif field_name.endswith('_id') and isinstance(value, int):
        # Convert pk to api_id
        data[field_name] = str(getattr(obj, field_name[:-3]).api_id)
      elif isinstance(value, (bool, int, str)):
        data[field_name] = value
      elif isinstance(getattr(self.model, field_name, None), property):
        # The API field was defined as a @property
        data[field_name] = value
      elif isinstance(value, models.Manager):
        if obj.pk is None:
          continue
        elif hasattr(value, 'through'):
          # ManyToManyField: only need a list of api_ids
          qs = value.values_list('api_id', flat=True)
          data[field_name] = list(map(str, qs))
        elif hasattr(value.model, 'serializer'):
          # ReverseForeignKey: serialize QuerySet
          if value.model.__name__ == 'ContactCustomField':
            # Model behaves as a through model, must get related ids
            field_types = 'all'
          qs = value.all()
          data[field_name] = [
            qs.model.serializer.serialize(o, field_types)
            for o in qs
          ]
      elif isinstance(value, models.Model):
        raise NotImplementedError
      else:
        raise NotImplementedError

    # Allow models to override manager serialization
    if hasattr(obj, 'serialize'):
      data = obj.serialize(data)

    return data

  def deserialize_related_obj_fields(
    self,
    data: JsonDict,
    parent_pk: int | None = None
  ) -> JsonDict:
    """Deserialize ForeignKeys and OneToOneFields.

    Notes
    -----
    These fields can be set using `field.attname`, so we don't need to return a
    `related_objs` dictionary like we do with ManyToManyFields and
    ReverseForeignKeys.

    """
    Field = Union[
      models.OneToOneField[models.Model],
      models.ForeignKey[models.Model],
    ]
    field: Field
    fields: list[Field]

    if parent_pk:
      otos, _, fks, _ = get_related_fields(self.model)
      fields = otos + fks
      for field in filter(lambda f: f.attname in data, fields):
        data[field.attname] = parent_pk
    return data

  def deserialize_related_objs_fields(
    self,
    data: JsonDict,
    parent_pk: int | None = None,
  ) -> tuple[JsonDict, list[RelatedObjects]]:
    """Deserialize ManyToManyFields and ReverseForeignKeys."""

    from django_ctct.models import is_model, is_serial

    related_objs: RelatedObjects
    list_of_related_objs: list[RelatedObjects] = []
    objs: list[models.Model]

    _, m2ms, _, rfks = get_related_fields(self.model)
    for rfk_field in filter(lambda f: f.name in data, rfks):
      # Reverse ForeignKeys get deserialized into model instances
      RelatedModel = rfk_field.related_model
      parent = {rfk_field.remote_field.attname: parent_pk}
      if is_serial(RelatedModel):
        objs = [
          RelatedModel.serializer.deserialize(datum | parent)[0]
          for datum in data.pop(rfk_field.name)
        ]
      else:
        continue

      if objs:
        related_objs = (RelatedModel, objs)
        list_of_related_objs.append(related_objs)

    for m2m_field in filter(lambda f: f.name in data, m2ms):
      # ManyToManyFields get deserialized into "through model" instances
      # NOTE: Contact.custom_field_set is handled by rfk of ContactCustomField
      ThroughModel = m2m_field.remote_field.through
      RelatedModel = m2m_field.related_model

      if is_model(ThroughModel) and is_model(RelatedModel):
        related_obj_pks = RelatedModel.objects.filter(  # type: ignore
          api_id__in=data.pop(m2m_field.name)
        ).values_list('pk', flat=True)

        objs = [
          ThroughModel(**{
            m2m_field.m2m_column_name(): parent_pk,  # Might be None
            m2m_field.m2m_reverse_name(): related_obj_pk,
          })
          for related_obj_pk in related_obj_pks
        ]
        if objs:
          related_objs = (ThroughModel, objs)
          list_of_related_objs.append(related_objs)

    return (data, list_of_related_objs)

  def deserialize(
    self,
    data: JsonDict,
    pk: int | None = None,
  ) -> tuple[S, list[RelatedObjects]]:
    """Convert from API response body to Django object."""

    # If API_ID_LABEL is not a model field name, it will be removed later
    data = data.copy()
    if hasattr(self.model, 'API_ID_LABEL'):
      data['api_id'] = data[self.model.API_ID_LABEL]

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
    #       ForeignKeys and OneToOneFields
    data = {
      k: v for k, v in data.items()
      if k in [getattr(f, 'attname', f.name) for f in model_fields]
    }

    # Convert any remaining API ids to Django PKs
    for k, v in data.items():
      if k.endswith('_id') and isinstance(v, str):
        RelatedModel = self.model._meta.get_field(k).related_model
        if RelatedModel is not None:
          data[k] = RelatedModel.objects.get(api_id=v).pk  # type: ignore

    if pk:
      # Preserve unrelated db fields (e.g. EmailCampaign.send_preview)
      obj = self.model.objects.get(pk=pk)
      for field_attname, value in data.items():
        setattr(obj, field_attname, value)
    else:
      # Instantiate new object
      obj = self.model(**data)

    return (obj, related_objs)


class RemoteManager(
  ConnectionManagerMixin[E],
  Serializer[E],
  Manager[E],
):
  """Manager for utilizing the CTCT API."""

  # @task(queue_name='ctct')
  def create(self, obj: E) -> E:  # type: ignore[override]
    """Creates an existing Django object on the remote server.

    Notes
    -----
    This method saves the API's response to the local database in order to
    preserve values calculated by the API (e.g. API_READONLY_FIELDS).

    """

    if not (pk := obj.pk):
      raise ValueError('Must create object locally first.')

    self._pre_api_call()
    response = self.session.post(
      url=self.get_url(),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    # NOTE: We don't need to do anything with `related_objs` since they were
    #       set locally before the API request.
    obj, _ = self.deserialize(data, pk=pk)

    # Overwrite local obj with CTCT's response
    obj.save()

    return obj

  def get(  # type: ignore[override]
    self,
    api_id: str | UUID,
  ) -> tuple[E, list[RelatedObjects]]:
    """Gets an existing object from the remote server.

    Notes
    -----
    This method will not save the object to the local database. We return the
    object as well as a dictionary of the form {field_name: [RelatedModel()]}.

    """

    self._pre_api_call()

    response = self.session.get(
      url=self.get_url(api_id),
      params=self.model.API_GET_QUERIES,
    )

    try:
      data = self.raise_or_json(response)
    except Http404:
      raise self.model.DoesNotExist(api_id)

    obj, list_of_related_objs = self.deserialize(data)
    return obj, list_of_related_objs

  def all(  # type: ignore[override]
    self,
    endpoint: str | None = None,
  ) -> list[tuple[E, list[RelatedObjects]]]:
    """Gets all existing objects from the remote server.

    Notes
    -----
    This method will not save the object to the local database.

    """

    list_of_tuples: list[tuple[E, list[RelatedObjects]]] = []

    paginated = True
    while paginated:
      self._pre_api_call()

      response = self.session.get(
        url=self.get_url(endpoint=endpoint),
        params=self.model.API_GET_QUERIES,
      )
      metadata = self.raise_or_json(response)

      # Data contains up to two keys: '_links' and e.g. 'lists' or 'contacts'
      links = metadata.pop('_links', None)
      data = next(iter(metadata.values()))
      list_of_tuples += map(self.deserialize, data)

      if links:
        endpoint = links['next']['href']
      else:
        paginated = False

    return list_of_tuples

  # @task(queue_name='ctct')
  def update(self, obj: E) -> E:  # type: ignore[override]
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

    self._pre_api_call()
    response = self.session.put(
      url=self.get_url(obj.api_id),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    # NOTE: We don't need to do anything with `related_objs` since they were
    #       set locally before the API request.
    obj, _ = self.deserialize(data, pk=pk)

    # Overwrite local obj with CTCT's response
    obj.save()

    return obj

  # @task(queue_name='ctct')
  def delete(
    self,
    obj: E,
    endpoint_suffix: str | None = None,
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
    self._pre_api_call()
    response = self.session.delete(url)

    if response.status_code != 404:
      # Allow 404
      self.raise_or_json(response)

  def bulk_delete(self, objs: Iterable[E]) -> None:
    """Deletes multiple objects from remote server in batches."""

    if self.model.API_ENDPOINT_BULK_DELETE is None:
      name = self.model.__name__
      message = _(
        f"ConstantContact does not support bulk deletion of {name}."
      )
      raise NotImplementedError(message)
    elif self.model.API_ENDPOINT_BULK_LIMIT is None:
      name = self.model.__name__
      message = _(
        f"No API limit specified for {name}."
      )
      raise ImproperlyConfigured(message)

    # Prepare connection and payloads
    self.connect()
    api_id_label = self.model.API_ID_LABEL + 's'
    api_ids = [str(o.api_id) for o in objs]

    # Remote delete in batches
    for i in range(0, len(api_ids), self.model.API_ENDPOINT_BULK_LIMIT):
      self._pre_api_call()
      response = self.session.post(
        url=self.get_url(endpoint=self.model.API_ENDPOINT_BULK_DELETE),
        json={api_id_label: api_ids[i:i + self.model.API_ENDPOINT_BULK_LIMIT]},
      )
      self.raise_or_json(response)


class ContactListRemoteManager(RemoteManager['ContactList']):
  """Extend RemoteManager to handle adding multiple Contacts."""

  # @task(queue_name='ctct')
  def add_list_memberships(
    self,
    contact_list: ContactList | None = None,
    contact_lists: QuerySet[ContactList] | None = None,
    contacts: QuerySet[Contact] | None = None,
  ) -> None:
    """Adds multiple Contacts to (multiple) ContactLists."""

    from django_ctct.models import is_ctct

    Contact = self.model._meta.get_field('members').related_model  # type: ignore # noqa: E501
    if is_ctct(Contact) and hasattr(Contact, 'API_ENDPOINT_BULK_LIMIT'):
      step_size = Contact.API_ENDPOINT_BULK_LIMIT
    else:
      message = _("Contact must specify 'API_ENDPOINT_BULK_LIMIT'.")
      raise ImproperlyConfigured(message)

    if contact_list is not None:
      list_ids = [str(contact_list.api_id)]
    elif contact_lists is not None:
      list_ids = list(map(str, contact_lists.values_list('api_id', flat=True)))
    else:
      message = _(
        "Must pass either `contact_list` or `contact_lists`."
      )
      raise ValueError(message)

    if contacts is not None:
      contact_ids = list(map(str, contacts.values_list('api_id', flat=True)))
    else:
      message = _(
        "Must pass a QuerySet of Contacts."
      )
      raise ValueError(message)

    for i in range(0, len(contact_ids), step_size):
      self._pre_api_call()
      response = self.session.post(
        url=self.get_url(endpoint='/activities/add_list_memberships'),
        json={
          'source': {'contact_ids': contact_ids[i:i + step_size]},
          'list_ids': list_ids,
        },
      )
      self.raise_or_json(response)


class ContactRemoteManager(RemoteManager['Contact']):
  """Extend RemoteManager to handle Contacts."""

  def create(self, obj: 'Contact') -> 'Contact':  # type: ignore[override]
    try:
      obj = super().create(obj)
    except HTTPError as e:
      if e.response.status_code == 409:
        # Locate the resource via email address and update
        obj = self.update_or_create(obj)
      else:
        raise e
    return obj

  def update_or_create(self, obj: 'Contact') -> 'Contact':  # type: ignore[override]  # noqa: E501
    """Updates or creates the Contact based on `email`.

    Notes
    -----

    The '/sign_up_form' endpoint will allow us to do a "update or create"
    request, based on the email address of the Contact. This can be useful
    when creating Contacts that may already exist in ConstantContact's
    database, even if they've been "deleted" before.

    Updates to existing contacts are partial updates and this endpoint will
    only update the fields that are included in the request body. Updates
    append new contact lists or custom fields the existing `list_memberships`
    or `custom_fields` arrays. As a result, we cannot use this endpoint to
    remove a list from `list_memberships`, but must use `.update()`.

    The PUT call (e.g. just using update()) will overwrite all properties not
    included in the request body with NULL, so the `serialize()` method must
    includes all important fields.

    """

    if not obj.pk:
      raise ValueError('Must create object locally first.')

    # This endpoint expects a slightly different serialization
    data = self.serialize(obj)
    data['email_address'] = data.pop('email_address')['address']

    self._pre_api_call()
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
    obj.api_id = api_id
    obj.save(update_fields=['api_id'])

    return obj


class EmailCampaignRemoteManager(RemoteManager['EmailCampaign']):
  """Extend RemoteManager to handle creating EmailCampaigns."""

  def serialize(
    self,
    obj: 'EmailCampaign',
    field_types: Literal['editable', 'readonly', 'all'] = 'editable',
  ) -> JsonDict:
    if obj.api_id and (field_types == 'editable'):
      # The only field that the API will update
      data: JsonDict = {'name': obj.name}
    else:
      data = super().serialize(obj, field_types)
    return data

  # @task(queue_name='ctct')
  def create(self, obj: 'EmailCampaign') -> 'EmailCampaign':  # type: ignore[override]  # noqa: E501
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
    self._pre_api_call()
    response = self.session.post(
      url=self.get_url(),
      json={
        'name': obj.name,
        'email_campaign_activities': [
          CampaignActivity.serializer.serialize(activity),
        ],
      },
    )
    data = self.raise_or_json(response)

    obj, list_of_related_objs = self.deserialize(data, pk=pk)

    # Set CTCT's assigned api_id on our local CampaignActivity instance
    for (model, related_objs) in list_of_related_objs:
      if (model is CampaignActivity):
        # NOTE: All lists are invariant, so they won't remember that
        #       `related_objs` is a list[CampaignActivity].
        for related_obj in cast(list[CampaignActivity], related_objs):
          if related_obj.role == 'primary_email':
            activity.api_id = related_obj.api_id
            break

    # Overwrite local obj with CTCT's response
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
  def update(self, obj: 'EmailCampaign') -> 'EmailCampaign':  # type: ignore[override]  # noqa: E501
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

    self._pre_api_call()
    response = self.session.patch(
      url=self.get_url(obj.api_id),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    # NOTE: We don't need to do anything with `related_objs` since they were
    #       set locally before the API request.
    obj, _ = self.deserialize(data, pk=pk)

    # Overwrite local obj with CTCT's response
    obj.save()

    return obj


class CampaignActivityRemoteManager(RemoteManager['CampaignActivity']):
  """Extend RemoteManager to handle scheduling."""

  # @task(queue_name='ctct')
  def create(self, obj: 'CampaignActivity') -> NoReturn:  # type: ignore[override]  # noqa: E501
    message = _(
      "ConstantContact API does not support creating CampaignActivities. "
      "They are created during the creation of an EmailCampaign."
    )
    raise NotImplementedError(message)

  # @task(queue_name='ctct')
  def update(self, obj: 'CampaignActivity') -> 'CampaignActivity':  # type: ignore[override]  # noqa: E501
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
    obj: 'CampaignActivity',
    recipients: list[str] | None = None,
    message: str | None = None,
  ) -> None:
    """Sends a preview of the EmailCampaign."""

    if recipients is None:
      default = getattr(settings, 'CTCT_PREVIEW_RECIPIENTS', settings.MANAGERS)
      recipients = [email for (name, email) in default]

    if message is None:
      message = getattr(settings, 'CTCT_PREVIEW_MESSAGE', '')

    self._pre_api_call()
    response = self.session.post(
      url=self.get_url(obj.api_id, endpoint_suffix='/tests'),
      json={
        'email_addresses': recipients,
        'personal_message': message,
      },
    )
    self.raise_or_json(response)

  # @task(queue_name='ctct')
  def schedule(self, obj: 'CampaignActivity') -> None:
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
    self._pre_api_call()
    response = self.session.post(
      url=self.get_url(obj.api_id, endpoint_suffix='/schedules'),
      json={'scheduled_date': obj.campaign.scheduled_datetime.isoformat()},
    )
    self.raise_or_json(response)

  # @task(queue_name='ctct')
  def unschedule(self, obj: 'CampaignActivity') -> None:
    """Unschedules the `primary_email` CampaignActivity."""
    if obj.role == 'primary_email':
      self.delete(obj, endpoint_suffix='/schedules')
    else:
      message = _(
        f"Cannot unschedule CampaignActivities with role '{obj.role}'."
      )
      raise ValueError(message)


class CampaignSummaryRemoteManager(RemoteManager['CampaignSummary']):
  """Extend RemoteManager to handle creating EmailCampaignSummarys."""

  def serialize(
    self,
    obj: 'CampaignSummary',
    field_types: Literal['editable', 'readonly', 'all'] = 'editable',
  ) -> JsonDict:
    data = super().serialize(obj, field_types)
    data['unique_counts'] = {
      stat_field: data.pop(stat_field)
      for stat_field in self.model.API_READONLY_FIELDS[1:]
    }
    return data
