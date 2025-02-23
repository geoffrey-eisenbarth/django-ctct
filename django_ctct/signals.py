from typing import Type, Optional

from django.conf import settings
from django.db.models import Model

from django_ctct.models import (
  CTCTModel, Contact, ContactList, ContactAndContactList,
)


def remote_save(
  sender: Type[Model],
  instance: Model,
  created: bool,
  update_fields: Optional[list],
  **kwargs,
) -> None:
  """Create or update the instance on CTCT servers."""

  if isinstance(instance, CTCTModel) and instance.save_to_ctct:
    sender.remote.connect()
    if instance.api_id:
      task = sender.remote.update
    else:
      task = sender.remote.create

    if getattr(instance, 'enqueue', settings.CTCT_ENQUEUE_DEFAULT):
      task.enqueue(obj=instance)
    else:
      task(obj=instance)


def remote_delete(sender, instance, **kwargs) -> None:
  """Delete the instance from CTCT servers."""

  if isinstance(instance, CTCTModel) and instance.save_to_ctct:
    sender.remote.connect()
    task = sender.remote.delete

    if getattr(instance, 'enqueue', settings.CTCT_ENQUEUE_DEFAULT):
      task.enqueue(obj=instance)
    else:
      task(obj=instance)


def remote_update_m2m(sender, instance, action, **kwargs):
  """Updates a Contact's list membership on CTCT servers."""

  actions = ['post_add', 'post_remove', 'post_clear']
  if (sender is ContactAndContactList) and (action in actions):

    if isinstance(instance, Contact):
      Contact.remote.connect()
      task = Contact.remote.update
      kwargs = {'obj': instance}
    elif isinstance(instance, ContactList):
      ContactList.remote.connect()
      task = ContactList.remote.add_list_memberships
      kwargs = {
        'contact_list': instance,
        'contacts': Contact.objects.filter(pk__in=kwargs['pk_set']),
      }

    if getattr(instance, 'enqueue', settings.CTCT_ENQUEUE_DEFAULT):
      task.enqueue(**kwargs)
    else:
      task(**kwargs)
