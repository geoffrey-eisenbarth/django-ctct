import factory
import factory.fuzzy
from factory.django import DjangoModelFactory

from django.contrib.auth import get_user_model

from django_ctct import models as ctct_models
from django_ctct.managers import TokenRemoteManager


def get_factory(
  model: ctct_models.CTCTModel,
  include_related: bool = False,
) -> DjangoModelFactory:
  if include_related:
    factories = {
      ctct_models.Contact: ContactWithRelatedObjsFactory,
      ctct_models.ContactNote: ContactNoteWithRelatedObjsFactory,
      ctct_models.EmailCampaign: EmailCampaignWithRelatedObjsFactory,
    }
  else:
    factories = {
      ctct_models.Token: TokenFactory,
      ctct_models.ContactList: ContactListFactory,
      ctct_models.CustomField: CustomFieldFactory,
      ctct_models.Contact: ContactFactory,
      ctct_models.ContactNote: ContactNoteFactory,
      ctct_models.ContactPhoneNumber: ContactPhoneNumberFactory,
      ctct_models.ContactStreetAddress: ContactStreetAddressFactory,
      ctct_models.ContactCustomField: ContactCustomFieldFactory,
      ctct_models.EmailCampaign: EmailCampaignFactory,
      ctct_models.CampaignActivity: CampaignActivityFactory,
    }
  return factories[model]


class UserFactory(DjangoModelFactory):
  class Meta:
    model = get_user_model()

  username = factory.Sequence(lambda n: f'user{n}')
  email = factory.Sequence(lambda n: f'user{n}@example.com')
  first_name = factory.Faker('first_name')
  last_name = factory.Faker('last_name')
  password = factory.django.Password('pw')


class TokenFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.Token

  access_token = factory.Faker('pystr', max_chars=1200)
  refresh_token = factory.Faker('pystr', max_chars=50)
  scope = TokenRemoteManager.API_SCOPE


class CTCTModelFactory(DjangoModelFactory):
  api_id = factory.Faker('uuid4')


class ContactListFactory(CTCTModelFactory):
  class Meta:
    model = ctct_models.ContactList

  name = factory.Sequence(lambda n: f'Contact List {n}')
  description = factory.Faker('sentence')
  favorite = False


class CustomFieldFactory(CTCTModelFactory):
  class Meta:
    model = ctct_models.CustomField

  label = factory.Sequence(lambda n: f'Custom Field {n}')
  type = 'string'


class ContactFactory(CTCTModelFactory):
  class Meta:
    model = ctct_models.Contact

  email = factory.Sequence(lambda n: f'contact{n}@example.com')
  first_name = factory.Faker('first_name')
  last_name = factory.Faker('last_name')
  job_title = factory.Faker('job')
  company_name = factory.Faker('company')


class ContactNoteFactory(CTCTModelFactory):
  class Meta:
    model = ctct_models.ContactNote

  contact = factory.SubFactory(ContactFactory)
  content = factory.Faker('sentence')


class ContactNoteWithRelatedObjsFactory(ContactNoteFactory):
  author = factory.SubFactory(UserFactory)


class ContactPhoneNumberFactory(CTCTModelFactory):
  class Meta:
    model = ctct_models.ContactPhoneNumber

  contact = factory.SubFactory(ContactFactory)
  kind = factory.fuzzy.FuzzyChoice(
    _[0] for _ in ctct_models.ContactPhoneNumber.KINDS
  )
  phone_number = factory.Faker('phone_number')


class ContactStreetAddressFactory(CTCTModelFactory):
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


class ContactWithRelatedObjsFactory(ContactFactory):
  notes = factory.RelatedFactoryList(
    factory=ContactNoteFactory,
    factory_related_name='contact',
    size=2,
  )
  phone_numbers = factory.RelatedFactoryList(
    ContactPhoneNumberFactory,
    factory_related_name='contact',
    size=2,
  )
  street_addresses = factory.RelatedFactoryList(
    factory=ContactStreetAddressFactory,
    factory_related_name='contact',
    size=2,
  )

  @factory.post_generation
  def custom_fields(self, create, extracted, **kwargs):
    if not create or not extracted:
      return
    self.custom_fields.add(*extracted)

  @factory.post_generation
  def list_memberships(self, create, extracted, **kwargs):
    if not create or not extracted:
      return
    self.list_memberships.add(*extracted)


class EmailCampaignFactory(CTCTModelFactory):
  class Meta:
    model = ctct_models.EmailCampaign

  name = factory.Sequence(lambda n: f'Email Campaign {n}')


class CampaignActivityFactory(CTCTModelFactory):
  class Meta:
    model = ctct_models.CampaignActivity

  campaign = factory.SubFactory(EmailCampaignFactory)
  subject = factory.Faker('sentence')
  preheader = factory.Faker('sentence')
  html_content = factory.Faker('text')

  @factory.post_generation
  def contact_lists(self, create, extracted, **kwargs):
    if not create or not extracted:
      # Simple build, or nothing to add, do nothing.
      return

    # Add the iterable of ContactLists using bulk addition
    self.contact_lists.add(*extracted)


class CampaignSummaryFactory(DjangoModelFactory):
  class Meta:
    model = ctct_models.CampaignSummary

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
    size=1,
  )
  summary = factory.RelatedFactory(
    factory=CampaignSummaryFactory,
    factory_related_name='campaign',
  )
