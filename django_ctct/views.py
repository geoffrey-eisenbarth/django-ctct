from urllib.parse import urlencode

import requests

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token as get_csrf_token
from django.shortcuts import redirect

from django_ctct.models import Token


for value in [
  'CTCT_PUBLIC_KEY',
  'CTCT_SECRET_KEY',
  'CTCT_REDIRECT_URI',
  'CTCT_FROM_NAME',
  'CTCT_FROM_EMAIL',
  'CTCT_POST_MODEL',
  # 'CTCT_REPLY_TO_EMAIL'  # Optional, defaults to CTCT_FROM_EMAIL
  'AUTH_USER_MODEL',
  'ADMINS',
]:
  if not hasattr(settings, value):
    raise ImproperlyConfigured(f"{value} must be defined in settings.py")


def auth(request: HttpRequest) -> HttpResponse:
  """Allows OAuth2 authentication with CTCT.

  Notes
  -----
  The value of CTCT_REDIRECT_URI must exactly match the value
  specified in the developer's page on constantcontact.com.

  """

  auth_code = request.GET.get('code')

  if auth_code:
    response = requests.post(
      url='https://authz.constantcontact.com/oauth2/default/v1/token',
      auth=(settings.CTCT_PUBLIC_KEY, settings.CTCT_SECRET_KEY),
      data={
        'code': auth_code,
        'redirect_uri': settings.CTCT_REDIRECT_URI,
        'grant_type': 'authorization_code',
      },
    ).json()
    if 'refresh_token' in response:
      token = Token(
        access_code=response['access_token'],
        refresh_code=response['refresh_token'],
        type=response['token_type'],
      )
      token.save()
      message = (
        'Sucessfully saved CTCT tokens.'
      )
    else:
      message = (
        f"Token does not contain `refresh_token`: {response}"
      )
    return HttpResponse(message)

  else:
    # An admin must provide CTCT access manually
    endpoint = 'https://authz.constantcontact.com/oauth2/default/v1/authorize'
    data = {
      'client_id': settings.CTCT_PUBLIC_KEY,
      'redirect_uri': settings.CTCT_REDIRECT_URI,
      'response_type': 'code',
      'state': get_csrf_token(request),
      'scope': '+'.join([
        'account_read',
        'account_update',
        'contact_data',
        'campaign_data',
        'offline_access',
      ]),
    }

    url = f"{endpoint}?{urlencode(data, safe='+')}"
    response = redirect(url)
    return response
