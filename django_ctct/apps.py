from django.apps import AppConfig
from django.db.models.signals import post_save, m2m_changed, pre_delete
from django.conf import settings


class CTCTConfig(AppConfig):
  name = 'django_ctct'
  verbose_name = 'Constant Contact'

  def ready(self):
    from django_ctct.signals import (
      ctct_save, ctct_delete, ctct_update_contact_lists,
    )


    if getattr(settings, 'CTCT_USE_SIGNALS', False):
      post_save.connect(ctct_save)
      pre_delete.connect(ctct_delete)
      m2m_changed.connect(
        ctct_update_contact_lists,
        sender='django_ctct.Contact.list_memberships.through',
      )
