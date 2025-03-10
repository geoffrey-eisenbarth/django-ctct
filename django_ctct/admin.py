import functools
from typing import List, Tuple, Optional

from requests.exceptions import HTTPError

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.db.models.query import QuerySet
from django.forms import ModelForm
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from django_ctct.models import (
  Token, ContactList,
  Contact, CustomField, ContactStreetAddress, ContactPhoneNumber, ContactNote,
  EmailCampaign, CampaignActivity
)
from django_ctct.signals import remote_save, remote_delete


def catch_api_errors(func):
  """Decorator to catch HTTP errors from CTCT API."""

  @functools.wraps(func)
  def wrapper(self, request, *args, **kwargs):
    try:
      return func(self, request, *args, **kwargs)
    except HTTPError as e:
      message = _(
        f"API Error: {e}"
      )
      self.message_user(request, message, level=messages.ERROR)

  return wrapper

class ViewModelAdmin(admin.ModelAdmin):
  """Remove CRUD permissions."""

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


class TokenAdmin(ViewModelAdmin):
  """Admin functionality for CTCT Tokens."""

  # ListView
  list_display_links = None
  list_display = (
    'scope',
    'created_at',
    'expires_at',
    'copy_access_token',
    'copy_refresh_token',
  )

  def copy_access_token(self, obj: Token) -> str:
    html = format_html(
      '<button class="button" onclick="{function}">{copy_icon}</button>',
      function=f"navigator.clipboard.writeText('{obj.access_token}')",
      copy_icon=mark_safe('&#128203;'),
    )
    return html
  copy_access_token.short_description = _('Access Token')

  def copy_refresh_token(self, obj: Token) -> str:
    html = format_html(
      '<button class="button" onclick="{function}">{copy_icon}</button>',
      function=f"navigator.clipboard.writeText('{obj.refresh_token}')",
      copy_icon=mark_safe('&#128203;'),
    )
    return html
  copy_refresh_token.short_description = _('Refresh Token')


class RemoteModelAdmin(admin.ModelAdmin):
  """Facilitate remote saving and deleting."""

  exclude = ('exists_remotely', )

  @property
  def remote_sync(self) -> bool:
    sync_admin = getattr(settings, 'CTCT_SYNC_ADMIN', False)
    sync_signals = getattr(settings, 'CTCT_SYNC_SIGNALS', False)
    return sync_admin and not sync_signals

  @catch_api_errors
  def save_model(self, request: HttpRequest, obj, form, change):
    super().save_model(request, obj, form, change)
    if self.remote_sync and not self.inlines:
      # If the primary object has related objects, we defer until `save_formset()`
      remote_save(sender=self.model, instance=obj, created=not change)

  @catch_api_errors
  def save_formset(
    self,
    request: HttpRequest,
    form: ModelForm,
    formset: BaseInlineFormSet,
    change: bool,
  ) -> None:
    super().save_formset(request, form, formset, change)
    breakpoint()
    if self.remote_sync:
      # Remote save the primary object after related objects have been saved
      # NOTE: The `change` arg refers to the parent object, not the formset
      remote_save(sender=self.model, instance=form.instance, created=not change)

  @catch_api_errors
  def delete_model(self, request: HttpRequest, obj):
    super().delete_model(request, obj)
    if self.remote_sync:
      remote_delete(sender=self.model, instance=obj)

  @catch_api_errors
  def delete_queryset(self, request: HttpRequest, queryset):
    if self.remote_sync:
      message = _("CTCT bulk activities not implemented yet.")
      raise NotImplementedError(message)
    else:
      super().delete_queryset(request, queryset)

  # TODO: How to get `sender` and `action`?
  # NOTE: save_related is only used for ManyToMany
  @catch_api_errors
  def save_related(self, request: HttpRequest, obj, form, change):
    super().save_related(request, obj, form, change)
    if self.remote_sync:
      pass
      # remote_update_m2m(sender=sender, instace=obj, action=action)


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
    'exists_remotely',
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
    'exists_remotely',
  )

  # ChangeView
  exclude = ('api_id', 'exists_remotely', )


class ContactStatusFilter(admin.SimpleListFilter):
  """Simple filter for CTCT Status."""

  STATUSES = (
    ('synced', _('Synced')),
    ('not_synced', _('Not Synced')),
    ('opted_out', _('Opted Out')),
  )

  title = 'Status'
  parameter_name = 'status'

  def lookups(
    self,
    request: HttpRequest,
    model_admin: admin.ModelAdmin,
  ) -> List[Tuple]:
    return self.STATUSES

  def queryset(
    self,
    request: HttpRequest,
    queryset: QuerySet,
  ) -> QuerySet[Contact]:
    if self.value() == 'synced':
      queryset = queryset.filter(exists_remotely=True)
    elif self.value() == 'not_synced':
      queryset = queryset.filter(exists_remotely=False)
    elif self.value() == 'opted_out':
      queryset = queryset.exclude(opt_out_source='')

    return queryset


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
  exclude = ('api_id', )

  extra = 1
  max_num = Contact.remote.API_MAX_NOTES

  readonly_fields = ('author', 'created_at')

  def has_change_permission(
    self,
    request: HttpRequest,
    obj: Optional[ContactNote] = None,
  ) -> bool:
    return False


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
    'status',
  )
  list_filter = (
    ContactStatusFilter,
    'list_memberships',
  )
  empty_value_display = '(None)'

  def status(self, obj: Contact) -> str:
    if not obj.exists_remotely:
      text, color = 'Not Synced', 'bad'
    elif obj.opt_out_source:
      text, color = 'Opted Out', 'warn'
    else:
      text, color = 'Synced', 'ok'

    html = (
      f'<span class="{color} badge">'
      f'{text}'
      '</span>'
    )
    return mark_safe(html)
  status.short_description = _('Status')

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
    if formset.model == ContactNote:
      instances = formset.save(commit=False)
      for obj in formset.deleted_objects:
        obj.delete()      # TODO: Hit API?
      for instance in instances:
        if getattr(instance, 'author', None) is None:
          instance.author = request.user
        instance.save()   # TODO: Hit API?
      formset.save_m2m()  # TODO: Hit API?
    else:
      return super().save_formset(request, form, formset, change)


class ContactNoteAdmin(ViewModelAdmin):
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
    'contact',
    'content',
    'created_at',
    'author',
  )
  list_filter = (
    'created_at',
    'author',
  )

  def has_delete_permission(self, request: HttpRequest, obj=None):
    """Allow superusers to delete Notes."""
    return request.user.is_superuser


# TODO: If action == 'SCEHDULE', validate EmailCampaign has scheduled_datetime
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
  """Admin functionality for CTCT EmailCampaigns."""

  # ListView
  search_fields = ('name', )
  list_display = (
    'name',
    'updated_at',
    'current_status',
    'scheduled_datetime',
    'open_rate',
    'sends',
    'bounces',
    'clicks',
    'optouts',
    'abuse',
  )

  def open_rate(self, obj: EmailCampaign) -> str:
    if obj.current_status == 'DONE':
      r = (obj.opens / obj.sends) if obj.sends else 0
      s = f'{r:0.2%}'
    else:
      s = '-'
    return s
  open_rate.admin_order_field = 'open_rate'
  open_rate.short_description = _('Open Rate')

  # ChangeView
  inlines = (CampaignActivityInline, )

  def get_fieldsets(self, request: HttpRequest, obj=None):
    if obj and (obj.current_status == 'DONE'):
      fieldsets = (
        (None, {
          'fields': ('name', 'current_status', 'scheduled_datetime'),
        }),
        ('ANALYTICS', {
          'fields': (
            'sends', 'opens', 'clicks', 'forwards',
            'optouts', 'abuse', 'bounces', 'not_opened',
          ),
        }),
      )
    else:
      fieldsets = (
        (None, {
          'fields': ('name', 'current_status', 'scheduled_datetime', 'send_preview'),
        }),
      )

    return fieldsets

  def get_readonly_fields(self, request: HttpRequest, obj=None):
    readonly_fields = EmailCampaign.remote.API_READONLY_FIELDS
    if obj and obj.current_status == 'DONE':
      readonly_fields += ('scheduled_datetime', )
    return readonly_fields

  # TODO: Maybe find a way to only trigger remote save if updating name?
  @catch_api_errors
  def save_model(
    self,
    request: HttpRequest,
    obj: EmailCampaign,
    form: ModelForm,
    change: bool,
  ) -> None:
    return super().save_model(request, obj, form, change)

  # TODO: super() isn't saving CampaignActivities when they update
  def save_formset(
    self,
    request: HttpRequest,
    form: ModelForm,
    formset: BaseInlineFormSet,
    change: bool,
  ) -> None:
    response = super().save_formset(request, form, formset, change)

    # Inform the user
    if (form.instance.scheduled_datetime is not None):
      message = _(
        f"The campaign is scheduled to be sent {form.instance.scheduled_datetime}"  # noqa 501
      )
    elif change and ('scheduled_datetime' in form.changed_data):
      message = _(
        "The campaign has been unscheduled"
      )
    else:
      message = _(
        "The campaign has been created remotely"
      )

    if form.instance.send_preview:
      message += _(
        " and a preview has been sent out."
      )
    else:
      message += "."
    self.message_user(request, message)

    return response


if getattr(settings, 'CTCT_USE_ADMIN', False):
  admin.site.register(Token, TokenAdmin)
  admin.site.register(ContactList, ContactListAdmin)
  admin.site.register(CustomField, CustomFieldAdmin)
  admin.site.register(Contact, ContactAdmin)
  admin.site.register(ContactNote, ContactNoteAdmin)
  admin.site.register(EmailCampaign, EmailCampaignAdmin)
