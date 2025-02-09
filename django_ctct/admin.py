from typing import List, Tuple, Optional

from django import forms
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
  Contact, ContactStreetAddress, ContactPhoneNumber, ContactNote,
  EmailCampaign, CampaignActivity
)


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
    if obj:
      readonly_fields = [
        field.name
        for field in obj._meta.fields
        if field.name != 'active'
      ]
    else:
      readonly_fields = []
    return readonly_fields

  def has_delete_permission(self, request, obj=None):
    """Prevent deletion in the Django admin."""
    return False


@admin.register(Token)
class TokenAdmin(ViewModelAdmin):
  """Admin functionality for CTCT Tokens."""

  # ListView
  list_display = (
    'inserted',
    'copy_access_code',
    'copy_refresh_code',
    'type',
  )

  def copy_access_code(self, obj: Token) -> str:
    html = format_html(
      '<button class="button" onclick="{function}">{copy_icon}</button>',
      function=f"navigator.clipboard.writeText('{obj.access_code}')",
      copy_icon=mark_safe('&#128203;'),
    )
    return html
  copy_access_code.short_description = _('Access Code')

  def copy_refresh_code(self, obj: Token) -> str:
    html = format_html(
      '<button class="button" onclick="{function}">{copy_icon}</button>',
      function=f"navigator.clipboard.writeText('{obj.refresh_code}')",
      copy_icon=mark_safe('&#128203;'),
    )
    return html
  copy_refresh_code.short_description = _('Refresh Code')

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


@admin.register(ContactList)
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
    return bool(obj.id)
  synced.boolean = True
  synced.short_description = _('Synced')

  def membership(self, obj: ContactList) -> int:
    return obj.contacts.count()
  membership.short_description = _('Membership')

  def optouts(self, obj: ContactList) -> int:
    return obj.contacts.filter(optout=True).count()
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


class ContactStatusFilter(admin.SimpleListFilter):
  """Simple filter for CTCT Status."""

  STATUSES = (
    ('sync', _('Synced')),
    ('not_synced', _('Not Synced')),
    ('optout', _('Opted Out')),
  )

  title = 'CTCT Status'
  parameter_name = 'ctct'

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
    if self.value() == 'sync':
      queryset = queryset.filter(id__isnull=False)
    elif self.value() == 'not_synced':
      queryset = queryset.filter(id__isnull=True)
    elif self.value() == 'optout':
      queryset = queryset.filter(optout=True)

    return queryset


class ContactStreetAddressInline(admin.TabularInline):
  """Inline for adding ContactStreetAddresses to a Contact."""

  model = ContactStreetAddress
  extra = 0
  max_num = ContactStreetAddress.API_MAX_NUM


class ContactPhoneNumberInline(admin.TabularInline):
  """Inline for adding ContactPhoneNumbers to a Contact."""

  model = ContactPhoneNumber
  extra = 0
  max_num = ContactPhoneNumber.API_MAX_NUM


class ContactNoteInline(admin.TabularInline):
  """Inline for adding ContactNotes to a Contact."""

  model = ContactNote
  extra = 0
  max_num = ContactNote.API_MAX_NUM

  readonly_fields = ['author', 'timestamp']

  def has_change_permission(
    self,
    request: HttpRequest,
    obj: Optional[ContactNote] = None,
  ) -> bool:
    return False


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
  """Admin functionality for CTCT Contacts."""

  # ListView
  search_fields = (
    'email',
    'first_name',
    'last_name',
    'company_name',
    'job_title',
    'city',
  )

  list_display = (
    'email',
    'name',
    'job',
    'updated_at',
    'ctct',
  )
  list_filter = (
    ContactStatusFilter,
    'list_memberships',
  )
  empty_value_display = '(None)'

  def ctct(self, obj: Contact) -> str:
    if not obj.id:
      text = 'Not Synced'
      color = 'bad'
    elif obj.optout:
      text = 'Opted Out'
      color = 'warn'
    else:
      text = 'Synced'
      color = 'ok'

    html = (
      f'<span class="{color} badge">'
      f'{text}'
      '</span>'
    )
    return mark_safe(html)
  ctct.short_description = 'CTCT'

  # ChangeView
  fieldsets = (
    (None, {
      'fields': (
        'email',
        ('first_name', 'last_name'),
        ('honorific', 'suffix'),
        ('job_title', 'company_name'),
      ),
    }),
    ('CONTACT LISTS', {
      'fields': (
        'list_memberships',
        ('optout', 'optout_at'),
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
    ContactStreetAddressInline,
    ContactPhoneNumberInline,
    ContactNoteInline,
  )

  def get_readonly_fields(
    self,
    request: HttpRequest,
    obj: Optional[Contact] = None,
  ) -> List[str]:
    readonly_fields = [
      'created_at',
      'updated_at',
      'optout',
      'optout_at',
    ]
    if getattr(obj, 'optout', False) and not request.user.is_superuser:
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
        obj.delete()
      for instance in instances:
        if getattr(instance, 'author', None) is None:
          instance.author = request.user
        instance.save()
      formset.save_m2m()
    else:
      return super().save_formset(request, form, formset, change)


@admin.register(ContactNote)
class ContactNoteAdmin(admin.ModelAdmin):
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
    'author',
    'created_at',
  )
  list_filter = (
    'created_at',
    'author',
  )

  # ChangeView
  fieldsets = (
    (None, {
      'fields': (
        'contact',
        'content',
      ),
    }),
    ('INTERNAL', {
      'fields': (
        'author',
        'timestamp',
      ),
    }),
  )
  readonly_fields = (
    'contact',
    'author',
    'created_at',
  )

  def has_change_permission(
    self,
    request: HttpRequest,
    obj: Optional[ContactNote] = None,
  ) -> bool:
    return False

  def has_add_permission(
    self,
    request: HttpRequest,
    obj: Optional[ContactNote] = None,
  ) -> bool:
    return False


class CampaignActivityInline(admin.StackedInline):
  """Inline for adding CampaignActivity to a EmailCampaign."""

  model = CampaignActivity
  extra = 1
  max_num = 1

  def formfield_for_dbfield(
    self,
    db_field: ModelField,
    request: HttpRequest,
  ) -> FormField:
    formfield = {
      'html_content': forms.Textarea,
    }.get(db_field.name, super().formfield_for_dbfield(db_field, request))
    return formfield


@admin.register(EmailCampaign)
class EmailCampaignAdmin(ViewModelAdmin):
  """Admin functionality for CTCT EmailCampaigns."""

  # ListView
  search_fields = ('name', )
  list_display = (
    'name',
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
      s = 'N/A'
    return s
  open_rate.admin_order_field = 'open_rate'
  open_rate.short_description = _('Open Rate')

  # ChangeView
  inlines = (CampaignActivityInline, )

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
        'out for approval.'
      ),
      'SCHEDULE': _(
        'The campaign has been scheduled and a preview has been sent out for approval.'  # noqa 501
      ),
      'UNSCHEDULE': _(
        'The campaign has been unscheduled.'
      ),
    }[obj.action]
    self.message_user(request, message)
