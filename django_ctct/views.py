from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.utils.translation import gettext as _

from django_ctct.models import Token


def auth(request: HttpRequest) -> HttpResponse:
  """Facilitates OAuth2 authentication with CTCT."""

  if auth_code := request.GET.get('code'):
    try:
      Token.remote.create(auth_code)
    except Exception as e:
      message = str(e)
    else:
      message = _("Sucessfully created and stored the token.")
    return HttpResponse(message)

  else:
    # An admin must provide CTCT access manually
    response = redirect(Token.remote.get_auth_url(request))
    return response
