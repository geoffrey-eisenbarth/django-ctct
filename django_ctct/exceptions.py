from django.utils.translation import gettext as _


class LocalObjectDoesNotExist(Exception):
  """Exception raised when a local object does not exist."""

  def __init__(self, message: str = _("Must create object locally first.")) -> None:
    super().__init__(message)


class RemoteObjectDoesNotExist(Exception):
  """Exception raised when a remote object does not exist."""

  def __init__(self, message: str = _("Must create object remotely first.")) -> None:
    super().__init__(message)
