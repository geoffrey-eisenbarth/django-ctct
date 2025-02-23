from django.apps import AppConfig
from django.db.models.signals import post_save, m2m_changed, pre_delete
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class CTCTConfig(AppConfig):
  name = 'django_ctct'
  verbose_name = 'Constant Contact'
  ctct_settings = [
    'CTCT_PUBLIC_KEY',
    'CTCT_SECRET_KEY',
    'CTCT_REDIRECT_URI',
    'CTCT_FROM_NAME',
    'CTCT_FROM_EMAIL',
    'CTCT_ENQUEUE_DEFAULT',
  ]

  def ready(self):
    from django_ctct.signals import (
      remote_save, remote_delete, remote_update_m2m
    )

    # Validate that necessary settings have been defined
    for value in self.ctct_settings:
      if not hasattr(settings, value):
        message = (
          f"[django-ctct] {value} must be defined in settings.py."
        )
        raise ImproperlyConfigured(message)

    # Hook up the signals
    if getattr(settings, 'CTCT_USE_SIGNALS', False):
      post_save.connect(remote_save)
      pre_delete.connect(remote_delete)
      m2m_changed.connect(remote_update_m2m)
