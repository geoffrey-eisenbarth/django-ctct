import sys
from django.test import TestCase, override_settings
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_ctct.apps import CTCTConfig


REQUIRED_SETTINGS = {
  'CTCT_PUBLIC_KEY': 'TEST_PK',
  'CTCT_SECRET_KEY': 'TEST_SK',
  'CTCT_REDIRECT_URI': 'http://test.com/django-ctct/auth/',
  'CTCT_FROM_NAME': 'Test User',
  'CTCT_FROM_EMAIL': 'test@example.com',
}


class TestConfigReady(TestCase):

  def test_ready_with_all_required_settings(self):
    with override_settings(**REQUIRED_SETTINGS):
      config = CTCTConfig('django_ctct', sys.modules[__name__])
      config.ready()

  def test_ready_with_missing_required_setting(self):
    for missing_setting in REQUIRED_SETTINGS:
      with self.subTest(missing_setting=missing_setting):
        with override_settings(**REQUIRED_SETTINGS):
          delattr(settings, missing_setting)
          config = CTCTConfig('django_ctct', sys.modules[__name__])
          with self.assertRaises(ImproperlyConfigured):
            config.ready()
