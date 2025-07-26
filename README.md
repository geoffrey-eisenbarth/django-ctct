# Constant Contact Integration for Django

[![codecov](https://codecov.io/gh/geoffrey-eisenbarth/django-ctct/graph/badge.svg?token=0UBX5CGCHA)](https://codecov.io/gh/geoffrey-eisenbarth/django-ctct)

This Django app provides a seamless interface to the Constant Contact API, allowing you to manage contacts, email campaigns, and other Constant Contact functionalities directly from your Django project.

**Warning:** This package is under active development.
While it is our intention to develop with a consistent API going forward, we will not make promises until a later version is released.


## Installation

```bash
pip install django-ctct
```


## Configuration

1) **Add to `INSTALLED_APPS`:**

In your Django project's settings.py file, add `'django_ctct'` to the `'INSTALLED_APPS'`list (order doesn't matter):

```python
INSTALLED_APPS = [
  # ... django apps
  'django_ctct',
  # ... other apps
]
```

2) **Include URLs:**

In your project's `urls.py` file, include the `django_ctct` URLs:

```python
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
  path('admin/', admin.site.urls),
  path('django-ctct/', include('django_ctct.urls')),
  # ... other URL patterns
]
```

3) **ConstantContact API Credentials:**

Head to https://app.constantcontact.com/pages/dma/portal/ to set up your application with ConstantContact.
Pay particular attention to the "Redirect URI" field: it's crucial that this matches your Django project's server name (with `/django-ctct/auth/` at the end).
For example, if your Django project is hosted at `www.example.com` with SSL, then the value should be `https://www.example.com/django-ctct/auth/`.
If you're running Django's development server at `127.0.0.1:8000`, then you should set it to `http://127.0.0.1:8000/django-ctct/auth/`.
After that's finished, you'll need to configure your ConstantContact API credentials in `settings.py`:

```python
# Required settings
CTCT_PUBLIC_KEY = "YOUR_PUBLIC_KEY"
CTCT_SECRET_KEY = "YOUR_SECRET_KEY"
CTCT_REDIRECT_URI = "REDIRECT_URI_FROM_CTCT"
CTCT_FROM_NAME = "YOUR_EMAIL_NAME"
CTCT_FROM_EMAIL = "YOUR_EMAIL_ADDRESS"

# Optional settings
CTCT_REPLY_TO_EMAIL = "YOUR_REPLY_TO_ADDRESS"
CTCT_PHYSICAL_ADDRESS = {
  'address_line1': '1060 W Addison St',
  'address_line2': '',
  'address_optional': '',
  'city': 'Chicago',
  'country_code': 'US',
  'country_name': 'United States',
  'organization_name': 'Wrigley Field',
  'postal_code': '60613',
  'state_code': 'IL',
}
CTCT_PREVIEW_RECIPIENTS = (
  ('First Recipient', 'first@recipient.com'),
  ('Second Recipient', 'second@recipient.com'),
)
CTCT_PREVIEW_MESSAGE = "This is an EmailCampaign preview."

# Optional functionality settings and their default settings
CTCT_USE_ADMIN = False       # Add django-ctct models to admin
CTCT_SYNC_ADMIN = False      # Django admin CRUD operations will sync with ctct account
```

**Important:** Store your API credentials securely. Avoid committing them directly to your version control repository.

  * `CTCT_FROM_EMAIL` must be a verified email address for your ConstantContact.com account.
  * `CTCT_REPLY_TO_EMAIL` will default to `CTCT_FROM_EMAIL` if not set.
  * `CTCT_PHYSICAL_ADDRESS` will default to the information set in your ConstantContact.com account if not set.
  * `CTCT_PREVIEW_RECIPIENTS` will default to `settings.MANAGERS` if not set.
  * `CTCT_PREVIEW_MESSAGE` will be blank by default.

3) **Run Migrations:**

```bash
> ./manage.py makemigrations
> ./manage.py migrate
```

4) **Create Authenticatin Token:**

After the app has been installed and configured, you must generate your first auth token:

 * Open up the `CTCT_REDIRECT_URI` address in your browser
 * Use your ConstantContact credentials to log in
 * From this point forward `django-ctct` should use refresh tokens, so no need to manually log in again


## Usage
If you wish to import data from ConstantContact.com into your local database (recommended), then run the following:

```bash
> ./manage.py import_ctct
```

You will be asked before each model type is imported.
**Note** ConstantContact does not provide a bulk API endpoint for fetching Campaign Activities, so depending on the size of your account, this might take some time and possible put you over their 10,000 request per day limit if you run it regularly.

Since ConstantContact does not offer any webhooks, you will need to set up a cron job if you want your account to remain syncronized with ConstantContact's database.
You can use the `--no-input` flag to bypass the interactive questions.
The `--stats-only` flag is useful for running a cron job to keep EmailCampaign statistics updated.

If you wish to use the Django admin to interact with ConstantContact, you must explicitly set the `CTCT_USE_ADMIN` and `CTCT_SYNC_ADMIN` settings to `True`.


## Testing

To install dev dependencies:

```bash
> git clone git@github.com:geoffrey-eisenbarth/django-ctct.git
> cd django-ctct
> python -m pip install --upgrade pip
> pip intall poetry
> poetry install --with dev
```

To run tests:

```bash
> poetry run coverage run tests/project/manage.py test
> poetry run coverage report
```


## Contributing

Once version 0.0.1 is released on PyPI, we hope to implement the following new features (in no particular order):

 * Support for API syncing using signals (`post_save`, `pre_delete`, `m2m_changed`, etc).
 This will be controlled by the `CTCT_SYNC_SIGNALS` setting.
 **Update** This probably won't work as desired since the primary object will be saved before related objects are.
 * Background task support using `django-tasks` (which hopefully will merge into Django). This will be controlled by the `CTCT_ENQUEUE_DEFAULT` setting. 
 * Add `models.CheckConstraint` and `models.UniqueConstraint` constraints that are currently commented out.
  

I'm always open to new suggestions, so please reach out on GitHub: https://github.com/geoffrey-eisenbarth/django-ctct/

 
## License

This package is currently distributed under the MIT license.


## Support

If you have any issues or questions, please feel free to reach out to me on GitHub: https://github.com/geoffrey-eisenbarth/django-ctct/issues
