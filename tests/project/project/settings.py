"""
Django settings for project project.

Generated by 'django-admin startproject' using Django 5.1.6.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/5.1/ref/settings/
"""

from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-6x+j&ce$ge4ujbic9xya-x9@19gd6l*l3soswcna(ee+-h*1*1'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = []


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'django_ctct',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'project.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.1/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Django-CTCT settings
CTCT_PUBLIC_KEY = "THIS-IS-NOT-A-REAL-PUBLIC-KEY"
CTCT_PRIVATE_KEY = "THIS-IS-NOT-A-REAL-PRIVATE-KEY"
CTCT_REDIRECT_URI = 'http://127.0.0.1:8000/django-ctct/auth/'
CTCT_FROM_NAME = "Django CTCT"
CTCT_FROM_EMAIL = "django@ctct.com"
CTCT_USE_ADMIN = True
CTCT_SYNC_ADMIN = True
CTCT_SYNC_SIGNALS = False
CTCT_ENQUEUE_DEFAULT = False

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

##################
# LOCAL SETTINGS #
##################

# Allow any settings to be defined in local_settings.py, which should be
# ignored in version control system, allowing for settings to be defined
# per machine.

# Instead of doing "from .local_settings import *", we use ``exec`` so that
# local_settings has full access to everything defined in this module.
# Also force into sys.modules so it's visible to Django's autoreload.

f = Path(BASE_DIR, 'project', 'local_settings.py')
if f.exists():
  import sys
  import imp
  module_name = 'project.local_settings'
  module = imp.new_module(module_name)
  module.__file__ = str(f)
  sys.modules[module_name] = module
  exec(open(f, 'rb').read())
