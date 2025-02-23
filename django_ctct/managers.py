from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Optional, NoReturn
from urllib.parse import urlencode

from jwt import ExpiredSignatureError
from ratelimit import limits, sleep_and_retry
import requests
from requests.exceptions import HTTPError
from requests.models import Response

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models import Model
from django.db.models.query import QuerySet
from django.http import HttpRequest, Http404
from django.middleware.csrf import get_token as get_csrf_token
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from django_tasks import task

from django_ctct.utils import get_related_fields


if TYPE_CHECKING:
  from django_ctct.models import (
    Token, Contact, ContactList,
    EmailCampaign, CampaignActivity,
  )


class BaseRemoteManager(models.Manager):
  """Base manager for utilizing an API."""

  def get_url(
    self,
    api_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    endpoint_suffix: Optional[str] = None,
  ) -> str:
    endpoint = endpoint or self.model.API_ENDPOINT
    if not endpoint.startswith(self.API_VERSION):
      endpoint = f'{self.API_VERSION}{endpoint}'

    url = f'{self.API_URL}{endpoint}'

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
      message = f"[{response.status_code}] {error_message}"
      raise HTTPError(message, response=response)

    return data

  def _improperly_configured(self):
    message = "You must define this method on a child class."
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
  API_SCOPES = (
    'account_read',
    'account_update',
    'contact_data',
    'campaign_data',
    'offline_access',
  )

  def get_auth_url(self, request: HttpRequest) -> str:
    """Returns a URL for logging into CTCT.com to grant permissions."""
    endpoint = self.get_url(endpoint='/authorize')
    data = {
      'client_id': settings.CTCT_PUBLIC_KEY,
      'redirect_uri': settings.CTCT_REDIRECT_URI,
      'response_type': 'code',
      'state': get_csrf_token(request),
      'scope': '+'.join(self.API_SCOPES),
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
    token = Token.objects.create(**data)
    return token

  def get(self) -> Token:
    """Fetches most recent token, refreshing if necessary."""

    token = self.model.objects.first()
    if not token:
      message = (
        "No tokens in the database yet. "
        f"Go to {reverse('ctct:auth')} and sign into ConstantContact."
      )
      raise ValueError(message)

    try:
      token.decode()
    except ExpiredSignatureError:
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
    token = Token.objects.create(**data)
    return token


class RemoteManager(BaseRemoteManager):
  """Manager for utilizing the CTCT API."""

  API_URL = 'https://api.cc.email'
  API_VERSION = '/v3'
  API_LIMIT_CALLS = 4   # four calls
  API_LIMIT_PERIOD = 1  # per second

  TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

  def get_queryset(self):
    """Prevent access to the db from within RemoteManager."""
    return super().get_queryset().none()

  def connect(self) -> None:
    token = Token.remote.get()
    self.session = requests.Session()
    self.session.headers.update({
      'Authorization': f"{token.token_type} {token.access_token}"
    })

  def serialize(self, obj: Model) -> dict:
    """Convert from Django object to API request body."""

    data = {}

    for field_name in obj.API_EDITABLE_FIELDS:
      try:
        field = self.model._meta.get_field(field_name)
      except FieldDoesNotExist:
        # Check if the API field was defined as a @property
        if value := getattr(obj, field_name, None):
          data[field_name] = value
        continue

      if field_name.endswith('_id'):
        # Convert related object UUID to string
        value = str(getattr(obj, field_name))
      elif isinstance(field, models.DateTimeField):
        # Convert datetime to string
        value = getattr(obj, field_name).strftime(self.TS_FORMAT)
      elif not field.is_relation:
        value = getattr(obj, field_name)
      elif field.many_to_many:
        if obj.pk:
          # TODO API: What if not str/UUID
          value = [str(_.api_id) for _ in getattr(obj, field_name).all()]
        else:
          value = []
      elif field.one_to_many:
        if obj.pk:
          serialize = field.related_model.remote.serialize
          value = [serialize(_) for _ in getattr(obj, field_name).all()]
        else:
          value = []
      elif field.one_to_one or field.many_to_one:
        serialize = field.related_model.remote.serialize
        value = serialize(getattr(obj, field_name))
      data[field_name] = value

    # Allow models to override manager serialization
    if hasattr(obj, 'serialize'):
      data = obj.serialize(data)

    return data

  def deserialize(self, data: dict) -> (Model, dict):
    """Convert from API response body to Django object."""

    if not isinstance(data, dict):
      message = f'Expected a {type({})}, got {type(data)}.'
      raise ValueError(message)

    try:
      data['api_id'] = data.pop(self.model.API_ID_LABEL)
    except AttributeError:
      message = f"{self.model} is missing the `API_ID_LABEL` attribute."
      raise ImproperlyConfigured(message)
    except KeyError as e:
      if self.model.API_ID_LABEL is None:
        # e.g. ContactCustomField
        pass
      else:
        raise e

    # Clean field values, must be done before field restriction
    model_fields = self.model._meta.get_fields()
    for field in model_fields:
      if clean := getattr(self.model, f'clean_remote_{field.name}', None):
        if (value := clean(data)) is not None:
          data[field.name] = value

    # Set related objects
    data = self.deserialize_related_obj_fields(data)
    data, related_objs = self.deserialize_related_objs_fields(data)

    # Restrict to the fields defined in the Django object
    # NOTE: We prefer `field.attname` over `field.name` in order to pick up
    # ForeignKeys and OneToOneFields
    data = {
      k: v for k, v in data.items()
      if k in [getattr(f, 'attname', f.name) for f in model_fields]
    }
    obj = self.model(**data)

    return obj, related_objs

  def deserialize_related_obj_fields(self, data: dict) -> dict:
    """Deserialize ForeignKeys and OneToOneFields.

    Notes
    -----
    These fields can be set using `_id` because `to_field` is set to `api_id`,
    so we don't need to return a `related_objs` dictionary like we do with
    ManyToManyFields and ReverseForeignKeys.

    """

    otos, _, fks, _ = get_related_fields(self.model)
    for field in filter(lambda f: f.name in data, otos + fks):
      if not isinstance(field, (models.ForeignKey, models.OneToOneField)):
        message = f'Expected ForeignKey or OneToOneField, got {type(field)}.'
        raise ValueError(message)

      related_data = data.pop(field.name)
      if isinstance(related_data, str):
        data[f'{field.name}_id'] = related_data
      else:
        # TODO PUSH: Not sure about this
        raise NotImplementedError
        obj, _ = field.related_model.remote.deserialize(related_data)
        data[field.name] = obj
    return data

  def deserialize_related_objs_fields(self, data: dict) -> (dict, dict):
    """Deserialize ManyToManyFields and ReverseForeignKeys.

    Notes
    -----
    In the case of ManyToManyFields, we just return a list of `api_id`s to
    help create ThroughModel instances.

    """

    related_objs = {}

    _, mtms, _, rfks = get_related_fields(self.model)
    for field in filter(lambda f: f.name in data, mtms + rfks):

      if related_data := data.pop(field.name):
        if all(isinstance(_, dict) for _ in related_data):
          # Add in the parent object's `api_id`
          parent = {field.remote_field.name: data['api_id']}
          deserialize = field.related_model.remote.deserialize
          objs = [deserialize(datum | parent)[0] for datum in related_data]
          related_objs[field.related_model] = objs
        elif all(isinstance(_, str) for _ in related_data):
          # TODO API: What if api_ids were ints instead of uuids?
          # ManyToManyField, make a list of "through model" instances
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
        else:
          # Mix of dict and str
          raise NotImplementedError

    return data, related_objs

  @sleep_and_retry
  @limits(calls=API_LIMIT_CALLS, period=API_LIMIT_PERIOD)
  def check_api_limit(self) -> None:
    """Honor the API's rate limit."""
    pass

  @task(queue_name='ctct')
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

    obj, related_objs = self.deserialize(data)

    obj.pk = pk
    obj.save()

    # TODO: Delete if RelatedModel is ManyToMany?
    for RelatedModel, objs in related_objs.items():
      RelatedModel.objects.bulk_create(objs, update_conflicts=False)

    return obj

  def get(self, api_id: str) -> (Optional[Model], dict):
    """Get an existing object from the remote server.

    Notes
    -----
    This method will not save the object to the local database. We return the
    object as well as a dictionary of the form {field_name: [RelatedModel()]}.

    """

    self.check_api_limit()

    response = self.session.get(
      url=self.get_url(api_id),
      params=self.model.API_GET_QUERIES,
    )

    try:
      data = self.raise_or_json(response)
    except Http404:
      obj, related_objs = None, {}
    else:
      obj, related_objs = self.deserialize(data)

    return obj, related_objs

  def all(self, endpoint: Optional[str] = None) -> (list[Model], dict):
    """Get all existing objects from the remote server.

    Notes
    -----
    This method will not save the object to the local database. We return
    objects as well as a dictionary of the form {field_name: [RelatedModel()]}.

    """

    objs, related_objs = [], defaultdict(list)

    paginated = True
    while paginated:
      self.check_api_limit()

      response = self.session.get(
        url=self.get_url(endpoint=endpoint),
        params=self.model.API_GET_QUERIES,
      )
      data = self.raise_or_json(response)

      # Data contains two keys: '_links' and e.g. 'contacts',  'lists', etc
      # TODO API: `_links`
      links = data.pop('_links', None)
      data = next(iter(data.values()))
      for row in data:
        obj, other = self.deserialize(row)
        objs.append(obj)

        # Merge related objects
        for RelatedModel, instances in other.items():
          related_objs[RelatedModel].extend(instances)

      try:
        endpoint = links.get('next').get('href')
      except AttributeError:
        paginated = False

    return objs, related_objs

  @task(queue_name='ctct')
  def update(self, obj: Model) -> Model:
    """Update exisiting Django object on the remote server.

    Notes
    -----
    This method saves the API's response to the local database in order to
    preserve values calculated by the API.

    """

    if not (pk := obj.pk):
      raise ValueError('Must create object locally first.')
    elif not obj.api_id:
      raise ValueError('Must create object remotely first.')

    self.check_api_limit()
    response = self.session.put(
      url=self.get_url(obj.api_id),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data)

    obj.pk = pk
    obj.save()

    # TODO: only do this for ManyToMany?
    for RelatedModel, objs in related_objs.items():
      RelatedModel.objects.bulk_create(objs)  # TODO: update_conflicts=True,

    return obj

  @task(queue_name='ctct')
  def delete(self, obj: Model, endpoint_suffix: Optional[str] = None) -> None:
    """Delete existing Django object on the remote server.

    Notes
    -----
    This method can be used to delete sub-resources of an object (such as a
    scheduled EmailCampaign) via the optional `endpoint_suffix` param.

    We ignore 404 responses in the situation that the remote object has already
    been deleted.

    """

    self.check_api_limit()

    url = self.get_url(obj.api_id, endpoint_suffix=endpoint_suffix)
    response = self.session.delete(url)

    if response.status_code != 404:
      # Allow 404
      self.raise_or_json(response)

    return None


class ContactListRemoteManager(RemoteManager):
  """Extend RemoteManager to handle adding multiple Contacts."""

  @task(queue_name='ctct')
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
      message = (
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


class ContactRemoteManager(RemoteManager):
  """Extend RemoteManager to handle ContactLists."""

  # TODO: Get error 400 if no list_memberships
  @task(queue_name='ctct')
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

    if not (pk := obj.pk):
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

    obj, related_objs = self.deserialize(data)

    obj.pk = pk
    obj.save()

    # TODO: only do this for ManyToMany?
    for RelatedModel, objs in related_objs.items():
      RelatedModel.objects.bulk_create(objs)  # TODO: update_conflicts=True,

  @task(queue_name='ctct')
  def update(self, obj: Contact) -> Optional[Contact]:
    """Update Contact and ContactList membership on CTCT servers.

    Notes
    -----
    The PUT call will overwrite all properties not included in the request
    body with NULL, so we need to make sure the `serialize()` method
    includes all important fields. While the `create_or_update()` method
    supports partial updates, it won't allow us to remove a ContactList.

    CTCT requires that all contacts be a member of at least one ContactList,
    so in the event of removing someone from all lists, we should actually
    issue a DELETE call; however, these 'deleted' Contacts retain their ID
    in ConstantContact's database and can be revived at any time.

    """

    if obj.list_memberships.exists():
      response = super().update(obj)
    else:
      response = self.delete(obj)
    return response


class EmailCampaignRemoteManager(RemoteManager):
  """Extend RemoteManager to handle creating EmailCampaigns."""

  def serialize(self, obj: Model) -> dict:
    if obj.api_id:
      # The only field that the API will update
      return {'name': obj.name}
    else:
      return super().serialize(obj)

  @task(queue_name='ctct')
  def create(self, obj: EmailCampaign) -> EmailCampaign:
    """Creates a local EmailCampaign on the remote servers.

    Notes
    -----
    This method will also create the new `primary_email` and `permalink`
    CampaignActivities on CTCT and associate the `primary_email` one
    with the new EmailCampaign in the database.

    """

    # Validate
    if not (pk := obj.pk):
      raise ValueError('Must create object locally first.')
    try:
      activity = obj.campaign_activities.get(role='primary_email')
    except CampaignActivity.DoesNotExist:
      message = _(
        "The related `primary_email` CampaignActivity must be saved locally "
        "before the EmailCampaign can be saved remotely."
      )
      raise CampaignActivity.DoesNotExist(message)

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

    obj, related_objs = self.deserialize(data)

    # Update local objects
    for related_obj in related_objs[CampaignActivity]:
      if related_obj.role == 'primary_email':
        activity.api_id = related_obj.api_id
        activity.save(update_fields=['api_id'])
        break

    obj.pk = pk
    obj.save()

    return obj

  @task(queue_name='ctct')
  def update(self, obj: EmailCampaign) -> EmailCampaign:
    """Update EmailCampaign on remote servers.

    Notes
    -----
    The only field that can be updated this way is the `name` field.

    """
    if not (pk := obj.pk):
      raise ValueError('Must create object locally first.')
    elif not obj.api_id:
      raise ValueError('Must create object remotely first.')

    self.check_api_limit()
    response = self.session.patch(
      url=self.get_url(obj.api_id),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, _ = self.deserialize(data)

    obj.pk = pk
    obj.save()

    return obj


class CampaignActivityRemoteManager(RemoteManager):
  """Extend RemoteManager to handle scheduling."""

  @task(queue_name='ctct')
  def create(self, obj: CampaignActivity) -> NoReturn:
    message = (
      "ConstantContact API does not support creating CampaignActivities. "
      "They are created during the creation of an EmailCampaign."
    )
    raise NotImplementedError(message)

  @task(queue_name='ctct')
  def update(self, obj: Model, send_preview: bool = False) -> Model:
    """Update CampaignActivity on CTCT servers.

    Notes
    -----
    CampaignActivities can only be updated if their associated EmailCampaign
    is in DRAFT or SENT status. If the EmailCampaign is already scheduled,
    we make an API call to unschedule it and then re-schedule it after
    updates were made. If you wish to send a new preview out after the activity
    has been updated, you can set `send_preview = True`.

    """

    if obj.role != 'primary_email':
      message = (
        f'CampaignActivity with role `{obj.role}` not supported yet.'
      )
      raise NotImplementedError(message)

    if was_scheduled := (obj.campaign.current_status == 'SCHEDULED'):
      self.unschedule(obj)

    obj = super().update(obj)

    if send_preview:
      self.send_preview(obj)

    if was_scheduled:
      self.schedule(obj)

    return obj

  @task(queue_name='ctct')
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

  @task(queue_name='ctct')
  def schedule(self, obj: CampaignActivity, update_first: bool = True) -> None:
    """Schedules the `primary_email` CampaignActivity.

    Notes
    -----
    Recipients must be set before scheduling; if recipients have already been
    set, this can be skipped by setting `update_first=False`.

    """

    # Validate role and scheduled_datetime
    if obj.role != 'primary_email':
      message = (
        f"Cannot schedule CampaignActivities with role '{obj.role}'."
      )
      raise ValueError(message)

    if obj.scheduled_datetime is None:
      message = "Must specify `scheduled_datetime`."
      raise ValueError(message)

    # Receipients must be set before scheduling
    if update_first:
      response = self.update(obj)

    # Finally, schedule the CampaignActivity
    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(obj.api_id, endpoint_suffix='/schedules'),
      json={'scheduled_date': obj.scheduled_datetime.isoformat()},
    )
    self.raise_or_json(response)

  @task(queue_name='ctct')
  def unschedule(self, obj: CampaignActivity) -> None:
    """Unschedules the `primary_email` CampaignActivity."""
    if obj.role == 'primary_email':
      self.delete(obj, endpoint_suffix='/schedules')
    else:
      message = (
        f"Cannot unschedule CampaignActivities with role '{obj.role}'."
      )
      raise ValueError(message)
