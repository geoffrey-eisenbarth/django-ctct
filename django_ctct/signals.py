from typing import Any, Type

from django.conf import settings
from django.db.models import Model

from django_ctct.models import CTCTEndpointModel


def remote_save(sender: Type[Model], instance: Model, **kwargs: Any) -> None:
  """Create or update the instance on CTCT servers."""

  if (
    issubclass(sender, CTCTEndpointModel) and
    isinstance(instance, CTCTEndpointModel)
  ):
    if instance.api_id:
      task = sender.remote.update
    else:
      task = sender.remote.create

    enqueue = getattr(settings, 'CTCT_ENQUEUE_DEFAULT', False)
    if getattr(instance, 'enqueue', enqueue) and hasattr(task, 'enqueue'):
      task.enqueue(obj=instance)
    else:
      task(obj=instance)


def remote_delete(sender: Type[Model], instance: Model, **kwargs: Any) -> None:
  """Delete the instance from CTCT servers."""

  if (
    issubclass(sender, CTCTEndpointModel) and
    isinstance(instance, CTCTEndpointModel)
  ):
    task = sender.remote.delete

    enqueue = getattr(settings, 'CTCT_ENQUEUE_DEFAULT', False)
    if getattr(instance, 'enqueue', enqueue) and hasattr(task, 'enqueue'):
      task.enqueue(obj=instance)
    else:
      task(obj=instance)
