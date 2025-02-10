from typing import Literal, Optional

import requests_mock

from django_ctct.models import CTCTModel


class MockConstantContactAPI:

  def __init__(self):
    self.mocker = requests_mock.Mocker()

  def start(self):
    self.mocker.start()

  def stop(self):
    self.mocker.stop()

  def mock(
    self,
    endpoint: str,
    method: Literal['GET', 'POST', 'PUT', 'DELETE'],
    status_code: int,
    id: Optional[str] = None,
  ):
    url = f'{CTCTModel.BASE_URL}{endpoint}'
    if id is not None:
      url = f'{url}/{id}'

    self.mocker.request(
      url=url,
      method=method,
      status_code=status_code,
      json=self.RESPONSES[endpoint][method][status_code],
    )

  # https://developer.constantcontact.com/api_reference/index.html
  RESPONSES = {
    '/contact_lists': {
      'GET': {
        201: {
        },
      },
      'POST': {
        201: {
          "list_id": "06526938-56dd-11e9-932a-fa163ea075fa",
          "name": "Multiple purchases",
          "description": "List of repeat customers.",
          "favorite": False,
          "created_at": "2016-01-23T13:48:44.108Z",
          "updated_at": "2016-03-03T10:56:29-05:00",
          "deleted_at": "2016-03-03T10:56:29-05:00"
        },
      },
      'PUT': {
        201: {
        },
      },
      'DELETE': {
        201: {},
      },
    },
    '/contact_custom_fields': {
    },
    '/contacts': {
      'GET': {
        201: {
        },
      },
      'POST': {
        201: {
        },
      },
      'PUT': {
        201: {
        },
      },
      'DELETE': {
        201: {}
      },
    },
    '/contacts/sign_up_form': {
      'POST': {
        201: {

        },
      },
    },
    '/emails': {
    },
    '/emails/activities': {
    }
  }
