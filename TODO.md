* Implement mypy
* models
  * EmailCampaign.current_status vs CampaignActivity.current_status
* testing
  * email campaign CRUD
  * contact w/o list_memberships -> DELETE verb
  * remote.all(), remote.bulk_delete()
* import
  * ManyToMany "upsert" (delete first?)
  * get more than one page worth of API results

  * email_campaign + contact_lists
  * TPG EmailCampaign '644a82af-90da-40ed-bf7b-bf4bfdb32031' had name > 80ch
  * Add a warning about importing CAs
  * CA import error: CampaignActivity.remote.get() returned None
    * EC: '35996b71-0217-4156-b89d-c3f0dfa008ce'
    * CA: '0a12c529-07c2-41e0-afad-9ff8d4052a4b'

    * EC: 'ea922e95-a938-4c28-9449-06efdc6c35fb'
    * CA: '6dbe8ba1-51b8-47b1-ac92-35107b26b4ca'

    * EC: 'd50414c7-ca35-4a78-a880-76eda25d1231'
    * CA: 'fe32318e-8f2a-4ee1-92d6-e4ee0d16ff08'

    * EC: 'ee1d4136-c988-4591-b13f-361446fdde94'
    * CA: 'eab987e3-a96a-4478-9b58-bf1b3f929be8'

    * Max preheader len is 330, but django is 130
    * can we verify ctct max lengh?

  * In campaign stats update: EC 6009b2ea-186c-4805-a37d-e8a9ec11694e appeared twice?
