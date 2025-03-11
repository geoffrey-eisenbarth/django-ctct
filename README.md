# Constant Contact Integration for Django

This Django app provides a seamless interface to the Constant Contact API, allowing you to manage contacts, email campaigns, and other Constant Contact functionalities directly from your Django project.

**Warning:** This package is under active development. While it is our intention to develop with a consistent API going forward, we will not make promises until a later version is released.

## Installation

```bash
pip install django-ctct
```

## Configuration

1) **Add to `INSTALLED_APPS`:**

In your Django project's settings.py file, add django_ctct to the INSTALLED_APPS list:

```python
INSTALLED_APPS = [
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

You'll need to configure your ConstantContact API credentials in `settings.py`:

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


## Usage

Describe how to use your app. Provide examples of common use cases. For example:

  * Managing Contacts: Explain how to create, update, and delete contacts using your app's models or API wrappers.
  * Email Campaigns: Show how to create, send, and manage email campaigns.
  * Django Admin Integration: Describe how your app integrates with the Django admin interface.
  * Views and Templates (if applicable): Explain how to use your app's views and templates (if it provides any).

## Testing

Explain how to install dev dependencies and run the tests for your app.

  * `git clone git@github.com:geoffrey-eisenbarth/django-ctct.git`
  * `cd django-ctct`
  * `poetry install --with dev`
  * `cd tests/project`
  * `poetry run ./manage.py migrate`
  * `poetry run ./manage.py runserver`
  * visit `127.0.0.1:8000/ctct/auth/` and log into CTCT to set up your first Token
  * `poetry run coverage run ./manage.py test`
  * `poetry run coverage report`


## Contributing

Once version 0.1.0 is released on PyPI, we hope to implement the following new features (in no particular order):

  * Support for API syncing using signals (`post_save`, `pre_delete`, `m2m_changed`, etc). This will be controlled by the `CTCT_SYNC_SIGNALS` setting. **Update** This probably won't work as desired since the primary object will be saved before related objects are.
  * Background task support using `django-tasks` (which hopefully will merge into Django). This will be controlled by the `CTCT_ENQUEUE_DEFAULT` setting. 
  * Add `models.CheckConstraint` and `models.UniqueConstraint` constraints that are currently commented out.
  

I'm always open to new suggestions, so please reach out on GitHub: https://github.com/geoffrey-eisenbarth/django-ctct/

 
## License

This package is currently distributed under the MIT license.


## Support

If you have any issues or questions, please feel free to reach out to me on GitHub: https://github.com/geoffrey-eisenbarth/django-ctct/issues
