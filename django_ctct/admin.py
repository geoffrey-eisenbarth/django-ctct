from typing import List, Tuple, Optional

from django import forms
from django.conf import settings
from django.contrib import admin
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

  def save_model(self, request: HttpRequest, obj, form, change):
    super().save_model(request, obj, form, change)
    if self.remote_sync:
      remote_save(sender=self.model, instance=obj, created=not change)

  def delete_model(self, request: HttpRequest, obj):
    super().delete_model(request, obj)
    if self.remote_sync:
      remote_delete(sender=self.model, instance=obj)

  def delete_queryset(self, request: HttpRequest, queryset):
    if self.remote_sync:
      message = _("CTCT bulk activities not implemented yet.")
      raise NotImplementedError(message)
    else:
      super().delete_queryset(request, queryset)

  # TODO: How to get `sender` and `action`?
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
    'api_id',
  )

  # ChangeView
  readonly_fields = ('api_id', )


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
    'company_name',
    'job_title',
  )

  list_display = (
    'email',
    'name',
    'job',
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
    elif obj.opted_out:
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
        'honorific',
        'first_name',
        'last_name',
        'suffix',
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
    if obj and obj.opted_out and not request.user.is_superuser:
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

  ACTIONS = (
    ('NONE', 'Select Action'),
    ('SCHEDULE', 'Schedule'),
    ('UNSCHEDULE', 'Unschedule'),
  )

  html_content = forms.CharField(
    widget=forms.Textarea,
    label=_('HTML Content'),
  )
  action = forms.ChoiceField(
    choices=ACTIONS,
    label=_('Action'),
  )

  class Meta:
    model = CampaignActivity
    fields = '__all__'

# TODO: Implement actions
#    if self.action == 'CREATE':
#      activity.ctct_send_preview()
#      self.current_status = 'DRAFT'
#    elif self.action == 'SCHEDULE':
#      activity.ctct_send_preview()
#      activity.ctct_schedule()
#      self.current_status = 'SCHEDULED'
#    elif self.campaign.action == 'UNSCHEDULE':
#      activity.ctct_unschedule()
#      self.current_status = 'DRAFT'
#    self.action = 'NONE'


class CampaignActivityInline(admin.StackedInline):
  """Inline for adding CampaignActivity to a EmailCampaign."""

  model = CampaignActivity
  form = CampaignActivityInlineForm
  fields = (
    'role', 'current_status', 'format_type',
    'from_name', 'from_email', 'reply_to_email',
    'subject', 'preheader', 'html_content',
    'action',
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
          'fields': ('name', 'current_status', 'scheduled_datetime'),
        }),
      )

    return fieldsets

  def get_readonly_fields(self, request: HttpRequest, obj=None):
    readonly_fields = EmailCampaign.remote.API_READONLY_FIELDS
    if obj and obj.current_status == 'DONE':
      readonly_fields += ('scheduled_datetime', )
    return readonly_fields

  def save_model(
    self,
    request: HttpRequest,
    obj: EmailCampaign,
    form: ModelForm,
    change: bool,
  ) -> None:
    super().save_model(request, obj, form, change)
    message = {
      'CREATE': _(
        'Campaign has been created and a preview has been sent for approval. '
        'Once the campaign has been approved, you must schedule it.'
      ),
      'UPDATE': _(
        'The campaign has been updated and a preview has been sent out for approval.'  # noqa 501
      ),
      'SCHEDULE': _(
        'The campaign has been scheduled and a preview has been sent out for approval.'  # noqa 501
      ),
      'UNSCHEDULE': _(
        'The campaign has been unscheduled.'
      ),
    }[obj.action]
    self.message_user(request, message)


if getattr(settings, 'CTCT_USE_ADMIN', False):
  admin.site.register(Token, TokenAdmin)
  admin.site.register(ContactList, ContactListAdmin)
  admin.site.register(CustomField, CustomFieldAdmin)
  admin.site.register(Contact, ContactAdmin)
  admin.site.register(ContactNote, ContactNoteAdmin)
  admin.site.register(EmailCampaign, EmailCampaignAdmin)
