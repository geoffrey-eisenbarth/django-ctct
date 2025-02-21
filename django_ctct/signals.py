from typing import Type, Optional

from django.db.models import Model

from django_ctct.models import CTCTModel, CTCTLocalModel, Contact, ContactList
from django_ctct.tasks import (
  ctct_save_task, ctct_delete_task,
  ctct_update_lists_task, ctct_add_list_memberships_task,
)


def ctct_save(
  sender: Type[Model],
  instance: Model,
  created: bool,
  update_fields: Optional[list],
  **kwargs,
) -> None:
  """Create or update the instance on CTCT servers."""
  if isinstance(instance, CTCTLocalModel):
    return
  elif isinstance(instance, CTCTModel) and instance.save_to_ctct:
    sender.remote.connect()
    if instance.api_id:
      method = sender.remote.update
    else:
      method = sender.remote.create

    if instance.save_to_ctct == 'sync':
      method(instance)
    elif instance.save_to_ctct == 'async':
      method(instance).enqueue()


def ctct_delete(sender, instance, **kwargs) -> None:
  """Delete the instance from CTCT servers."""
  if isinstance(instance, CTCTLocalModel):
    return
  elif isinstance(instance, CTCTModel) and instance.save_to_ctct:
    sender.remote.connect()
    method = sender.remote.delete

    if instance.save_to_ctct == 'sync':
      method(instance)
    elif instance.save_to_ctct == 'async':
      method(instance).enqueue()


def ctct_update_contact_lists(sender, instance, action, **kwargs):
  """Updates a Contact's list membership on CTCT servers."""

  if action in ['post_add', 'post_remove', 'post_clear']:

    if isinstance(instance, Contact):
      if instance.save_to_ctct == 'sync':
        ctct_update_lists_task(instance)
      elif instance.save_to_ctct == 'async':
        ctct_update_lists_task.delay(instance)

    elif isinstance(instance, ContactList):
      contacts = Contact.objects.filter(pk__in=kwargs['pk_set'])
      if instance.save_to_ctct == 'async':
        ctct_add_list_memberships_task(instance, contacts)
      elif instance.save_to_ctct == 'async':
        ctct_add_list_memberships_task.delay(instance, contacts)


# @task
# def ctct_update_lists_task(contact: 'Contact') -> None:
#   """Update ContactLists on CTCT, or delete Contact if no ContactLists.
#
#   Notes
#   -----
#   Due to the way Django admin saves related models, I haven't been able
#   to determine a good way to address this other than just delaying this
#   method for a few minutes.
#
#   The primary issue is that we want to make sure that a Contact.ctct_save()
#   call isn't made after this call, since that will revive the Contact in
#   the event that they had been deleted from CTCT servers due to no longer
#   belonging to any ContactLists (CTCT requires that Contacts must belong to
#   at least one ContactList).
#
#   """
#
#   if contact.api_id is not None:
#     sleep(60 * 1)  # 1 minute
#     contact.ctct_update_lists()
#
#
# @task
# def ctct_add_list_memberships_task(
#   contact_list: 'ContactList',
#   contacts: QuerySet['Contact'],
# ) -> None:
#   contact_list.ctct_add_list_memberships(contacts)
