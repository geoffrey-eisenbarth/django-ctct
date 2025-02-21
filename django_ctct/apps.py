from django.apps import AppConfig
from django.db.models.signals import post_save, m2m_changed, pre_delete
from django.conf import settings

from django_ctct.models import Contact
from django_ctct.signals import (
  ctct_save, ctct_delete, ctct_update_contact_lists,
)


class CTCTConfig(AppConfig):
  name = 'django_ctct'
  verbose_name = 'Constant Contact'

  def ready(self):
    if getattr(settings, 'CTCT_USE_SIGNALS', False):
      post_save.connect(ctct_save)
      pre_delete.connect(ctct_delete)
      m2m_changed.connect(
        ctct_update_contact_lists,
        sender=Contact.list_memberships.through,
      )
