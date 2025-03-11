import datetime as dt
import re
from typing import Optional

import jwt

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models
from django.db.models import Model
from django.db.models.fields import NOT_PROVIDED
from django.utils import timezone, formats
from django.utils.translation import gettext_lazy as _

from django_ctct.utils import to_dt
from django_ctct.managers import (
  RemoteManager, TokenRemoteManager,
  ContactListRemoteManager, CustomFieldRemoteManager,
  ContactRemoteManager, ContactNoteRemoteManager,
  ContactPhoneNumberRemoteManager, ContactStreetAddressRemoteManager,
  ContactCustomFieldRemoteManager,
  EmailCampaignRemoteManager, CampaignActivityRemoteManager,
)


class Token(Model):
  """Authorization token for CTCT API access."""

  API_JWKS_URL = (
    'https://identity.constantcontact.com/'
    'oauth2/aus1lm3ry9mF7x2Ja0h8/v1/keys'
  )

  TOKEN_TYPE = 'Bearer'
  TOKEN_TYPES = (
    (TOKEN_TYPE, TOKEN_TYPE),
  )

  # Must explicitly specify both
  objects = models.Manager()
  remote = TokenRemoteManager()

  access_token = models.TextField(
    verbose_name=_('Access Token'),
  )
  refresh_token = models.CharField(
    max_length=50,
    verbose_name=_('Refresh Token'),
  )
  token_type = models.CharField(
    max_length=6,
    choices=TOKEN_TYPES,
    default=TOKEN_TYPE,
    verbose_name=_('Token Type'),
  )
  scope = models.CharField(
    max_length=255,
    verbose_name=_('Scope'),
  )
  expires_in = models.IntegerField(
    default=60 * 60 * 24,
    verbose_name=_('Expires In'),
  )
  created_at = models.DateTimeField(
    auto_now=False,
    auto_now_add=True,
    verbose_name=_('Created At'),
  )

  class Meta:
    ordering = ('-created_at', )

  def __str__(self) -> str:
    s = formats.date_format(
      timezone.localtime(self.created_at),
      settings.DATETIME_FORMAT,
    )
    return s

  @property
  def expires_at(self) -> dt.datetime:
    return self.created_at + dt.timedelta(seconds=self.expires_in)

  def decode(self) -> dict:
    """Decode JWT Token, which also verifies that it hasn't expired."""

    client = jwt.PyJWKClient(self.API_JWKS_URL)
    signing_key = client.get_signing_key_from_jwt(self.access_token)
    data = jwt.decode(
      self.access_token,
      signing_key.key,
      algorithms=['RS256'],
      audience=f'{RemoteManager.API_URL}{RemoteManager.API_VERSION}',
    )
    return data


class CTCTModel(Model):
  """Common CTCT model methods and properties."""

  # Must explicitly specify both
  objects = models.Manager()
  remote = RemoteManager()

  api_id = models.UUIDField(
    null=True,     # Allow objects to be created without CTCT IDs
    default=None,  # Models often created without CTCT IDs
    unique=True,   # Note: None != None for uniqueness check
    verbose_name=_('API ID'),
  )

  class Meta:
    abstract = True

  @classmethod
  def clean_remote_counts(cls, field_name: str, data: dict) -> int:
    return data.get('unique_counts', {}).get(field_name, 0)

  @classmethod
  def clean_remote_string(cls, field_name: str, data: dict) -> str:
    s = data.get(field_name, '')
    s = s.replace('\n', ' ').replace('\t', ' ').strip()
    max_length = cls.remote.API_MAX_LENGTH[field_name]
    s = s[:max_length]
    return s

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


class CTCTRemoteModel(CTCTModel):
  """Django implementation of a CTCT model that has API endpoints."""

  # Must explicitly specify both
  objects = models.Manager()
  remote = RemoteManager()

  class Meta:
    abstract = True


class ContactList(CTCTRemoteModel):
  """Django implementation of a CTCT Contact List."""

  # Must explicitly specify both
  objects = models.Manager()
  remote = ContactListRemoteManager()

  # API editable fields
  name = models.CharField(
    max_length=remote.API_MAX_LENGTH['name'],
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


class CustomField(CTCTRemoteModel):
  """Django implementation of a CTCT Contact's CustomField."""

  # Must explicitly specify both
  objects = models.Manager()
  remote = CustomFieldRemoteManager()

  TYPES = (
    ('string', 'Text'),
    ('date', 'Date'),
  )

  # API editable fields
  label = models.CharField(
    max_length=remote.API_MAX_LENGTH['label'],
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


class Contact(CTCTRemoteModel):
  """Django implementation of a CTCT Contact.

  Notes
  -----
  The following editable fields are specified in `Contact.serialize()`:
    1) 'email'
    2) 'permission_to_send'
    3) 'create_source'
    4) 'update_source'

  """

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
    max_length=remote.API_MAX_LENGTH['first_name'],
    blank=True,
    verbose_name=_('First Name'),
    help_text=_('The first name of the contact'),
  )
  last_name = models.CharField(
    max_length=remote.API_MAX_LENGTH['last_name'],
    blank=True,
    verbose_name=_('Last Name'),
    help_text=_('The last name of the contact'),
  )
  job_title = models.CharField(
    max_length=remote.API_MAX_LENGTH['job_title'],
    blank=True,
    verbose_name=_('Job Title'),
    help_text=_('The job title of the contact'),
  )
  company_name = models.CharField(
    max_length=remote.API_MAX_LENGTH['company_name'],
    blank=True,
    verbose_name=_('Company Name'),
    help_text=_('The name of the company where the contact works'),
  )

  list_memberships = models.ManyToManyField(
    ContactList,
    #through='ContactAndContactList',
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
    max_length=remote.API_MAX_LENGTH['opt_out_reason'],
    blank=True,
    verbose_name=_('Opt Out Reason'),
  )

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
    return self.email

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


class ContactAndContactList(Model):
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
    #to_field='api_id',
  )
  contactlist = models.ForeignKey(
    ContactList,
    on_delete=models.CASCADE,
    #to_field='api_id',
  )

  class Meta:
    # https://code.djangoproject.com/ticket/12203#comment:22
    # See GH: https://github.com/django/django/pull/18612
    pass
    #auto_created = True


class ContactNote(CTCTModel):
  """Django implementation of a CTCT Note."""

  # Must explicitly specify both
  objects = models.Manager()
  remote = ContactNoteRemoteManager()

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    #to_field='api_id',
    related_name='notes',
    verbose_name=_('Contact'),
  )
  author = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    null=True,
    verbose_name=_('Author'),
  )

  # API editable fields
  content = models.CharField(
    max_length=remote.API_MAX_LENGTH['content'],
    verbose_name=_('Content'),
    help_text=_('The content for the note'),
  )
  created_at = models.DateTimeField(
    auto_now=True,
    verbose_name=_('Created at'),
    help_text=_('The date the note was created'),
  )

  class Meta:
    verbose_name = _('Note')
    verbose_name_plural = _('Notes')

    # TODO PUSH: CheckConstraint
    # constraints = [
    #   models.CheckConstraint(
    #     check=Q(contact__notes__count__lte=ContactRemoteManager.API_MAX_NOTES),
    #     name='limit_notes'
    #   ),
    # ]


class ContactPhoneNumber(CTCTModel):
  """Django implementation of a CTCT Contact's PhoneNumber."""

  # Must explicitly specify both
  objects = models.Manager()
  remote = ContactPhoneNumberRemoteManager()

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
    #to_field='api_id',
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
  phone_number = models.CharField(
    max_length=25,
    verbose_name=_('Phone Number'),
    help_text=_("The contact's phone number"),
  )

  class Meta:
    verbose_name = _('Phone Number')
    verbose_name_plural = _('Phone Numbers')

    constraints = [
      # TODO PUSH: UniqueConstraint not enforced by CTCT?
      # models.UniqueConstraint(
      #   fields=['contact', 'kind'],
      #   name='unique_phone_number',
      # ),
      # TODO PUSH: CheckConstraint
      # models.CheckConstraint(
      #   check=Q(contact__phone_numbers__count__lte=ContactRemoteManager.API_MAX_PHONE_NUMBERS),
      #   name='limit_phone_numbers',
      # ),
    ]

  def __str__(self) -> str:
    return f'[{self.get_kind_display()}] {self.phone_number}'

  @classmethod
  def clean_remote_phone_number(cls, data: dict) -> str:
    numbers = r'\d+'
    if phone_number := ''.join(re.findall(numbers, data['phone_number'])):
      pass
    else:
      phone_number = cls.MISSING_NUMBER
    return phone_number


class ContactStreetAddress(CTCTModel):
  """Django implementation of a CTCT Contact's StreetAddress."""

  # Must explicitly specify both
  objects = models.Manager()
  remote = ContactStreetAddressRemoteManager()

  KINDS = (
    ('home', 'Home'),
    ('work', 'Work'),
    ('other', 'Other'),
  )

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    #to_field='api_id',
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
    max_length=remote.API_MAX_LENGTH['street'],
    verbose_name=_('Street'),
    help_text=_('Number and street of the address'),
  )
  city = models.CharField(
    max_length=remote.API_MAX_LENGTH['city'],
    verbose_name=_('City'),
    help_text=_('The name of the city where the contact lives'),
  )
  state = models.CharField(
    max_length=remote.API_MAX_LENGTH['state'],
    verbose_name=_('State'),
    help_text=_('The name of the state or province where the contact lives'),
  )
  postal_code = models.CharField(
    max_length=remote.API_MAX_LENGTH['postal_code'],
    verbose_name=_('Postal Code'),
    help_text=_('The zip or postal code of the contact'),
  )
  country = models.CharField(
    max_length=remote.API_MAX_LENGTH['country'],
    verbose_name=_('Country'),
    help_text=_('The name of the country where the contact lives'),
  )

  class Meta:
    verbose_name = _('Street Address')
    verbose_name_plural = _('Street Addresses')

    constraints = [
      # TODO PUSH: UniqueConstraint not enforced by CTCT?
      # models.UniqueConstraint(
      #   fields=['contact', 'kind'],
      #   name='unique_street_address',
      # ),
      # TODO PUSH: CheckConstraint
      # models.CheckConstraint(
      #   check=Q(contact__street_addresses__count__lte=ContactRemoteManager.API_MAX_STREET_ADDRESSES),
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
  def clean_remote_street(cls, data: dict) -> str:
    return cls.clean_remote_string('street', data)

  @classmethod
  def clean_remote_city(cls, data: dict) -> str:
    return cls.clean_remote_string('city', data)

  @classmethod
  def clean_remote_state(cls, data: dict) -> str:
    return cls.clean_remote_string('state', data)

  @classmethod
  def clean_remote_postal_code(cls, data: dict) -> str:
    return cls.clean_remote_string('postal_code', data)

  @classmethod
  def clean_remote_country(cls, data: dict) -> str:
    return cls.clean_remote_string('country', data)


class ContactCustomField(CTCTModel):
  """Django implementation of a CTCT Contact's CustomField.

  Notes
  -----
  It's important to specify `custom_field_id` in `API_EDITABLE_FIELDS` instead
  of `custom_field`; using the latter will result in a serialized CustomField
  instance when Contact is serialized.

  """

  # Must explicitly specify both
  objects = models.Manager()
  remote = ContactCustomFieldRemoteManager()

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    #to_field='api_id',
    related_name='custom_fields',
    verbose_name=_('Contact'),
  )

  # API editable fields
  custom_field = models.ForeignKey(
    CustomField,
    on_delete=models.CASCADE,
    #to_field='api_id',
    related_name='instances',
    verbose_name=_('Field'),
  )
  value = models.CharField(
    max_length=remote.API_MAX_LENGTH['value'],
    verbose_name=_('Value'),
  )

  class Meta:
    verbose_name = _('Custom Field')
    verbose_name_plural = _('Custom Fields')

    constraints = [
      # TODO PUSH: UniqueConstraint not enforced by CTCT?
      # models.UniqueConstraint(
      #   fields=['contact', 'custom_field'],
      #   name='unique_custom_field',
      # ),
      # TODO PUSH: CheckConstraint
      # models.CheckConstraint(
      #   check=Q(contact__custom_fields__count__lte=ContactRemoteManager.API_MAX_CUSTOM_FIELDS),
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


# TODO: EmailCampaign.current_status vs CampaignActivity.current_status
class EmailCampaign(CTCTRemoteModel):
  """Django implementation of a CTCT EmailCampaign."""

  # Must explicitly specify both
  objects = models.Manager()
  remote = EmailCampaignRemoteManager()

  STATUSES = (
    ('NONE', 'Processing'),
    ('DRAFT', 'Draft'),
    ('SCHEDULED', 'Scheduled'),
    ('EXECUTING', 'Executing'),
    ('DONE', 'Sent'),
    ('ERROR', 'Error'),
    ('REMOVED', 'Removed'),
  )

  # API editable fields
  name = models.CharField(
    max_length=remote.API_MAX_LENGTH['name'],
    # unique=True,  # TODO PUSH: UniqueConstraint not enforced by CTCT?
    verbose_name=_('Name'),
  )
  scheduled_datetime = models.DateTimeField(
    blank=True,
    null=True,
    verbose_name=_('Scheduled'),
    help_text=_('Leave blank to unschedule'),
  )

  # Internal fields
  send_preview = models.BooleanField(
    default=False,
    verbose_name=_('Send Preview'),
  )

  # API read-only fields
  current_status = models.CharField(
    choices=STATUSES,
    max_length=20,
    default='DRAFT',
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
    null=True,
    default=None,
    verbose_name=_('Sends'),
    help_text=_('The total number of unique sends'),
  )
  opens = models.IntegerField(
    null=True,
    default=None,
    verbose_name=_('Opens'),
    help_text=_('The total number of unique opens'),
  )
  clicks = models.IntegerField(
    null=True,
    default=None,
    verbose_name=_('Clicks'),
    help_text=_('The total number of unique clicks'),
  )
  forwards = models.IntegerField(
    null=True,
    default=None,
    verbose_name=_('Forwards'),
    help_text=_('The total number of unique forwards'),
  )
  optouts = models.IntegerField(
    null=True,
    default=None,
    verbose_name=_('Opt Out'),
    help_text=_('The total number of people who unsubscribed'),
  )
  abuse = models.IntegerField(
    null=True,
    default=None,
    verbose_name=_('Spam'),
    help_text=_('The total number of people who marked as spam'),
  )
  bounces = models.IntegerField(
    null=True,
    default=None,
    verbose_name=_('Bounces'),
    help_text=_('The total number of bounces'),
  )
  not_opened = models.IntegerField(
    null=True,
    default=None,
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
        "Must schedule the campaign for at least 30 minutes in the future."
      )
      raise ValidationError(message)

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


class CampaignActivity(CTCTRemoteModel):
  """Django implementation of a CTCT CampaignActivity.

  Notes
  -----
  The CTCT API is set up so that EmailCampaigns have multiple
  CampaignActivities ('primary_email', 'permalink', 'resend'). For
  our purposes, the `primary_email` CampaignActivity is the most
  important one, and as such the design of this model is primarily
  based off of them.

  """

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
  TRACKING_IMAGE = '[[trackingImage]]'

  campaign = models.ForeignKey(
    EmailCampaign,
    on_delete=models.CASCADE,
    #to_field='api_id',
    related_name='campaign_activities',
    verbose_name=_('Campaign'),
  )

  # API editable fields
  from_name = models.CharField(
    max_length=remote.API_MAX_LENGTH['from_name'],
    default=settings.CTCT_FROM_NAME,
    verbose_name=_('From Name'),
  )
  from_email = models.EmailField(
    max_length=remote.API_MAX_LENGTH['from_email'],
    default=settings.CTCT_FROM_EMAIL,
    verbose_name=_('From Email'),
  )
  reply_to_email = models.EmailField(
    max_length=remote.API_MAX_LENGTH['reply_to_email'],
    default=getattr(settings, 'CTCT_REPLY_TO_EMAIL', settings.CTCT_FROM_EMAIL),
    verbose_name=_('Reply-to Email'),
  )
  subject = models.CharField(
    max_length=remote.API_MAX_LENGTH['subject'],
    verbose_name=_('Subject'),
    help_text=_(
      'The text to display in the subject line that describes the email '
      'campaign activity'
    ),
  )
  preheader = models.CharField(
    max_length=remote.API_MAX_LENGTH['preheader'],
    verbose_name=_('Preheader'),
    help_text=_(
      'Contacts will view your preheader as a short summary that follows '
      'the subject line in their email client'
    ),
  )
  html_content = models.CharField(
    max_length=remote.API_MAX_LENGTH['html_content'],
    verbose_name=_('HTML Content'),
    help_text=_('The HTML content for the email campaign activity'),
  )
  contact_lists = models.ManyToManyField(
    ContactList,
    #through='CampaignActivityAndContactList',
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
    default='DRAFT',
    verbose_name=_('Current Status'),
  )

  # Must be set to 5 on outgoing requests,
  # but imports could have other values
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
    if contact_lists := data.pop('contact_lists', None):
      data['contact_list_ids'] = contact_lists

    if self.TRACKING_IMAGE not in data['html_content']:
      data['html_content'] = self.TRACKING_IMAGE + '\n' + data['html_content']
    return data

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


class CampaignActivityAndContactList(Model):
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
    #to_field='api_id',
  )
  contactlist = models.ForeignKey(
    ContactList,
    on_delete=models.CASCADE,
    #to_field='api_id',
  )
