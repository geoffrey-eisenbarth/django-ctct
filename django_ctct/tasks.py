from __future__ import annotations

from time import sleep
from typing import Optional

from django.db.models import QuerySet

from django_rq import job


"""
Notes
-----

Not sure why, but this seems to be the cleanest method of turning class methods
into jobs. While preliminary testing did work for CTCTModel.ctct_save(), it
didn't work for other class methods, so we decided to put them all together
here.

If you want to avoid writing to CTCT servers (via the ctct_save method), you can
use `bulk_create` or `bulk_update`.

"""


@job
def ctct_save_job(obj: 'CTCTModel') -> Optional[dict]:
  if obj._id:
    ctct_obj = obj.ctct_save()

    # Update locally
    if obj._meta.object_name == 'CampaignActivity':
      # CampaignActivity.ctct_save addresses this
      pass
    elif ctct_obj:
      model = obj._meta.model
      obj = model.from_ctct(ctct_obj, save=True)

    return ctct_obj
  else:
    message = (
      "Object must be saved locally first!"
    )
    raise ValueError(message)


@job
def ctct_delete_job(obj: 'CTCTModel') -> None:
  obj.ctct_delete()


@job
def ctct_rename_job(obj: 'EmailCampaign') -> None:
  obj.ctct_rename()


@job
def ctct_update_lists_job(contact: 'Contact') -> None:
  """Update ContactLists on CTCT, or delete Contact if no ContactLists.

  Notes
  -----
  Due to the way Django admin saves related models, I haven't been able
  to determine a good way to address this other than just delaying this
  method for a few minutes.

  The primary issue is that we want to make sure that a Contact.ctct_save()
  call isn't made after this call, since that will revive the Contact in
  the event that they had been deleted from CTCT servers due to no longer
  belonging to any ContactLists (CTCT requires that Contacts must belong to
  at least one ContactList).

  """

  if contact.id is not None:
    sleep(60 * 1)  # 1 minute
    contact.ctct_update_lists()


@job
def ctct_add_list_memberships_job(
  contact_list: 'ContactList',
  contacts: QuerySet['Contact'],
) -> None:
  contact_list.ctct_add_list_memberships(contacts)
