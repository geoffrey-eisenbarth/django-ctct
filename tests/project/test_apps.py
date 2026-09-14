from django.conf import settings
from django.test import TestCase, override_settings

from django_ctct.apps import check_required_settings
from django_ctct.conf import REQUIRED_SETTINGS

TEST_SETTINGS = {
  "CTCT_PUBLIC_KEY": "TEST_PK",
  "CTCT_SECRET_KEY": "TEST_SK",
  "CTCT_REDIRECT_URI": "http://test.com/django-ctct/auth/",
  "CTCT_FROM_NAME": "Test User",
  "CTCT_FROM_EMAIL": "test@example.com",
}


class RequiredSettingsCheckTests(TestCase):
  def test_check_with_all_required_settings(self) -> None:
    with override_settings(**TEST_SETTINGS):
      self.assertEqual(check_required_settings(), [])

  def test_check_with_missing_required_setting(self) -> None:
    for missing_setting in REQUIRED_SETTINGS:
      with self.subTest(missing_setting=missing_setting):
        with override_settings(**TEST_SETTINGS):
          delattr(settings, missing_setting)
          errors = check_required_settings()
          self.assertEqual(len(errors), 1)
          self.assertEqual(errors[0].id, "django_ctct.E001")
          self.assertIn(missing_setting, errors[0].msg)
