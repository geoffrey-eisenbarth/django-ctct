from unittest.mock import MagicMock, patch

from django.http import HttpRequest
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.translation import gettext as _

from django_ctct.models import Token

AUTH_URL: str = reverse("ctct:auth")


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

  def test_token_get_auth_url(self) -> None:
    """Token.remote.get_auth_url() builds the full authorization URL."""
    request = HttpRequest()
    request.session = self.client.session
    url = Token.remote.get_auth_url(request)
    self.assertIn("https://authz.constantcontact.com/oauth2/default/v1/authorize?", url)
    self.assertIn("client_id=TEST_KEY", url)
    self.assertIn("state=", url)
    self.assertIn(Token.remote.OAUTH_STATE_SESSION_KEY, request.session)

  @patch("django_ctct.models.Token.remote.get_auth_url")
  def test_auth_initial_request_redirects(
    self,
    mock_get_auth_url: MagicMock,
  ) -> None:
    mock_auth_url = "https://auth.constantcontact.com/oauth2/some_long_url"
    mock_get_auth_url.return_value = mock_auth_url

    response = self.client.get(AUTH_URL)

    self.assertEqual(response.status_code, 302)
    self.assertEqual(
      response["Location"],  # django-stubs GH Issue #971
      mock_auth_url,
    )

    mock_get_auth_url.assert_called_once()
    self.assertIsInstance(mock_get_auth_url.call_args[0][0], HttpRequest)

  def set_oauth_state(self, state: str) -> None:
    session = self.client.session
    session[Token.remote.OAUTH_STATE_SESSION_KEY] = state
    session.save()

  @patch("django_ctct.models.Token.remote.create")
  def test_auth_with_code_success(self, mock_create: MagicMock) -> None:
    auth_code = "valid-authorization-code-123"
    url_with_code = f"{AUTH_URL}?code={auth_code}&state=valid-state"
    self.set_oauth_state("valid-state")

    # mock_create will return None by default, simulating success
    response = self.client.get(url_with_code)

    self.assertEqual(response.status_code, 200)

    success_message = _("Successfully created and stored the token.")
    self.assertIn(success_message, response.content.decode())

    mock_create.assert_called_once_with(auth_code)

  @patch("django_ctct.models.Token.remote.create")
  def test_auth_with_invalid_state(self, mock_create: MagicMock) -> None:
    url_with_code = f"{AUTH_URL}?code=some-code&state=tampered-state"
    self.set_oauth_state("valid-state")

    response = self.client.get(url_with_code)

    self.assertEqual(response.status_code, 403)
    mock_create.assert_not_called()

  @patch(
    "django_ctct.models.Token.remote.create", side_effect=Exception("Mocked API Error")
  )
  def test_auth_with_code_failure(self, mock_create: MagicMock) -> None:
    auth_code = "invalid-authorization-code-456"
    url_with_code = f"{AUTH_URL}?code={auth_code}&state=valid-state"
    self.set_oauth_state("valid-state")

    with self.assertLogs("django_ctct", level="ERROR"):
      response = self.client.get(url_with_code)

    self.assertEqual(response.status_code, 502)
    failure_message = _(
      "Failed to create the token. Check the server logs for details."
    )
    self.assertIn(failure_message, response.content.decode())
    mock_create.assert_called_once_with(auth_code)
