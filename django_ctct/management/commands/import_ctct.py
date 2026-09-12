from argparse import ArgumentParser
from collections import defaultdict
from collections.abc import Collection
from typing import TYPE_CHECKING, cast
from uuid import UUID

from django.core.management.base import BaseCommand
from django.db.models import Model
from django.utils.translation import gettext as _
from tqdm import tqdm

from django_ctct.models import (
  CampaignActivity,
  CampaignSummary,
  Contact,
  ContactCustomField,
  ContactList,
  CTCTEndpointModel,
  CTCTModel,
  CustomField,
  EmailCampaign,
  RelatedObjects,
)
from django_ctct.utils import get_related_fields


if TYPE_CHECKING:
  from typing import Any


class Command(BaseCommand):
  """Imports django-ctct model instances from CTCT servers.

  Notes
  -----
  CTCT does not provide an endpoint for fetching bulk CampaignActivities.
  As a result, we must loop through the EmailCampaigns, make a request to get
  the associated CampaignActivities, and then make a second request to get the
  details of the CampaignActivity.

  As a result, importing CampaignActivities will be slow, and running it
  multiple times may result in exceeding CTCT's 10,000 requests per day
  limit.

  """

  help = "Imports data from ConstantContact"

  CTCT_MODELS: list[type[CTCTEndpointModel]] = [
    ContactList,
    CustomField,
    Contact,
    EmailCampaign,
    CampaignActivity,
    CampaignSummary,
  ]

  def upsert[M: Model](
    self,
    model: type[M],
    objs: list[M],
    update_conflicts: bool = True,
    unique_fields: Collection[str] | None = ["api_id"],
    update_fields: Collection[str] | None = None,
    silent: bool | None = None,
  ) -> list[M]:
    """Perform upsert using `bulk_create()`."""

    verb = "Imported" if (update_fields is None) else "Updated"
    if silent is None:
      silent = self.noinput

    if model._meta.auto_created and hasattr(model, "contactlist_id"):
      # Delete existing through model instances
      model.objects.all().delete()  # type: ignore
      update_conflicts = False
      unique_fields = update_fields = None
    elif issubclass(model, ContactCustomField):
      update_conflicts = True
      unique_fields = ["contact_id", "custom_field_id"]
      update_fields = ["value"]
    elif issubclass(model, CampaignSummary):
      update_conflicts = True
      unique_fields = ["campaign_id"]
      update_fields = model.API_READONLY_FIELDS[1:]
    elif update_fields is None:
      update_fields = [
        f.name for f in model._meta.fields if not f.primary_key and (f.name != "api_id")
      ]

    # Remove possible duplicates (CTCT API can't be trusted)
    id_field: str | None = None
    if model is CampaignSummary:
      id_field = "campaign_id"
    elif issubclass(model, CTCTModel):
      id_field = "api_id"

    if id_field is not None:
      seen, unique_objs = set(), []
      for obj in objs:
        if getattr(obj, id_field) not in seen:
          seen.add(getattr(obj, id_field))
          unique_objs.append(obj)
    else:
      unique_objs = objs

    # Perform the upsert
    objs_w_pks = model._default_manager.bulk_create(
      objs=unique_objs,
      update_conflicts=update_conflicts,
      unique_fields=unique_fields,
      update_fields=update_fields,
    )

    # Inform the user
    if not silent:  # pragma: no cover
      message = self.style.SUCCESS(f"{verb} {len(objs):,} {model.__name__} instances.")
      self.stdout.write(message)

    return objs_w_pks

  def set_related_object_pks[E: CTCTEndpointModel, M: Model](
    self,
    model: type[E],
    objs_w_pks: list[M],
    per_obj_list_of_related_objs: list[list[RelatedObjects]],
  ) -> None:
    _, mtms, _, rfks = get_related_fields(model)
    field_name = {
      field.remote_field.through: field.m2m_field_name() for field in mtms
    } | {field.related_model: field.remote_field.name for field in rfks}

    for obj_w_pk, list_of_related_objs in zip(
      objs_w_pks,
      per_obj_list_of_related_objs,
    ):
      for related_model, related_objs in list_of_related_objs:
        for related_obj in related_objs:
          setattr(related_obj, field_name[related_model], obj_w_pk)

  def import_model[E: CTCTEndpointModel](self, model: type[E]) -> None:
    """Imports objects from CTCT into Django's database."""

    list_of_tuples: list[tuple[E, list[RelatedObjects]]]
    objs: list[E]
    per_obj_list_of_related_objs: list[list[RelatedObjects]]

    if model is CampaignActivity:
      # CampaignActivities do not have a bulk API endpoint
      return self.import_campaign_activities()

    try:
      # Split apart so we can save objs to db and get pks
      list_of_tuples = model.remote.all()
      objs, per_obj_list_of_related_objs = zip(*list_of_tuples)  # type: ignore[assignment]  # noqa: E501
    except ValueError:
      # No values returned
      return

    # Upsert models to get Django pks
    objs_w_pks = self.upsert(model, objs)

    # Set Django object PK on related objects
    if any(per_obj_list_of_related_objs):
      self.set_related_object_pks(
        model,
        objs_w_pks,
        per_obj_list_of_related_objs,
      )

    # Reshape related_objs for efficiency
    dict_of_related_objs = defaultdict(list)
    for list_of_related_objs in per_obj_list_of_related_objs:
      for related_model, related_objs in list_of_related_objs:
        dict_of_related_objs[related_model].extend(related_objs)

    # Upsert related_obj_list
    for related_model, related_objs in dict_of_related_objs.items():
      self.upsert(related_model, related_objs)

  def import_campaign_activities(self) -> None:
    """CampaignActivities must be imported one at a time."""

    # First, make sure all CampaignActivity API id's are stored locally
    for campaign in EmailCampaign.objects.exclude(api_id__isnull=True):
      # Fetch from API
      assert isinstance(campaign.api_id, UUID)
      try:
        _, list_of_related_objs = EmailCampaign.remote.get(campaign.api_id)
      except EmailCampaign.DoesNotExist:
        # Available in bulk endpoint but not detail endpoint
        continue
      else:
        # Set related object pk and store in db
        for related_model, objs in list_of_related_objs:
          if issubclass(related_model, CampaignActivity):
            for obj in cast(list[CampaignActivity], objs):
              if obj.role == "primary_email":
                obj.campaign_id = campaign.pk
                obj.save()

    # Then, fetch CampaignActivity details
    activities = CampaignActivity.objects.filter(
      role="primary_email",
      api_id__isnull=False,
    )

    objs_w_pks = []
    per_obj_list_of_related_objs = []

    for activity in tqdm(activities, disable=self.noinput):
      assert isinstance(activity.api_id, UUID)
      try:
        obj, list_of_related_objs = CampaignActivity.remote.get(activity.api_id)  # noqa: E501
      except CampaignActivity.DoesNotExist:
        # Came from EmailCampaign detail endpoint but doesn't exist elsewhere
        continue
      else:
        obj.pk = activity.pk
        obj.campaign_id = activity.campaign_id
        objs_w_pks.append(obj)
        per_obj_list_of_related_objs.append(list_of_related_objs)

    # Upsert objects to update fields
    self.upsert(
      model=CampaignActivity,
      objs=objs_w_pks,
      unique_fields=["campaign_id", "role"],
      update_fields=["role", "subject", "preheader", "html_content"],
    )

    # Set Django object PK on related objects
    if any(per_obj_list_of_related_objs):
      self.set_related_object_pks(
        CampaignActivity,
        objs_w_pks,
        per_obj_list_of_related_objs,
      )

    # Reshape related_objs for efficiency
    dict_of_related_objs = defaultdict(list)
    for list_of_related_objs in per_obj_list_of_related_objs:
      for related_model, related_objs in list_of_related_objs:
        dict_of_related_objs[related_model].extend(related_objs)

    # Upsert related_obj_list
    for related_model, related_objs in dict_of_related_objs.items():
      self.upsert(related_model, related_objs)

  def add_arguments(self, parser: ArgumentParser) -> None:
    """Allow optional keyword arguments."""

    parser.add_argument(
      "--noinput",
      action="store_true",
      default=False,
      help="Automatic yes to prompts",
    )
    parser.add_argument(
      "--stats_only",
      action="store_true",
      default=False,
      help="Only fetch EmailCampaign statistics",
    )

  def handle(self, *args: Any, **kwargs: Any) -> None:
    """Primary access point for Django management command."""

    self.noinput = kwargs["noinput"]
    self.stats_only = kwargs["stats_only"]

    if self.stats_only:
      self.CTCT_MODELS = [CampaignSummary]

    for model in self.CTCT_MODELS:
      if model is CampaignActivity:
        note = "Note: This will result in 1 API request per EmailCampaign! "
      else:
        note = ""
      question = _(f"Import {model.__name__}? {note}(y/n): ")

      if self.noinput or (input(question).lower()[0] == "y"):
        self.import_model(model)
      else:  # pragma: no cover
        message = _(f"Skipping {model.__name__}")
        self.stdout.write(self.style.NOTICE(message))
