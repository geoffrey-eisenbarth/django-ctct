from unittest.mock import MagicMock, patch
from uuid import uuid4

import requests_mock
from django.http import Http404
from django.test import TestCase
from jwt import ExpiredSignatureError
from requests.exceptions import HTTPError

from django_ctct.models import Contact, ContactList, JsonDict, Token
from tests.factories import TokenFactory, get_factory
from tests.project.test_models import RequestsMockMixin


class TokenRemoteManagerTest(TestCase):
  """Tests for TokenRemoteManager's full auth lifecycle (create/get/update)."""

  def setUp(self) -> None:
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

  @patch("django_ctct.models.Token.decode")
  def test_get_refreshes_expired_token(self, token_decode: MagicMock) -> None:
    """Token.remote.get() should refresh an expired Token automatically."""

    TokenFactory.create()
    token_decode.side_effect = ExpiredSignatureError

    self.mock_api.post(
      url=Token.remote.get_url(),
      status_code=200,
      json=self.get_token_response(),
    )

    token = Token.remote.get()

    self.assertEqual(Token.objects.count(), 2)
    self.assertEqual(token.access_token, "new-access-token")

  def test_get_raises_without_existing_token(self) -> None:
    with self.assertRaises(ValueError):
      Token.remote.get()

  def test_get_rejects_kwargs(self) -> None:
    TokenFactory.create()
    with self.assertRaises(TypeError):
      Token.remote.get(foo="bar")  # type: ignore[misc]

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
