from __future__ import annotations

from collections import defaultdict
import datetime as dt
from typing import Literal, Optional, Self

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
from django.db.models.query import QuerySet
from django.db.models.signals import post_save, m2m_changed, pre_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone, formats
from django.utils.translation import gettext_lazy as _

from phonenumber_field.phonenumber import PhoneNumber
from phonenumber_field.modelfields import PhoneNumberField

from django_ctct.utils import to_dt, get_related_fields

from django_ctct.tasks import (
  ctct_save_job, ctct_delete_job, ctct_rename_job,
  ctct_update_lists_job, ctct_add_list_memberships_job,
)


HttpMethod = Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE']


# TODO 1: Remove setup()
class APIManager(models.Manager):

  API_URL = 'https://api.cc.email'
  API_VERSION = '/v3'
  API_LIMIT_CALLS = 4   # four calls
  API_LIMIT_PERIOD = 1  # per second

  TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

  def setup(self) -> None:
    self.session = requests.Session()
    self.session.headers.update(Token.get().authorization_header)

  def serialize(self, obj: Model) -> dict:
    """Convert from Django object to API request body."""

    data = {}

    for field_name in obj.API_BODY_FIELDS:
      field = self.model._meta.get_field(field_name)
      if field_name.endswith('_id'):
        # Convert related object UUID to string
        value = str(getattr(obj, field_name))
      elif isinstance(field, models.DateTimeField):
        # Convert datetime to string
        value = getattr(obj, field_name).strftime(self.TS_FORMAT)
      elif not field.is_relation:
        value = getattr(obj, field_name)
      elif field.one_to_many or field.many_to_many:
        if obj.pk:
          serialize = field.related_model.remote.serialize
          value = [serialize(_) for _ in getattr(obj, field_name).all()]
        else:
          value = []
      elif field.one_to_one or field.many_to_one:
        serialize = field.related_model.remote.serialize
        value = serialize(getattr(obj, field_name))
      data[field_name] = value

    # Allow individual models to override serialization
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
        data[field.name] = clean(data)

    # Restrict to the fields defined in the Django object
    data = {
      k: v for k, v in data.items()
      if k in [f.name for f in model_fields]
    }

    # Set related objects
    data = self.deserialize_related_obj_fields(data)
    data, related_objs = self.deserialize_related_objs_fields(data)

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
    In the case of ManyToManyFields, we typically just return a list of
    `api_id`s, and create ThroughModel instances.

    """

    related_objs = {}

    _, mtms, _, rfks = get_related_fields(self.model)
    for field in filter(lambda f: f.name in data, mtms + rfks):

      if related_data := data.pop(field.name):

        if all(isinstance(_, str) for _ in related_data):
          # ManyToManyField, just keep api_ids
          related_objs[field.related_model] = related_data
        elif all(isinstance(_, dict) for _ in related_data):
          # Add in the parent object's `api_id`
          parent = {field.remote_field.name: data['api_id']}
          deserialize = field.related_model.remote.deserialize
          objs = [deserialize(datum | parent)[0] for datum in related_data]
          related_objs[field.related_model] = objs
        else:
          # Mix of dict and str
          raise NotImplementedError

    return data, related_objs

  def get_url(
    self,
    method: HttpMethod,
    api_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    suffix_endpoint: Optional[str] = None,
  ) -> str:
    endpoint = endpoint or self.model.API_ENDPOINT
    if not endpoint.startswith(self.API_VERSION):
      endpoint = f'{self.API_VERSION}{endpoint}'

    url = f'{self.API_URL}{endpoint}'

    if api_id:
      url += f'/{api_id}'

    if suffix_endpoint:
      url += f'{suffix_endpoint}'

    if (method == 'GET') and ('?' not in url) and self.model.API_GET_QUERIES:
      url += '?'
      for name, value in self.model.API_GET_QUERIES.items():
        url += f'{name}={value}'

    return url

  def raise_or_json(self, response: Response) -> Optional[dict]:
    if response.status_code == 204:
      data = None
    else:
      data = response.json()

    try:
      response.raise_for_status()
    except HTTPError:
      raise HTTPError(data[0]['error_message'])

    return data

  @sleep_and_retry
  @limits(calls=API_LIMIT_CALLS, period=API_LIMIT_PERIOD)
  def check_api_limit(self) -> None:
    """Honor the API's rate limit."""
    pass

  # TODO: save related_objs?
  def create(self, obj: Model) -> Model:
    """Create existing Django object on the remote server.

    Notes
    -----
    This method saves the API's response to the local database in order to
    preserve values calculated by the API (e.g. `api_id`).

    """

    if not (pk := obj.pk):
      raise ValueError('Must create object locally first!')
    response = self.session.post(
      url=self.get_url(method='POST'),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data)
    obj.pk = pk
    obj.save()

    return obj

  def get(self, api_id: str) -> (Model, dict):
    """Get an existing object from the remote server.

    Notes
    -----
    This method will not save the object to the local database. We return the
    object as well as a dictionary of the form {field_name: [RelatedModel()]}.

    """

    url = self.get_url(method='GET', api_id=api_id)
    response = self.session.get(url)
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data)

    return obj, related_objs

  def all(self) -> (list[Model], dict):
    """Get all existing objects from the remote server.

    Notes
    -----
    This method will not save the object to the local database. We return
    objects as well as a dictionary of the form {field_name: [RelatedModel()]}.

    """

    objs, related_objs = [], defaultdict(list)

    paginated, endpoint = True, None
    while paginated:
      url = self.get_url(method='GET', endpoint=endpoint)
      self.check_api_limit()
      response = self.session.get(url)
      data = self.raise_or_json(response)

      # Data contains two keys: '_links' and e.g. 'contacts',  'lists', etc
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

  # TODO: save related_objs?
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

    response = self.session.put(
      url=self.get_url(method='PUT', api_id=obj.api_id),
      json=self.serialize(obj),
    )
    data = self.raise_or_json(response)

    obj, related_objs = self.deserialize(data)
    obj.pk = pk
    obj.save()

    return obj

  def delete(self, obj: Model, suffix_endpoint: Optional[str] = None) -> None:
    """Delete existing Django object on the remote server.

    Notes
    -----
    This method can be used to delete sub-resources of an object (such as a
    scheduled EmailCampaign) via the optional `suffix_endpoint` param.

    We ignore 404 responses in the situation that the remote object has already
    been deleted.

    """

    url = self.get_url(
      method='DELETE',
      api_id=obj.api_id,
      suffix_endpoint=suffix_endpoint,
    )
    response = self.session.delete(url)

    if response.status_code != 404:
      # Allow 404
      self.raise_or_json(response)

    return None


class CTCTModel(Model):
  """Common CTCT model methods and properties."""

  API_URL = 'https://api.cc.email'
  API_VERSION = '/v3'
  API_GET_QUERIES = {}

  save_to_ctct: Optional[Literal['sync', 'async']] = 'async'

  api_id = models.UUIDField(
    null=True,     # Allow objects to be created without CTCT IDs
    default=None,  # Models often created without CTCT IDs
    unique=True,   # Note: None != None for uniqueness check
    blank=True,
  )

  objects = models.Manager()
  remote = APIManager()

  class Meta:
    abstract = True

  def ctct_save(self) -> dict:
    """Create or Update on CTCT servers."""
    if not self.api_id:
      ctct_obj = self.ctct_create()
    else:
      ctct_obj = self.ctct_update()
    return ctct_obj

  def save(self, *args, **kwargs) -> None:
    self.save_to_ctct = kwargs.pop('save_to_ctct', self.save_to_ctct)
    super().save(*args, **kwargs)


class CTCTLocalModel(CTCTModel):
  """Local model without remote saving support."""

  class Meta(CTCTModel.Meta):
    abstract = True

  def ctct_save(self) -> dict:
    return {}


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
    data = APIManager.raise_or_json(response)

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

# TODO: ctct_add_list_memberships
class ContactList(CTCTModel):
  """Django implementation of a CTCT Contact List."""

  API_ENDPOINT = '/contact_lists'
  API_ID_LABEL = 'list_id'
  API_BODY_FIELDS = (
    'name',
    'description',
    'favorite',
  )
  API_MAX_NUM = 500

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

  def ctct_add_list_memberships(
    self,
    contacts: QuerySet['Contact']
  ) -> list(dict):
    """Adds multiple Contacts to a single ContactList."""

    responses = []
    contact_ids = list(map(str, contacts.values_list('api_id', flat=True)))
    for i in range(0, len(contact_ids), self.API_MAX_NUM):
      response = requests.post(
        url=self.get_url(
          method='POST',
          endpoint='/activities/add_list_memberships',
        ),
        headers=self.headers,
        json={
          'source': {
            'contact_ids': contact_ids[i:i + self.API_MAX_NUM],
          },
          'list_ids': [str(self.api_id)],
        },
      )
      responses.append(self.raise_or_json(response))
    return responses


# DONE
class CustomField(CTCTModel):
  """Django implementation of a CTCT Contact's CustomField."""

  API_ENDPOINT = '/contact_custom_fields'
  API_ID_LABEL = 'custom_field_id'
  API_BODY_FIELDS = (
    'label',
    'type',
  )

  TYPES = (
    ('string', 'Text'),
    ('date', 'Date'),
  )

  label = models.CharField(
    max_length=50,
    verbose_name=_('Label'),
    help_text=_(
      'The display name for the custom_field shown in the UI as free-form text'
    ),
  )
  name = models.CharField(
    max_length=50,
    editable=False,
    verbose_name=_('Name'),
    help_text=_(
      'Unique name constructed by replacing blanks with underscores'
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
  created_at = models.DateTimeField(
    auto_now_add=True,
    verbose_name=_('Created At'),
  )
  updated_at = models.DateTimeField(
    auto_now=True,
    verbose_name=_('Updated At'),
  )

  def __str__(self) -> str:
    return self.label


# TODO: ctct_update_or_create, ctct_update_lists
class Contact(CTCTModel):
  """Django implementation of a CTCT Contact."""

  API_ENDPOINT = '/contacts'
  API_ID_LABEL = 'contact_id'
  API_BODY_FIELDS = (
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
    related_name='contacts',
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

  def serialize(self, data: dict) -> dict:
    data['email_address'] = {
      'address': self.email,
      'permission_to_send': self.permission_to_send,
    }
    data.update(self.ctct_source)
    return data

  def ctct_create(self) -> dict:
    """Redirect to the 'update_or_create' endpoint.

    Notes
    -----
    Since CTCT could already have entries for Contacts that do not have Django
    objects yet, we use CTCT's `update_or_create` method which will re-activate
    any contacts that were previously added to CTCT and then later deleted
    ('deactivated' to use CTCT's term).

    """
    return self.ctct_update_or_create()

  def ctct_update(self) -> dict:
    """Redirect to the 'update_or_create' endpoint.

    Notes
    -----
    While CTCT does support using PUT request to /contact/{api_id}, we defer to
    using `update_or_create` until it's necessary to implement the PUT method.

    """
    return self.ctct_update_or_create()

  def ctct_update_or_create(self) -> dict:
    """Update or create a Contact on CTCT servers.

    Notes
    -----

    Use this method to create a new contact or update an existing contact. This
    method uses the email_address string value you include in the request body
    to determine if it should create an new contact or update an existing
    contact.

    Updates to existing contacts are partial updates. This method only updates
    the contact properties you include in the request body. Updates append new
    contact lists or custom fields to the existing list_memberships or
    custom_fields arrays.

    This is basically a CTCTModel.ctct_create() method with a different
    API_ENDPOINT.

    """
    endpoint = self.API_ENDPOINT
    self.API_ENDPOINT += '/sign_up_form'
    try:
      ctct_obj = super().ctct_create()
    except Exception as e:
      exception = e
    else:
      # Response only contains CTCT ID and action status
      exception = None
      ctct_obj['email'] = self.email
    finally:
      self.API_ENDPOINT = endpoint

    if exception:
      raise exception

    return ctct_obj

  # NOTE: This is the inverse of add_list_memberships()
  def ctct_update_lists(self) -> Optional[dict]:
    """Update Contact and ContactList membership on CTCT servers.

    Notes
    -----
    The PUT call will overwrite all properties not included in the request
    body with NULL, so we need to make sure the `serialize()` method
    includes all important fields. While the `create_or_update()` method
    supports partial updates, it won't allow us to remove a ContactList,
    so we must use the PUT method for that.

    CTCT requires that all contacts be a member of at least one ContactList,
    so in the event of removing someone from all lists, we should actually
    issue a DELETE call; however, these 'deleted' Contacts retain their ID
    in ConstantContact's database and can be revived at any time.

    """

    if self.opted_out:
      response = {}
    elif not self.list_memberships.exists():
      # CTCT requires that Contacts be a member of at least one ContactList
      self.ctct_delete()
      return None
    else:
      response = requests.put(
        url=self.get_url(method='PUT'),
        headers=self.headers,
        json=self.serialize(method='PUT'),
      )
      ctct_obj = self.raise_or_json(response)
      return ctct_obj


# DONE
class ContactNote(CTCTLocalModel):
  """Django implementation of a CTCT Note."""

  API_ID_LABEL = 'note_id'
  API_BODY_FIELDS = (
    'note_id',
    'created_at',
    'content',
  )

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='notes',
    verbose_name=_('Contact'),
  )
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
  author = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    null=True,
    verbose_name=_('Author'),
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
  API_BODY_FIELDS = (
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
      models.UniqueConstraint(
        fields=['contact', 'kind'],
        name='unique_phone_number',
      ),
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
  API_BODY_FIELDS = (
    'kind',
    'street',
    'city',
    'state',
    'postal_code',
    'country',
  )

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
      models.UniqueConstraint(
        fields=['contact', 'kind'],
        name='unique_street_address',
      ),
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
  It's important to specify `custom_field_id` in `API_BODY_FIELDS` instead
  of `custom_field`; using the latter will result in a serialized CustomField
  instance when Contact is serialized.

  """

  API_ID_LABEL = None
  API_BODY_FIELDS = (
    'custom_field_id',
    'value',
  )

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='custom_fields',
    verbose_name=_('Contact'),
  )
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
      models.UniqueConstraint(
        fields=['contact', 'custom_field'],
        name='unique_custom_field',
      ),
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


# TODO 1: Deserialize
# TODO: ctct_update, ctct_create, ctct_rename
class EmailCampaign(CTCTModel):
  """Django implementation of a CTCT EmailCampaign."""

  API_ENDPOINT = '/emails'
  API_ID_LABEL = 'campaign_id'

  STATUSES = (
    ('NONE', 'Processing'),
    ('DRAFT', 'Draft'),
    ('SCHEDULED', 'Scheduled'),
    ('EXECUTING', 'Executing'),
    ('DONE', 'Sent'),
    ('ERROR', 'Error'),
    ('REMOVED', 'Removed'),
  )
  ACTIONS = (
    ('NONE', 'Select Action'),
    ('CREATE', 'Create Draft'),
    ('SCHEDULE', 'Schedule'),
    ('UNSCHEDULE', 'Unschedule'),
  )
  ACTIONS_FROM_STATUS = {
    'NONE': ACTIONS[:3],
    'DRAFT': ACTIONS[:1] + ACTIONS[2:4],
    'SCHEDULED': ACTIONS[:1] + ACTIONS[3:4],
    'UNSCHEDULED': ACTIONS[:1] + ACTIONS[2:3],
    'EXECUTING': ACTIONS[:1],
    'DONE': ACTIONS[:1],
    'ERROR': ACTIONS[:1],
    'REMOVED': ACTIONS[:1],
  }
  NAME_MAX_LENGTH = 80

  name = models.CharField(
    max_length=NAME_MAX_LENGTH,
    unique=True,
    verbose_name=_('Name'),
  )
  current_status = models.CharField(
    choices=STATUSES,
    max_length=20,
    default='NONE',
    verbose_name=_('Campaign Status'),
  )
  action = models.CharField(
    choices=ACTIONS,
    max_length=20,
    default='NONE',
    verbose_name=_('Action'),
  )
  scheduled_datetime = models.DateTimeField(
    blank=True,
    null=True,
    verbose_name=_('Scheduled'),
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
  opt_outs = models.IntegerField(
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

    ordering = ('-scheduled_datetime', )

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
  def deserialize(self, ctct_obj) -> dict:
    """Add extra data from the /email_campaign_summaries/ API endpoint"""
    if counts := ctct_obj.get('unique_counts'):
      ctct_obj.update(counts)
      ctct_obj['current_status'] = 'DONE'
    return super().deserialize(ctct_obj)

  def ctct_create(self) -> dict:
    """Creates the CTCT EmailCampaign and sets relevant model fields.

    Notes
    -----
    This method will also create the new `primary_email` and `permalink`
    CampaignActivities on CTCT and associate the `primary_email` one
    with the new EmailCampaign in the database.

    """

    # Set name and initialize the CampaignActivity object (do not create it!)
    activity = CampaignActivity(
      campaign=self,
      role='primary_email',
    )

    # Create EmailCampaign (and CampaignActivities) on CTCT servers
    response = requests.post(
      url=self.get_url(method='POST'),
      headers=self.headers,
      json={
        'name': self.name,
        'email_campaign_activities': [activity.serialize()],
      },
    )

    if response.status_code == 409:
      # EmailCampaign name not unique, fetch existing EmailCampaign from CTCT
      ctct_obj = self.ctct_read(name=self.name)
    else:
      ctct_obj = self.raise_or_json(response)

    # Set field values that `deserialize(ctct_obj)` doesn't have access to
    ctct_obj['action'] = self.action

    # Get the associated CampaignActivity ID and save
    for campaign_activity in ctct_obj['campaign_activities']:
      if campaign_activity['role'] == 'primary_email':
        activity.api_id = campaign_activity['campaign_activity_id']
        activity.save(save_to_ctct=False)
        break

    # Schedule and send preview
    if self.action == 'CREATE':
      activity.ctct_send_preview()
      self.current_status = 'DRAFT'
    elif self.action == 'SCHEDULE':
      activity.ctct_send_preview()
      activity.ctct_schedule()
      self.current_status = 'SCHEDULED'
    elif self.campaign.action == 'UNSCHEDULE':
      activity.ctct_unschedule()
      self.current_status = 'DRAFT'
    self.action = 'NONE'

    # Update CTCT response with local field values
    ctct_obj.update({
      'current_status': self.current_status,
      'action': self.action,
    })

    return ctct_obj

  def ctct_update(self):
    """Update associated CampaignActivity on CTCT servers."""
    if self.action in ['SCHEDULE', 'UNSCHEDULE']:
      activity = self.activities.get(role='primary_email')
      method_name = f'ctct_{self.action.lower()}'
      getattr(activity, method_name)()

  def ctct_rename(self) -> dict:
    """Rename EmailCampaign on CTCT servers."""
    response = requests.patch(
      url=self.get_url(method='PATCH'),
      headers=self.headers,
      json={'name': self.name},
    )
    ctct_obj = self.raise_or_json(response)
    return ctct_obj


# TODO: ctct_save, ctct_update, ctct_send_preview, ctct_schedule/unschedule
# TODO: get_preview_recipients, get_preview_message
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
  API_BODY_FIELDS = (
    'format_type',
    'from_name',
    'from_email',
    'reply_to_email',
    'subject',
    'html_content',
    'preheader',
    'physical_address_in_footer',
  )

  ROLES = (
    ('primary_email', 'Primary Email'),
    ('permalink', 'Permalink'),
    ('resend', 'Resent'),
  )
  MODERN_CUSTOM_CODE = 5

  campaign = models.ForeignKey(
    EmailCampaign,
    on_delete=models.CASCADE,
    to_field='api_id',
    related_name='activities',
    verbose_name=_('Campaign'),
  )
  role = models.CharField(
    max_length=25,
    choices=ROLES,
    default='primary_email',
    verbose_name=_('Role'),
  )
  contact_lists = models.ManyToManyField(
    ContactList,
    verbose_name=_('Contact Lists'),
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

  format_type = MODERN_CUSTOM_CODE
  from_email = settings.CTCT_FROM_EMAIL
  reply_to_email = getattr(settings, 'CTCT_REPLY_TO_EMAIL', from_email)
  from_name = settings.CTCT_FROM_NAME

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

  def ctct_save(self) -> None:
    """Updates CampaignActivity on CTCT servers.

    Notes
    -----
    Unlike other models, this method does NOT return a `ctct_obj`
    dictionary since the "schedule" and "unschedule" responses
    from CTCT do not contain the entire CampaignActivity object.

    """

    if self.role != 'primary_email':
      message = (
        f'CampaignActivity with role `{self.role}` not supported yet.'
      )
      raise NotImplementedError(message)
    else:
      # Primary Email Activities can only be updated, not created
      self.ctct_update()

  def ctct_update(self) -> dict:
    """Update CampaignActivity on CTCT servers."""

    # Can only update if status is in DRAFT or SENT status
    if self.campaign.current_status == 'SCHEDULED':
      self.ctct_unschedule()

    # Update the CampaignActivity and send updated preview
    ctct_obj = super().ctct_update()
    self.ctct_send_preview()

    # Re-schedule if EmailCampaign was originally scheduled
    if self.campaign.current_status == 'SCHEDULED':
      self.ctct_schedule()

    return ctct_obj

  def ctct_send_preview(self, user) -> None:
    """Sends preview email for approval."""
    response = requests.post(
      url=self.get_url(method='POST', suffix_endpoint='/tests'),
      headers=self.headers,
      json={
        'email_addresses': self.get_preview_recipients(),
        'personal_message': self.get_preview_message(user),
      },
    )
    self.raise_or_json(response)

  def ctct_schedule(self) -> None:
    """Schedules the `primary_email` CampaignActivity.

    Notes
    -----
    Recipients must be set before scheduling.

    """

    if self.campaign.scheduled_datetime is None:
      message = "Must specify `scheduled_datetime`!"
      raise ValueError(message)

    # Set recipients on CTCT
    response = requests.put(
      url=self.get_url(method='PUT'),
      headers=self.headers,
      json=self.serialize(),
    )
    self.raise_or_json(response)

    # Then schedule the CampaignActivity
    response = requests.post(
      url=self.get_url(method='POST', suffix_endpoint='/schedules'),
      headers=self.headers,
      json={'scheduled_date': self.campaign.scheduled_datetime.isoformat()},
    )
    self.raise_or_json(response)

  def ctct_unschedule(self) -> None:
    """Unschedules the `primary_email` CampaignActivity."""
    super().ctct_delete(suffix_endpoint='/schedules')

  def get_preview_recipients(self) -> list[str]:
    """Determines who receives the CTCT preview emails."""
    recipients = getattr(settings, 'CTCT_PREVIEW_RECIPIENTS', settings.MANAGERS)  # noqa: 501
    return [email for (name, email) in recipients]

  def get_preview_message(self, user) -> str:
    """Writes the message sent with preview emails."""
    message = _(
      f'Please let {user.first_name} {user.last_name} know if you have edits.'
    )
    if self.campaign.current_status == 'SCHEDULED':
      datetime = self.campaign.scheduled_datetime.strftime(
        settings['DATETIME_FORMAT']
      )
      message += _(
        f' This campaign is scheduled to be sent on {datetime}.'
      )
    return message


# @receiver(post_save)
# def ctct_save_signal(
#   sender: Type[Model],
#   instance: Model,
#   created: bool,
#   update_fields: Optional[list],
#   **kwargs,
# ) -> None:
#   """Create or update the instance on CTCT servers."""
#   if isinstance(instance, CTCTLocalModel):
#     return
#   elif isinstance(instance, EmailCampaign) and (update_fields == ['name']):
#     if instance.save_to_ctct == 'sync':
#       ctct_rename_job(instance)
#     elif instance.save_to_ctct == 'async':
#       ctct_rename_job.delay(instance)
#   elif isinstance(instance, CTCTModel):
#     if instance.save_to_ctct == 'sync':
#       ctct_save_job(instance)
#     elif instance.save_to_ctct == 'async':
#       ctct_save_job.delay(instance)
#
#
# @receiver(pre_delete)
# def ctct_delete_signal(sender, instance, **kwargs) -> None:
#   """Delete the instance from CTCT servers."""
#   if isinstance(instance, CTCTLocalModel):
#     return
#   elif isinstance(instance, CTCTModel):
#     if instance.save_to_ctct == 'sync':
#       ctct_delete_job(instance)
#     elif instance.save_to_ctct == 'async':
#       ctct_delete_job.delay(instance)
#
#
# @receiver(m2m_changed, sender=Contact.list_memberships.through)
# def ctct_update_contact_lists(sender, instance, action, **kwargs):
#   """Updates a Contact's list membership on CTCT servers."""
#
#   if action in ['post_add', 'post_remove', 'post_clear']:
#
#     if isinstance(instance, Contact):
#       if instance.save_to_ctct == 'sync':
#         ctct_update_lists_job(instance)
#       elif instance.save_to_ctct == 'async':
#         ctct_update_lists_job.delay(instance)
#
#     elif isinstance(instance, ContactList):
#       contacts = Contact.objects.filter(pk__in=kwargs['pk_set'])
#       if instance.save_to_ctct == 'async':
#         ctct_add_list_memberships_job(instance, contacts)
#       elif instance.save_to_ctct == 'async':
#         ctct_add_list_memberships_job.delay(instance, contacts)
