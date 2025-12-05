from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _


class CTCTConfig(AppConfig):
  name = 'django_ctct'
  verbose_name = _('Constant Contact')
  ctct_settings = [
    'CTCT_PUBLIC_KEY',
    'CTCT_SECRET_KEY',
    'CTCT_REDIRECT_URI',
    'CTCT_FROM_NAME',
    'CTCT_FROM_EMAIL',
  ]

  def ready(self) -> None:
    # Validate that necessary settings have been defined
    for value in self.ctct_settings:
      if not hasattr(settings, value):
        message = _(
          f"[django-ctct] {value} must be defined in settings.py."
        )
        raise ImproperlyConfigured(message)
