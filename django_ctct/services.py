from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from typing import Any

  from django.db.models import Model

  from django_ctct.models import EmailCampaign


def sync_campaign_for_host(
  host_obj: Model,
  cleaned_data: dict[str, Any],
  campaign_field_name: str,
) -> EmailCampaign | None:
  """
  1. Creates or updates the local EmailCampaign instance linked to host_obj.
  2. Creates or updates the primary_email CampaignActivity instance.
  3. Pushes creation/updates to Constant Contact API via
     EmailCampaign.remote.create()/update() and
     CampaignActivity.remote.update().
  4. Saves the foreign key relation on host_obj if not already set.
  """
  return None
