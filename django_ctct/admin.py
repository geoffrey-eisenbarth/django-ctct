import functools
from typing import Optional, Callable, Iterable, Generic, Union, ParamSpec
from requests.exceptions import HTTPError

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.db.models import signals
from django.db.models import Model, QuerySet, When, Case, F, FloatField
from django.db.models.functions import Cast
from django.forms import ModelForm, BaseFormSet
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from django_ctct.models import (
  CTCTEndpointModel, E, ContactList, CustomField,
  Contact,
  ContactCustomField, ContactStreetAddress, ContactPhoneNumber, ContactNote,
  EmailCampaign, CampaignActivity, CampaignSummary,
)
from django_ctct.signals import remote_save, remote_delete
from django_ctct.vendor import mute_signals


P = ParamSpec('P')

# TODO: Is there a way to simplify this?
ContactInlineModel = Union[
  ContactCustomField,
  ContactPhoneNumber,
  ContactStreetAddress,
  ContactNote,
]
ContactInlineFormSet = BaseInlineFormSet[
  ContactInlineModel,
  Contact,
  ModelForm[ContactInlineModel]
]


def catch_api_errors(func: Callable[P, None]) -> Callable[P, None]:
  """Decorator to catch HTTP errors from CTCT API.

  Notes
  -----
  If the wrapped functioms (e.g., `save_related()` didn't return `None`, then
  we would need to adjust the types above to `Callable[P, R]`, where R is
  defined as TypeVar('R').

  """

  @functools.wraps(func)
  def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
    try:
      return func(*args, **kwargs)
    except HTTPError as e:
      if getattr(settings, 'CTCT_RAISE_FOR_API', False):
        raise e
      else:
        self, request, *_ = args
        assert isinstance(self, admin.ModelAdmin)
        assert isinstance(request, HttpRequest)
        self.message_user(
          request=request,
          message=format_html(_(f"ConstantContact: {e}")),
          level=messages.ERROR,
        )

  return wrapper


class RemoteSyncMixin:
  @admin.display(
    boolean=True,
    description=_('Synced'),
    ordering='api_id',
  )
  def is_synced(self, obj: CTCTEndpointModel) -> bool:
    return (obj.api_id is not None)


class ViewModelAdmin(admin.ModelAdmin[Model]):
  """Remove CRUD permissions."""

  actions = None

  def has_add_permission(
    self,
    request: HttpRequest,
    obj: Optional[Model] = None,
  ) -> bool:
    """Prevent creation in the Django admin."""
    return False

  def has_change_permission(
    self,
    request: HttpRequest,
    obj: Optional[Model] = None,
  ) -> bool:
    """Prevent updates in the Django admin."""
    return False

  def get_readonly_fields(
    self,
    request: HttpRequest,
    obj: Optional[Model] = None,
  ) -> tuple[str, ...]:
    """Prevent updates in the Django admin."""
    if obj is not None:
      readonly_fields = tuple(
        field.name
        for field in obj._meta.fields
        if field.name != 'active'
      )
    else:
      readonly_fields = tuple()
    return readonly_fields

  def has_delete_permission(
    self,
    request: HttpRequest,
    obj: Optional[Model] = None,
  ) -> bool:
    """Allow superusers to delete objects."""
    return request.user.is_superuser


class RemoteModelAdmin(
  RemoteSyncMixin, admin.ModelAdmin[E], Generic[E]
):
  """Facilitate remote saving and deleting."""

  # ChangeView
  @property
  def remote_sync(self) -> bool:
    sync_admin = getattr(settings, 'CTCT_SYNC_ADMIN', False)
    sync_signals = getattr(settings, 'CTCT_SYNC_SIGNALS', False)
    return sync_admin and not sync_signals

  @catch_api_errors
  def delete_model(self, request: HttpRequest, obj: Model) -> None:
    obj.delete()
    if self.remote_sync:
      remote_delete(sender=self.model, instance=obj)

  @catch_api_errors
  def delete_queryset(
    self,
    request: HttpRequest,
    queryset: QuerySet[CTCTEndpointModel],
  ) -> None:
    if self.remote_sync:
      queryset.model.remote.bulk_delete(queryset)
    with mute_signals(signals.pre_delete):
      queryset.delete()

  @catch_api_errors
  def save_related(
    self,
    request: HttpRequest,
    form: ModelForm[CTCTEndpointModel],
    formsets: list[BaseFormSet[ModelForm[Model]]],
    change: bool,
  ) -> None:
    """Default implementation with an added line for saving remotely.

    Notes
    -----
    This gets called even if related fields don't exist, so we use it as a hook
    for saving objects remotely.

    """
    with mute_signals(signals.m2m_changed):
      # ManyToMany information is sent to CTCT in PUT call
      form.save_m2m()
    for formset in formsets:
      self.save_formset(request, form, formset, change=change)
    self.save_remotely(request, form, formsets, change)

  @catch_api_errors
  def save_remotely(
    self,
    request: HttpRequest,
    form: ModelForm[CTCTEndpointModel],
    formsets: list[BaseFormSet[ModelForm[Model]]],
    change: bool,
  ) -> None:
    if self.remote_sync:
      # Remote save the primary object after related objects have been saved
      remote_save(
        sender=self.model,
        instance=form.instance,
        created=not change,
      )


class ContactListForm(forms.ModelForm[ContactList]):
  """Custom widget choices for ContactList admin."""

  class Meta:
    model = ContactList
    widgets = {
      'description': forms.Textarea,
    }
    fields = '__all__'


class ContactListAdmin(RemoteModelAdmin[ContactList]):
  """Admin functionality for CTCT ContactLists."""

  # ListView
  list_display = (
    'name',
    'membership',
    'optouts',
    'created_at',
    'updated_at',
    'favorite',
    'is_synced',
  )

  @admin.display(description=_('Membership'))
  def membership(self, obj: ContactList) -> int:
    return obj.members.all().count()

  @admin.display(description=_('Opt Outs'))
  def optouts(self, obj: ContactList) -> int:
    return obj.members.exclude(opt_out_source='').count()

  # ChangeView
  form = ContactListForm
  fieldsets = (
    (None, {
      'fields': (
        ('name', 'favorite'),
        'description',
      ),
    }),
  )


class CustomFieldAdmin(RemoteModelAdmin[CustomField]):
  """Admin functionality for CTCT CustomFields."""

  # ListView
  list_display = (
    'label',
    'type',
    'created_at',
    'updated_at',
    'is_synced',
  )

  # ChangeView
  exclude = ('api_id', )


class ContactStreetAddressInline(
  admin.StackedInline[ContactStreetAddress, Contact]
):
  """Inline for adding ContactStreetAddresses to a Contact."""

  model = ContactStreetAddress
  exclude = ('api_id', )

  extra = 0
  max_num = Contact.API_MAX_STREET_ADDRESSES


class ContactPhoneNumberInline(
  admin.TabularInline[ContactPhoneNumber, Contact]
):
  """Inline for adding ContactPhoneNumbers to a Contact."""

  model = ContactPhoneNumber
  exclude = ('api_id', )

  extra = 0
  max_num = Contact.API_MAX_PHONE_NUMBERS


class ContactNoteInline(admin.TabularInline[ContactNote, Contact]):
  """Inline for adding ContactNotes to a Contact."""

  model = ContactNote
  fields = ('content', )

  extra = 0
  max_num = Contact.API_MAX_NOTES

  def has_change_permission(
    self,
    request: HttpRequest,
    obj: Optional[Contact] = None,  # type: ignore[override]
  ) -> bool:
    return False


class ContactCustomFieldInline(
  admin.TabularInline[ContactCustomField, Contact]
):

  model = ContactCustomField
  excldue = ('api_id', )

  extra = 0


class ContactAdmin(RemoteModelAdmin[Contact]):
  """Admin functionality for CTCT Contacts."""

  # ListView
  search_fields = (
    'email',
    'first_name',
    'last_name',
    'job_title',
    'company_name',
  )

  list_display = (
    'email',
    'first_name',
    'last_name',
    'job_title',
    'company_name',
    'updated_at',
    'opted_out',
    'is_synced',
  )
  list_filter = (
    'list_memberships',
  )
  empty_value_display = '(None)'

  @admin.display(
    boolean=True,
    description=_('Opted Out'),
    ordering='opt_out_date',
  )
  def opted_out(self, obj: Contact) -> bool:
    return bool(obj.opt_out_source)

  # ChangeView
  fieldsets = (
    (None, {
      'fields': (
        'email',
        'first_name',
        'last_name',
        'job_title',
        'company_name',
      ),
    }),
    ('CONTACT LISTS', {
      'fields': (
        'list_memberships',
        ('opt_out_source', 'opt_out_date', 'opt_out_reason'),
      ),
    }),
    ('TIMESTAMPS', {
      'fields': (
        'created_at',
        'updated_at',
      ),
    }),
  )
  filter_horizontal = ('list_memberships', )
  inlines = (
    ContactCustomFieldInline,
    ContactPhoneNumberInline,
    ContactStreetAddressInline,
    ContactNoteInline,
  )

  def get_readonly_fields(
    self,
    request: HttpRequest,
    obj: Optional[Contact] = None,
  ) -> list[str]:
    readonly_fields = list(Contact.API_READONLY_FIELDS)
    if obj and obj.opt_out_source and not request.user.is_superuser:
      readonly_fields.append('list_memberships')
    return readonly_fields

  def save_formset(
    self,
    request: HttpRequest,
    form: ModelForm[Contact],
    formset: ContactInlineFormSet,
    change: bool,
  ) -> None:
    """Set the current user as ContactNote author.

    Notes
    -----
    We don't need to worry about calling the API after .delete() since we use a
    PUT method, which overwrites all sub-resources.

    """

    instances = formset.save(commit=False)
    for obj in formset.deleted_objects:
      obj.delete()
    for instance in instances:
      if isinstance(instance, ContactNote) and instance.pk is None:
        instance.author = request.user
      instance.save()

    with mute_signals(signals.m2m_changed):
      # ManyToMany information is sent to CTCT in PUT call
      formset.save_m2m()


class ContactNoteAuthorFilter(admin.SimpleListFilter):
  """Only display Users that have authored a ContactNote."""

  title = _('Author')
  parameter_name = 'author'

  def lookups(
    self,
    request: HttpRequest,
    model_admin: admin.ModelAdmin[Model],
  ) -> Optional[Iterable[tuple[str, str]]]:
    authors = get_user_model().objects.exclude(notes__isnull=True)
    return [(str(obj.id), str(obj)) for obj in authors]

  def queryset(
    self,
    request: HttpRequest,
    queryset: QuerySet[ContactNote],
  ) -> QuerySet[ContactNote]:
    author_id = self.value()
    if author_id is not None and author_id.isdigit():
      queryset = queryset.filter(author_id=int(author_id))
    return queryset


class ContactNoteAdmin(RemoteSyncMixin, ViewModelAdmin):
  """Admin functionality for ContactNotes."""

  # ListView
  search_fields = (
    'content',
    'contact__email',
    'contact__first_name',
    'contact__last_name',
    'author__email',
    'author__first_name',
    'author__last_name',
  )

  list_display_links = None
  list_display = (
    'contact_link',
    'content',
    'author',
    'created_at',
    'is_synced',
  )
  list_filter = (
    'created_at',
    ContactNoteAuthorFilter,
  )

  @admin.display(
    description=_('Contact'),
    ordering='contact__email',
  )
  def contact_link(self, obj: ContactNote) -> str:
    url = reverse(
      'admin:django_ctct_contact_change',
      args=[obj.contact.pk],
    )
    html = format_html('<a href="{}">{}</a>', url, obj.contact)
    return html

  def has_delete_permission(
    self,
    request: HttpRequest,
    obj: Optional[Model] = None,
  ) -> bool:
    """Allow superusers to delete Notes."""
    return request.user.is_superuser


class CampaignActivityInlineForm(forms.ModelForm[CampaignActivity]):
  """Custom widget choices for ContactList admin."""

  html_content = forms.CharField(
    widget=forms.Textarea,
    label=_('HTML Content'),
  )

  class Meta:
    model = CampaignActivity
    fields = '__all__'


class CampaignActivityInline(
  admin.StackedInline[CampaignActivity, EmailCampaign]
):
  """Inline for adding CampaignActivity to a EmailCampaign."""

  model = CampaignActivity
  form = CampaignActivityInlineForm
  fields = (
    'role', 'current_status',
    'from_name', 'from_email', 'reply_to_email',
    'subject', 'preheader', 'html_content',
    'contact_lists',
  )

  filter_horizontal = (
    'contact_lists',
  )

  extra = 1
  max_num = 1

  def get_readonly_fields(
    self,
    request: HttpRequest,
    obj: Optional[CampaignActivity] = None,
  ) -> list[str]:
    readonly_fields = list(CampaignActivity.API_READONLY_FIELDS)
    if obj and obj.current_status == 'DONE':
      readonly_fields += list(CampaignActivity.API_EDITABLE_FIELDS)
    return readonly_fields


class EmailCampaignAdmin(RemoteModelAdmin[EmailCampaign]):
  """Admin functionality for CTCT EmailCampaigns.

  Notes
  -----
  CTCT does not provide an endpoint for bulk deleting EmailCampaigns, so we set
  `actions` to None.

  """

  # ListView
  actions = None
  search_fields = ('name', )
  list_display = (
    'name',
    'current_status',
    'scheduled_datetime',
    'created_at',
    'updated_at',
    'is_synced',
  )

  # ChangeView
  fieldsets = (
    (None, {
      'fields': (
        'name', 'current_status', 'scheduled_datetime', 'send_preview'
      ),
    }),
  )
  inlines = (CampaignActivityInline, )

  def get_readonly_fields(
    self,
    request: HttpRequest,
    obj: Optional[EmailCampaign] = None,
  ) -> list[str]:
    readonly_fields = list(EmailCampaign.API_READONLY_FIELDS)
    if obj and obj.current_status == 'DONE':
      readonly_fields.append('scheduled_datetime')
    return readonly_fields

  @catch_api_errors
  def save_remotely(
    self,
    request: HttpRequest,
    form: ModelForm[EmailCampaign],  # type: ignore[override]
    formsets: list[BaseFormSet[ModelForm[Model]]],
    change: bool,
  ) -> None:
    if self.remote_sync:

      campaign = form.instance
      activity = formsets[0][0].instance

      # Handle remote saving the EmailCampaign
      campaign_created = not change
      campaign_updated = change and ('name' in form.changed_data)
      if campaign_created or campaign_updated:
        # The only EmailCampaign field that can be updated is 'name'
        remote_save(
          sender=self.model,
          instance=campaign,
          created=campaign_created,
        )

      # Handle remote saving the primary_email CampaignActivity
      inline_changed = formsets[0][0].changed_data and not campaign_created
      schedule_changed = ('scheduled_datetime' in form.changed_data)
      preview_sent = ('send_preview' in form.changed_data) and campaign.send_preview  # noqa: E501
      recipients_changed = ('contact_lists' in formsets[0][0].changed_data)

      if (
        inline_changed or schedule_changed or preview_sent or recipients_changed  # noqa: E501
      ):
        # Refresh to get API id and remote save
        activity.refresh_from_db()
        remote_save(sender=CampaignActivity, instance=activity, created=False)

        # Inform the user
        self.ctct_message_user(request, form, formsets, change)

  def ctct_message_user(
    self,
    request: HttpRequest,
    form: ModelForm[EmailCampaign],
    formsets: list[BaseFormSet[ModelForm[Model]]],
    change: bool,
  ) -> None:
    """Inform the user of API actions."""

    campaign = form.instance

    if campaign.scheduled_datetime is not None:
      date = date_format(campaign.scheduled_datetime, settings.DATETIME_FORMAT)
      action = f"scheduled to be sent {date}"
    elif change and ('scheduled_datetime' in form.changed_data):
      action = "unscheduled remotely"
    elif change:
      action = "updated remotely"
    else:
      action = "created remotely"

    if campaign.send_preview:
      preview = " and a preview has been sent out"
    else:
      preview = ""

    message = format_html(
      _("The {name} “{obj}” has been {action}{preview}."),
      **{
        'name': campaign._meta.verbose_name,
        'obj': campaign,
        'action': action,
        'preview': preview,
      },
    )
    self.message_user(request, message)


class CampaignSummaryAdmin(ViewModelAdmin):
  """Admin functionality for CTCT EmailCampaign Summary Report."""

  # ListView
  search_fields = ('name', )
  list_display = (
    'campaign',
    'open_rate',
    'sends',
    'opens',
    'bounces',
    'clicks',
    'optouts',
    'abuse',
  )

  def get_queryset(self, request: HttpRequest) -> QuerySet[Model]:
    qs = super().get_queryset(request)
    qs = qs.annotate(
      open_rate=Case(
        When(sends=0, then=0.0),
        default=Cast(
          F('opens') * 1.0 / F('sends'),  # Avoid int division
          output_field=FloatField(),
        ),
      ),
    )
    return qs

  @admin.display(
    description=_('Open Rate'),
    ordering='open_rate',
  )
  def open_rate(self, obj: EmailCampaign) -> str:
    assert hasattr(obj, 'open_rate')
    return f'{obj.open_rate:0.0%}'

  # ChangeView
  fieldsets = (
    (None, {
      'fields': (
        'campaign',
      ),
    }),
    ('ANALYTICS', {
      'fields': (
        'sends', 'opens', 'clicks', 'forwards',
        'optouts', 'abuse', 'bounces', 'not_opened',
      ),
    }),
  )


if getattr(settings, 'CTCT_USE_ADMIN', False):
  admin.site.register(ContactList, ContactListAdmin)
  admin.site.register(CustomField, CustomFieldAdmin)
  admin.site.register(Contact, ContactAdmin)
  admin.site.register(ContactNote, ContactNoteAdmin)
  admin.site.register(EmailCampaign, EmailCampaignAdmin)
  admin.site.register(CampaignSummary, CampaignSummaryAdmin)
