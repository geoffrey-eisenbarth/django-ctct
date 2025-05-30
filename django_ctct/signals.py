from typing import Type, Literal

from django.conf import settings
from django.db.models import Model

from django_ctct.models import (
  CTCTRemoteModel, Contact, ContactList, CampaignActivity
)


def remote_save(sender: Type[Model], instance: Model, **kwargs) -> None:
  """Create or update the instance on CTCT servers."""

  if isinstance(instance, CTCTRemoteModel):
    sender.remote.connect()
    if instance.api_id:
      task = sender.remote.update
    else:
      task = sender.remote.create

    if getattr(instance, 'enqueue', settings.CTCT_ENQUEUE_DEFAULT):
      task.enqueue(obj=instance)
    else:
      task(obj=instance)


def remote_delete(sender: Type[Model], instance: Model, **kwargs) -> None:
  """Delete the instance from CTCT servers."""

  if isinstance(instance, CTCTRemoteModel):
    sender.remote.connect()
    task = sender.remote.delete

    if getattr(instance, 'enqueue', settings.CTCT_ENQUEUE_DEFAULT):
      task.enqueue(obj=instance)
    else:
      task(obj=instance)


# TODO: Wait, since we're using PUT and specifing list_memberships, we should
#       not be testing here. And probably shouldn't have a m2m_changed signal
def remote_update_m2m(
  sender: Type[Model],
  instance: Model,
  action: Literal['pre_add', 'post_add', 'pre_remove', 'post_remove', 'pre_clear', 'post_clear'],  # noqa: E501
  **kwargs,
):
  """Updates a Contact's list membership on CTCT servers."""

  actions = ['post_add', 'post_remove', 'post_clear']
  senders = [
    Contact.list_memberships.through,
    ContactList.members.through,
    CampaignActivity.contact_lists.through,
    ContactList.campaign_activities.through,
  ]

  if (sender in senders) and (action in actions):

    if isinstance(instance, (Contact, CampaignActivity)):
      # Just update the instance using PUT
      task_name = 'update'
      kwargs = {'obj': instance}
    elif isinstance(instance, ContactList):
      # Must use special methods defined on the remote manager
      if sender is ContactList.members.through:
        task_name = 'add_list_memberships'
        kwargs = {
          'contact_list': instance,
          'contacts': Contact.objects.filter(pk__in=kwargs['pk_set']),
        }
      elif sender is ContactList.campaign_activities.through:
        raise NotImplementedError

    instance._meta.model.remote.connect()
    task = getattr(instance._meta.model.remote, task_name)
    if getattr(instance, 'enqueue', settings.CTCT_ENQUEUE_DEFAULT):
      task.enqueue(**kwargs)
    else:
      task(**kwargs)
