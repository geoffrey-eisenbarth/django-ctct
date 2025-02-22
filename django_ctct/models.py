from __future__ import annotations

from collections import defaultdict
import datetime as dt
from typing import Literal, Optional, Self, NoReturn

import jwt
from ratelimit import limits, sleep_and_retry
import re
import requests
from requests.exceptions import HTTPError
from requests.models import Response

from django.conf import settings
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.core.validators import validate_email
from django.db import models
from django.db.models import Model
from django.db.models.fields import NOT_PROVIDED
from django.db.models.query import QuerySet
from django.http import Http404
from django.urls import reverse
from django.utils import timezone, formats
from django.utils.translation import gettext_lazy as _

from django_tasks import task

from phonenumber_field.phonenumber import PhoneNumber
from phonenumber_field.modelfields import PhoneNumberField

from django_ctct.utils import to_dt, get_related_fields


HttpMethod = Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE']


# TODO: update(), create()
class RemoteManager(models.Manager):
  """Base class for utilizing the API."""

  API_URL = 'https://api.cc.email'
  API_VERSION = '/v3'
  API_LIMIT_CALLS = 4   # four calls
  API_LIMIT_PERIOD = 1  # per second

  TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

  def get_queryset(self):
    """Prevent access to the db from within RemoteManager."""
    return super().get_queryset().none()

  def connect(self) -> None:
    self.session = requests.Session()
    self.session.headers.update(Token.get().authorization_header)

  def serialize(self, obj: Model) -> dict:
    """Convert from Django object to API request body."""

    data = {}

    for field_name in obj.API_EDITABLE_FIELDS:
      field = self.model._meta.get_field(field_name)
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
      message = f'Expected a {type({})}, got {type(data)}!'
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
        message = f'Expected ForeignKey or OneToOneField, got {type(field)}!'
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

  def get_url(
    self,
    method: HttpMethod,
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

    if (method == 'GET') and ('?' not in url) and self.model.API_GET_QUERIES:
      url += '?'
      for name, value in self.model.API_GET_QUERIES.items():
        url += f'{name}={value}'

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
      message = f"[{response.status_code}] {data['error_message']}"
      raise HTTPError(message, response=response)

    return data

  @sleep_and_retry
  @limits(calls=API_LIMIT_CALLS, period=API_LIMIT_PERIOD)
  def check_api_limit(self) -> None:
    """Honor the API's rate limit."""
    pass

  @task()
  def create(self, obj: Model) -> Model:
    """Creates an existing Django object on the remote server.

    Notes
    -----
    This method saves the API's response to the local database in order to
    preserve values calculated by the API (e.g. API_READONLY_FIELDS).

    """

    if not (pk := obj.pk):
      raise ValueError('Must create object locally first!')

    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(method='POST'),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data)

    obj.pk = pk
    obj.save()

    # TODO: Delete if RelatedModel is ManyToMany?
    #       Can we only deleted through models that are related?
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

    url = self.get_url(method='GET', api_id=api_id)
    response = self.session.get(url)

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

      url = self.get_url(method='GET', endpoint=endpoint)
      response = self.session.get(url)
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

  @task()
  def update(self, obj: Model) -> Model:
    """Update exisiting Django object on the remote server.

    Notes
    -----
    This method saves the API's response to the local database in order to
    preserve values calculated by the API.

    """

    if not (pk := obj.pk):
      raise ValueError('Must create object locally first')
    elif not obj.api_id:
      raise ValueError('Must create object remotely first')

    self.check_api_limit()
    response = self.session.put(
      url=self.get_url(method='PUT', api_id=obj.api_id),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data)

    obj.pk = pk
    obj.save()

    for RelatedModel, objs in related_objs.items():
      RelatedModel.objects.bulk_create(objs)  # TODO: update_conflicts=True,

    return obj

  @task()
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

    url = self.get_url(
      method='DELETE',
      api_id=obj.api_id,
      endpoint_suffix=endpoint_suffix,
    )
    response = self.session.delete(url)

    if response.status_code != 404:
      # Allow 404
      self.raise_or_json(response)

    return None


# DONE
class CTCTModel(Model):
  """Common CTCT model methods and properties."""

  API_URL = 'https://api.cc.email'
  API_VERSION = '/v3'
  API_GET_QUERIES = {}

  # Must explicitly specify both
  objects = models.Manager()
  remote = RemoteManager()

  save_to_ctct: Optional[Literal['sync', 'async']] = 'async'

  api_id = models.UUIDField(
    null=True,     # Allow objects to be created without CTCT IDs
    default=None,  # Models often created without CTCT IDs
    unique=True,   # Note: None != None for uniqueness check
    blank=True,
    verbose_name=_('API ID'),
  )

  class Meta:
    abstract = True

  def save(self, *args, **kwargs) -> None:
    self.save_to_ctct = kwargs.pop('save_to_ctct', self.save_to_ctct)
    super().save(*args, **kwargs)


# DONE
class CTCTLocalModel(CTCTModel):
  """Local model without remote saving support.

  Notes
  -----
  The crucial check that prevents CTCTLocalModels from being saved remotely is
  the `isinstance(instance, CTCTLocalModel)` lines in signals.py.

  """

  class Meta(CTCTModel.Meta):
    abstract = True


# DONE
class Token(Model):
  """Authorization token for CTCT API access.

  Notes
  -----
  To get the latest Token, use the `get()` class method.

  """

  API_AUTH_URL = 'https://authz.constantcontact.com/oauth2/default/v1/token'
  API_JWKS_URL = (
    'https://identity.constantcontact.com/'
    'oauth2/aus1lm3ry9mF7x2Ja0h8/v1/keys'
  )

  access_code = models.TextField(
    verbose_name=_('Access Code'),
  )
  refresh_code = models.CharField(
    max_length=50,
    verbose_name=_('Refresh Code'),
  )
  type = models.CharField(
    max_length=50,
    default='Bearer',
    verbose_name=_('Type'),
  )
  inserted = models.DateTimeField(
    auto_now=False,
    auto_now_add=True,
    verbose_name=_('Inserted'),
  )

  class Meta:
    ordering = ('-inserted', )

  def __str__(self) -> str:
    s = formats.date_format(
      timezone.localtime(self.inserted),
      settings.DATETIME_FORMAT,
    )
    return s

  def decode(self) -> dict:
    """Decode JWT Token, which also verifies that it hasn't expired."""

    client = jwt.PyJWKClient(self.API_JWKS_URL)
    signing_key = client.get_signing_key_from_jwt(self.access_code)
    data = jwt.decode(
      self.access_code,
      signing_key.key,
      algorithms=['RS256'],
      audience=f'{CTCTModel.API_URL}{CTCTModel.API_VERSION}',
    )
    return data

  def refresh(self) -> Self:
    """Obtain a new Token from CTCT using the refresh code."""

    response = requests.post(
      url=self.API_AUTH_URL,
      auth=(settings.CTCT_PUBLIC_KEY, settings.CTCT_SECRET_KEY),
      data={
        'refresh_token': self.refresh_code,
        'grant_type': 'refresh_token',
      },
    )
    data = RemoteManager.raise_or_json(response)

    # Create new Token for future use
    if 'refresh_token' in data:
      token = Token.objects.create(
        access_code=data['access_token'],
        refresh_code=data['refresh_token'],
        type=data['token_type'],
      )
    else:
      message = (
        "Token does not contain `refresh_token`.\n"
        f"{data}"
      )
      raise ValueError(message)
    return token

  @classmethod
  def get(cls) -> Self:
    """Fetches most recent token, refreshing if necessary."""

    token = Token.objects.first()
    if not token:
      message = (
        "No tokens in the database yet. "
        f"Go to {reverse('ctct:auth')} and sign into ConstantContact."
      )
      raise ValueError(message)

    try:
      token.decode()
    except jwt.ExpiredSignatureError:
      token = token.refresh()

    return token

  @property
  def authorization_header(self) -> dict:
    return {'Authorization': f"{self.type} {self.access_code}"}


# DONE
class ContactListRemoteManager(RemoteManager):
  """Extend RemoteManager to handle adding multiple Contacts."""

  def add_list_memberships(
    self,
    lists: QuerySet['ContactList'],
    contacts: QuerySet['Contact']
  ) -> None:
    """Adds multiple Contacts to (multiple) ContactLists."""

    API_MAX_CONTACTS = 500

    list_ids = list(map(str, lists.values_list('api_id', flat=True)))
    contact_ids = list(map(str, contacts.values_list('api_id', flat=True)))

    for i in range(0, len(contact_ids), API_MAX_CONTACTS):
      self.check_api_limit()
      response = self.session.post(
        url=self.get_url(
          method='POST',
          endpoint='/activities/add_list_memberships',
        ),
        json={
          'source': {'contact_ids': contact_ids[i:i + API_MAX_CONTACTS]},
          'list_ids': list_ids,
        },
      )
      self.raise_or_json(response)


# DONE
class ContactList(CTCTModel):
  """Django implementation of a CTCT Contact List."""

  API_ENDPOINT = '/contact_lists'
  API_ID_LABEL = 'list_id'
  API_EDITABLE_FIELDS = (
    'name',
    'description',
    'favorite',
  )
  API_READONLY_FIELDS = (
    'created_at',
    'updated_at',
  )

  # Must explicitly specify both
  objects = models.Manager()
  remote = ContactListRemoteManager()

  # API editable fields
  name = models.CharField(
    max_length=255,
    verbose_name=_('Name'),
  )
  description = models.CharField(
    max_length=255,
    verbose_name=_('Description'),
    help_text=_('For internal use only'),
  )
  favorite = models.BooleanField(
    default=False,
    verbose_name=_('Favorite'),
    help_text=_('Mark the list as a favorite'),
  )

  # API read-only fields
  created_at = models.DateTimeField(
    auto_now_add=True,
    verbose_name=_('Created At'),
  )
  updated_at = models.DateTimeField(
    auto_now=True,
    verbose_name=_('Updated At'),
  )

  class Meta:
    verbose_name = _('Contact List')
    verbose_name_plural = _('Contact Lists')
    ordering = ('-favorite', 'name')

  def __str__(self) -> str:
    return self.name


# DONE
class CustomField(CTCTModel):
  """Django implementation of a CTCT Contact's CustomField."""

  API_ENDPOINT = '/contact_custom_fields'
  API_ID_LABEL = 'custom_field_id'
  API_EDITABLE_FIELDS = (
    'label',
    'type',
  )
  API_READONLY_FIELDS = (
    'name',
    'created_at',
    'updated_at',
  )

  TYPES = (
    ('string', 'Text'),
    ('date', 'Date'),
  )

  # API editable fields
  label = models.CharField(
    max_length=50,
    verbose_name=_('Label'),
    help_text=_(
      'The display name for the custom_field shown in the UI as free-form text'
    ),
  )
  type = models.CharField(
    max_length=6,
    choices=TYPES,
    verbose_name=_('Type'),
    help_text=_(
      'Specifies the type of value the custom_field field accepts'
    ),
  )

  # API read-only fields
  name = models.CharField(
    max_length=50,
    editable=False,
    verbose_name=_('Name'),
    help_text=_(
      'Unique name constructed by replacing blanks with underscores'
    ),
  )
  created_at = models.DateTimeField(
    auto_now_add=True,
    verbose_name=_('Created At'),
  )
  updated_at = models.DateTimeField(
    auto_now=True,
    verbose_name=_('Updated At'),
  )

  class Meta:
    verbose_name = _('Custom Field')
    verbose_name_plural = _('Custom Fields')

  def __str__(self) -> str:
    return self.label


# TODO: update_or_create()
class ContactRemoteManager(RemoteManager):
  """Extend RemoteManager to handle ContactLists."""

  # TODO: Get error 400 if no list_memberships
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
      raise ValueError('Must create object locally first')

    # This endpoint expects a slightly different serialization
    data = self.serialize(obj)
    data['email_address'] = data.pop('email_address')['address']

    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(method='POST', endpoint_suffix='/sign_up_form'),
      json=data,
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data)

    obj.pk = pk
    obj.save()

    for RelatedModel, objs in related_objs.items():
      RelatedModel.objects.bulk_create(objs)  # TODO: update_conflicts=True,

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


# DONE
class Contact(CTCTModel):
  """Django implementation of a CTCT Contact.

  Notes
  -----
  The following editable fields are specified in `Contact.serialize()`:
    1) 'email'
    2) 'permission_to_send'
    3) 'create_source'
    4) 'update_source'

  The following editable fields are specified as `custom_fields`:A
    1) 'honorific'
    2) 'suffix'

  """

  API_ENDPOINT = '/contacts'
  API_ID_LABEL = 'contact_id'
  API_EDITABLE_FIELDS = (
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
  API_MAX_NOTES = 150
  API_MAX_PHONE_NUMBERS = 3
  API_MAX_STREET_ADDRESSES = 3
  API_MAX_CUSTOM_FIELDS = 25
  API_MAX_LIST_MEMBERSHIPS = 50

  # Must explicitly specify both
  objects = models.Manager()
  remote = ContactRemoteManager()

  SALUTATIONS = (
    ('Mr.', 'Mr.'),
    ('Ms.', 'Ms.'),
    ('Dr.', 'Dr.'),
    ('Hon.', 'The Honorable'),
    ('Amb.', 'Ambassador'),
    ('Prof.', 'Professor'),
  )
  PERMISSIONS = (
    ('explicit', 'Explicit'),
    ('implicit', 'Implicit'),
    ('not_set', 'Not set'),
    ('pending_confirmation', 'Pending confirmation'),
    ('temp_hold', 'Temporary hold'),
    ('unsubscribed', 'Unsubscribed'),
  )
  SOURCES = (
    ('Contact', 'Contact'),
    ('Account', 'Account'),
  )

  email = models.EmailField(
    unique=True,
    verbose_name=_('Email Address'),
  )
  first_name = models.CharField(
    max_length=50,
    blank=True,
    verbose_name=_('First Name'),
    help_text=_('The first name of the contact'),
  )
  last_name = models.CharField(
    max_length=50,
    blank=True,
    verbose_name=_('Last Name'),
    help_text=_('The last name of the contact'),
  )
  job_title = models.CharField(
    max_length=50,
    blank=True,
    verbose_name=_('Job Title'),
    help_text=_('The job title of the contact'),
  )
  company_name = models.CharField(
    max_length=50,
    blank=True,
    verbose_name=_('Company Name'),
    help_text=_('The name of the company where the contact works'),
  )
  honorific = models.CharField(
    choices=SALUTATIONS,
    max_length=5,
    blank=True,
    verbose_name=_('Honorific'),
    help_text=_('The honorific of the contact'),
  )
  suffix = models.CharField(
    max_length=10,
    blank=True,
    verbose_name=_('Suffix'),
    help_text=_('The suffix of the contact'),
  )

  list_memberships = models.ManyToManyField(
    ContactList,
    through='ContactAndContactList',
    related_name='members',
    verbose_name=_('List Memberships'),
    blank=True,
  )

  permission_to_send = models.CharField(
    max_length=20,
    choices=PERMISSIONS,
    default='implicit',
    verbose_name=_('Permission to Send'),
    help_text=_(
      'Identifies the type of permission that the Constant Contact account has to send email to the contact'  # noqa: 501
    ),
  )
  create_source = models.CharField(
    max_length=7,
    choices=SOURCES,
    default='Account',
    verbose_name=_('Create Source'),
    help_text=_('Describes who added the contact'),
  )
  created_at = models.DateTimeField(
    auto_now=False,
    default=timezone.now,
    null=True,
    verbose_name=_('Created At'),
    help_text=_('Date and time that the resource was created'),
  )
  update_source = models.CharField(
    max_length=7,
    choices=SOURCES,
    default='Account',
    verbose_name=_('Update Source'),
    help_text=_('Identifies who last updated the contact'),
  )
  updated_at = models.DateTimeField(
    auto_now=True,
    null=True,
    verbose_name=_('Updated At'),
    help_text=_('Date and time that the contact was last updated'),
  )

  opt_out_source = models.CharField(
    max_length=7,
    choices=SOURCES,
    default='',
    editable=False,
    blank=True,
    verbose_name=_('Opted Out By'),
    help_text=_('Handled by ConstantContact'),
  )
  opt_out_date = models.DateTimeField(
    blank=True,
    null=True,
    verbose_name=_('Opted Out On'),
  )
  opt_out_reason = models.CharField(
    max_length=255,
    blank=True,
    verbose_name=_('Opt Out Reason'),
  )

  @property
  def name(self) -> str:
    field_names = ['honorific', 'first_name', 'last_name', 'suffix']
    name = ' '.join(getattr(self, _) for _ in field_names).strip()
    return name

  @property
  def job(self) -> str:
    return ' @ '.join(filter(None, [self.job_title, self.company_name]))

  @property
  def opted_out(self) -> bool:
    return bool(self.opt_out_source)

  @property
  def ctct_source(self) -> dict:
    if self.api_id:
      source = {'update_source': self.update_source}
    else:
      source = {'create_source': self.create_source}
    return source

  class Meta:
    verbose_name = _('Contact')
    verbose_name_plural = _('Contacts')

    ordering = ('-updated_at', )

  def __str__(self) -> str:
    if self.name and self.email:
      s = f'{self.name} ({self.email})'
    else:
      s = self.email or self.name or 'N/A'
    return s

  def clean(self) -> None:
    self.email = self.email.lower().strip()
    validate_email(self.email)
    return super().clean()

  @classmethod
  def clean_remote_email(cls, data: dict) -> str:
    return data['email_address']['address'].lower()

  @classmethod
  def clean_remote_opt_out_source(cls, data: dict) -> str:
    return data['email_address'].get('opt_out_source', '')

  @classmethod
  def clean_remote_opt_out_date(cls, data: dict) -> Optional[dt.datetime]:
    if opt_out_date := data['email_address'].get('opt_out_date'):
      return to_dt(opt_out_date)

  @classmethod
  def clean_remote_opt_out_reason(cls, data: dict) -> str:
    return data['email_address'].get('opt_out_reason', '')

  def serialize(self, data: dict) -> dict:
    data['email_address'] = {
      'address': self.email,
      'permission_to_send': self.permission_to_send,
    }
    data.update(self.ctct_source)
    return data


# DONE
class ContactAndContactList(models.Model):
  """Custom through model that uses CTCT API ids.

  Notes
  -----
  In order to bulk import the ManyToMany relationship between Contact and
  ContactList, we create instances of the associated ThroughModel using the
  API ids of Contact and ContactList. However, Django currently does not
  support setting `to_field` in the Django-created ThroughModel, so we must
  create our own ThroughModel in order specify the `to_field` values for the
  ForeignKeys.

  """

  is_through_model = True

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    to_field='api_id',
  )
  contactlist = models.ForeignKey(
    ContactList,
    on_delete=models.CASCADE,
    to_field='api_id',
  )


# DONE
class ContactNote(CTCTLocalModel):
  """Django implementation of a CTCT Note."""

  API_ID_LABEL = 'note_id'
  API_EDITABLE_FIELDS = (
    'note_id',
    'created_at',
    'content',
  )
  API_READONLY_FIELDS = tuple()

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='notes',
    verbose_name=_('Contact'),
  )
  author = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    null=True,
    verbose_name=_('Author'),
  )

  # API read-only fields
  created_at = models.DateTimeField(
    auto_now=True,
    verbose_name=_('Created at'),
    help_text=_('The date the note was created'),
  )
  content = models.CharField(
    max_length=2000,
    verbose_name=_('Content'),
    help_text=_('The content for the note'),
  )

  class Meta:
    verbose_name = _('Note')
    verbose_name_plural = _('Notes')

    # TODO PUSH:
    # constraints = [
    #   models.CheckConstraint(
    #     check=Q(contact__notes__count__lte=Contact.API_MAX_NOTES),
    #     name='limit_notes'
    #   ),
    # ]


# DONE
class ContactPhoneNumber(CTCTLocalModel):
  """Django implementation of a CTCT Contact's PhoneNumber."""

  API_ID_LABEL = 'phone_number_id'
  API_EDITABLE_FIELDS = (
    'kind',
    'phone_number',
  )

  MISSING_NUMBER = '000-000-0000'
  KINDS = (
    ('home', 'Home'),
    ('work', 'Work'),
    ('mobile', 'Mobile'),
    ('other', 'Other'),
  )

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='phone_numbers',
    verbose_name=_('Contact'),
  )

  # API editable fields
  kind = models.CharField(
    choices=KINDS,
    max_length=6,
    verbose_name=_('Kind'),
    help_text=_('Identifies the type of phone number'),
  )
  phone_number = PhoneNumberField(
    verbose_name=_('Phone Number'),
    help_text=_("The contact's phone number"),
  )

  class Meta:
    verbose_name = _('Phone Number')
    verbose_name_plural = _('Phone Numbers')

    constraints = [
      # TODO PUSH: This doesn't seem enforced via CTCT
      # models.UniqueConstraint(
      #   fields=['contact', 'kind'],
      #   name='unique_phone_number',
      # ),
      # TODO PUSH:
      # models.CheckConstraint(
      #   check=Q(contact__phone_numbers__count__lte=Contact.API_MAX_PHONE_NUMBERS),
      #   name='limit_phone_numbers',
      # ),
    ]

  def __str__(self) -> str:
    return f'[{self.get_kind_display()}] {self.phone_number}'

  @classmethod
  def clean_remote_phone_number(cls, data: dict) -> Optional[PhoneNumber]:
    numbers = r'\d+'
    if phone_number := ''.join(re.findall(numbers, data['phone_number'])):
      phone_number = PhoneNumber.from_string(phone_number)
    else:
      phone_number = PhoneNumber.from_string(cls.MISSING_NUMBER)
    return phone_number


# DONE
class ContactStreetAddress(CTCTLocalModel):
  """Django implementation of a CTCT Contact's StreetAddress."""

  API_ID_LABEL = 'street_address_id'
  API_EDITABLE_FIELDS = (
    'kind',
    'street',
    'city',
    'state',
    'postal_code',
    'country',
  )
  API_READONLY_FIELDS = tuple()

  KINDS = (
    ('home', 'Home'),
    ('work', 'Work'),
    ('other', 'Other'),
  )

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='street_addresses',
    verbose_name=_('Contact'),
  )

  # API editable fields
  kind = models.CharField(
    choices=KINDS,
    max_length=5,
    verbose_name=_('Kind'),
    help_text=_('Describes the type of address'),
  )
  street = models.CharField(
    max_length=255,
    verbose_name=_('Street'),
    help_text=_('Number and street of the address'),
  )
  city = models.CharField(
    max_length=50,
    verbose_name=_('City'),
    help_text=_('The name of the city where the contact lives'),
  )
  state = models.CharField(
    max_length=50,
    verbose_name=_('State'),
    help_text=_('The name of the state or province where the contact lives'),
  )
  postal_code = models.CharField(
    max_length=50,
    verbose_name=_('Postal Code'),
    help_text=_('The zip or postal code of the contact'),
  )
  country = models.CharField(
    max_length=50,
    verbose_name=_('Country'),
    help_text=_('The name of the country where the contact lives'),
  )

  class Meta:
    verbose_name = _('Street Address')
    verbose_name_plural = _('Street Addresses')

    constraints = [
      # TODO PUSH: This doesn't seem enforced via CTCT
      # models.UniqueConstraint(
      #   fields=['contact', 'kind'],
      #   name='unique_street_address',
      # ),
      # TODO PUSH:
      # models.CheckConstraint(
      #   check=Q(contact__street_addresses__count__lte=Contact.API_MAX_STREET_ADDRESSES),
      #   name='limit_street_addresses',
      # ),
    ]

  def __str__(self) -> str:
    field_names = ['street', 'city', 'state']
    address = ', '.join(
      getattr(self, _) for _ in field_names if getattr(self, _)
    )
    return f'[{self.get_kind_display()}] {address}'

  @classmethod
  def clean_remote_string(cls, s: str) -> str:
    return s.replace('\n', ' ').replace('\t', ' ').strip()

  @classmethod
  def clean_remote_street(cls, data: dict) -> str:
    return cls.clean_remote_string(data.get('street', ''))

  @classmethod
  def clean_remote_city(cls, data: dict) -> str:
    return cls.clean_remote_string(data.get('city', ''))

  @classmethod
  def clean_remote_state(cls, data: dict) -> str:
    return cls.clean_remote_string(data.get('state', ''))

  @classmethod
  def clean_remote_postal_code(cls, data: dict) -> str:
    return cls.clean_remote_string(data.get('postal_code', ''))

  @classmethod
  def clean_remote_country(cls, data: dict) -> str:
    return cls.clean_remote_string(data.get('country', ''))


# DONE
class ContactCustomField(CTCTLocalModel):
  """Django implementation of a CTCT Contact's CustomField.

  Notes
  -----
  It's important to specify `custom_field_id` in `API_EDITABLE_FIELDS` instead
  of `custom_field`; using the latter will result in a serialized CustomField
  instance when Contact is serialized.

  """

  API_ID_LABEL = None
  API_EDITABLE_FIELDS = (
    'custom_field_id',
    'value',
  )
  API_READONLY_FIELDS = tuple()

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='custom_fields',
    verbose_name=_('Contact'),
  )

  # API editable fields
  custom_field = models.ForeignKey(
    CustomField,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='instances',
    verbose_name=_('Field'),
  )
  value = models.CharField(
    max_length=255,
    verbose_name=_('Value'),
  )

  class Meta:
    verbose_name = _('Custom Field')
    verbose_name_plural = _('Custom Fields')

    constraints = [
      # TODO PUSH: This doesn't seem enforced via CTCT
      # models.UniqueConstraint(
      #   fields=['contact', 'custom_field'],
      #   name='unique_custom_field',
      # ),
      # TODO PUSH:
      # models.CheckConstraint(
      #   check=Q(contact__custom_fields__count__lte=Contact.API_MAX_CUSTOM_FIELDS),
      #   name='limit_custom_fields',
      # ),
    ]

  def __str__(self) -> str:
    try:
      s = f'[{self.custom_field.label}] {self.value}'
    except CustomField.DoesNotExist:
      s = super().__str__()
    return s

  @classmethod
  def clean_remote_custom_field(cls, data: dict) -> str:
    return data['custom_field_id']


# TODO: create()
class EmailCampaignRemoteManager(RemoteManager):
  """Extend RemoteManager to handle creating EmailCampaigns."""

  def create(self, obj: EmailCampaign) -> EmailCampaign:
    """Creates an existing EmailCampaign on the remote servers.

    Notes
    -----
    This method will also create the new `primary_email` and `permalink`
    CampaignActivities on CTCT and associate the `primary_email` one
    with the new EmailCampaign in the database.

    """

    # Set name and initialize the CampaignActivity object (do not create it!)
    activity = CampaignActivity(
      campaign=obj,
      role='primary_email',
    )

    # Create EmailCampaign (and CampaignActivities) on CTCT servers
    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(method='POST'),
      json={
        'name': obj.name,
        'email_campaign_activities': [
          CampaignActivity.remote.serialize(activity),
        ]
      },
    )

    if response.status_code == 409:
      # EmailCampaign name not unique, fetch existing EmailCampaign from CTCT
      data = self.read(name=self.name)  # TODO: name=self.name?
    else:
      data = self.raise_or_json(response)

    # Get the associated CampaignActivity ID and save
    for campaign_activity in data['campaign_activities']:
      if campaign_activity['role'] == 'primary_email':
        activity.api_id = campaign_activity['campaign_activity_id']
        activity.save(save_to_ctct=False)
        break

    # Schedule and send preview
    # NOTE Moved to admin.
    # TODO: How to create + schedule async? Can we have task callbacks?

    # Update CTCT response with local field values
    data.update({
      'current_status': self.current_status,
    })

    return data

  def update(self, obj: EmailCampaign) -> EmailCampaign:
    """Update EmailCampaign on remote servers.

    Notes
    -----
    The only field that can be updated this way is the `name` field.

    """
    if not (pk := obj.pk):
      raise ValueError('Must create object locally first')
    elif not obj.api_id:
      raise ValueError('Must create object remotely first')

    self.check_api_limit()
    response = self.session.patch(
      url=self.get_url(method='PATCH', api_id=obj.api_id),
      json={'name': obj.name},
    )
    data = self.raise_or_json(response)

    obj, _ = self.deserialize(data)

    obj.pk = pk
    obj.save()

    return obj


# TODO EmailCampaign.current_status vs CampaignActivity.current_status
class EmailCampaign(CTCTModel):
  """Django implementation of a CTCT EmailCampaign."""

  API_ENDPOINT = '/emails'
  API_ID_LABEL = 'campaign_id'
  API_EDITABLE_FIELDS = (
    'name',
    'scheduled_datetime',
  )
  API_READONLY_FIELDS = (
    'current_status',
    'created_at',
    'updated_at',
    'sends',
    'opens',
    'clicks',
    'forwards',
    'optouts',
    'abuse',
    'bounces',
    'not_opened',
  )

  STATUSES = (
    ('NONE', 'Processing'),
    ('DRAFT', 'Draft'),
    ('SCHEDULED', 'Scheduled'),
    ('EXECUTING', 'Executing'),
    ('DONE', 'Sent'),
    ('ERROR', 'Error'),
    ('REMOVED', 'Removed'),
  )
  NAME_MAX_LENGTH = 80

  # API editable fields
  name = models.CharField(
    max_length=NAME_MAX_LENGTH,
    # unique=True,  # TODO PUSH: It seems like CTCT isn't enforcing this
    verbose_name=_('Name'),
  )
  scheduled_datetime = models.DateTimeField(
    blank=True,
    null=True,
    verbose_name=_('Scheduled'),
  )

  # API read-only fields
  current_status = models.CharField(
    choices=STATUSES,
    max_length=20,
    default='NONE',
    verbose_name=_('Current Status'),
  )
  created_at = models.DateTimeField(
    auto_now_add=True,
    verbose_name=_('Created At'),
  )
  updated_at = models.DateTimeField(
    auto_now=True,
    verbose_name=_('Updated At'),
  )
  sends = models.IntegerField(
    default=0,
    verbose_name=_('Sends'),
    help_text=_('The total number of unique sends'),
  )
  opens = models.IntegerField(
    default=0,
    verbose_name=_('Opens'),
    help_text=_('The total number of unique opens'),
  )
  clicks = models.IntegerField(
    default=0,
    verbose_name=_('Clicks'),
    help_text=_('The total number of unique clicks'),
  )
  forwards = models.IntegerField(
    default=0,
    verbose_name=_('Forwards'),
    help_text=_('The total number of unique forwards'),
  )
  optouts = models.IntegerField(
    default=0,
    verbose_name=_('Opt Out'),
    help_text=_('The total number of people who unsubscribed'),
  )
  abuse = models.IntegerField(
    default=0,
    verbose_name=_('Spam'),
    help_text=_('The total number of people who marked as spam'),
  )
  bounces = models.IntegerField(
    default=0,
    verbose_name=_('Bounces'),
    help_text=_('The total number of bounces'),
  )
  not_opened = models.IntegerField(
    default=0,
    verbose_name=_('Not Opened'),
    help_text=_('The total number of people who didn\'t open'),
  )

  class Meta:
    verbose_name = _('Email Campaign')
    verbose_name_plural = _('Email Campaigns')

    ordering = ('-updated_at', '-created_at', '-scheduled_datetime')

  def __str__(self) -> str:
    return self.name

  def clean(self):
    """Validate scheduled_datetime."""
    if (
      (self.current_status != 'DONE') and
      (self.scheduled_datetime is not None) and
      (self.scheduled_datetime < timezone.now() + dt.timedelta(minutes=30))
    ):
      message = (
        "Must schedule the campaign for at least 30 minutes in the future!"
      )
      raise ValidationError(message)

  @classmethod
  def clean_remote_counts(cls, stat: str, data: dict) -> int:
    return data.get('unique_counts', {}).get(stat, 0)

  @classmethod
  def clean_remote_sends(cls, data: dict) -> int:
    return cls.clean_remote_counts('sends', data)

  @classmethod
  def clean_remote_opens(cls, data: dict) -> int:
    return cls.clean_remote_counts('opens', data)

  @classmethod
  def clean_remote_clicks(cls, data: dict) -> int:
    return cls.clean_remote_counts('clicks', data)

  @classmethod
  def clean_remote_forwards(cls, data: dict) -> int:
    return cls.clean_remote_counts('forwards', data)

  @classmethod
  def clean_remote_optouts(cls, data: dict) -> int:
    return cls.clean_remote_counts('optouts', data)

  @classmethod
  def clean_remote_abuse(cls, data: dict) -> int:
    return cls.clean_remote_counts('abuse', data)

  @classmethod
  def clean_remote_bounces(cls, data: dict) -> int:
    return cls.clean_remote_counts('bounces', data)

  @classmethod
  def clean_remote_not_opened(cls, data: dict) -> int:
    return cls.clean_remote_counts('not_opened', data)

  @classmethod
  def clean_remote_current_status(cls, data: dict) -> str:
    if data.get('unique_counts'):
      current_status = 'DONE'
    else:
      current_status = data.get('current_status')
    return current_status

  @classmethod
  def clean_remote_scheduled_datetime(cls, data: dict) -> dt.datetime | None:
    if scheduled_datetime := data.get('last_sent_date'):
      # Not sure why this ts_format is different
      return to_dt(scheduled_datetime, ts_format='%Y-%m-%dT%H:%M:%S.000Z')


# DONE
class CampaignActivityRemoteManager(RemoteManager):
  """Extend RemoteManager to handle scheduling."""

  def create(self, obj: CampaignActivity) -> NoReturn:
    message = (
      "ConstantContact API does not support creating CampaignActivities. "
      "They are created during the creation of an EmailCampaign."
    )
    raise NotImplementedError(message)

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
      url=self.get_url(
        method='POST',
        api_id=obj.api_id,
        endpoint_suffix='/tests',
      ),
      json={
        'email_addresses': recipients,
        'personal_message': message,
      },
    )
    self.raise_or_json(response)

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
      message = "Must specify `scheduled_datetime`!"
      raise ValueError(message)

    # Receipients must be set before scheduling
    if update_first:
      response = self.update(obj)

    # Finally, schedule the CampaignActivity
    self.check_api_limit()
    response = self.session.post(
      url=self.get_url(
        method='POST',
        api_id=obj.api_id,
        endpoint_suffix='/schedules',
      ),
      json={'scheduled_date': obj.scheduled_datetime.isoformat()},
    )
    self.raise_or_json(response)

  def unschedule(self, obj: CampaignActivity) -> None:
    """Unschedules the `primary_email` CampaignActivity."""
    if obj.role == 'primary_email':
      self.delete(obj, endpoint_suffix='/schedules')
    else:
      message = (
        f"Cannot unschedule CampaignActivities with role '{obj.role}'."
      )
      raise ValueError(message)


# DONE
class CampaignActivity(CTCTModel):
  """Django implementation of a CTCT CampaignActivity.

  Notes
  -----
  The CTCT API is set up so that EmailCampaigns have multiple
  CampaignActivities ('primary_email', 'permalink', 'resend'). For
  our purposes, the `primary_email` CampaignActivity is the most
  important one, and as such the design of this model is primarily
  based off of them.

  """

  API_ENDPOINT = '/emails/activities'
  API_ID_LABEL = 'campaign_activity_id'
  API_EDITABLE_FIELDS = (
    'from_name',
    'from_email',
    'reply_to_email',
    'subject',
    'preheader',
    'html_content',
    'physical_address_in_footer',
  )
  API_READONLY_FIELDS = (
    'role',
    'current_status',
    'format_type',
  )
  API_GET_QUERIES = {
    'include': ','.join([
      # 'physical_address_in_footer',
      # 'permalink_url',
      'html_content',
      # 'document_properties',
    ]),
  }

  # Must explicitly specify both
  objects = models.Manager()
  remote = CampaignActivityRemoteManager()

  ROLES = (
    ('primary_email', 'Primary Email'),
    ('permalink', 'Permalink'),
    ('resend', 'Resent'),
  )
  FORMAT_TYPES = (
    (1, 'Custom code (API v2)'),
    (2, 'CTCT UI (2nd gen)'),
    (3, 'CTCT UI (3rd gen)'),
    (4, 'CTCT UI (4th gen)'),
    (5, 'Custom code (API v3)'),
  )
  MISSING_SUBJECT = 'No Subject'

  campaign = models.ForeignKey(
    EmailCampaign,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='campaign_activities',
    verbose_name=_('Campaign'),
  )

  # API editable fields
  from_name = models.CharField(
    max_length=255,
    default=settings.CTCT_FROM_NAME,
    verbose_name=_('From Name'),
  )
  from_email = models.EmailField(
    default=settings.CTCT_FROM_EMAIL,
    verbose_name=_('From Email'),
  )
  reply_to_email = models.EmailField(
    default=getattr(settings, 'CTCT_REPLY_TO_EMAIL', settings.CTCT_FROM_EMAIL),
    verbose_name=_('Reply-to Email'),
  )
  subject = models.CharField(
    max_length=200,
    verbose_name=_('Subject'),
    help_text=_(
      'The text to display in the subject line that describes the email '
      'campaign activity'
    ),
  )
  preheader = models.CharField(
    max_length=130,
    verbose_name=_('Preheader'),
    help_text=_(
      'Contacts will view your preheader as a short summary that follows '
      'the subject line in their email client'
    ),
  )
  html_content = models.CharField(
    max_length=int(15e4),
    verbose_name=_('HTML Content'),
    help_text=_('The HTML content for the email campaign activity'),
  )
  contact_lists = models.ManyToManyField(
    ContactList,
    through='CampaignActivityAndContactList',
    related_name='campaign_activities',
    verbose_name=_('Contact Lists'),
  )

  # API read-only fields
  role = models.CharField(
    max_length=25,
    choices=ROLES,
    default='primary_email',
    verbose_name=_('Role'),
  )
  current_status = models.CharField(
    choices=EmailCampaign.STATUSES,
    max_length=20,
    default='NONE',
    verbose_name=_('Current Status'),
  )
  format_type = models.IntegerField(
    choices=FORMAT_TYPES,
    default=5,  # CustomCode API v3
    verbose_name=_('Format Type'),
  )

  class Meta:
    verbose_name = _('Email Campaign Activity')
    verbose_name_plural = _('Email Campaign Activities')

    constraints = [
      models.UniqueConstraint(
        fields=['campaign', 'role'],
        name='unique_campaign_activity',
      ),
    ]

  @property
  def physical_address_in_footer(self) -> Optional[dict]:
    """Returns the company address for email footers.

    Notes
    -----
    If you do not include a physical address in the email campaign activity,
    Constant Contact will use the physical address information saved for the
    Constant Contact user account.

    """
    return getattr(settings, 'CTCT_PHYSICAL_ADDRESS', None)

  def __str__(self) -> str:
    try:
      s = f'{self.campaign}, {self.get_role_display()}'
    except EmailCampaign.DoesNotExist:
      s = super().__str__()
    return s

  def serialize(self, data: dict) -> dict:
    if self.api_id and (contact_lists := data.pop('contact_lists', None)):
      data['contact_list_ids'] = contact_lists
    return data

  @classmethod
  def clean_remote_string_with_default(
    cls,
    field_name: str,
    data: dict,
    default: Optional[str] = None,
  ) -> Optional[str]:

    if default is None:
      default = cls._meta.get_field(field_name).default
      if default is NOT_PROVIDED:
        message = _(
          f"Must provide a default value for {cls.__name__}.{field_name}."
        )
        raise ValueError(message)

    if field_name in data:
      # If ConstantContact sends a `None` value, we get the default value
      s = data[field_name] or default
    else:
      # A return value of `None` will remove the field from the cleaned dict
      s = None

    return s

  @classmethod
  def clean_remote_from_name(cls, data: dict) -> Optional[str]:
    return cls.clean_remote_string_with_default('from_name', data)

  @classmethod
  def clean_remote_from_email(cls, data: dict) -> Optional[str]:
    return cls.clean_remote_string_with_default('from_email', data)

  @classmethod
  def clean_remote_reply_to_email(cls, data: dict) -> Optional[str]:
    return cls.clean_remote_string_with_default('reply_to_email', data)

  @classmethod
  def clean_remote_subject(cls, data: dict) -> Optional[str]:
    """Pass a `default` here so it won't appear in admin forms."""
    default = cls.MISSING_SUBJECT
    return cls.clean_remote_string_with_default('subject', data, default)

  @classmethod
  def clean_remote_contact_lists(cls, data: dict) -> list[str]:
    return data.pop('contact_list_ids', [])


# DONE
class CampaignActivityAndContactList(models.Model):
  """Custom through model that uses CTCT API ids.

  Notes
  -----
  In order to bulk import the ManyToMany relationship between CampaignActivity
  and ContactList, we create instances of the associated ThroughModel using the
  API ids of CampaignActivity and ContactList. However, Django currently does
  not support setting `to_field` in the Django-created ThroughModel, so we must
  create our own ThroughModel in order specify the `to_field` values for the
  ForeignKeys.

  """

  is_through_model = True

  campaignactivity = models.ForeignKey(
    CampaignActivity,
    on_delete=models.CASCADE,
    to_field='api_id',
  )
  contactlist = models.ForeignKey(
    ContactList,
    on_delete=models.CASCADE,
    to_field='api_id',
  )
