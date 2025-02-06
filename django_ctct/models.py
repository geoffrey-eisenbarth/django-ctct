from __future__ import annotations

import datetime as dt
import warnings

from bs4 import BeautifulSoup
import jwt
import pytz
import requests
from requests.exceptions import HTTPError
from requests.models import Response

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, validate_email
from django.db import models
from django.db.models import Q
from django.db.models.query import QuerySet
from django.db.models.signals import m2m_changed, pre_delete
from django.dispatch import receiver
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from django_hosts.resolvers import reverse
from phonenumber_field.modelfields import PhoneNumberField

from django_ctct.tasks import (
  ctct_save_job, ctct_delete_job,
  ctct_update_lists_job, ctct_add_list_memberships_job,
)
from systems.metrics.models import Region
from www.posts.models import Post


class CTCTModel(models.Model):
  """Common CTCT model methods and properties."""

  BASE_URL = 'https://api.cc.email'
  TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

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

  def save(
    self,
    save_to_ctct: bool = True,
    delay: bool = True,
    *args,
    **kwargs
  ) -> None:
    """Wrap `ctct_save()` method to ensure Django model gets saved.

    Notes
    -----
    Any API-related update/create methods should go in the `ctct_save`
    method, which is called as an asynchronous task.

    """

    # Save to database first (and set PK)
    super().save(*args, **kwargs)

    # Save on CTCT's servers
    if save_to_ctct:
      if settings.DEBUG:
        message = (
          "Saving to CTCT not supported while in DEBUG mode."
        )
        warnings.warn(message)
      elif delay:
        ctct_save_job.delay(self)
      else:
        self.ctct_obj = ctct_save_job(self)

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
      # Allow 404, if object not on CTCT servers
      if e.response.status_code == 404:
        pass
      else:
        raise e

  @classmethod
  def deserialize(cls, ctct_obj: dict) -> dict:
    """Convert CTCT object to model field values."""
    data = {}
    for field in cls._meta.fields:
      if (value := ctct_obj.get(field.name)) is not None:
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
        if post_id := data.get('post_id'):
          query = {'post_id': post_id}
        elif id := data.get('id'):
          query = {'id': id}
        else:
          raise NotImplementedError
        try:
          django_obj = EmailCampaign.objects.get(**query)
        except EmailCampaign.DoesNotExist:
          django_obj = EmailCampaign(**query)
      else:
        django_obj = cls()

    for field_name, value in data.items():
      setattr(django_obj, field_name, value)

    if save:
      django_obj.save(save_to_ctct=False)

    return django_obj


class Token(models.Model):
  """Authorization token for CTCT API access.

  Notes
  -----
  To get the latest Token, use the `get()` class method.

  """

  access_code = models.TextField()
  refresh_code = models.CharField(
    max_length=50,
  )
  type = models.CharField(
    max_length=50,
    default='Bearer',
  )
  inserted = models.DateTimeField(
    auto_now=False,
    auto_now_add=True,
  )

  class Meta:
    ordering = ['-inserted']

  def __str__(self) -> str:
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
        f"visit {reverse('ctct_auth')} and sign in to "
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

  name = models.CharField(
    max_length=255,
  )
  description = models.CharField(
    max_length=255,
    help_text=_('For internal use only'),
  )
  favorite = models.BooleanField(
    default=False,
    help_text=_('Mark the list as a favorite'),
  )
  created_at = models.DateTimeField(
    auto_now_add=True,
  )
  updated_at = models.DateTimeField(
    auto_now=True,
  )

  class Meta:
    verbose_name = _('Contact List')
    verbose_name_plural = _('Contact Lists')
    ordering = ['-favorite', 'name']

  def __str__(self) -> str:
    return self.name

  def serialize(self) -> dict:
    data = {
      'name': self.name,
      'favorite': self.favorite,
      'description': self.description,
    }
    return data

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


@receiver(pre_delete)
def ctct_delete_signal(sender, instance, **kwargs):
  """Delete the ContactList from CTCT servers."""
  if not isinstance(instance, (ContactList, Contact)):
    return
  elif settings.DEBUG:
    message = (
      "Deleting from CTCT not supported while in DEBUG mode."
    )
    warnings.warn(message)
  elif getattr(instance, 'save_to_ctct', True):
    if getattr(instance, 'delay', True):
      ctct_delete_job.delay(instance)
    else:
      ctct_delete_job(instance)


class Contact(CTCTModel):
  """Django implementation of a CTCT Contact."""

  API_ENDPOINT = '/v3/contacts'
  API_ID_LABEL = 'contact_id'

  SALUTATIONS = (
    ('Mr.', 'Mr.'),
    ('Ms.', 'Ms.'),
    ('Dr.', 'Dr.'),
    ('Hon.', 'The Honorable'),
    ('Amb.', 'Ambassador'),
    ('Prof.', 'Professor'),
  )
  SOURCES = (
    ('tpg', 'Office'),
    ('www', 'Website'),
  )

  email = models.EmailField(
    _('Email Address'),
    blank=True,         # Allow new entries without email :(
    null=True,          # TPG office has Contacts without emails
    unique=True,        # Note: None != None for uniqueness check
  )
  honorific = models.CharField(
    choices=SALUTATIONS,
    max_length=5,
    blank=True,
  )
  first_name = models.CharField(
    _('First Name'),
    max_length=255,
    blank=True,
  )
  last_name = models.CharField(
    _('Last Name'),
    max_length=255,
    blank=True,
  )
  suffix = models.CharField(
    max_length=10,
    blank=True,
  )
  job_title = models.CharField(
    _('Job Title'),
    max_length=50,  # Limit set by CTCT
    blank=True,
  )
  company = models.CharField(
    _('Company'),
    max_length=255,
    blank=True,
  )
  county = models.ForeignKey(
    Region,
    limit_choices_to={'category': 'COUNTY'},
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    help_text=_(
      'Select and type to filter options'
    ),
    related_name='county_contacts',
  )
  address = models.CharField(
    max_length=255,
    blank=True,
  )
  address_2 = models.CharField(
    max_length=255,
    blank=True,
  )
  city = models.CharField(
    max_length=255,
    blank=True,
  )
  state = models.ForeignKey(
    Region,
    limit_choices_to={'category': 'STATE'},
    on_delete=models.SET_NULL,
    null=True,
    related_name='state_contacts',
  )
  zip_code = models.CharField(
    max_length=10,
    blank=True,
    verbose_name=_("ZIP Code"),
    validators=[RegexValidator(
      regex=r'(^\d{5}$)|(^\d{9}$)|(^\d{5}-\d{4}$)',
      message=_("ZIP Coode must be 5 or 9 digits"),
    )],
  )
  home_phone = PhoneNumberField(
    verbose_name=_("Home Phone"),
    blank=True,
  )
  office_phone = PhoneNumberField(
    verbose_name=_("Office Phone"),
    blank=True,
  )
  direct_phone = PhoneNumberField(
    verbose_name=_("Direct Phone"),
    blank=True,
  )
  cell_phone = PhoneNumberField(
    verbose_name=_("Cell Phone"),
    blank=True,
  )
  fax = PhoneNumberField(
    verbose_name=_("Fax Number"),
    blank=True,
  )
  user = models.OneToOneField(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    null=True,
    blank=True,
    related_name='contact',
  )
  lists = models.ManyToManyField(
    ContactList,
    related_name='contacts',
    verbose_name=_('Contact Lists'),
    blank=True,
  )
  flat_notes = models.TextField(
    blank=True,
    help_text=_(
      "Intended for internal notes, "
      "not to be displayed publically."
    ),
  )
  created_at = models.DateTimeField(
    auto_now=False,
    default=timezone.now,
    null=True,
  )
  updated_at = models.DateTimeField(
    auto_now=True,
    null=True,
  )
  source = models.CharField(
    max_length=3,
    choices=SOURCES,
    default='www',
  )
  optout = models.BooleanField(
    default=False,
    editable=False,
    verbose_name=_("Opted Out"),
    help_text=_(
      "Enforced by ConstantContact."
    ),
  )
  optout_at = models.DateTimeField(
    blank=True,
    null=True,
    verbose_name=_("Opted Out On"),
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
    return ' @ '.join(filter(None, [self.job_title, self.company]))

  @property
  def phone(self) -> str:
    phone_numbers = [
      self.direct_phone,
      self.home_phone,
      self.office_phone,
      self.cell_phone,
    ]
    try:
      phone = str(next(filter(lambda x: x, phone_numbers)))
    except StopIteration:
      phone = ''
    return phone

  @cached_property
  def column(self) -> ContactList:
    return self.get_list('Column: Website')

  @cached_property
  def newsletter(self) -> ContactList:
    lists = [
      'Newsletter: Paid',
      'Newsletter: Comps',
      'Newsletter: Public Officials',
      'Legislators',
    ]
    return self.get_list(lists)

  @cached_property
  def forecast(self) -> ContactList:
    lists = ['Forecast: Major Texas Metros', 'Forecast: All Texas Regions']
    return self.get_list(lists)

  @cached_property
  def is_public_official(self) -> bool:
    lists = [
      'Public Officials',
      'Legislators',
    ]
    return bool(self.get_list(lists))

  @cached_property
  def is_media(self) -> bool:
    return self.lists.filter(name__startswith='Media').exists()

  @cached_property
  def is_newsletter(self) -> bool:
    return self.lists.filter(name__startswith='Newsletter').exists()

  @cached_property
  def is_comped(self) -> bool:
    return bool(self.get_list('Newsletter: Comps'))

  @cached_property
  def is_column(self) -> bool:
    return self.lists.filter(name__startswith='Column').exists()

  @cached_property
  def is_website(self) -> bool:
    return self.lists.filter(name__contains='Website').exists()

  class Meta:
    verbose_name = _('Contact')
    verbose_name_plural = _('Contacts')
    ordering = ['email']

  def __str__(self) -> str:
    if self.name and self.email:
      s = f'{self.name} ({self.email})'
    elif self.email:
      s = self.email
    elif self.name:
      s = self.name
    elif self.company and self.address:
      s = f'{self.company} ({self.address})'
    elif self.company:
      s = self.company
    else:
      s = 'N/A'
    return s

  def clean(self) -> None:
    """Allow blank emails, but enforce uniqueness.

    Notes
    ----
    Keep in mind that None != None for Django uniqueness.

    """

    if self.email:
      self.email = self.email.lower().strip()

    if self.email == '':
      self.email = None

    if self.email is not None:
      validate_email(self.email)

    return super().clean()

  def save(self, *args, **kwargs) -> None:
    if (
      (self.user is not None)
      and (self.email is not None)
      and (self.email != self.user.email)
    ):
      # Force user.email to agree with contact.email
      # TODO: This is because ProfileForm updates CONTACT, not USER
      # TODO: Split ProfileForm into Contact and User (email + pw change)
      # NOTE: This is assuming we want contact's email to mirrow USER's email
      #       as opposed to the other way around.
      self.user.email = self.email
      self.user.save(update_fields=['email'])

    if (not self.email) or (self._id and not self.lists.exists()):
      kwargs['save_to_ctct'] = False
    return super().save(*args, **kwargs)

  def serialize(self, method: str = 'POST') -> dict:
    """Serialize to CTCT object, depending on PUT or POST method."""
    data = {
      'first_name': self.first_name.replace('.', ''),  # CTCT Bug
      'last_name': self.last_name,
      'job_title': self.job_title,
      'company_name': self.company[:50],               # CTCT Limit
    }
    for field_name in ['created_at', 'updated_at']:
      if date := getattr(self, field_name):
        data[field_name] = date.strftime(self.TS_FORMAT)

    if method == 'POST':
      data.update({
        'email_address': self.email,
      })
    elif method == 'PUT':
      data.update({
        'email_address': {'address': self.email},
        'update_source': 'Contact',
      })
    else:
      message = (
        f"Unsupported method: {method}."
      )
      raise ValueError(message)

    if self.county:
      data['custom_fields'] = [{
        'custom_field_id': self.get_ctct_custom_field_id('county'),
        'value': self.county.name,
      }]

    # Must specify one of 'source' or 'list_memberships'
    if self.lists.count() == 0:
      data['source'] = 'Contact'
    else:
      data['list_memberships'] = list(
        map(str, self.lists.values_list('id', flat=True))
      )
    return data

  @classmethod
  def deserialize(cls, ctct_obj) -> dict:
    """Convert CTCT object to model field values."""
    CTCT_CUSTOM_FIELDS = {
      'county': '061bbb55bf6-11e3-a00e-d4ae529a863c',
    }

    custom_fields = {}
    for field_name, ctct_id in CTCT_CUSTOM_FIELDS.items():
      for ctct_custom_field in ctct_obj.get('custom_fields', []):
        if ctct_custom_field['custom_field_id'] == ctct_id:
          custom_fields[field_name] = ctct_custom_field['value']

    data = super().deserialize(ctct_obj)
    if ctct_obj.get('action') in ['created', 'updated']:
      # Only ID is returned when updating or creating,
      # but the job might have added email address
      if email := ctct_obj.get('email'):
        data.update({'email': email})
    else:
      # Full data available (e.g., from `ctct_read()`)
      data.update({
        'email': ctct_obj['email_address']['address'],
        'company': ctct_obj.get('company_name', ''),
        'county': Region.objects.filter(
          name=custom_fields.get('county'),
        ).first(),
        'created_at': timezone.make_aware(
          dt.datetime.strptime(ctct_obj['created_at'], cls.TS_FORMAT)
        ),
        'updated_at': timezone.make_aware(
          dt.datetime.strptime(ctct_obj['updated_at'], cls.TS_FORMAT)
        ),
      })

      if ctct_obj['email_address']['permission_to_send'] == 'unsubscribed':
        data.update({
          'optout': True,
          'optout_at': timezone.make_aware(
            dt.datetime.strptime(
              ctct_obj['email_address']['opt_out_date'],
              cls.TS_FORMAT,
            )
          ),
        })
    return data

  def ctct_create(self) -> dict:
    """Redirect to the 'update_or_create' endpoint.

    Notes
    -----

    Due to the fact that CTCT could already have entries for
    Contacts that do not have Django objects yet, we must use
    CTCT's `update_or_create` method, which will re-activate any
    contacts that were previously added to CTCT and then later
    deleted ('deactivated' to use CTCT's term).

    Due to the design of CTCT's API, updates to existing Contacts
    are only partial updates, and as such cannot be used to remove/
    add the Contact from ContactLists; use the `ctct_update_lists()`
    method.

    """
    if self.email:
      ctct_obj = self.ctct_update_or_create()
    else:
      ctct_obj = {}
    return ctct_obj

  def ctct_update(self) -> dict:
    return self.ctct_update_or_create()

  def ctct_update_or_create(self) -> dict:
    """Update or create a Contact on CTCT servers.

    Notes
    -----
    This is basically a CTCTModel.ctct_create() method with a
    different API_ENDPOINT.

    """
    endpoint = self.API_ENDPOINT
    self.API_ENDPOINT += '/sign_up_form'
    try:
      ctct_obj = super().ctct_create()
      exception = None
    except Exception as e:
      exception = e
    else:
      # Response only contains CTCT ID and action status
      ctct_obj['email'] = self.email
    finally:
      self.API_ENDPOINT = endpoint

    if exception:
      raise exception

    return ctct_obj

  def ctct_update_lists(self) -> None | dict:
    """Update Contact and ContactList membership on CTCT servers.

    Notes
    -----
    The PUT call will overwrite all properties not included in the request
    body with NULL, so we need to make sure the `serialize()` method
    includes all important fields. While the `create_or_update()` method
    supports partial updates, it won't allow us to remove a ContactList,
    so we must use the PUT method.

    CTCT requires that all contacts be a member of at least one ContactList,
    so in the event of removing someone from all lists, we much actually
    issue a DELETE call; however, these 'deleted' Contacts retain their ID
    and can be revived at any time.

    """
    if self.optout:
      response = {}
    elif not self.lists.exists():
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

  @classmethod
  def get_ctct_custom_field_id(cls, name: str) -> dict:
    """Get the CTCT id for custom field.

    Notes
    -----
    Can't use `CTCTModel.headers` since it relies on this class method.

    """

    token = Token.get()

    response = requests.get(
      url=f'{cls.BASE_URL}/v3/contact_custom_fields',
      headers={'Authorization': f'{token.type} {token.access_code}'},
    )
    ctct_obj = cls.raise_or_json(response)

    # Return the requested list_id
    custom_fields = ctct_obj.get('custom_fields', [])
    for custom_field in custom_fields:
      if custom_field['name'].lower() == name.lower():
        return custom_field['custom_field_id']

  def get_list(
    self,
    name: str = '',
    names: list = [],
  ) -> ContactList | None:
    if name:
      names = [name]
    contact_list = self.lists.filter(name__in=names).first()
    return contact_list


@receiver(m2m_changed, sender=Contact.lists.through)
def ctct_update_contact_lists(sender, instance, action, **kwargs):
  """Updates a Contact's list membership on CTCT servers.

  Notes
  -----
  Since the bulk M2M methods `.add()`, `.remove()` etc do not fire
  the `save()` method, we must update values on CTCT servers using
  the `m2m_changed` signal.

  """

  if action in ['post_add', 'post_remove', 'post_clear']:
    # Update list membership on CTCT servers
    if settings.DEBUG:
      message = (
        "Saving to ConstantContact not supported while in DEBUG mode."
      )
      warnings.warn(message)
    elif getattr(instance, 'save_to_ctct', True):
      delay = getattr(instance, 'delay', True)
      if isinstance(instance, Contact):
        if delay:
          ctct_update_lists_job.delay(instance)
        else:
          ctct_update_lists_job(instance)
      elif isinstance(instance, ContactList):
        contacts = Contact.objects.filter(pk__in=kwargs['pk_set'])
        if delay:
          ctct_add_list_memberships_job.delay(instance, contacts)
        else:
          ctct_add_list_memberships_job(instance, contacts)


class ContactNote(models.Model):
  """A note regarding a Contact."""

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    related_name='notes',
  )
  note = models.CharField(
    max_length=255,
  )
  timestamp = models.DateTimeField(
    auto_now=True,
  )
  author = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    limit_choices_to={'is_staff': True},
    editable=False,
  )

  class Meta:
    verbose_name = _('Note')
    verbose_name_plural = _('Notes')

  def __str__(self) -> str:
    return self.note


# class Address(models.Model):
#   """Sub-model representing an address."""
#
#   contact = models.ForeignKey(
#     Contact,
#     on_delete=models.CASCADE,
#     related_name='addresses',
#   )
#   address = models.CharField(
#     max_length=255,
#     blank=True,
#   )
#   address_2 = models.CharField(
#     max_length=255,
#     blank=True,
#   )
#   city = models.CharField(
#     max_length=255,
#     blank=True,
#   )
#   county = models.ForeignKey(
#     Region,
#     limit_choices_to={'category': 'COUNTY'},
#     on_delete=models.SET_NULL,
#     null=True,
#     blank=True,
#     help_text=_(
#       'Select and type to filter options'
#     ),
#     related_name='county_contacts',
#   )
#   state = models.ForeignKey(
#     Region,
#     limit_choices_to={'category': 'STATE'},
#     on_delete=models.SET_NULL,
#     null=True,
#     related_name='state_contacts',
#   )
#   zip_code = models.CharField(
#     max_length=10,
#     blank=True,
#     verbose_name=_("ZIP Code"),
#     validators=[RegexValidator(
#       regex=r'(^\d{5}$)|(^\d{9}$)|(^\d{5}-\d{4}$)',
#       message=_("ZIP Code must be 5 or 9 digits"),
#     )],
#   )


class EmailCampaign(CTCTModel):
  """Django implementation of a CTCT EmailCampaign.

  Notes
  -----
  We must be careful to NOT send Newsletters any other
  ContactList except for the 'Newsletter: Comps' list,
  since the default email contains a paywall URL.

  Instead, a copy of the EmailCampaign must be made in
  ConstantContact and the button URL changed to that of
  the hosted PDF (e.g., upload the PDF to "My Documents"
  using CTCT's website and then copy/paste the URL into
  BOTH buttons in the EmailCampaing (e.g., Outlook one
  and the regular one).

  """

  API_ENDPOINT = '/v3/emails'
  API_ID_LABEL = 'campaign_id'

  POST_CATEGORIES = {
    'COLUMN': [
      'Internal',
      'Column: Website',
      'Column: Office',
    ],
    'BRIEF': [
      'Internal',
      'Media',
      'Public Officials',
      'Column: Website',
      'Column: Office',
      'Newsletter: Paid',
      'Newsletter: Comps',
    ],
    'REPORT': [
      'Internal',
      'Media',
      'Public Officials',
      'Column: Website',
      'Column: Office',
      'Newsletter: Paid',
      'Newsletter: Comps',
    ],
    'PRESSRELEASE': [
      'Internal',
      'Media',
      'Public Officials',
      'Column: Website',
      'Column: Office',
      'Newsletter: Paid',
      'Newsletter: Comps',
    ],
    'NEWSLETTER': [
      # DO NOT SEND to "Newsletter: Comps", see Notes!
      'Internal',
      'Newsletter: Paid',
    ],
    'EMAIL': [],
  }
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
  NAME_MAX_LENGTH = 80  # Set by CTCT API

  name = models.CharField(
    max_length=NAME_MAX_LENGTH,
    unique=True,
  )
  post = models.OneToOneField(
    settings.CTCT_POST_MODEL,
    on_delete=models.CASCADE,
    related_name='campaign',
  )
  status = models.CharField(
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
  open_rate = models.DecimalField(
    max_digits=5,
    decimal_places=4,
    default=0,
    help_text=_('Number of opens as a percentage of total sends'),
  )
  debug = models.BooleanField(
    default=False,
  )

  class Meta:
    verbose_name = _('Email Campaign')
    verbose_name_plural = _('Email Campaigns')
    ordering = ('-post__publish_date', '-scheduled_datetime')

  def __str__(self) -> str:
    return self.name

  def clean(self):
    """Validate scheduled_datetime."""
    if (
      (self.status != 'DONE') and
      (self.scheduled_datetime is not None) and
      (self.scheduled_datetime < timezone.now() + dt.timedelta(minutes=30))
    ):
      message = (
        "Must schedule the post for at least 30 minutes in the future!"
      )
      raise ValidationError(message)

  def save(self, *args, **kwargs) -> None:
    # Set `name` field to avoid uniqueness conflicts in case API call fails
    self.name = self._get_name()
    super().save(*args, **kwargs)

  @classmethod
  def deserialize(self, ctct_obj) -> dict:
    data = super().deserialize(ctct_obj)

    # Set model fields based on CTCT response
    if status := ctct_obj.get('current_status'):
      data['status'] = status.upper()
    if post_id := ctct_obj.get('post_id'):
      # Set in `ctct_create()`
      data['post_id'] = post_id

    if counts := ctct_obj.get('unique_counts'):
      for field, value in counts.items():
        data[field] = value

      opens = counts.get('opens', 0)
      sends = counts.get('sends', 0)
      data.update({
        'opens': opens,
        'sends': sends,
        'open_rate': (opens / sends) if sends else 0,
      })
      if sends:
        data['status'] = 'DONE'

    return data

  def ctct_create(self) -> dict:
    """Creates the CTCT EmailCampaign and sets relevant model fields.

    Notes
    -----
    This method will also create the new `primary_email` and `permalink`
    CampaignActivities on CTCT and associate the `primary_email` one
    with the new EmailCampaign in the database.

    """

    if self.post.category not in self.POST_CATEGORIES:
      message = (
        f'EmailCampaigns not available for {self.post.get_category_display()}.'
      )
      raise ValidationError(message)

    # Set name and initialize the CampaignActivity object (do not create it!)
    self.name = self._get_name()
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
        'email_campaign_activities': [{
          'format_type': activity.format_type,
          'from_email': activity.from_email,
          'reply_to_email': activity.reply_to_email,
          'from_name': activity.from_name,
          'subject': activity.subject,
          'html_content': activity.html_content,
          'preheader': activity.preheader,
          'physical_address_in_footer': activity.physical_address,
        }],
      },
    )
    if response.status_code == 409:
      # EmailCampaign name not unique, fetch existing EmailCampaign from CTCT
      ctct_obj = self.ctct_read(name=self.name)
    else:
      ctct_obj = self.raise_or_json(response)

    # Set field values that `deserialize(ctct_obj)` doesn't have access to
    ctct_obj['post_id'] = self.post.id
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
      self.status = 'DRAFT'
    elif self.action == 'SCHEDULE':
      activity.ctct_send_preview()
      activity.ctct_schedule()
      self.status = 'SCHEDULED'
    elif self.campaign.action == 'UNSCHEDULE':
      activity.ctct_unschedule()
      self.status = 'DRAFT'
    self.action = 'NONE'

    # Update CTCT response with local field values
    ctct_obj.update({
      'current_status': self.status,
      'action': self.action,
    })

    return ctct_obj

  def ctct_read(self, name: str = '') -> dict | None:
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
    # TODO: Does this result in double CTCT requests in Django admin?
    if self.action in ['SCHEDULE', 'UNSCHEDULE']:
      activity = self.activities.get(role='primary_email')
      method_name = f'ctct_{self.action.lower()}'
      getattr(activity, method_name)()

  def ctct_rename(self, new_name: str = '') -> dict:
    """Rename EmailCampaign on CTCT servers."""
    response = requests.patch(
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}',
      headers=self.headers,
      json={'name': new_name or self._get_name()},
    )
    ctct_obj = self.raise_or_json(response)
    return ctct_obj

  def _get_name(self) -> str:
    """Returns the CTCT EmailCampaign name."""

    category = 'DEBUG' if self.debug else self.post.category
    date = self.post.publish_date.strftime('%Y-%m-%d')
    if category == 'COLUMN':
      name = f'{category} {date}'
    elif category == 'NEWSLETTER':
      name = f'{category} {date} - PAID'
    else:
      name = f'{category} {date} {self.post.title}'

    if len(name) > 80:
      idx = (self.NAME_MAX_LENGTH - len('...')) // 2
      name = f'{name[:idx]}...{name[-idx:]}'

    return name

  def _get_message(self) -> str:
    """Django admin message about EmailCampaign status."""
    if self.action == 'CREATE':
      message = _(
        'Campaign has been created and a preview has been sent for '
        'approval. Once the campaign has been approved, you must '
        'schedule it.'
      )
    elif self.action == 'UPDATE':
      message = _(
        'The campaign has been updated and a preview has been sent '
        'out for approval.'
      )
    elif self.action == 'SCHEDULE':
      message = _(
        'The campaign has been scheduled and a preview has been sent '
        'out for approval.'
      )
    elif self.action == 'UNSCHEDULE':
      message = _(
        'The campaign has been unscheduled.'
      )
    else:
      message = None
    return message


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

  ROLES = (
    ('primary_email', 'Primary Email'),
    ('permalink', 'Permalink'),
    ('resend', 'Resent'),
  )
  PREVIEW_CONTACTS = {
    'DEBUG': [
      ('Geoffrey Eisenbarth', 'geoffrey.eisenbarth@gmail.com'),
    ],
    'COLUMN': [
      ('Internal', 'info@perrymangroup.com'),
      ('Shelia Smith', 'sheliaws64@gmail.com'),
      ('Kevin Gleghorn', 'ckevingleghorn@gmail.com'),
      ('Geoffrey Eisenbarth', 'geoffrey.eisenbarth@gmail.com'),
    ],
    'RELEASE': [
      ('Internal', 'info@perrymangroup.com'),
      ('Ray Perryman', 'ray@perrymangroup.com'),
      ('Karen Amos', 'kamos@perrymangroup.com'),
      ('Ginnie Gleghorn', 'ginniegleghorn@gmail.com'),
      ('Geoffrey Eisenbarth', 'geoffrey.eisenbarth@gmail.com'),
    ],
  }

  campaign = models.ForeignKey(
    EmailCampaign,
    on_delete=models.CASCADE,
    related_name='activities',
  )
  role = models.CharField(
    max_length=25,
    choices=ROLES,
    default='primary_email',
  )
  contact_lists = models.ManyToManyField(
    ContactList,
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

  def __str__(self) -> str:
    if self.campaign:
      s = f'{self.campaign}, {self.get_role_display()}'
    else:
      s = super().__str__
    return s

  def serialize(self) -> dict:
    data = {
      'from_name': self.from_name,
      'from_email': self.from_email,
      'reply_to_email': self.reply_to_email,
      'subject': self.subject,
      'html_content': self.html_content,
      'preheader': self.preheader,
    }
    if self.id:
      data.update({
        'contact_list_ids': [str(cl.id) for cl in self.contact_lists.all()],
      })

    return data

  def ctct_save(self) -> None:
    """Updates CampaignActivity on CTCT servers.

    Notes
    -----
    Unlike other models, this method does NOT return a `ctct_obj`
    dictionary. This is because the schedule and unschedule responses
    from CTCT do not contain the entire CampaignActivity object. As
    a result, we save local changes here instead of using the `from_ctct`
    method that the other models use in the `ctct_save_job`.

    """

    if self.role != 'primary_email':
      message = (
        f'CampaignActivity with role `{self.role}` not supported yet.'
      )
      raise NotImplementedError(message)
    else:
      # Primary Email Activities can only be updated, not created
      self.ctct_update()

  def _get_contact_lists(self) -> 'QuerySet[ContactList]':
    """Sets ContactLists based on the EmailCampaign's Post."""
    if self.campaign.debug:
      names = ['DEBUG']
    else:
      names = EmailCampaign.POST_CATEGORIES[self.campaign.post.category]
    return ContactList.objects.filter(name__in=names)

  def ctct_update(self) -> dict:
    """Update CampaignActivity on CTCT servers."""

    # Can only update if status is in DRAFT or SENT status
    if self.campaign.status == 'SCHEDULED':
      self.ctct_unschedule()

    # Rename EmailCampaign on CTCT servers if the title changed
    new_name = self.campaign._get_name()
    if new_name != self.campaign.name:
      self.campaign.ctct_rename()
      self.campaign.name = new_name
      self.campaign.save(update_fields=['name'])

    # Update the CampaignActivity (email content)
    ctct_obj = super().ctct_update()

    # Send updated preview
    self.ctct_send_preview()

    # Re-schedule if EmailCampaign was originally scheduled
    if self.campaign.status == 'SCHEDULED':
      self.ctct_schedule()

    return ctct_obj

  def ctct_send_preview(self) -> None:
    """Sends preview email for approval."""
    response = requests.post(
      url=f'{self.BASE_URL}{self.API_ENDPOINT}/{self.id}/tests',
      headers=self.headers,
      json={
        'email_addresses': self._get_preview_recipients(),
        'personal_message': self._get_preview_message(),
      },
    )
    self.raise_or_json(response)

  def _get_preview_recipients(self) -> list:
    """Determines who receives the CTCT preview emails."""
    if self.campaign.debug:
      preview_list = 'DEBUG'
    elif self.campaign.post.category == 'COLUMN':
      preview_list = 'COLUMN'
    else:
      preview_list = 'RELEASE'
    return [email for (name, email) in self.PREVIEW_CONTACTS[preview_list]]

  def _get_preview_message(self) -> str:
    """Writes the message sent with preview emails."""

    author = self.campaign.post.author.first_name
    admin = settings.ADMINS[0][0].split()[0]
    message = f'Please let {author or admin} know if you have any edits. '

    if datetime := self.campaign.scheduled_datetime:
      datetime = datetime.strftime('%A, %B %d @ %I:%M %p')
      message += f'This campaign will be sent on {datetime}.'

    return message

  def ctct_schedule(self) -> None:
    """Schedules the `primary_email` CampaignActivity.

    Notes
    -----
    Recipients must be set before scheduling.

    """

    if self.campaign.scheduled_datetime is None:
      message = "Must specify `scheduled_datetime`!"
      raise ValueError(message)

    # Set the recipients based on EmailCampaign's Post category
    contact_lists = self._get_contact_lists()
    self.contact_lists.set(contact_lists)

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
  def subject(self) -> str:
    """Returns the subject line based on the Post title."""
    if self.campaign.post.category == 'COLUMN':
      subject = f'Dr. Ray Perryman: "{self.campaign.post.title}"'
    elif self.campaign.post.category == 'EMAIL':
      subject = self.campaign.post.title
    else:
      subject = (
        f'{self.campaign.post.get_category_display()}: '
        f'{self.campaign.post.title}'
      )
    return subject

  @property
  def preheader(self) -> str:
    """Returns the email preheader based on the Post content."""
    preheader = (
      BeautifulSoup(self.campaign.post.content, features='lxml')
      .find('p')
      .text
      .split('.')[0]
      .replace('\r', '')
      .replace('\n', '')
    )
    return preheader

  @property
  def html_content(self) -> str:
    """Returns the email content as HTML."""

    html_content = render_to_string(
      template_name='accounts/ctct/post_email.html',
      context={
        'post': self.campaign.post,
        'base_url': f'http://www.{settings.PARENT_HOST}',
      },
    )
    return html_content

  @property
  def physical_address(self) -> dict:
    """Returns the company address for email footers."""
    physical_address = getattr(settings, 'CTCT_PHYSICAL_ADDRESS', {
      'address_line1': '',
      'address_line2': '',
      'address_optional': '',
      'city': '',
      'country_code': '',
      'country_name': '',
      'organization_name': '',
      'postal_code': '',
      'state_code': '',
    })
    return physical_address
