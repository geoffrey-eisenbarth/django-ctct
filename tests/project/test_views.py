from unittest.mock import patch, MagicMock

from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.test import TestCase, override_settings
from django.utils.translation import gettext as _


AUTH_URL: str = reverse('ctct:auth')


# Minimal settings required for the app to load without ImproperlyConfigured
@override_settings(
  CTCT_PUBLIC_KEY="TEST_KEY",
  CTCT_SECRET_KEY="TEST_SECRET",
  CTCT_REDIRECT_URI="http://testserver/django-ctct/auth/",
  CTCT_FROM_NAME="Test Name",
  CTCT_FROM_EMAIL="test@example.com",
)
class AuthViewTest(TestCase):
  """Tests for the OAuth2 authentication view (django_ctct.views.auth)."""

  @patch('django_ctct.models.Token.remote.get_auth_url')
  def test_auth_initial_request_redirects(
    self,
    mock_get_auth_url: MagicMock,
  ) -> None:
    mock_auth_url = 'https://auth.constantcontact.com/oauth2/some_long_url'
    mock_get_auth_url.return_value = mock_auth_url

    response: HttpResponse = self.client.get(AUTH_URL)

    self.assertEqual(response.status_code, 302)
    self.assertEqual(response.url, mock_auth_url)

    mock_get_auth_url.assert_called_once()
    self.assertIsInstance(mock_get_auth_url.call_args[0][0], HttpRequest)

  @patch('django_ctct.models.Token.remote.create')
  def test_auth_with_code_success(self, mock_create: MagicMock) -> None:
    auth_code = 'valid-authorization-code-123'
    url_with_code = f'{AUTH_URL}?code={auth_code}'

    # mock_create will return None by default, simulating success
    response: HttpResponse = self.client.get(url_with_code)

    self.assertEqual(response.status_code, 200)

    success_message = _("Sucessfully created and stored the token.")
    self.assertIn(success_message, response.content.decode())

    mock_create.assert_called_once_with(auth_code)

  @patch(
    'django_ctct.models.Token.remote.create',
    side_effect=Exception('Mocked API Error')
  )
  def test_auth_with_code_failure(self, mock_create: MagicMock) -> None:
    auth_code = 'invalid-authorization-code-456'
    url_with_code = f'{AUTH_URL}?code={auth_code}'
    mock_error_message = "Mocked API Error"

    response: HttpResponse = self.client.get(url_with_code)

    self.assertEqual(response.status_code, 200)
    self.assertIn(mock_error_message, response.content.decode())
    mock_create.assert_called_once_with(auth_code)
