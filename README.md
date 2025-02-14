# django-ctct: Constant Contact Integration for Django

This Django app provides a seamless interface to the Constant Contact API, allowing you to manage contacts, email campaigns, and other Constant Contact functionalities directly from your Django project.

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
  path('django-rq/', include('django_rq.urls')),  # Optional
  path('django-ctct/', include('django_ctct.urls')),
  # ... other URL patterns
]
```
3) **ConstantContact API Credentials:**

You'll need to configure your ConstantContact API credentials in `settings.py`:

```python
CTCT_PUBLIC_KEY = "YOUR_PUBLIC_KEY"
CTCT_SECRET_KEY = "YOUR_SECRET_KEY"
CTCT_REDIRECT_URI = "REDIRECT_URI_FROM_CTCT"
CTCT_FROM_NAME = "YOUR_EMAIL_NAME"
CTCT_FROM_EMAIL = "YOUR_EMAIL_ADDRESS"
```

Important:  Store your API credentials securely.  Avoid committing them directly to your version control repository.

4) **Other Settings:**

You'll want to set `settings.PHONENUMBER_DEFAULT_REGION` in order for `django-phonenumber-field` to work properly.

5) **RQ Configuration (Optional, for Asynchronous Tasks)**:

If you want to use `django-rq` for asynchronous tasks (recommended for API calls that might take a while), configure it in your `settings.py`:

```Python
RQ_QUEUES = {
    'default': {
        'HOST': 'localhost',  # Redis host
        'PORT': 6379,       # Redis port
        'DB': 0,            # Redis database
        'PASSWORD': 'your_redis_password', # Redis password (if any)
    },
    'ctct': {  # Dedicated queue for Constant Contact tasks
        'HOST': 'localhost',
        'PORT': 6379,
        'DB': 0,
        'PASSWORD': 'your_redis_password',
    },
}

# Add django-rq to INSTALLED_APPS if you haven't already
INSTALLED_APPS += ['django_rq']
```

Then, run the RQ worker:

```Bash
python manage.py rqworker ctct  # Or python manage.py rqworker for all queues
```

To view tasks in Django admin, you'll need to add `django-rq` to your `urls.py` as mentioned above.

## Usage

Describe how to use your app.  Provide examples of common use cases.  For example:

  * Managing Contacts: Explain how to create, update, and delete contacts using your app's models or API wrappers.
  * Email Campaigns: Show how to create, send, and manage email campaigns.
  * Django Admin Integration: Describe how your app integrates with the Django admin interface.
  * Views and Templates (if applicable): Explain how to use your app's views and templates (if it provides any).

## Testing

Explain how to install dev dependencies and run the tests for your app.

  * `./manage.py migrate`
  * `./manage.py runserver`
  * Visit `127.0.0.1:8000/ctct/auth/` and log into CTCT

## Contributing

Explain how others can contribute to your app.


## License

Specify the license under which your app is distributed.  (e.g., MIT, GPL, etc.)


## Support

Provide contact information or a link to your issue tracker.
