from typing import List, Tuple, Optional

from django import forms
from django.conf import settings
from django.contrib import admin
from django.db.models import Field as ModelField
from django.db.models.query import QuerySet
from django.forms import ModelForm
from django.forms import Field as FormField
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from django_ctct.models import (
  Token, ContactList,
  Contact, CustomField, ContactStreetAddress, ContactPhoneNumber, ContactNote,
  EmailCampaign, CampaignActivity
)


# TODO:
# def ctct_update_lists_task(contact: 'Contact') -> None:
#   """Update ContactLists on CTCT, or delete Contact if no ContactLists.
#
#   Notes
#   -----
#   Due to the way Django admin saves related models, I haven't been able
#   to determine a good way to address this other than just delaying this
#   method for a few minutes.
#
#   The primary issue is that we want to make sure that a Contact.ctct_save()
#   call isn't made after this call, since that will revive the Contact in
#   the event that they had been deleted from CTCT servers due to no longer
#   belonging to any ContactLists (CTCT requires that Contacts must belong to
#   at least one ContactList).
#   """
#   if contact.api_id is not None:
#     sleep(60 * 1)  # 1 minute
#     contact.ctct_update_lists()


class ViewModelAdmin(admin.ModelAdmin):
  """Remove CRUD permissions."""

  def has_add_permission(self, request, obj=None):
    """Prevent creation in the Django admin."""
    return False

  def has_change_permission(self, request, obj=None):
    """Prevent updates in the Django admin."""
    return False

  def get_readonly_fields(self, request, obj=None):
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

  def has_delete_permission(self, request, obj=None):
    """Prevent deletion in the Django admin."""
    return False


class TokenAdmin(ViewModelAdmin):
  """Admin functionality for CTCT Tokens."""

  # ListView
  list_display = (
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

  # ChangeView
  def changeform_view(
    self,
    request: HttpRequest,
    object_id: Optional[int] = None,
    form_url: str = '',
    extra_context: Optional[dict] = None
  ) -> TemplateResponse:
    """Remove extra buttons."""
    extra_context = extra_context or {}
    extra_context.update({
      'show_save_and_continue': False,
      'show_save_and_add_another': False,
    })
    return super().changeform_view(request, object_id, form_url, extra_context)


class ContactListForm(forms.ModelForm):
  """Custom widget choices for ContactList admin."""

  class Meta:
    model = ContactList
    widgets = {
      'description': forms.Textarea,
    }
    fields = '__all__'


class ContactListAdmin(admin.ModelAdmin):
  """Admin functionality for CTCT ContactLists."""

  # ListView
  list_display = (
    'name',
    'membership',
    'optouts',
    'created_at',
    'updated_at',
    'favorite',
    'synced',
  )

  def synced(self, obj: ContactList) -> bool:
    return bool(obj.api_id)
  synced.boolean = True
  synced.short_description = _('Synced')

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


class CustomFieldAdmin(admin.ModelAdmin):
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
      queryset = queryset.filter(api_id__isnull=False)
    elif self.value() == 'not_synced':
      queryset = queryset.filter(api_id__isnull=True)
    elif self.value() == 'opted_out':
      queryset = queryset.exclude(opt_out_source='')

    return queryset


class ContactStreetAddressInline(admin.TabularInline):
  """Inline for adding ContactStreetAddresses to a Contact."""

  model = ContactStreetAddress
  exclude = ('api_id', )

  extra = 0
  max_num = Contact.API_MAX_STREET_ADDRESSES


class ContactPhoneNumberInline(admin.TabularInline):
  """Inline for adding ContactPhoneNumbers to a Contact."""

  model = ContactPhoneNumber
  exclude = ('api_id', )

  extra = 0
  max_num = Contact.API_MAX_PHONE_NUMBERS


class ContactNoteInline(admin.TabularInline):
  """Inline for adding ContactNotes to a Contact."""

  model = ContactNote
  exclude = ('api_id', )

  extra = 0
  max_num = Contact.API_MAX_NOTES

  readonly_fields = ('author', 'created_at')

  def has_change_permission(
    self,
    request: HttpRequest,
    obj: Optional[ContactNote] = None,
  ) -> bool:
    return False


class ContactAdmin(admin.ModelAdmin):
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
    'list_memberships',  # TODO: this doesn't work?
  )
  empty_value_display = '(None)'

  def status(self, obj: Contact) -> str:
    if not obj.api_id:
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
        # 'list_memberships',  # TODO: This breaks if using Custom ThroughModel?
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
  # TODO: This breaks if using Custom ThroughModel?
  #filter_horizontal = ('list_memberships', )
  inlines = (
    ContactStreetAddressInline,
    ContactPhoneNumberInline,
    ContactNoteInline,
  )

  def get_readonly_fields(
    self,
    request: HttpRequest,
    obj: Optional[Contact] = None,
  ) -> List[str]:
    readonly_fields = Contact.API_READONLY_FIELDS
    if obj.opted_out and not request.user.is_superuser:
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

  def has_delete_permission(self, request, obj=None):
    """Allow superusers to delete Notes."""
    return request.user.is_superuser


class CampaignActivityInlineForm(forms.ModelForm):
  """Custom widget choices for ContactList admin."""

  ACTIONS = (
    ('NONE', 'Select Action'),
    ('CREATE', 'Create Draft'),
    ('SCHEDULE', 'Schedule'),
    ('UNSCHEDULE', 'Unschedule'),
  )
  # TODO: Is this needed?
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

  action = forms.ChoiceField(
    choices=ACTIONS,
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
  exclude = ('api_id', )

  extra = 1
  max_num = 1

  class Meta:
    widgets = {
      'html_content': forms.Textarea,  # TODO: Allow user to set RichTextEditor?
    }

  def get_readonly_fields(self, request, obj=None):
    readonly_fields = CampaignActivity.API_READONLY_FIELDS
    if obj and obj.current_status == 'DONE':
      readonly_fields += CampaignActivity.API_EDITABLE_FIELDS
    return readonly_fields

  # TODO?
  #def formfield_for_dbfield(
  #  self,
  #  db_field: ModelField,
  #  request: HttpRequest,
  #) -> FormField:
  #  formfield = {
  #    'html_content': forms.Textarea,
  #  }.get(db_field.name, super().formfield_for_dbfield(db_field, request))
  #  return formfield


class EmailCampaignAdmin(admin.ModelAdmin):
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

  def get_fieldsets(self, request, obj=None):
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

  def get_readonly_fields(self, request, obj=None):
    readonly_fields = EmailCampaign.API_READONLY_FIELDS
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
