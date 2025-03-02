import datetime as dt

import factory
import factory.fuzzy
from factory.django import DjangoModelFactory

from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import formats

from django_ctct import models as ctct_models
from django_ctct.managers import TokenRemoteManager


class UserFactory(factory.django.DjangoModelFactory):
  class Meta:
    model = get_user_model()

  username = factory.Faker('user_name')
  email = factory.Sequence(lambda n: f'user{n}@example.com')
  first_name = factory.Faker('first_name')
  last_name = factory.Faker('last_name')
  password = factory.django.Password('pw')


class TokenFactory(factory.django.DjangoModelFactory):
  class Meta:
    model = ctct_models.Token

  access_token = factory.Faker('text', max_nb_chars=1200)
  refresh_token = factory.Faker('pystr', max_chars=50)
  scope = TokenRemoteManager.API_SCOPE


class ContactListFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.ContactList

  name = factory.Sequence(lambda n: f'Contact List {n}')
  description = factory.Faker('sentence')
  favorite = False


class CustomFieldFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.CustomField

  label = factory.Sequence(lambda n: f'Custom Field {n}')
  type = 'string'


# TODO: list_memberships
class ContactFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.Contact

  email = factory.Sequence(lambda n: f'contact{n}@example.com')
  first_name = factory.Faker('first_name')
  last_name = factory.Faker('last_name')
  job_title = factory.Faker('job')
  company_name = factory.Faker('company')


class ContactNoteFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.ContactNote

  author = factory.SubFactory(UserFactory)
  contact = factory.SubFactory(ContactFactory)
  content = factory.Faker('sentence')


class ContactPhoneNumberFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.ContactPhoneNumber

  contact = factory.SubFactory(ContactFactory)
  kind = factory.fuzzy.FuzzyChoice(
    _[0] for _ in ctct_models.ContactPhoneNumber.KINDS
  )
  phone_number = factory.Faker('phone_number')


class ContactStreetAddressFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.ContactStreetAddress

  contact = factory.SubFactory(ContactFactory)
  kind = factory.fuzzy.FuzzyChoice(
    _[0] for _ in ctct_models.ContactStreetAddress.KINDS
  )
  street = factory.Faker('street_address')
  city = factory.Faker('city')
  state = factory.Faker('state_abbr')
  postal_code = factory.Faker('postcode')
  country = factory.Faker('country')


class ContactCustomFieldFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.ContactCustomField

  contact = factory.SubFactory(ContactFactory)
  custom_field = factory.SubFactory(CustomFieldFactory)
  value = factory.Faker('word')


class EmailCampaignFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.EmailCampaign

  name = factory.Sequence(lambda n: f'Email Campaign {n}')


# TODO: contact_lists
class CampaignActivityFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.CampaignActivity

  campaign = factory.SubFactory(EmailCampaignFactory)
  subject = factory.Faker('sentence')
  preheader = factory.Faker('sentence')
  html_content = factory.Faker('text')
