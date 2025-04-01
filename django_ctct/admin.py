import functools
from typing import List, Optional

from requests.exceptions import HTTPError

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.db.models import signals
from django.db.models import Model, QuerySet, When, Case, F, FloatField
from django.db.models.functions import Cast
from django.forms import ModelForm
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from django_ctct.models import (
  CTCTRemoteModel, ContactList, CustomField,
  Contact,
  ContactCustomField, ContactStreetAddress, ContactPhoneNumber, ContactNote,
  EmailCampaign, CampaignActivity, CampaignSummary,
)
from django_ctct.signals import remote_save, remote_delete
from django_ctct.vendor import mute_signals


def catch_api_errors(func):
  """Decorator to catch HTTP errors from CTCT API."""

  @functools.wraps(func)
  def wrapper(self, request, *args, **kwargs):
    try:
      return func(self, request, *args, **kwargs)
    except HTTPError as e:
      if getattr(settings, 'CTCT_RAISE_FOR_API', False):
        raise e
      else:
        message = format_html(_(f"ConstantContact: {e}"))
        self.message_user(request, message, level=messages.ERROR)

  return wrapper


class RemoteSyncMixin:
  def is_synced(self, obj: CTCTRemoteModel) -> bool:
    return (obj.api_id is not None)
  is_synced.boolean = True
  is_synced.admin_order_field = 'api_id'
  is_synced.short_description = _('Synced')


class ViewModelAdmin(admin.ModelAdmin):
  """Remove CRUD permissions."""

  actions = None

  def has_add_permission(self, request: HttpRequest, obj=None):
    """Prevent creation in the Django admin."""
    return False

  def has_change_permission(self, request: HttpRequest, obj=None):
    """Prevent updates in the Django admin."""
    return False

  def get_readonly_fields(self, request: HttpRequest, obj=None):
    """Prevent updates in the Django admin."""
    if obj is not None:
      readonly_fields = (
        field.name
        for field in obj._meta.fields
        if field.name != 'active'
      )
    else:
      readonly_fields = tuple()
    return readonly_fields

  def has_delete_permission(self, request: HttpRequest, obj=None):
    """Prevent deletion in the Django admin."""
    return False


class RemoteModelAdmin(RemoteSyncMixin, admin.ModelAdmin):
  """Facilitate remote saving and deleting."""

  # ChangeView
  @property
  def remote_sync(self) -> bool:
    sync_admin = getattr(settings, 'CTCT_SYNC_ADMIN', False)
    sync_signals = getattr(settings, 'CTCT_SYNC_SIGNALS', False)
    return sync_admin and not sync_signals

  @catch_api_errors
  def delete_model(self, request: HttpRequest, obj: Model):
    obj.delete()
    if self.remote_sync:
      remote_delete(sender=self.model, instance=obj)

  @catch_api_errors
  def delete_queryset(self, request: HttpRequest, queryset: QuerySet):
    if self.remote_sync:
      queryset.model.remote.bulk_delete(queryset)
    with mute_signals(signals.pre_delete):
      queryset.delete()

  @catch_api_errors
  def save_related(self, request: HttpRequest, form, formsets, change):
    """Default implementation with an added line for saving remotely.

    Notes
    -----
    This gets called even if related fields don't exist, so we use it as a hook
    for saving objects remotely.

    """
    form.save_m2m()
    for formset in formsets:
      self.save_formset(request, form, formset, change=change)
    self.save_remotely(request, form, formsets, change)

  @catch_api_errors
  def save_remotely(self, request, form, formsets, change):
    if self.remote_sync:
      # Remote save the primary object after related objects have been saved
      remote_save(
        sender=self.model,
        instance=form.instance,
        created=not change,
      )


class ContactListForm(forms.ModelForm):
  """Custom widget choices for ContactList admin."""

  class Meta:
    model = ContactList
    widgets = {
      'description': forms.Textarea,
    }
    fields = '__all__'


class ContactListAdmin(RemoteModelAdmin):
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

  def membership(self, obj: ContactList) -> int:
    return obj.members.all().count()
  membership.short_description = _('Membership')

  def optouts(self, obj: ContactList) -> int:
    return obj.members.exclude(opt_out_source='').count()
  optouts.short_description = _('Opt Outs')

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


class CustomFieldAdmin(RemoteModelAdmin):
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


class ContactStreetAddressInline(admin.StackedInline):
  """Inline for adding ContactStreetAddresses to a Contact."""

  model = ContactStreetAddress
  exclude = ('api_id', )

  extra = 0
  max_num = Contact.remote.API_MAX_STREET_ADDRESSES


class ContactPhoneNumberInline(admin.TabularInline):
  """Inline for adding ContactPhoneNumbers to a Contact."""

  model = ContactPhoneNumber
  exclude = ('api_id', )

  extra = 0
  max_num = Contact.remote.API_MAX_PHONE_NUMBERS


class ContactNoteInline(admin.TabularInline):
  """Inline for adding ContactNotes to a Contact."""

  model = ContactNote
  fields = ('content', )

  extra = 0
  max_num = Contact.remote.API_MAX_NOTES

  def has_change_permission(
    self,
    request: HttpRequest,
    obj: Optional[ContactNote] = None,
  ) -> bool:
    return False


class ContactCustomFieldInline(admin.TabularInline):

  model = ContactCustomField
  excldue = ('api_id', )

  extra = 0


class ContactAdmin(RemoteModelAdmin):
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

  def opted_out(self, obj: Contact) -> bool:
    return bool(obj.opt_out_source)
  opted_out.boolean = True
  opted_out.admin_order_field = 'opt_out_date'
  opted_out.short_description = _('Opted Out')

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
  ) -> List[str]:
    readonly_fields = Contact.remote.API_READONLY_FIELDS
    if obj and obj.opt_out_source and not request.user.is_superuser:
      readonly_fields.append('list_memberships')
    return readonly_fields

  def save_formset(
    self,
    request: HttpRequest,
    form: ModelForm,
    formset: BaseInlineFormSet,
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
    formset.save_m2m()


class ContactNoteAuthorFilter(admin.SimpleListFilter):
  """Only display Users that have authored a ContactNote."""

  title = _('Author')
  parameter_name = 'author'

  def lookups(self, request, model_admin):
    authors = get_user_model().objects.exclude(notes__isnull=True)
    return [(obj.id, str(obj)) for obj in authors]

  def queryset(self, request, queryset):
    if self.value():
      queryset = queryset.filter(author_id=self.value())
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

  def contact_link(self, obj: ContactNote) -> str:
    url = reverse(
      'admin:django_ctct_contact_change',
      args=[obj.contact.pk],
    )
    html = format_html('<a href="{}">{}</a>', url, obj.contact)
    return html
  contact_link.short_description = _('Contact')
  contact_link.admin_order_field = 'contact__email'

  def has_delete_permission(self, request: HttpRequest, obj=None):
    """Allow superusers to delete Notes."""
    return request.user.is_superuser


class CampaignActivityInlineForm(forms.ModelForm):
  """Custom widget choices for ContactList admin."""

  html_content = forms.CharField(
    widget=forms.Textarea,
    label=_('HTML Content'),
  )

  class Meta:
    model = CampaignActivity
    fields = '__all__'


class CampaignActivityInline(admin.StackedInline):
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

  def get_readonly_fields(self, request: HttpRequest, obj=None):
    readonly_fields = CampaignActivity.remote.API_READONLY_FIELDS
    if obj and obj.current_status == 'DONE':
      readonly_fields += CampaignActivity.remote.API_EDITABLE_FIELDS
    return readonly_fields


class EmailCampaignAdmin(RemoteModelAdmin):
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

  def get_readonly_fields(self, request: HttpRequest, obj=None):
    readonly_fields = EmailCampaign.remote.API_READONLY_FIELDS
    if obj and obj.current_status == 'DONE':
      readonly_fields += ('scheduled_datetime', )
    return readonly_fields

  @catch_api_errors
  def save_remotely(self, request, form, formsets, change) -> None:
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

  def ctct_message_user(self, request, form, formsets, change) -> None:
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

  def get_queryset(self, request):
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

  def open_rate(self, obj: EmailCampaign) -> str:
    return f'{obj.open_rate:0.0%}'
  open_rate.admin_order_field = 'open_rate'
  open_rate.short_description = _('Open Rate')

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
