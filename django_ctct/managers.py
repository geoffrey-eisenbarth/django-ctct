from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models.manager import Manager
from django.urls import reverse
from django.utils.translation import gettext as _
from jwt import ExpiredSignatureError

if TYPE_CHECKING:  # pragma: no cover
  from django_ctct.models import Token


class TokenManager(Manager["Token"]):
  """Local manager for Token with in-memory caching and automatic refresh."""

  _cached_token: Token | None = None

  def get_valid(self, force_refresh: bool = False) -> Token:
    """Fetches active Token, refreshing remotely if expired or on 401."""
    if not force_refresh and self._cached_token is not None:
      if not self._cached_token.is_expired:
        return self._cached_token

    token = self.first()
    if not token:
      self._cached_token = None
      raise ValueError(
        _(
          "No tokens in the database yet. "
          f"Go to {reverse('ctct:auth')} and sign into ConstantContact."
        )
      )

    if force_refresh or token.is_expired:
      token = self.model.remote.refresh(token)
    else:
      try:
        token.decode()
      except ExpiredSignatureError:
        token = self.model.remote.refresh(token)

    self._cached_token = token
    return token
