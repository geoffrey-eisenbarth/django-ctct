from django import forms
from django.contrib.admin import widgets as admin_widgets
from django.utils.translation import gettext_lazy as _

from django_ctct.models import ContactList


class CampaignComposeForm(forms.Form):
  # EmailCampaign fields
  name = forms.CharField(
    max_length=80,
    label=_("Name"),
  )
  scheduled_datetime = forms.SplitDateTimeField(
    required=False,
    label=_("Scheduled Date and Time"),
  )
  send_preview = forms.BooleanField(
    required=False,
    label=_("Send Preview?"),
  )
  preview_message = forms.CharField(
    widget=forms.Textarea, required=False, label=_("Preview Message")
  )
  preview_recipients = forms.CharField(
    widget=forms.Textarea,
    required=False,
    label=_("Preview Recipients (comma-separated emails)"),
  )

  # CampaignActivity fields
  subject = forms.CharField(max_length=80, label=_("Email Subject"))
  preheader = forms.CharField(
    max_length=120, required=False, label=_("Email Preheader")
  )
  html_content = forms.CharField(
    widget=forms.Textarea, max_length=150000, label=_("HTML Content")
  )
  contact_lists = forms.ModelMultipleChoiceField(
    queryset=ContactList.objects.all(),
    widget=admin_widgets.FilteredSelectMultiple(
      "Contact Lists",
      is_stacked=False,
    ),
    label=_("Contact Lists"),
  )
