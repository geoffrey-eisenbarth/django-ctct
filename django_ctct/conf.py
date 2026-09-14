"""Centralized access to django-ctct settings.

Notes
-----
Required settings (validated by the app's system checks) are accessed
directly via `django.conf.settings`; this module handles the optional
settings and their default values.

"""

from __future__ import annotations

from typing import Any, Literal, overload

from django.conf import settings

REQUIRED_SETTINGS: tuple[str, ...] = (
  "CTCT_PUBLIC_KEY",
  "CTCT_SECRET_KEY",
  "CTCT_REDIRECT_URI",
  "CTCT_FROM_NAME",
  "CTCT_FROM_EMAIL",
)

# Values may be zero-argument callables, evaluated lazily so that defaults
# can depend on other Django settings.
DEFAULTS: dict[str, Any] = {
  "CTCT_USE_ADMIN": False,
  "CTCT_SYNC_ADMIN": False,
  "CTCT_ENQUEUE_DEFAULT": False,
  "CTCT_RAISE_FOR_API": False,
  "CTCT_API_TIMEOUT": 30,  # seconds
  "CTCT_PHYSICAL_ADDRESS": None,
  "CTCT_REPLY_TO_EMAIL": lambda: settings.CTCT_FROM_EMAIL,
  "CTCT_PREVIEW_RECIPIENTS": lambda: settings.MANAGERS,
  "CTCT_PREVIEW_RECIPIENTS_CALLABLE": None,
  "CTCT_PREVIEW_MESSAGE": "",
  "CTCT_PREVIEW_MESSAGE_CALLABLE": None,
}


@overload
def get_setting(
  name: Literal[
    "CTCT_USE_ADMIN",
    "CTCT_SYNC_ADMIN",
    "CTCT_ENQUEUE_DEFAULT",
    "CTCT_RAISE_FOR_API",
  ],
) -> bool: ...


@overload
def get_setting(name: Literal["CTCT_API_TIMEOUT"]) -> int: ...


@overload
def get_setting(
  name: Literal["CTCT_PHYSICAL_ADDRESS"],
) -> dict[str, str] | None: ...


@overload
def get_setting(
  name: Literal["CTCT_REPLY_TO_EMAIL", "CTCT_PREVIEW_MESSAGE"],
) -> str: ...


@overload
def get_setting(
  name: Literal["CTCT_PREVIEW_RECIPIENTS"],
) -> list[tuple[str, str]]: ...


@overload
def get_setting(
  name: Literal[
    "CTCT_PREVIEW_RECIPIENTS_CALLABLE",
    "CTCT_PREVIEW_MESSAGE_CALLABLE",
  ],
) -> str | None: ...


def get_setting(name: str) -> Any:
  """Return an optional django-ctct setting, or its documented default."""
  try:
    return getattr(settings, name)
  except AttributeError:
    default = DEFAULTS[name]
    return default() if callable(default) else default
