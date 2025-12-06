from typing import Type, TypeVar, Generic, Any, cast

import factory
import factory.fuzzy
from factory.django import DjangoModelFactory
from faker import Faker as RealFaker

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from django.db.models import Model

from django_ctct.models import (
  CTCTModel, Token, ContactList, CustomField, Contact,
  ContactNote, ContactPhoneNumber, ContactStreetAddress, ContactCustomField,
  EmailCampaign, CampaignActivity, CampaignSummary,
)


NUM_RELATED_OBJS: dict[Type[CTCTModel], int] = {
  Contact: 2,
  EmailCampaign: 1,
}


M = TypeVar('M', bound=Model)
U = TypeVar('U', bound=AbstractUser)


def get_factory(model: Type[M]) -> Type[DjangoModelFactory[M]]:
  return cast(Type[DjangoModelFactory[M]], FACTORIES[model])


class UserFactory(DjangoModelFactory[U]):
  class Meta:
    model = get_user_model()

  username = factory.Sequence(lambda n: f'user{n}')
  email = factory.Sequence(lambda n: f'user{n}@example.com')
  first_name = factory.Faker('first_name')
  last_name = factory.Faker('last_name')
  password = factory.django.Password('pw')  # type: ignore[attr-defined]


class TokenFactory(DjangoModelFactory[Token]):
  class Meta:
    model = Token

  access_token = factory.Faker('pystr', max_chars=1200)
  refresh_token = factory.Faker('pystr', max_chars=50)
  scope = Token.API_SCOPE


class CTCTModelFactory(DjangoModelFactory[M], Generic[M]):
  api_id = factory.Faker('uuid4')


class ContactListFactory(CTCTModelFactory[ContactList]):
  class Meta:
    model = ContactList

  name = factory.Sequence(lambda n: f'Contact List {n}')
  description = factory.Faker('sentence')
  favorite = False


class CustomFieldFactory(CTCTModelFactory[CustomField]):
  class Meta:
    model = CustomField

  label = factory.Sequence(lambda n: f'Custom Field {n}')
  type = 'string'


class ContactFactory(CTCTModelFactory[Contact]):
  class Meta:
    model = Contact

  email = factory.Sequence(lambda n: f'contact{n}@example.com')
  first_name = factory.Faker('first_name')
  last_name = factory.Faker('last_name')
  job_title = factory.Faker('job')
  company_name = factory.Faker('company')


class ContactNoteFactory(CTCTModelFactory[ContactNote]):
  class Meta:
    model = ContactNote

  contact = factory.SubFactory(ContactFactory)
  content = factory.Faker('sentence')


class ContactNoteWithRelatedObjsFactory(ContactNoteFactory):
  author = factory.SubFactory(UserFactory)


class ContactPhoneNumberFactory(CTCTModelFactory[ContactPhoneNumber]):
  class Meta:
    model = ContactPhoneNumber

  contact = factory.SubFactory(ContactFactory)
  kind = factory.Sequence(lambda n: ContactPhoneNumber.KINDS[n % 3][0])
  phone_number = factory.Faker('phone_number')


class ContactStreetAddressFactory(CTCTModelFactory[ContactStreetAddress]):
  class Meta:
    model = ContactStreetAddress

  contact = factory.SubFactory(ContactFactory)
  kind = factory.Sequence(lambda n: ContactStreetAddress.KINDS[n % 3][0])
  street = factory.Faker('street_address')
  city = factory.Faker('city')
  state = factory.Faker('state_abbr')
  postal_code = factory.Faker('postcode')

  @factory.lazy_attribute
  def country(self):
    max_length = ContactStreetAddress.API_MAX_LENGTH['country']
    s = RealFaker().country()[:max_length]
    return s


class ContactCustomFieldFactory(DjangoModelFactory[ContactCustomField]):
  class Meta:
    model = ContactCustomField

  contact = factory.SubFactory(ContactFactory)
  custom_field = factory.SubFactory(CustomFieldFactory)
  value = factory.Faker('word')


class ContactWithRelatedObjsFactory(ContactFactory):

  notes = factory.RelatedFactoryList(
    factory=ContactNoteFactory,
    factory_related_name='contact',
    size=NUM_RELATED_OBJS[Contact],
  )
  phone_numbers = factory.RelatedFactoryList(
    ContactPhoneNumberFactory,
    factory_related_name='contact',
    size=NUM_RELATED_OBJS[Contact],
  )
  street_addresses = factory.RelatedFactoryList(
    factory=ContactStreetAddressFactory,
    factory_related_name='contact',
    size=NUM_RELATED_OBJS[Contact],
  )

  @factory.post_generation
  def custom_fields(
    self,
    create: bool,
    extracted: list[ContactCustomField] | None,
    **kwargs: Any,
  ) -> None:
    if not create or not extracted:
      return
    self.custom_fields.add(*extracted)  # type: ignore[attr-defined]

  @factory.post_generation
  def list_memberships(
    self,
    create: bool,
    extracted: list[ContactList] | None,
    **kwargs: Any,
  ) -> None:
    if not create or not extracted:
      return
    self.list_memberships.add(*extracted)  # type: ignore[attr-defined]


class EmailCampaignFactory(CTCTModelFactory[EmailCampaign]):
  class Meta:
    model = EmailCampaign

  name = factory.Sequence(lambda n: f'Email Campaign {n}')


class CampaignActivityFactory(CTCTModelFactory[CampaignActivity]):
  class Meta:
    model = CampaignActivity

  campaign = factory.SubFactory(EmailCampaignFactory)
  subject = factory.Faker('sentence')
  preheader = factory.Faker('sentence')
  html_content = factory.Faker('text')

  @factory.post_generation
  def contact_lists(
    self,
    create: bool,
    extracted: list[ContactList] | None,
    **kwargs: Any,
  ) -> None:
    if not create or not extracted:
      # Simple build, or nothing to add, do nothing.
      return

    # Add the iterable of ContactLists using bulk addition
    self.contact_lists.add(*extracted)  # type: ignore[attr-defined]


class CampaignSummaryFactory(DjangoModelFactory[CampaignSummary]):
  class Meta:
    model = CampaignSummary

  campaign = factory.SubFactory(EmailCampaignFactory)

  sends = factory.Faker('pyint', min_value=100, max_value=1000)
  opens = factory.Faker('pyint', min_value=100, max_value=1000)
  clicks = factory.Faker('pyint', min_value=0, max_value=100)
  forwards = factory.Faker('pyint', min_value=0, max_value=100)
  optouts = factory.Faker('pyint', min_value=0, max_value=100)
  abuse = factory.Faker('pyint', min_value=0, max_value=10)
  bounces = factory.Faker('pyint', min_value=0, max_value=10)
  not_opened = factory.Faker('pyint', min_value=0, max_value=10)


class EmailCampaignWithRelatedObjsFactory(EmailCampaignFactory):

  campaign_activities = factory.RelatedFactoryList(
    factory=CampaignActivityFactory,
    factory_related_name='campaign',
    size=NUM_RELATED_OBJS[EmailCampaign],
  )
  summary = factory.RelatedFactory(
    factory=CampaignSummaryFactory,
    factory_related_name='campaign',
  )


FACTORIES: dict[Type[Model], Any] = {
  Token: TokenFactory,
  ContactList: ContactListFactory,
  CustomField: CustomFieldFactory,
  Contact: ContactWithRelatedObjsFactory,
  ContactNote: ContactNoteWithRelatedObjsFactory,
  ContactPhoneNumber: ContactPhoneNumberFactory,
  ContactStreetAddress: ContactStreetAddressFactory,
  ContactCustomField: ContactCustomFieldFactory,
  EmailCampaign: EmailCampaignWithRelatedObjsFactory,
  CampaignActivity: CampaignActivityFactory,
  CampaignSummary: CampaignSummaryFactory,
}
