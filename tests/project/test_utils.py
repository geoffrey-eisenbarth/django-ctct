import datetime as dt
from django.test import TestCase
from django.utils import timezone

from django_ctct.utils import to_dt


class DatetimeUtilityTests(TestCase):
  """Tests for the to_dt utlity function."""

  def test_standard_iso_format(self):
    result = to_dt('2023-10-27T15:30:00Z')
    self.assertTrue(timezone.is_aware(result))
    self.assertEqual(
      result.replace(tzinfo=None),
      dt.datetime(2023, 10, 27, 15, 30, 0),
    )

  def test_with_milliseconds_stripping(self):
    result = to_dt('2023-11-01T08:45:12.987654Z')
    self.assertTrue(timezone.is_aware(result))
    self.assertEqual(
      result.replace(tzinfo=None),
      dt.datetime(2023, 11, 1, 8, 45, 12),
    )

  def test_end_of_year_timestamp(self):
    result = to_dt('2023-12-31T23:59:59Z')
    self.assertEqual(
      result.replace(tzinfo=None),
      dt.datetime(2023, 12, 31, 23, 59, 59)
    )

  def test_custom_format_without_milliseconds(self):
    result = to_dt('01/15/2024 10:00:00', ts_format='%m/%d/%Y %H:%M:%S')
    self.assertEqual(
      result.replace(tzinfo=None),
      dt.datetime(2024, 1, 15, 10, 0, 0),
    )

  def test_custom_format_with_milliseconds_handling(self):
    result = to_dt('2024-02-20 14:00:00.123456', ts_format='%Y-%m-%d %H:%M:%S')
    self.assertEqual(
      result.replace(tzinfo=None),
      dt.datetime(2024, 2, 20, 14, 0, 0),
    )
