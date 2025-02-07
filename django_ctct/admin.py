from typing import List, Tuple, Optional

from django import forms
from django.contrib import admin
from django.db.models.query import QuerySet
from django.forms import ModelForm
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from django_ctct.models import (
  Token, Contact, ContactList, EmailCampaign,
  StreetAddress, PhoneNumber, Note,
)

from accounts.core.admin import ViewModelAdmin
from accounts.users.forms import ProfileForm


@admin.register(Token)
class TokenAdmin(ViewModelAdmin):
  """Admin functionality for CTCT Tokens."""

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

  list_display = (
    'name',
    'membership',
    'optouts',
    'created_at',
    'updated_at',
    'favorite',
    'synced',
  )

  form = ContactListForm
  fieldsets = (
    (None, {
      'fields': (
        ('name', 'favorite'),
        'description',
      ),
    }),
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


class StreetAddressInline(admin.TabularInline):
  """Inline for adding StreetAddresses to a Contact."""

  model = StreetAddress
  extra = 0
  max_num = StreetAddress.MAX_NUM


class PhoneNumberInline(admin.TabularInline):
  """Inline for adding PhoneNumbers to a Contact."""

  model = PhoneNumber
  extra = 0
  max_num = PhoneNumber.MAX_NUM


class NoteInline(admin.TabularInline):
  """Inline for adding Notes to a Contact."""

  model = Note
  extra = 0
  max_num = Note.MAX_NUM

  readonly_fields = ['author', 'timestamp']

  def has_change_permission(
    self,
    request: HttpRequest,
    obj: Optional[Note] = None,
  ) -> bool:
    return False


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
  """Admin functionality for CTCT Contacts."""

  search_fields = (
    'email',
    'first_name',
    'last_name',
    'company_name',
    'job_title',
    'city',
  )

  ordering = ('-updated_at', )
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
    StreetAddressInline,
    PhoneNumberInline,
    NoteInline,
  )

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
    if getattr(obj, 'user', False):
      readonly_fields += ProfileForm._meta.fields
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
    if formset.model == Note:
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


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
  """Admin functionality for Notes."""

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
    obj: Optional[Note] = None,
  ) -> bool:
    return False

  def has_add_permission(
    self,
    request: HttpRequest,
    obj: Optional[Note] = None,
  ) -> bool:
    return False


@admin.register(EmailCampaign)
class EmailCampaignAdmin(ViewModelAdmin):
  """Admin functionality for CTCT EmailCampaigns."""

  search_fields = ('name', )
  list_display = (
    'post__detail',             # TODO
    'post__category',           # TODO
    'scheduled_datetime',
    'open_rate_str',
    'sends',
    'bounces',
    'clicks',
    'optouts',
    'abuse',
  )
  list_select_related = ('post', )

  def post__detail(self, obj: EmailCampaign) -> str:
    url = reverse(
      viewname='admin:posts_post_change',  # TODO
      args=(obj.post.id, ),
    )
    html = (
      f'<a href="{url}">'
      f'<b>{obj.post.title}</b>'
      '</a>'
    )
    return mark_safe(html)
  post__detail.admin_order_field = 'post'
  post__detail.short_description = _('Post')

  def post__category(self, obj: EmailCampaign) -> str:
    return obj.post.get_category_display()
  post__category.admin_order_field = 'post__category'
  post__category.short_description = _('Category')

  def open_rate_str(self, obj: EmailCampaign) -> str:
    return f'{obj.open_rate:0.2%}'
  open_rate_str.admin_order_field = 'open_rate'
  open_rate_str.short_description = _('Open Rate')

  def get_queryset(self, request: HttpRequest) -> QuerySet[EmailCampaign]:
    qs = super().get_queryset(request)
    qs = qs.filter(current_status='DONE')
    return qs
