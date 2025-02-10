from __future__ import annotations

import datetime as dt
from typing import Type, Literal, Optional
import uuid

import jwt
import pytz
import requests
from requests.exceptions import HTTPError
from requests.models import Response

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models
from django.db.models.query import QuerySet
from django.db.models.signals import post_save, m2m_changed, pre_delete
from django.dispatch import receiver
from django.forms import model_to_dict
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from phonenumber_field.modelfields import PhoneNumberField

from django_ctct.utils import to_dt

from django_ctct.tasks import (
  ctct_save_job, ctct_delete_job, ctct_rename_job,
  ctct_update_lists_job, ctct_add_list_memberships_job,
)


class SerializerMixin:
  """Converts between CTCT API responses and Django models."""

  API_BODY_FIELDS = tuple()
  TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

  def serialize(self) -> dict:
    """Convert from Django object to expected CTCT request body."""

    data = {}

    for field in filter(
      lambda f: f.name in self.API_BODY_FIELDS,
      self._meta.get_fields()
    ):
      if isinstance(field, models.DateTimeField):
        value = getattr(self, field.name).strftime(self.TS_FORMAT)
      elif not field.is_relation:
        value = getattr(self, field.name)
      elif field.one_to_many or field.many_to_many:
        value = [o.serialize() for o in getattr(self, field.name).all()]
      elif field.one_to_one or field.many_to_one:
        value = o.serialize
      data[field.name] = value

    return data

  @classmethod
  def deserialize(cls, ctct_obj: dict) -> dict:
    """Convert CTCT response body to model field values."""
    data = {}
    for field in cls._meta.fields:
      if value := ctct_obj.get(field.name):
        if isinstance(field, models.DateTimeField):
          value = to_dt(value)
        data[field.name] = value
    data['id'] = ctct_obj[cls.API_ID_LABEL]
    return data

  @classmethod
  def from_ctct(
    cls,
    ctct_obj: dict,
    save: bool = True,
  ) -> CTCTModel:
    """Returns a Django model instance based on CTCT API object."""

    data = cls.deserialize(ctct_obj)
    try:
      django_obj = cls.objects.get(id=data['id'])
    except cls.DoesNotExist:
      if cls is Contact:
        try:
          django_obj = Contact.objects.get(email=data['email'])
        except Contact.DoesNotExist:
          django_obj = Contact(id=data['id'])
      elif cls is EmailCampaign:
        try:
          django_obj = EmailCampaign.objects.get(id=data['id'])
        except EmailCampaign.DoesNotExist:
          django_obj = EmailCampaign(id=data['id'])
      else:
        django_obj = cls()

    for field_name, value in data.items():
      setattr(django_obj, field_name, value)

    if save:
      django_obj.save(save_to_ctct=False)

    return django_obj


class CTCTModel(SerializerMixin, models.Model):
  """Common CTCT model methods and properties."""

  BASE_URL = 'https://api.cc.email'

  save_to_ctct: Optional[Literal['sync', 'async']] = 'async'

  _id = models.AutoField(
    primary_key=True,
  )
  id = models.UUIDField(
    null=True,     # Allow objects to be created without CTCT IDs
    default=None,  # Models often created without CTCT IDs
    unique=True,   # Note: None != None for uniqueness check
    blank=True,
  )

  class Meta:
    abstract = True

  def ctct_save(self) -> dict:
    """Create or Update on CTCT servers."""
    if not self.id:
      ctct_obj = self.ctct_create()
    else:
      ctct_obj = self.ctct_update()
    return ctct_obj

  def save(self, *args, **kwargs) -> None:
    self.save_to_ctct = kwargs.pop('save_to_ctct', self.save_to_ctct)
    super().save(*args, **kwargs)

  @property
  def headers(self) -> dict:
    """Returns the authorization headers necessary for CTCT API."""
    token = Token.get()
    headers = {
      'Authorization': f"{token.type} {token.access_code}",
    }
    return headers

  @classmethod
  def raise_or_json(cls, response: Response) -> dict:
    """Extends `response.raise_for_status` to provide a better error report."""

    try:
      response.raise_for_status()
    except HTTPError as e:
      message = e.args[0]

      # Convert to list for consistency
      errors = e.response.json()
      if type(errors) is dict:
        errors = [errors]

      for error in errors:
        for key, value in error.items():
          message += f'\n{key}: {value}'
      raise HTTPError(message, response=response)

    if response.status_code == 204:
      response = {}
    else:
      try:
        response = response.json()
      except ValueError:
        # Response is not valid JSON
        pass

    return response

  def ctct_create(self) -> dict:
    response = requests.post(
      url=f'{self.BASE_URL}{self.API_ENDPOINT}',
      headers=self.headers,
      json=self.serialize(),
    )
    ctct_obj = self.raise_or_json(response)

    # Set CTCT id in Django database
    if not self.id:
      self.id = ctct_obj[self.API_ID_LABEL]
    return ctct_obj

  def ctct_read(self) -> dict:
    if not self.id:
      raise AttributeError(f"{self} has no id.")

    response = requests.get(
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}',
      headers=self.headers,
    )
    ctct_obj = self.raise_or_json(response)
    return ctct_obj

  def ctct_update(self) -> dict:
    response = requests.put(
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}',
      headers=self.headers,
      json=self.serialize(),
    )
    ctct_obj = self.raise_or_json(response)
    return ctct_obj

  def ctct_delete(self, suffix: str = '') -> None:
    response = requests.delete(
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}{suffix}',
      headers=self.headers,
    )
    try:
      self.raise_or_json(response)
    except HTTPError as e:
      # Allow 404 (object not on CTCT servers)
      if e.response.status_code == 404:
        pass
      else:
        raise e


class CTCTLocalModel(CTCTModel):
  """Local model without remote saving support."""

  class Meta(CTCTModel.Meta):
    abstract = True

  def ctct_save(self) -> dict:
    return {}


@receiver(post_save)
def ctct_save_signal(
  sender: Type[models.Model],
  instance: models.Model,
  created: bool,
  update_fields: Optional[list],
  **kwargs,
) -> None:
  """Create or update the object on CTCT servers."""
  if isinstance(instance, CTCTLocalModel):
    return
  elif isinstance(instance, EmailCampaign) and (update_fields == ['name']):
    if instance.save_to_ctct == 'sync':
      ctct_rename_job(instance)
    elif instance.save_to_ctct == 'async':
      ctct_rename_job.delay(instance)
  elif isinstance(instance, CTCTModel):
    if instance.save_to_ctct == 'sync':
      ctct_save_job(instance)
    elif instance.save_to_ctct == 'async':
      ctct_save_job.delay(instance)


class Token(models.Model):
  """Authorization token for CTCT API access.

  Notes
  -----
  To get the latest Token, use the `get()` class method.

  """

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
    # TODO PUSH: Remove pytz in favor of timezone?
    tz = pytz.timezone(settings.TIME_ZONE)
    datetime = self.inserted.astimezone(tz)
    return f'{datetime:%a %d @ %I:%M %p}'

  def decode(self) -> dict:
    """Decode JWT Token, which also verifies that it hasn't expired."""

    client = jwt.PyJWKClient(
      'https://identity.constantcontact.com/'
      'oauth2/aus1lm3ry9mF7x2Ja0h8/v1/keys'
    )
    signing_key = client.get_signing_key_from_jwt(self.access_code)
    data = jwt.decode(
      self.access_code,
      signing_key.key,
      algorithms=['RS256'],
      audience='https://api.cc.email/v3',
    )
    return data

  def refresh(self) -> 'Token':
    """Obtain a new Token from CTCT using the refresh code."""

    response = requests.post(
      url='https://authz.constantcontact.com/oauth2/default/v1/token',
      auth=(settings.CTCT_PUBLIC_KEY, settings.CTCT_SECRET_KEY),
      data={
        'refresh_token': self.refresh_code,
        'grant_type': 'refresh_token',
      },
    )
    ctct_obj = self.raise_or_json(response)

    # Create new Token for future use
    if 'refresh_token' in ctct_obj:
      token = Token.objects.create(
        access_code=ctct_obj['access_token'],
        refresh_code=ctct_obj['refresh_token'],
        type=ctct_obj['token_type'],
      )
    else:
      message = (
        "Token does not contain `refresh_token`.\n"
        f"{ctct_obj}"
      )
      raise ValueError(message)
    return token

  @classmethod
  def get(cls) -> 'Token':
    """Fetches most recent token, refreshing if necessary."""

    token = Token.objects.first()
    if not token:
      message = (
        "No tokens in the database yet. You must "
        f"visit {reverse('ctct:auth')} and sign in to "
        "ConstantContact to create the initial token."
      )
      raise ValueError(message)

    try:
      token.decode()
    except jwt.ExpiredSignatureError:
      token = token.refresh()

    return token

  @classmethod
  def raise_or_json(cls, response: Response) -> dict:
    """Extends `response.raise_for_status` to provide a better error report.

    Notes
    -----
    This is duplicated from CTCTModel since Token doesn't inherit.

    """

    try:
      response.raise_for_status()
    except HTTPError as e:
      message = e.args[0]

      # Convert to list for consistency
      errors = e.response.json()
      if type(errors) is dict:
        errors = [errors]

      for error in errors:
        for key, value in error.items():
          message += f'\n{key}: {value}'
      raise HTTPError(message, response=response)

    try:
      response = response.json()
    except ValueError:
      # Response is not valid JSON
      pass

    return response


class ContactList(CTCTModel):
  """Django implementation of a CTCT Contact List."""

  API_ENDPOINT = '/v3/contact_lists'
  API_ID_LABEL = 'list_id'
  API_BODY_FIELDS = (
    'name',
    'description',
    'favorite',
  )

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

    CTCT_MAX = 500

    responses = []
    contact_ids = list(map(str, contacts.values_list('id', flat=True)))
    for i in range(0, len(contact_ids), CTCT_MAX):
      response = requests.post(
        url=f'{self.BASE_URL}/v3/activities/add_list_memberships',
        headers=self.headers,
        json={
          'source': {
            'contact_ids': contact_ids[i:i + CTCT_MAX],
          },
          'list_ids': [str(self.id)],
        },
      )
      responses.append(self.raise_or_json(response))
    return responses


# TODO after tests: Add this to sync_ctct
class CustomField(CTCTModel):
  """Django implementation of a CTCT Contact's CustomField."""

  API_ENDPOINT = '/v3/contact_custom_fields'
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


@receiver(pre_delete)
def ctct_delete_signal(sender, instance, **kwargs) -> None:
  """Delete the instance from CTCT servers."""
  if isinstance(instance, CTCTLocalModel):
    return
  elif isinstance(instance, CTCTModel):
    if instance.save_to_ctct == 'sync':
      ctct_delete_job(instance)
    elif instance.save_to_ctct == 'async':
      ctct_delete_job.delay(instance)


class Contact(CTCTModel):
  """Django implementation of a CTCT Contact."""

  API_ENDPOINT = '/v3/contacts'
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

  SALUTATIONS = (
    ('Mr.', 'Mr.'),
    ('Ms.', 'Ms.'),
    ('Dr.', 'Dr.'),
    ('Hon.', 'The Honorable'),
    ('Amb.', 'Ambassador'),
    ('Prof.', 'Professor'),
  )
  SOURCES = (
    ('Contact', 'Contact'),
    ('Account', 'Account'),
  )
  CUSTOM_FIELD_NAMES = (
    'honorific',
    'suffix',
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

  # TODO PUSH: Review opt_out in CTCT's API
  opt_out = models.BooleanField(
    default=False,
    editable=False,
    verbose_name=_('Opted Out'),
    help_text=_('Handled by ConstantContact'),
  )
  opt_out_date = models.DateTimeField(
    blank=True,
    null=True,
    verbose_name=_('Opted Out On'),
  )

  @property
  def name(self) -> str:
    name = f'{self.first_name} {self.last_name}'
    if self.honorific:
      name = f'{self.honorific} {name}'
    if self.suffix:
      name = f'{name} {self.suffix}'
    if not name.strip():
      name = ''
    return name

  @property
  def job(self) -> str:
    return ' @ '.join(filter(None, [self.job_title, self.company_name]))

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

  def serialize(self, method: str = 'POST') -> dict:
    """Serialize to CTCT object differently depending on PUT or POST method."""

    data = model_to_dict(self, fields=self.API_BODY_FIELDS)

    # CTCT Bug  # TODO after tests: Verify bug hasn't been fixed
    data['first_name'] = data['first_name'].replace('.', '')

    data['list_memberships'] = [o['id'] for o in data['list_memberships']]

    # The `email_address` field behaves differently on update vs create
    if method == 'POST':
      data.update({
        'email_address': self.email,
      })
    elif method == 'PUT':
      data.update({
        'email_address': {'address': self.email},
        'update_source': self.update_source,
      })
    else:
      message = (
        f"Unsupported method: {method}."
      )
      raise ValueError(message)

    # TODO after tests: Not sure why this exists
    # # Must specify one of 'source' or 'list_memberships'
    # if data['list_memberships'] == []:
    #   data.pop('list_memberships')
    #   data['source'] = 'Contact'
    # else:
    #   # Only need to specify ContactList UUIDs
    #   data['list_memberships'] = [o['id'] for o in data['list_memberships']]

    return data

  @classmethod
  def deserialize(cls, ctct_obj) -> Optional[dict]:
    """Convert CTCT object to model field values."""

    if ctct_obj.get('action') in ['created', 'updated']:
      # Only ID is returned when using update_or_create endpoint
      return None

    data = super().deserialize(ctct_obj)
    data['email'] = ctct_obj['email_address']['address']

    if ctct_obj['email_address']['permission_to_send'] == 'unsubscribed':
      data.update({
        'optout': True,
        'optout_at': to_dt(ctct_obj['email_address']['opt_out_date']),
      })
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
    While CTCT does support using PUT request to /contact/{id}, we defer to
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

    if self.optout:
      response = {}
    elif not self.list_memberships.exists():
      # CTCT requires that Contacts be a member of at least one ContactList
      self.ctct_delete()
      return None
    else:
      response = requests.put(
        url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}',
        headers=self.headers,
        json=self.serialize(method='PUT'),
      )
      ctct_obj = self.raise_or_json(response)
      return ctct_obj


@receiver(m2m_changed, sender=Contact.list_memberships.through)
def ctct_update_contact_lists(sender, instance, action, **kwargs):
  """Updates a Contact's list membership on CTCT servers."""

  if action in ['post_add', 'post_remove', 'post_clear']:

    if isinstance(instance, Contact):
      if instance.save_to_ctct == 'sync':
        ctct_update_lists_job(instance)
      elif instance.save_to_ctct == 'async':
        ctct_update_lists_job.delay(instance)

    elif isinstance(instance, ContactList):
      contacts = Contact.objects.filter(pk__in=kwargs['pk_set'])
      if instance.save_to_ctct == 'async':
        ctct_add_list_memberships_job(instance, contacts)
      elif instance.save_to_ctct == 'async':
        ctct_add_list_memberships_job.delay(instance, contacts)


class ContactNote(CTCTLocalModel):
  """Django implementation of a CTCT Note."""

  API_ID_LABEL = 'note_id'
  API_BODY_FIELDS = (
    'note_id',
    'created_at',
    'content',
  )
  API_MAX_NUM = 150

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
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
    verbose_name=_('Author'),
  )

  class Meta:
    verbose_name = _('Note')
    verbose_name_plural = _('Notes')

  def __str__(self) -> str:
    return self.content


class ContactPhoneNumber(CTCTLocalModel):
  """Django implementation of a CTCT Contact's PhoneNumber."""

  API_BODY_FIELDS = (
    'kind',
    'phone_number',
  )
  API_MAX_NUM = 3

  KINDS = (
    ('home', 'Home'),
    ('work', 'Work'),
    ('mobile', 'Mobile'),
    ('other', 'Other'),
  )

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
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

  def __str__(self) -> str:
    return f'{self.kind}: {self.phone_number}'


class ContactStreetAddress(CTCTLocalModel):
  """Django implementation of a CTCT Contact's StreetAddress."""

  API_BODY_FIELDS = (
    'kind',
    'street',
    'city',
    'state',
    'postal_code',
    'country',
  )
  API_MAX_NUM = 3

  KINDS = (
    ('home', 'Home'),
    ('work', 'Work'),
    ('other', 'Other'),
  )

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
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

  def __str__(self) -> str:
    return f'{self.kind}: {self.street}, {self.city} {self.state}'


class ContactCustomField(CTCTLocalModel):

  API_MAX_NUM = 25

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    related_name='custom_fields',
    verbose_name=_('Contact'),
  )
  custom_field = models.ForeignKey(
    CustomField,
    on_delete=models.CASCADE,
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

  def __str__(self) -> str:
    return f'{self.custom_field.label}: {self.value}'

  def serialize(self) -> dict:
    data = {
      'custom_field_id': str(self.custom_field.id),
      'value': self.value,
    }
    return data

  @classmethod
  def deserialize(cls, ctct_obj: dict) -> dict:
    """Convert CTCT object to model field values."""
    data = {}
    for field in cls._meta.fields:
      if (value := ctct_obj.get(field.name)) is not None:
        data[field.name] = value
    data['id'] = ctct_obj[cls.API_ID_LABEL]
    return data


class EmailCampaign(CTCTModel):
  """Django implementation of a CTCT EmailCampaign."""

  API_ENDPOINT = '/v3/emails'
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
    if counts := ctct_obj.get('unique_counts'):
      # Extra data from /email_campaign_summaries API endpoint
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
      url=f'{self.BASE_URL}{self.API_ENDPOINT}',
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
        activity.id = campaign_activity['campaign_activity_id']
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

  def ctct_read(self, name: Optional[str] = None) -> Optional[dict]:
    """Retrieve EmailCampaign from CTCT servers."""

    if self.id:
      ctct_obj = super().ctct_read()
    elif name:
      # Fetch a EmailCampaign from CTCT servers by name
      endpoint = self.API_ENDPOINT
      paginated, ctct_obj = True, None
      while paginated:

        response = requests.get(
          url=f'{self.BASE_URL}{endpoint}',
          headers=self.headers,
        )
        ctct_objs = self.raise_or_json(response)
        ctct_objs = filter(lambda x: x['name'] == name, ctct_objs['campaigns'])

        try:
          # See if the Campaign in the current page of results
          ctct_obj = next(ctct_objs)
          paginated = False
        except StopIteration:
          # Campaign not found, move to next paginated response
          try:
            endpoint = ctct_objs.get('_links').get('next').get('href')
          except AttributeError:
            paginated = False
        else:
          # Fetch full EmailCampaign (with CampaignActivity information)
          self.id = ctct_obj['campaign_id']
          ctct_obj = self.ctct_read()
    else:
      message = (
        "Object has no id, so you must pass a 'name' parameter."
      )
      raise ValueError(message)

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
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}',
      headers=self.headers,
      json={'name': self.name},
    )
    ctct_obj = self.raise_or_json(response)
    return ctct_obj


class CampaignActivity(CTCTModel):
  """Django implementation of a CTCT CampaignActivity

  Notes
  -----
  The CTCT API is set up so that EmailCampaigns have multiple
  CampaignActivities ('primary_email', 'permalink', 'resend'). For
  our purposes, the `primary_email` CampaignActivity is the most
  important one, and as such the design of this model is primarily
  based off of them.

  """

  API_ENDPOINT = '/v3/emails/activities'
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

  campaign = models.ForeignKey(
    EmailCampaign,
    on_delete=models.CASCADE,
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
  def format_type(self) -> int:
    """CTCT's 'Modern Custom Code' format."""
    return 5

  @property
  def from_email(self) -> str:
    return settings.CTCT_FROM_EMAIL

  @property
  def reply_to_email(self) -> str:
    return getattr(settings, 'CTCT_REPLY_TO_EMAIL', settings.CTCT_FROM_EMAIL)

  @property
  def from_name(self) -> str:
    return settings.CTCT_FROM_NAME

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
    if self.campaign:
      s = f'{self.campaign}, {self.get_role_display()}'
    else:
      s = super().__str__
    return s

  def serialize(self) -> dict:
    data = super().serialize()
    if self.id:
      data['contact_list_ids'] = [
        str(cl.id) for cl in self.contact_lists.all()
      ]

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
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}/tests',
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
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}',
      headers=self.headers,
      json=self.serialize(),
    )
    self.raise_or_json(response)

    # Then schedule the CampaignActivity
    response = requests.post(
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}/schedules',
      headers=self.headers,
      json={'scheduled_date': self.campaign.scheduled_datetime.isoformat()},
    )
    self.raise_or_json(response)

  def ctct_unschedule(self) -> None:
    """Unschedules the `primary_email` CampaignActivity."""
    super().ctct_delete(suffix='/schedules')

  def get_preview_recipients(self) -> list[str]:
    """Determines who receives the CTCT preview emails."""
    recipients = getattr(settings, 'CTCT_PREVIEW_RECIPIENTS', settings.MANAGERS)  # noqa: 501
    return [email for (name, email) in recipients]

  def get_preview_message(self, user) -> str:
    """Writes the message sent with preview emails."""
    message = _(
      f'Please let {user.first_name} {user.last_name} know if you have any edits.'
    )
    if self.campaign.current_status == 'SCHEDULED':
      datetime = self.campaign.scheduled_datetime.strftime(
        settings['DATETIME_FORMAT']
      )
      message += _(
        f' This campaign is scheduled to be sent on {datetime}.'
      )
    return message
