from typing import Any

from django.apps import AppConfig
from django.conf import settings
from django.core import checks
from django.utils.translation import gettext_lazy as _

from django_ctct.conf import REQUIRED_SETTINGS


@checks.register(checks.Tags.compatibility)
def check_required_settings(
  app_configs: Any = None,
  **kwargs: Any,
) -> list[checks.Error]:
  """Validate that necessary settings have been defined."""
  errors = []
  for name in REQUIRED_SETTINGS:
    if not hasattr(settings, name):
      errors.append(
        checks.Error(
          f"{name} must be defined in settings.",
          hint=f"Add {name} to your project's settings.py.",
          id="django_ctct.E001",
        )
      )
  return errors


class CTCTConfig(AppConfig):
  name = "django_ctct"
  verbose_name = _("Constant Contact")
  default_auto_field = "django.db.models.BigAutoField"
