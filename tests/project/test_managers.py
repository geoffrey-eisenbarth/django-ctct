from unittest.mock import MagicMock, patch
from uuid import uuid4

import requests_mock
from django.http import Http404
from django.test import TestCase
from jwt import ExpiredSignatureError
from requests.exceptions import HTTPError

from django_ctct.models import (
  CampaignActivity,
  CampaignSummary,
  Contact,
  ContactList,
  EmailCampaign,
  JsonDict,
  Token,
)
from tests.factories import TokenFactory, get_factory
from tests.project.test_models import RequestsMockMixin


class TokenRemoteManagerTest(TestCase):
  """Tests for TokenRemoteManager and TokenManager auth lifecycle."""

  def setUp(self) -> None:
    Token.objects._cached_token = None
    self.mock_api = requests_mock.Mocker()
    self.mock_api.start()
    self.addCleanup(self.mock_api.stop)

  def get_token_response(self, access_token: str = "new-access-token") -> JsonDict:
    return {
      "access_token": access_token,
      "refresh_token": "new-refresh-token",
      "token_type": Token.TOKEN_TYPE,
      "scope": Token.API_SCOPE,
      "expires_in": 7200,
    }

  def test_create(self) -> None:
    """Token.remote.create() should exchange an auth code for a Token."""

    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=200,
      json=self.get_token_response(),
    )

    token = Token.remote.create("some-auth-code")

    self.assertEqual(Token.objects.count(), 1)
    self.assertEqual(token.access_token, "new-access-token")
    self.assertIn("Token (Expires:", str(token))

  @patch("jwt.PyJWKClient")
  @patch("jwt.decode")
  def test_token_decode(self, mock_jwt_decode: MagicMock, mock_jwk: MagicMock) -> None:
    token = TokenFactory.create()
    mock_jwt_decode.return_value = {"aud": "test"}
    data = token.decode()
    self.assertEqual(data, {"aud": "test"})

  def test_refresh(self) -> None:
    """Token.remote.refresh() exchanges a refresh token for a new Token."""
    token = TokenFactory.create(refresh_token="old-refresh-token")

    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=200,
      json=self.get_token_response(access_token="refreshed-token"),
    )

    new_token = Token.remote.refresh(token)
    self.assertEqual(Token.objects.count(), 2)
    self.assertEqual(new_token.access_token, "refreshed-token")

  @patch("django_ctct.models.Token.decode")
  def test_get_valid_refreshes_expired(self, token_decode: MagicMock) -> None:
    """Token.objects.get_valid() should refresh an expired Token automatically."""

    TokenFactory.create()
    token_decode.side_effect = ExpiredSignatureError

    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=200,
      json=self.get_token_response(),
    )

    token = Token.objects.get_valid()

    self.assertEqual(Token.objects.count(), 2)
    self.assertEqual(token.access_token, "new-access-token")

  @patch("django_ctct.models.Token.decode")
  def test_get_valid_uses_cache(self, token_decode: MagicMock) -> None:
    """Token.objects.get_valid() uses cached token when valid."""
    token_decode.return_value = True
    token1 = TokenFactory.create()

    fetched1 = Token.objects.get_valid()
    self.assertEqual(fetched1.pk, token1.pk)

    # Second call returns the cached instance
    fetched2 = Token.objects.get_valid()
    self.assertIs(fetched1, fetched2)

  @patch("django_ctct.models.Token.decode")
  def test_401_retry_refreshes_token(self, token_decode: MagicMock) -> None:
    """401 responses trigger token refresh and retry."""
    token_decode.return_value = True
    TokenFactory.create()

    # Initial call gives 401, refresh gives new token, retry succeeds with 200
    test_url = "https://api.cc.email/v3/account/summary"
    self.mock_api.register_uri(
      "GET",
      test_url,
      [
        {"status_code": 401, "json": {"error_message": "Unauthorized"}},
        {"status_code": 200, "json": {"data": "ok"}},
      ],
    )
    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=200,
      json=self.get_token_response(access_token="refreshed-token"),
    )

    response = Contact.remote.request("get", test_url)
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {"data": "ok"})
    self.assertEqual(Token.objects.count(), 2)

  def test_get_valid_raises_without_existing_token(self) -> None:
    with self.assertRaises(ValueError):
      Token.objects.get_valid()

  def test_raise_or_json_handles_404_and_http_errors(self) -> None:
    """Covers ConnectionManagerMixin.raise_or_json()'s error branches."""

    # 404 should be raised as Django's Http404, not HTTPError
    self.mock_api.post(url=Token.remote.get_url(), status_code=404)
    with self.assertRaises(Http404):
      Token.remote.create("some-auth-code")

    # Non-404 errors should raise HTTPError with CTCT's error message
    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=400,
      json={"error_description": "invalid_grant"},
    )
    with self.assertRaises(HTTPError) as ctx:
      Token.remote.create("some-auth-code")
    self.assertIn("invalid_grant", str(ctx.exception))

    # Error response formatted as list of dicts
    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=400,
      json=[{"error_message": "List error format"}],
    )
    with self.assertRaises(HTTPError) as ctx:
      Token.remote.create("some-auth-code")
    self.assertIn("List error format", str(ctx.exception))

    # Non-JSON error body (e.g. an HTML error page from a proxy)
    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=502,
      text="<html>Bad Gateway</html>",
    )
    with self.assertRaises(HTTPError):
      Token.remote.create("some-auth-code")

  def test_force_refresh_with_cached_token(self) -> None:
    token = TokenFactory.create()
    Token.objects._cached_token = token
    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=200,
      json=self.get_token_response(access_token="forced-refresh-token"),
    )
    refreshed = Token.objects.get_valid(force_refresh=True)
    self.assertEqual(refreshed.access_token, "forced-refresh-token")

  def test_get_valid_with_expired_cached_token(self) -> None:
    token = TokenFactory.create(expires_in=0)
    Token.objects._cached_token = token
    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=200,
      json=self.get_token_response(access_token="refreshed-cached-token"),
    )
    refreshed = Token.objects.get_valid()
    self.assertEqual(refreshed.access_token, "refreshed-cached-token")


@patch("django_ctct.models.Token.decode")
class ContactListManagerTests(RequestsMockMixin[ContactList], TestCase):
  """Tests for ContactListRemoteManager.add_list_memberships()."""

  model = ContactList

  def test_add_list_memberships(self, token_decode: MagicMock) -> None:
    token_decode.return_value = True

    contacts = get_factory(Contact).create_batch(2)
    contact_pks = [c.pk for c in contacts]

    self.mock_api.post(
      url=self.model.remote.get_url(endpoint="/activities/add_list_memberships"),
      status_code=201,
      json={},
    )

    self.model.remote.add_list_memberships(
      contact_list=self.existing_obj,
      contacts=Contact.objects.filter(pk__in=contact_pks),
    )

    self.assertEqual(self.mock_api.call_count, 1)

  def test_add_list_memberships_with_multiple_lists(
    self,
    token_decode: MagicMock,
  ) -> None:
    token_decode.return_value = True

    contacts = get_factory(Contact).create_batch(2)
    contact_pks = [c.pk for c in contacts]

    self.mock_api.post(
      url=self.model.remote.get_url(endpoint="/activities/add_list_memberships"),
      status_code=201,
      json={},
    )

    self.model.remote.add_list_memberships(
      contact_lists=self.model.objects.all(),
      contacts=Contact.objects.filter(pk__in=contact_pks),
    )

    self.assertEqual(self.mock_api.call_count, 1)

  def test_add_list_memberships_requires_list_and_contacts(
    self,
    token_decode: MagicMock,
  ) -> None:
    token_decode.return_value = True

    with self.assertRaises(ValueError):
      self.model.remote.add_list_memberships(contacts=Contact.objects.all())

    with self.assertRaises(ValueError):
      self.model.remote.add_list_memberships(contact_list=self.existing_obj)


@patch("django_ctct.models.Token.decode")
class ContactManagerTests(RequestsMockMixin[Contact], TestCase):
  """Tests for ContactRemoteManager's conflict-handling behavior."""

  model = Contact

  def test_create_falls_back_to_update_or_create_on_conflict(
    self,
    token_decode: MagicMock,
  ) -> None:
    """A 409 from CTCT (duplicate email) should trigger update_or_create()."""

    token_decode.return_value = True

    obj = self.factory.build(api_id=None)

    # Simulate CTCT rejecting the create because the email already exists
    self.mock_api.post(
      url=self.model.remote.get_url(),
      status_code=409,
      json={"error_message": "Contact already exists"},
    )
    # update_or_create() posts to the sign_up_form endpoint instead
    self.mock_api.post(
      url=self.model.remote.get_url(endpoint_suffix="/sign_up_form"),
      status_code=200,
      json={"action": "created", "contact_id": str(uuid4())},
    )

    obj.save()
    obj = self.model.remote.create(obj)  # type: ignore[misc]

    self.assertIsNotNone(obj.api_id)
    self.assertEqual(self.mock_api.call_count, 2)

  def test_create_reraises_non_conflict_http_errors(
    self,
    token_decode: MagicMock,
  ) -> None:
    """Non-409 HTTP errors should propagate, not fall back silently."""

    token_decode.return_value = True

    obj = self.factory.build(api_id=None)
    obj.save()

    self.mock_api.post(
      url=self.model.remote.get_url(),
      status_code=400,
      json={"error_message": "Bad request"},
    )

    with self.assertRaises(HTTPError):
      self.model.remote.create(obj)  # type: ignore[misc]

  def test_update_or_create_unexpected_response_raises(
    self,
    token_decode: MagicMock,
  ) -> None:
    """Extra unexpected fields returned from sign_up_form should raise ValueError."""

    token_decode.return_value = True

    obj = self.factory.create(api_id=None)
    self.mock_api.post(
      url=self.model.remote.get_url(endpoint_suffix="/sign_up_form"),
      status_code=200,
      json={"action": "created", "contact_id": str(uuid4()), "unexpected_field": "foo"},
    )
    with self.assertRaises(ValueError):
      self.model.remote.update_or_create(obj)


@patch("django_ctct.models.Token.decode")
class EmailCampaignAndActivityManagerTests(TestCase):
  """Tests for EmailCampaign and CampaignActivity manager edge cases."""

  def setUp(self) -> None:
    Token.objects._cached_token = None
    self.mock_api = requests_mock.Mocker()
    self.mock_api.start()
    self.addCleanup(self.mock_api.stop)
    TokenFactory.create()

  def test_email_campaign_create_with_existing_activity(
    self,
    token_decode: MagicMock,
  ) -> None:
    token_decode.return_value = True

    campaign = get_factory(EmailCampaign).create()
    primary_activity = campaign.campaign_activities.get(role="primary_email")
    api_id = uuid4()
    self.mock_api.post(
      url=EmailCampaign.remote.get_url(),
      status_code=201,
      json={
        "campaign_id": str(campaign.api_id or api_id),
        "name": campaign.name,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "campaign_activities": [
          {
            "campaign_activity_id": str(primary_activity.api_id or api_id),
            "role": "primary_email",
          }
        ],
      },
    )
    res = EmailCampaign.remote.create(campaign)  # type: ignore[misc]
    self.assertEqual(res.pk, campaign.pk)

  def test_email_campaign_update_requires_api_id(
    self,
    token_decode: MagicMock,
  ) -> None:
    token_decode.return_value = True
    campaign = get_factory(EmailCampaign).create(api_id=None)
    with self.assertRaises(ValueError):
      EmailCampaign.remote.update(campaign)

  def test_campaign_activity_create_raises_not_implemented(
    self,
    token_decode: MagicMock,
  ) -> None:
    token_decode.return_value = True
    activity = get_factory(CampaignActivity).create()
    with self.assertRaises(NotImplementedError):
      CampaignActivity.remote.create(activity)

  def test_campaign_activity_send_preview_with_callables(
    self,
    token_decode: MagicMock,
  ) -> None:
    token_decode.return_value = True
    activity = get_factory(CampaignActivity).create()

    self.mock_api.post(
      url=CampaignActivity.remote.get_url(activity.api_id, endpoint_suffix="/tests"),
      status_code=200,
      json={},
    )
    with self.settings(
      CTCT_PREVIEW_RECIPIENTS_CALLABLE="django_ctct.models.campaign_activity__from_email__default",
      CTCT_PREVIEW_MESSAGE_CALLABLE="django_ctct.models.campaign_activity__from_name__default",
    ):
      with (
        patch(
          "django_ctct.models.campaign_activity__from_email__default",
          return_value=["custom@example.com"],
        ),
        patch(
          "django_ctct.models.campaign_activity__from_name__default",
          return_value="Custom Message",
        ),
      ):
        CampaignActivity.remote.send_preview(activity)

    self.assertEqual(self.mock_api.call_count, 1)

  def test_campaign_summary_serialize(self, token_decode: MagicMock) -> None:
    summary = get_factory(CampaignSummary).create()
    data = CampaignSummary.remote.serialize(summary, field_types="all")
    self.assertIn("unique_counts", data)

  def test_remote_manager_get_404_raises_does_not_exist(
    self,
    token_decode: MagicMock,
  ) -> None:
    token_decode.return_value = True
    fake_id = uuid4()
    self.mock_api.get(
      url=Contact.remote.get_url(fake_id),
      status_code=404,
    )
    with self.assertRaises(Contact.DoesNotExist):
      Contact.remote.get(fake_id)
