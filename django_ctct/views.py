import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext as _

from django_ctct.models import Token

logger = logging.getLogger("django_ctct")


def auth(request: HttpRequest) -> HttpResponse:
  """Facilitates OAuth2 authentication with CTCT."""

  if auth_code := request.GET.get("code"):
    # Verify the OAuth state to ensure this flow was initiated by this user
    state = request.GET.get("state", "")
    expected_state = request.session.pop(Token.remote.OAUTH_STATE_SESSION_KEY, "")
    if not (state and constant_time_compare(state, expected_state)):
      return HttpResponse(_("Invalid OAuth state."), status=403)

    try:
      Token.remote.create(auth_code)
    except Exception:
      logger.exception("Failed to create CTCT Token.")
      return HttpResponse(
        _("Failed to create the token. Check the server logs for details."),
        status=502,
      )
    else:
      return HttpResponse(_("Successfully created and stored the token."))

  else:
    # An admin must provide CTCT access manually
    response = redirect(Token.remote.get_auth_url(request))
    return response
