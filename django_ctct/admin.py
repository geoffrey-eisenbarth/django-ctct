from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Q, OuterRef, Exists
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from import_export import resources
from import_export.admin import ExportActionMixin
from import_export.fields import Field
from import_export.formats import base_formats
from import_export.widgets import ManyToManyWidget

from django_ctct.models import (
  Token, Contact, ContactList, EmailCampaign,
  validate_contact_list_membership,
  ContactNote,
)

from accounts.core.admin import ViewModelAdmin
from accounts.users.forms import ProfileForm


@admin.register(Token)
class TokenAdmin(ViewModelAdmin):
  """Basic admin functionality for CTCT Tokens."""

  list_display = (
    'inserted',
    'copy_access_code',
    'copy_refresh_code',
    'type',
  )

  def copy_access_code(self, obj):
    html = format_html(
      '<button class="button" onclick="{function}">{copy_icon}</button>',
      function=f"navigator.clipboard.writeText('{obj.access_code}')",
      copy_icon=mark_safe('&#128203;'),
    )
    return html
  copy_access_code.short_description = 'Access Code'

  def copy_refresh_code(self, obj):
    html = format_html(
      '<button class="button" onclick="{function}">{copy_icon}</button>',
      function=f"navigator.clipboard.writeText('{obj.refresh_code}')",
      copy_icon=mark_safe('&#128203;'),
    )
    return html
  copy_refresh_code.short_description = 'Refresh Code'

  def changeform_view(
    self,
    request,
    object_id=None,
    form_url='',
    extra_context=None
  ):
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
  """Basic admin functionality for CTCT ContactLists."""

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

  def synced(self, obj):
    return bool(obj.id)
  synced.boolean = True

  def membership(self, obj):
    return obj.contacts.count()

  def optouts(self, obj):
    return obj.contacts.filter(optout=True).count()
  optouts.short_description = 'Opt Outs'


class ContactResource(resources.ModelResource):
  """Import-Export Resource for Contacts."""

  # Use ContactList.name
  lists = Field(
    attribute='lists',
    column_name='lists',
    widget=ManyToManyWidget(
      model=ContactList,
      separator=', ',
      field='name',
    ),
  )

  # Rename column names used for ID fields
  _id = Field(
    attribute='_id',
    column_name='contact_id',
  )
  id = Field(
    attribute='id',
    column_name='ctct_id',
  )
  user__id = Field(
    attribute='user__id',
    column_name='user_id',
  )
  customer__id = Field(
    attribute='customer__id',
    column_name='stripe_id',
  )

  class Meta:
    model = Contact
    fields = (
      'email',
      'honorific',
      'first_name',
      'last_name',
      'suffix',
      'job_title',
      'company',
      'county__name',
      'address',
      'address_2',
      'city',
      'state__postal_code',
      'zip_code',
      'home_phone',
      'office_phone',
      'direct_phone',
      'cell_phone',
      'fax',
      'created_at',
      'updated_at',
      'lists',
      '_id',
      'id',
      'user__id',
      'customer__id',
    )
    export_order = fields


class ContactStatusFilter(admin.SimpleListFilter):
  """Simple filter for CTCT Status."""

  STATUSES = (
    ('sync', _('Synced')),
    ('not_synced', _('Not Synced')),
    ('optout', _('Opted Out')),
  )

  title = 'CTCT Status'
  parameter_name = 'ctct'

  def lookups(self, request, model_admin):
    return self.STATUSES

  def queryset(self, request, queryset):
    if self.value() == 'sync':
      queryset = queryset.filter(id__isnull=False)
    elif self.value() == 'not_synced':
      queryset = queryset.filter(id__isnull=True)
    elif self.value() == 'optout':
      queryset = queryset.filter(optout=True)

    return queryset


class ContactListGroupFilter(admin.SimpleListFilter):
  """Simple filter for CTCT Lists."""

  LISTS = (
    ('comped', _('Comped')),
    ('not_comped', _('Not Comped')),
    ('none', _('No Lists')),
    ('missing', _('Missing Email')),
    ('media', _('Any Media')),
    ('any', _('Any List')),
  )

  title = 'Contact List Group'
  parameter_name = 'list_group'

  def lookups(self, request, model_admin):
    return self.LISTS

  def queryset(self, request, queryset):

    if self.value() == 'comped':
      queryset = queryset.filter(lists__name='Newsletter: Comps')
    elif self.value() == 'not_comped':
      queryset = queryset.exclude(lists__name__in=[
        'Newsletter: Comps', 'Public Officials'
      ]).exclude(lists__name__startswith='Media')
    elif self.value() == 'none':
      queryset = queryset.filter(lists__isnull=True)
    elif self.value() == 'missing':
      queryset = queryset.filter(email=None)
    elif self.value() == 'media':
      ThroughModel = Contact.lists.through
      condition = ThroughModel.objects.filter(
        contact=OuterRef('pk'),
        contactlist__name__startswith='Media',
      )
      queryset = queryset.filter(Exists(condition))
    elif self.value() == 'any':
      queryset = queryset.filter(lists__isnull=False)

    return queryset


class ContactAdminForm(forms.ModelForm):
  """Custom form for Many2Many Validation."""

  class Meta:
    model = Contact
    fields = '__all__'

  def clean(self) -> None:
    """Verifies Many2Many relations."""

    # Validate ContactList membership
    lists = self.cleaned_data.get('lists')
    if not lists:
      return super().clean()

    official = Q(name__contains='Public Officials') | Q(name='Legislators')
    media = Q(name__startswith='Media')
    column = Q(name__contains='Column')
    comped = Q(name='Newsletter: Comps')

    message = None
    if lists.filter(official) and lists.exclude(official | column):
      message = _(
        "Public Officials cannot be included in any other ContactLists."
      )
    elif lists.filter(media) and lists.exclude(media | column):
      message = _(
        "Media Contacts can only be included in other 'Media' ContactLists."
      )
    elif lists.filter(comped) and lists.filter(official | media):
      message = _(
        "Comped Contacts cannot be added to 'Public Officials' or 'Media' "
        "ContactLists."
      )

    if message is not None:
      raise ValidationError(message)

    return super().clean()


class ContactNoteInline(admin.TabularInline):
  """Inline for adding Notes to a Contact."""

  model = ContactNote
  extra = 0

  readonly_fields = ['author', 'timestamp']

  def has_change_permission(self, request, obj=None) -> bool:
    return False


@admin.register(Contact)
class ContactAdmin(ExportActionMixin, admin.ModelAdmin):
  """Basic admin functionality for CTCT Contacts."""

  resource_classes = (
    ContactResource,
  )
  formats = (
    base_formats.CSV,
    base_formats.XLS,
    base_formats.XLSX,
    base_formats.JSON,
  )

  actions = ['comp_contacts', 'uncomp_contacts']

  search_fields = (
    'email',
    'first_name',
    'last_name',
    'company',
    'job_title',
    'county__name',
    'city',
    'state__postal_code',
    'state__name',
  )

  ordering = ('-updated_at', )
  list_display = (
    'email',
    'name',
    'job',
    'updated_at',
    'ctct',
    'list',
  )
  list_filter = (
    ContactStatusFilter,
    ContactListGroupFilter,
    'lists',
  )
  empty_value_display = '(None)'

  form = ContactAdminForm
  fieldsets = (
    (None, {
      'fields': (
        'email',
        ('first_name', 'last_name'),
        ('honorific', 'suffix'),
        ('job_title', 'company'),
        ('address', 'address_2'),
        ('city', 'zip_code'),
        'county',
        ('office_phone', 'direct_phone'),
        ('home_phone', 'cell_phone'),
        'fax',
      ),
    }),
    ('CONTACT LISTS', {
      'fields': (
        'lists',
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
  filter_horizontal = ('lists', )
  inlines = (ContactNoteInline, )

  def county__name(self, obj) -> str:
    if obj.county is not None:
      name = obj.county.name
    else:
      name = ''
    return name
  county__name.admin_order_field = 'county__name'
  county__name.short_description = 'County'

  def state__postal_code(self, obj) -> str:
    if obj.state is not None:
      postal_code = obj.state.postal_code
    else:
      postal_code = ''
    return postal_code
  state__postal_code.admin_order_field = 'state'
  state__postal_code.short_description = 'State'

  def ctct(self, obj) -> str:
    if not obj.id:
      text = 'Not Synced'
      color = 'red'
    elif obj.optout:
      text = 'Opted Out'
      color = 'yellow'
    else:
      text = 'Synced'
      color = 'teal'

    html = (
      f'<span class="badge {color}">'
      f'{text}'
      '</span>'
    )
    return mark_safe(html)

  def list(self, obj) -> str:
    if (
      (obj.is_public_official and obj.is_comped)
      or (obj.is_media and obj.is_comped)
    ):
      text = 'Error'
      color = 'red'
    elif obj.is_public_official:
      text = 'Public Official'
      color = 'yellow'
    elif obj.is_media:
      text = 'Media'
      color = 'orange'
    elif obj.is_newsletter:
      text = 'Newsletter'
      color = 'purple'
    elif obj.is_column:
      text = 'Column'
      color = 'teal'
    elif not obj.lists.exists():
      text = 'None'
      color = 'gold'
    else:
      text = 'Other'
      color = 'green'
    html = (
      f'<span class="badge {color}">'
      f'{text}'
      '</span>'
    )
    return mark_safe(html)

  def uncomp_contacts(self, request, queryset) -> None:
    """Removes Contacts from the Newsletter: Comp ContactList."""
    comp_list = ContactList.objects.get(name='Newsletter: Comps')
    comp_list.contacts.remove(*queryset)
  uncomp_contacts.short_description = (
    'Remove selected %(verbose_name_plural)s from comps'
  )

  def comp_contacts(self, request, queryset) -> None:
    """Adds Contacts to the Newsletter: Comp List."""
    comp_list = ContactList.objects.get(name='Newsletter: Comps')
    try:
      validate_contact_list_membership(
        sender=ContactList,
        instance=comp_list,
        action='pre_add',
        pk_set=set(queryset.values_list('pk', flat=True)),
      )
    except ValidationError as e:
      messages.error(request, e.messages[0])
    else:
      comp_list.contacts.add(*queryset)
  comp_contacts.short_description = (
    'Comp selected %(verbose_name_plural)s'
  )

  def get_readonly_fields(self, request, obj=None):
    readonly_fields = [
      'created_at',
      'updated_at',
      'optout',
      'optout_at',
    ]
    if getattr(obj, 'user', False):
      readonly_fields += ProfileForm._meta.fields
    if getattr(obj, 'optout', False) and not request.user.is_superuser:
      readonly_fields.append('lists')
    return readonly_fields

  def save_formset(self, request, form, formset, change) -> None:
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
  """Basic admin functionality for Notes."""

  search_fields = (
    'note',
    'contact__first_name',
    'contact__last_name',
    'contact__email',
    'author__email',
    'author__contact__first_name',
    'author__contact__last_name',
  )

  list_display_links = None
  list_display = (
    'contact',
    'note',
    'author',
    'timestamp',
  )
  list_filter = (
    'timestamp',
    'author',
  )

  readonly_fields = (
    'contact',
    'author',
    'timestamp',
  )

  fieldsets = (
    (None, {
      'fields': (
        'contact',
        'note',
      ),
    }),
    ('INTERNAL', {
      'fields': (
        'author',
        'timestamp',
      ),
    }),
  )

  def has_change_permission(self, request, obj=None) -> bool:
    return False

  def has_add_permission(self, request, obj=None) -> bool:
    return False


@admin.register(EmailCampaign)
class EmailCampaignAdmin(ViewModelAdmin):
  """Basic admin functionality for CTCT EmailCampaigns."""

  search_fields = ('name', )
  list_display = (
    'post__detail',
    'post__category',
    'scheduled_datetime',
    'open_rate_str',
    'sends',
    'bounces',
    'clicks',
    'optouts',
    'abuse',
  )
  list_select_related = ('post', )

  def post__detail(self, obj):
    url = reverse(
      viewname='admin:posts_post_change',
      args=(obj.post.id, ),
    )
    html = (
      f'<a href="{url}">'
      f'<b>{obj.post.title}</b>'
      '</a>'
    )
    return mark_safe(html)
  post__detail.admin_order_field = 'post'
  post__detail.short_description = 'Post'

  def post__category(self, obj):
    return obj.post.get_category_display()
  post__category.admin_order_field = 'post__category'
  post__category.short_description = 'Category'

  def open_rate_str(self, obj):
    return f'{obj.open_rate:0.2%}'
  open_rate_str.admin_order_field = 'open_rate'
  open_rate_str.short_description = 'Open Rate'

  def get_queryset(self, request):
    qs = super().get_queryset(request)
    return qs.filter(status='DONE')
