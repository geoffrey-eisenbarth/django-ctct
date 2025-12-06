import datetime as dt
import re
from typing import (
  Type, TypeAlias, ClassVar, TypeGuard,
  Any, Literal,
)
from typing_extensions import Self

import jwt

from django.conf import settings
from django.core.validators import validate_email
from django.db import models
from django.db.models import Model
from django.db.models.base import Model as BaseModel
from django.db.models.fields import NOT_PROVIDED
from django.utils import timezone, formats
from django.utils.translation import gettext_lazy as _

from django_ctct.utils import to_dt
from django_ctct.managers import (
  RemoteManager, TokenRemoteManager,
  Serializer,
  ContactListRemoteManager,
  ContactRemoteManager,
  EmailCampaignRemoteManager,
  CampaignActivityRemoteManager, CampaignSummaryRemoteManager,
)


JsonDict = dict[str, Any]
RelatedObjects: TypeAlias = tuple[Type[Model], list[Model]]


class CreatedAtMixin(Model):
  created_at = models.DateTimeField(
    default=timezone.now,
    editable=False,
    verbose_name=_('Created At'),
  )

  class Meta:
    abstract = True

  @classmethod
  def clean_remote_created_at(cls, data: JsonDict) -> dt.datetime:
    created_at = data.get('created_at')
    assert isinstance(created_at, str)
    return to_dt(created_at)


class UpdatedAtMixin(Model):
  updated_at = models.DateTimeField(
    default=timezone.now,
    editable=False,
    verbose_name=_('Updated At'),
  )

  class Meta:
    abstract = True

  @classmethod
  def clean_remote_updated_at(cls, data: JsonDict) -> dt.datetime:
    updated_at = data.get('updated_at')
    assert isinstance(updated_at, str)
    return to_dt(updated_at)


class EndpointMixin(Model):
  """Django implementation of a CTCT model that has API endpoints."""

  API_URL: str = 'https://api.cc.email'
  API_VERSION: str = '/v3'
  API_ENDPOINT: str
  API_GET_QUERIES: dict[str, str] = {}
  API_ENDPOINT_BULK_DELETE: str | None = None
  API_ENDPOINT_BULK_LIMIT: int | None = None

  class Meta:
    abstract = True


class Token(CreatedAtMixin, EndpointMixin, Model):
  """Authorization token for CTCT API access."""

  API_URL = 'https://authz.constantcontact.com/oauth2/default'
  API_VERSION = '/v1'
  API_ENDPOINT = '/token'

  API_JWKS_URL: str = (
    'https://identity.constantcontact.com/'
    'oauth2/aus1lm3ry9mF7x2Ja0h8/v1/keys'
  )
  API_SCOPE: str = '+'.join([
    'account_read',
    'account_update',
    'contact_data',
    'campaign_data',
    'offline_access',
  ])

  TOKEN_TYPE = 'Bearer'
  TOKEN_TYPES = (
    (TOKEN_TYPE, TOKEN_TYPE),
  )

  # Must explicitly specify both
  objects: ClassVar[models.Manager[Self]] = models.Manager()
  remote: ClassVar[TokenRemoteManager] = TokenRemoteManager()

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

  @property
  def expires_at(self) -> dt.datetime:
    return self.created_at + dt.timedelta(seconds=self.expires_in)

  class Meta:
    ordering = ('-created_at', )

  def __str__(self) -> str:
    expires_at = formats.date_format(
      timezone.localtime(self.expires_at),
      settings.DATETIME_FORMAT,
    )
    s = f"{self.token_type} Token (Expires: {expires_at})"
    return s

  def decode(self) -> JsonDict:
    """Decode JWT Token, which also verifies that it hasn't expired.

    Notes
    -----
    Notice that the `audience` value uses the v3 API URL and VERSION.

    """

    client = jwt.PyJWKClient(self.API_JWKS_URL)
    signing_key = client.get_signing_key_from_jwt(self.access_token)
    data = jwt.decode(
      self.access_token,
      signing_key.key,
      algorithms=['RS256'],
      audience=f'{EndpointMixin.API_URL}{EndpointMixin.API_VERSION}',
    )
    assert isinstance(data, dict)
    return data


class SerialModel(Model):
  API_ID_LABEL: str
  API_EDITABLE_FIELDS: tuple[str, ...] = tuple()
  API_READONLY_FIELDS: tuple[str, ...] = (
    'api_id',
  )

  # Must explicitly specify both
  objects: ClassVar[models.Manager[Self]] = models.Manager()
  serializer: ClassVar[Serializer[Self]] = Serializer()

  class Meta:
    abstract = True


class CTCTModel(SerialModel):
  """Common CTCT model methods and properties."""

  API_MAX_LENGTH: dict[str, int] = {}

  api_id = models.UUIDField(
    null=True,     # Allow objects to be created without CTCT IDs
    default=None,  # Models often created without CTCT IDs
    unique=True,   # Note: None != None for uniqueness check
    verbose_name=_('API ID'),
  )

  class Meta:
    abstract = True

  @classmethod
  def clean_remote_string(cls, field_name: str, data: JsonDict) -> str:
    s = data.get(field_name, '')
    assert isinstance(s, str)
    s = s.replace('\n', ' ').replace('\t', ' ').strip()
    max_length = cls.API_MAX_LENGTH[field_name]
    s = s[:max_length]
    return s

  @classmethod
  def clean_remote_string_with_default(
    cls,
    field_name: str,
    data: JsonDict,
    default: str | None = None,
  ) -> str | None:
    if default is None:
      field = cls._meta.get_field(field_name)
      assert hasattr(field, 'default')
      if field.default is NOT_PROVIDED:  # pragma: no cover
        message = _(
          f"Must provide a default value for {cls.__name__}.{field_name}."
        )
        raise ValueError(message)
      else:
        default = field.default

    if field_name in data:
      # If ConstantContact sends a `None` value, we get the default value
      s = data[field_name] or default
      assert isinstance(s, str)
    else:
      # A return value of `None` will remove the field from the cleaned dict
      s = None

    return s


class CTCTEndpointModel(EndpointMixin, CTCTModel):

  # Must explicitly specify both
  objects: ClassVar[models.Manager[Self]] = models.Manager()
  remote: ClassVar[RemoteManager[Self]] = RemoteManager()

  class Meta:
    abstract = True


class ContactList(CreatedAtMixin, UpdatedAtMixin, CTCTEndpointModel):
  """Django implementation of a CTCT Contact List."""

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

  # Must explicitly specify both
  objects: ClassVar[models.Manager[Self]] = models.Manager()
  remote: ClassVar[ContactListRemoteManager] = ContactListRemoteManager()  # noqa: E501

  # API editable fields
  name = models.CharField(
    max_length=API_MAX_LENGTH['name'],
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

  class Meta:
    verbose_name = _('Contact List')
    verbose_name_plural = _('Contact Lists')
    ordering = ('-favorite', 'name')

  def __str__(self) -> str:
    return self.name


class ContactCustomField(SerialModel):
  """Django implementation of a CTCT Contact's CustomField.

  Notes
  -----
  CTCT does not provide UUIDs for these, so we do not inherit from CTCTModel.

  """

  API_EDITABLE_FIELDS = (
    'value',
  )
  API_READONLY_FIELDS = (
    'custom_field_id',
  )
  API_MAX_LENGTH = {
    'value': 255,
  }

  contact = models.ForeignKey(
    'Contact',
    related_name='custom_fields',
    on_delete=models.CASCADE,
    verbose_name=_('Contact'),
  )
  custom_field = models.ForeignKey(
    'CustomField',
    related_name='contacts',
    on_delete=models.CASCADE,
    verbose_name=_('Field'),
  )

  value = models.CharField(
    max_length=API_MAX_LENGTH['value'],
    verbose_name=_('Value'),
  )

  class Meta:
    verbose_name = _('Custom Field')
    verbose_name_plural = _('Custom Fields')

    constraints = [
       models.UniqueConstraint(
         fields=['contact', 'custom_field'],
         name='django_ctct_unique_custom_field',
       ),
       # models.CheckConstraint(   # TODO: GH #8
       #   check=Q(contact__custom_fields__count__lte=ContactRemoteManager.API_MAX_NUM['custom_fields']),
       #   name='django_ctct_limit_custom_fields',
       # ),
    ]

  def __str__(self) -> str:
    try:
      s = f'[{self.custom_field.label}] {self.value}'
    except CustomField.DoesNotExist:  # pragma: no cover
      s = super().__str__()
    return s


class CustomField(CreatedAtMixin, UpdatedAtMixin, CTCTEndpointModel):
  """Django implementation of a CTCT Contact's CustomField."""

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
    'created_at',
    'updated_at',
  )
  API_MAX_LENGTH = {
    'label': 50,
  }

  TYPES = (
    ('string', 'Text'),
    ('date', 'Date'),
  )

  # API editable fields
  label = models.CharField(
    max_length=API_MAX_LENGTH['label'],
    unique=True,
    verbose_name=_('Label'),
    help_text=_(
      'The display name for the custom_field shown in the UI as free-form text'
    ),
  )
  type = models.CharField(
    max_length=6,
    choices=TYPES,
    default=TYPES[0][0],
    verbose_name=_('Type'),
    help_text=_(
      'Specifies the type of value the custom_field field accepts'
    ),
  )

  class Meta:
    verbose_name = _('Custom Field')
    verbose_name_plural = _('Custom Fields')

  def __str__(self) -> str:
    return self.label


class Contact(CreatedAtMixin, UpdatedAtMixin, CTCTEndpointModel):
  """Django implementation of a CTCT Contact.

  Notes
  -----
  The following editable fields are specified in `Contact.serialize()`:
    1) 'email'
    2) 'permission_to_send'
    3) 'create_source'
    4) 'update_source'

  """

  API_ENDPOINT = '/contacts'
  API_GET_QUERIES = {
    'include': ','.join([
      'custom_fields',
      'list_memberships',
      'notes',
      'phone_numbers',
      'street_addresses',
    ]),
  }
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
  API_MAX_LENGTH = {
    'first_name': 50,
    'last_name': 50,
    'job_title': 50,
    'company_name': 50,
    'opt_out_reason': 255,
  }

  API_MAX_NUM = {
    'notes': 150,
    'phone_numbers': 3,
    'street_addresses': 3,
    'custom_fields': 25,
    'list_memberships': 50,
  }

  # Must explicitly specify both
  objects: ClassVar[models.Manager[Self]] = models.Manager()
  remote: ClassVar[ContactRemoteManager] = ContactRemoteManager()  # noqa: E501

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
    max_length=API_MAX_LENGTH['first_name'],
    blank=True,
    verbose_name=_('First Name'),
    help_text=_('The first name of the contact'),
  )
  last_name = models.CharField(
    max_length=API_MAX_LENGTH['last_name'],
    blank=True,
    verbose_name=_('Last Name'),
    help_text=_('The last name of the contact'),
  )
  job_title = models.CharField(
    max_length=API_MAX_LENGTH['job_title'],
    blank=True,
    verbose_name=_('Job Title'),
    help_text=_('The job title of the contact'),
  )
  company_name = models.CharField(
    max_length=API_MAX_LENGTH['company_name'],
    blank=True,
    verbose_name=_('Company Name'),
    help_text=_('The name of the company where the contact works'),
  )

  list_memberships = models.ManyToManyField(
    ContactList,
    related_name='members',
    verbose_name=_('List Memberships'),
    blank=True,
  )
  custom_field_set = models.ManyToManyField(
    CustomField,
    through=ContactCustomField,  # c.f. django-stubs GH PR #1719
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
  update_source = models.CharField(
    max_length=7,
    choices=SOURCES,
    default='Account',
    verbose_name=_('Update Source'),
    help_text=_('Identifies who last updated the contact'),
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
    max_length=API_MAX_LENGTH['opt_out_reason'],
    blank=True,
    verbose_name=_('Opt Out Reason'),
  )

  @property
  def ctct_source(self) -> dict[str, str]:
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
  def clean_remote_email(cls, data: JsonDict) -> str:
    assert isinstance(data['email_address'], dict)
    s = data['email_address'].get('address', '')
    assert isinstance(s, str)
    return s.lower()

  @classmethod
  def clean_remote_opt_out_source(cls, data: JsonDict) -> str:
    assert isinstance(data['email_address'], dict)
    s = data['email_address'].get('opt_out_source', '')
    assert isinstance(s, str)
    return s

  @classmethod
  def clean_remote_opt_out_date(cls, data: JsonDict) -> dt.datetime | None:
    assert isinstance(data['email_address'], dict)
    if opt_out_date := data['email_address'].get('opt_out_date', None):
      assert isinstance(opt_out_date, str)
      return to_dt(opt_out_date)
    else:
      assert opt_out_date is None
      return opt_out_date

  @classmethod
  def clean_remote_opt_out_reason(cls, data: JsonDict) -> str:
    assert isinstance(data['email_address'], dict)
    s = data['email_address'].get('opt_out_reason', '')
    assert isinstance(s, str)
    return s

  def serialize(self, data: JsonDict) -> JsonDict:
    data['email_address'] = {
      'address': self.email,
      'permission_to_send': self.permission_to_send,
    }
    data.update(self.ctct_source)
    return data


class ContactNote(CreatedAtMixin, CTCTModel):
  """Django implementation of a CTCT Note."""

  API_ID_LABEL = 'note_id'
  API_EDITABLE_FIELDS = (
    'content',
  )
  API_READONLY_FIELDS = (
    'api_id',
    'created_at',
  )
  API_MAX_LENGTH = {
    'content': 2000,
  }

  contact = models.ForeignKey(
    Contact,
    on_delete=models.CASCADE,
    related_name='notes',
    verbose_name=_('Contact'),
  )
  author = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    null=True,
    related_name='notes',
    verbose_name=_('Author'),
  )

  # API editable fields
  content = models.CharField(
    max_length=API_MAX_LENGTH['content'],
    verbose_name=_('Content'),
    help_text=_('The content for the note'),
  )

  class Meta:
    verbose_name = _('Note')
    verbose_name_plural = _('Notes')

    # TODO: GH #8
    # constraints = [
    #   models.CheckConstraint(
    #     check=Q(contact__notes__count__lte=ContactRemoteManager.API_MAX_NUM['notes']),
    #     name='django_ctct_limit_notes',
    #   ),
    # ]

  def __str__(self) -> str:
    author = self.author or _('Unknown author')
    created_at = formats.date_format(
      timezone.localtime(self.created_at),
      settings.DATETIME_FORMAT,
    )
    return f'{author} on {created_at}'


class ContactPhoneNumber(CreatedAtMixin, UpdatedAtMixin, CTCTModel):
  """Django implementation of a CTCT Contact's PhoneNumber."""

  API_ID_LABEL = 'phone_number_id'
  API_EDITABLE_FIELDS = (
    'kind',
    'phone_number',
  )
  API_READONLY_FIELDS = (
    'api_id',
    'created_at',
    'updated_at',
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
       models.UniqueConstraint(
         fields=['contact', 'kind'],
         name='django_ctct_unique_phone_number',
       ),
       # models.CheckConstraint(   # TODO: GH #8
       #   check=Q(contact__phone_numbers__count__lte=ContactRemoteManager.API_MAX_NUM['phone_numbers'])
       #   name='django_ctct_limit_phone_numbers',
       # ),
    ]

  def __str__(self) -> str:
    return f'[{self.get_kind_display()}] {self.phone_number}'

  @classmethod
  def clean_remote_phone_number(cls, data: JsonDict) -> str:
    numbers = r'\d+'
    s = data.get('phone_number', '')
    assert isinstance(s, str)
    s = ''.join(re.findall(numbers, s)) or cls.MISSING_NUMBER
    return s


class ContactStreetAddress(CreatedAtMixin, UpdatedAtMixin, CTCTModel):
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
  API_READONLY_FIELDS = (
    'api_id',
    'created_at',
    'updated_at',
  )
  API_MAX_LENGTH = {
    'street': 255,
    'city': 50,
    'state': 50,
    'postal_code': 50,
    'country': 50,
  }

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

  # API editable fields
  kind = models.CharField(
    choices=KINDS,
    max_length=5,
    verbose_name=_('Kind'),
    help_text=_('Describes the type of address'),
  )
  street = models.CharField(
    max_length=API_MAX_LENGTH['street'],
    verbose_name=_('Street'),
    help_text=_('Number and street of the address'),
  )
  city = models.CharField(
    max_length=API_MAX_LENGTH['city'],
    verbose_name=_('City'),
    help_text=_('The name of the city where the contact lives'),
  )
  state = models.CharField(
    max_length=API_MAX_LENGTH['state'],
    verbose_name=_('State'),
    help_text=_('The name of the state or province where the contact lives'),
  )
  postal_code = models.CharField(
    max_length=API_MAX_LENGTH['postal_code'],
    verbose_name=_('Postal Code'),
    help_text=_('The zip or postal code of the contact'),
  )
  country = models.CharField(
    max_length=API_MAX_LENGTH['country'],
    verbose_name=_('Country'),
    help_text=_('The name of the country where the contact lives'),
  )

  class Meta:
    verbose_name = _('Street Address')
    verbose_name_plural = _('Street Addresses')

    constraints = [
       models.UniqueConstraint(
         fields=['contact', 'kind'],
         name='django_ctct_unique_street_address',
       ),
       # models.CheckConstraint(   # TODO: GH #8
       #   check=Q(contact__street_addresses__count__lte=ContactRemoteManager.API_MAX_NUM['street_addresses']),
       #   name='django_ctct_limit_street_addresses',
       # ),
    ]

  def __str__(self) -> str:
    field_names = ['street', 'city', 'state']
    address = ', '.join(
      getattr(self, _) for _ in field_names if getattr(self, _)
    )
    return f'[{self.get_kind_display()}] {address}'

  @classmethod
  def clean_remote_street(cls, data: JsonDict) -> str:
    return cls.clean_remote_string('street', data)

  @classmethod
  def clean_remote_city(cls, data: JsonDict) -> str:
    return cls.clean_remote_string('city', data)

  @classmethod
  def clean_remote_state(cls, data: JsonDict) -> str:
    return cls.clean_remote_string('state', data)

  @classmethod
  def clean_remote_postal_code(cls, data: JsonDict) -> str:
    return cls.clean_remote_string('postal_code', data)

  @classmethod
  def clean_remote_country(cls, data: JsonDict) -> str:
    return cls.clean_remote_string('country', data)


class EmailCampaign(CreatedAtMixin, UpdatedAtMixin, CTCTEndpointModel):
  """Django implementation of a CTCT EmailCampaign."""

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
    'name': 80,
  }

  # Must explicitly specify both
  objects: ClassVar[models.Manager[Self]] = models.Manager()
  remote: ClassVar[EmailCampaignRemoteManager] = EmailCampaignRemoteManager()  # noqa: E501

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
    max_length=API_MAX_LENGTH['name'],
    unique=True,
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

  class Meta:
    verbose_name = _('Email Campaign')
    verbose_name_plural = _('Email Campaigns')

    ordering = ('-created_at', '-scheduled_datetime')

  def __str__(self) -> str:
    return self.name

  @classmethod
  def clean_remote_scheduled_datetime(cls, data: JsonDict) -> dt.datetime | None:  # noqa: E501
    if last_sent_date := data.get('last_sent_date', None):
      # Not sure why this ts_format is different
      assert isinstance(last_sent_date, str)
      return to_dt(last_sent_date, ts_format='%Y-%m-%dT%H:%M:%S.000Z')
    else:
      assert last_sent_date is None
      return last_sent_date


def campaign_activity__from_name__default() -> str:
  return settings.CTCT_FROM_NAME


def campaign_activity__from_email__default() -> str:
  return settings.CTCT_FROM_EMAIL


def campaign_activity__reply_to_email__default() -> str:
  return getattr(settings, 'CTCT_REPLY_TO_EMAIL', settings.CTCT_FROM_EMAIL)


class CampaignActivity(CTCTEndpointModel):
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
  API_GET_QUERIES = {
    'include': ','.join([
      # 'physical_address_in_footer',
      # 'permalink_url',
      'html_content',
      # 'document_properties',
    ]),
  }

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

  # Must explicitly specify both
  objects: ClassVar[models.Manager[Self]] = models.Manager()
  remote: ClassVar[CampaignActivityRemoteManager] = CampaignActivityRemoteManager()  # noqa: E501

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
    related_name='campaign_activities',
    verbose_name=_('Campaign'),
  )

  # API editable fields
  from_name = models.CharField(
    max_length=API_MAX_LENGTH['from_name'],
    default=campaign_activity__from_name__default,
    verbose_name=_('From Name'),
  )
  from_email = models.EmailField(
    max_length=API_MAX_LENGTH['from_email'],
    default=campaign_activity__from_email__default,
    verbose_name=_('From Email'),
  )
  reply_to_email = models.EmailField(
    max_length=API_MAX_LENGTH['reply_to_email'],
    default=campaign_activity__reply_to_email__default,
    verbose_name=_('Reply-to Email'),
  )
  subject = models.CharField(
    max_length=API_MAX_LENGTH['subject'],
    verbose_name=_('Subject'),
    help_text=_(
      'The text to display in the subject line that describes the email '
      'campaign activity'
    ),
  )
  preheader = models.CharField(
    max_length=API_MAX_LENGTH['preheader'],
    verbose_name=_('Preheader'),
    help_text=_(
      'Contacts will view your preheader as a short summary that follows '
      'the subject line in their email client'
    ),
  )
  html_content = models.CharField(
    max_length=API_MAX_LENGTH['html_content'],
    verbose_name=_('HTML Content'),
    help_text=_('The HTML content for the email campaign activity'),
  )
  contact_lists = models.ManyToManyField(
    ContactList,
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
    default=FORMAT_TYPES[4][0],  # evals to 5
    verbose_name=_('Format Type'),
  )

  @property
  def physical_address_in_footer(self) -> dict[str, str] | None:
    """Returns the company address for email footers.

    Notes
    -----
    If you do not include a physical address in the email campaign activity,
    Constant Contact will use the physical address information saved for the
    Constant Contact user account.

    """
    return getattr(settings, 'CTCT_PHYSICAL_ADDRESS', None)

  class Meta:
    verbose_name = _('Email Campaign Activity')
    verbose_name_plural = _('Email Campaign Activities')

    constraints = [
      models.UniqueConstraint(
        fields=['campaign', 'role'],
        name='django_ctct_unique_campaign_activity',
      ),
    ]

  def __str__(self) -> str:
    try:
      s = f'{self.campaign}, {self.get_role_display()}'
    except EmailCampaign.DoesNotExist:
      s = super().__str__()
    return s

  def serialize(self, data: JsonDict) -> JsonDict:
    if 'contact_lists' in data:
      # Be careful to include data['contact_lists'] even if it's empty
      data['contact_list_ids'] = data.pop('contact_lists')

    assert isinstance(data['html_content'], str)
    data['html_content'] = self.clean_html_content(data['html_content'])
    return data

  @classmethod
  def clean_remote_from_name(cls, data: JsonDict) -> str | None:
    return cls.clean_remote_string_with_default('from_name', data)

  @classmethod
  def clean_remote_from_email(cls, data: JsonDict) -> str | None:
    return cls.clean_remote_string_with_default('from_email', data)

  @classmethod
  def clean_remote_reply_to_email(cls, data: JsonDict) -> str | None:
    return cls.clean_remote_string_with_default('reply_to_email', data)

  @classmethod
  def clean_remote_subject(cls, data: JsonDict) -> str | None:
    """Pass a `default` here so it won't appear in admin forms."""
    default = cls.MISSING_SUBJECT
    return cls.clean_remote_string_with_default('subject', data, default)

  @classmethod
  def clean_remote_contact_lists(cls, data: JsonDict) -> list[str]:
    l = data.pop('contact_list_ids', [])  # noqa: E741
    assert isinstance(l, list)
    assert all([isinstance(i, str) for i in l])
    return l

  def clean_html_content(self, html_content: str) -> str:
    if self.TRACKING_IMAGE not in html_content:
      html_content = self.TRACKING_IMAGE + '\n' + html_content
    return html_content

  def save(self, *args: Any, **kwargs: Any) -> None:
    self.html_content = self.clean_html_content(self.html_content)
    super().save(*args, **kwargs)


class CampaignSummary(CTCTEndpointModel):
  """Django implementation of a CTCT EmailCampaign report."""

  API_ENDPOINT = '/reports/summary_reports/email_campaign_summaries'

  API_ID_LABEL = 'campaign_id'
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

  # Must explicitly specify both
  objects: ClassVar[models.Manager[Self]] = models.Manager()
  remote: ClassVar[CampaignSummaryRemoteManager] = CampaignSummaryRemoteManager()  # noqa: E501

  campaign = models.OneToOneField(
    EmailCampaign,
    on_delete=models.CASCADE,
    related_name='summary',
    verbose_name=_('Email Campaign'),
  )

  # API read-only fields
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
    verbose_name = _('Email Campaign Report')
    verbose_name_plural = _('Email Campaign Reports')

    ordering = ('-campaign', )

  @classmethod
  def clean_remote_counts(cls, field_name: str, data: JsonDict) -> int:
    counts = data.get('unique_counts', {})
    assert isinstance(counts, dict)
    i = counts.get(field_name, 0)
    assert isinstance(i, int)
    return i

  @classmethod
  def clean_remote_sends(cls, data: JsonDict) -> int:
    return cls.clean_remote_counts('sends', data)

  @classmethod
  def clean_remote_opens(cls, data: JsonDict) -> int:
    return cls.clean_remote_counts('opens', data)

  @classmethod
  def clean_remote_clicks(cls, data: JsonDict) -> int:
    return cls.clean_remote_counts('clicks', data)

  @classmethod
  def clean_remote_forwards(cls, data: JsonDict) -> int:
    return cls.clean_remote_counts('forwards', data)

  @classmethod
  def clean_remote_optouts(cls, data: JsonDict) -> int:
    return cls.clean_remote_counts('optouts', data)

  @classmethod
  def clean_remote_abuse(cls, data: JsonDict) -> int:
    return cls.clean_remote_counts('abuse', data)

  @classmethod
  def clean_remote_bounces(cls, data: JsonDict) -> int:
    return cls.clean_remote_counts('bounces', data)

  @classmethod
  def clean_remote_not_opened(cls, data: JsonDict) -> int:
    return cls.clean_remote_counts('not_opened', data)

  def save(self, *args: Any, **kwargs: Any) -> None:
    """Must set api_id manually."""
    if (self.api_id is None) and (self.campaign_id is not None):
      self.api_id = self.campaign.api_id
    super().save(*args, **kwargs)


def is_ctct(
  val: Type[BaseModel] | Literal['self'] | None
) -> TypeGuard[Type[CTCTModel]]:
  return isinstance(val, type) and issubclass(val, CTCTModel)


def is_model(
  val: Type[BaseModel] | Literal['self'] | None
) -> TypeGuard[Type[Model]]:
  return isinstance(val, type) and issubclass(val, Model)


def is_serial(
   val: Type[BaseModel] | Literal['self'] | None
) -> TypeGuard[Type[SerialModel]]:
  return isinstance(val, type) and issubclass(val, SerialModel)
