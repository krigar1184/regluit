# coding=utf-8
from .common import *
try:
    from .keys.host import *
except ImportError:
    from .dummy.host import *

DEBUG = True
TEMPLATES[0]['OPTIONS']['debug'] = DEBUG

# if you're doing development work, you'll want this to be zero
IS_PREVIEW = False

# SITE_ID for your particular site -- must be configured in /core/fixtures/initial_data.json
SITE_ID = 3

ADMINS = (
    ('Raymond Yee', 'rdhyee+ungluebugs@gluejar.com'),
    ('Eric Hellman', 'eric@gluejar.com'),
)

MANAGERS = ADMINS

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'unglueit',
        'USER': '',
        'PASSWORD': '',
        'HOST': '',
        'PORT': '',
        'TEST': {
            'CHARSET': 'utf8',
        }
    }
}

STATIC_ROOT = '/var/www/static'
CKEDITOR_UPLOAD_PATH = '/var/www/static/media/'


TIME_ZONE = 'America/New_York'

# settings for outbout email
# if you have a gmail account you can use your email address and password

EMAIL_USE_TLS = True
EMAIL_HOST = 'smtp.gmail.com'
#  EMAIL_HOST_USER is in keys/host
#  EMAIL_HOST_PASSWORD is in keys/host
EMAIL_PORT = 587
DEFAULT_FROM_EMAIL = 'info@ebookfoundation.org'

# for use with test google account only
GOOGLE_DISPLAY_NAME = 'Unglue.It'
REDIRECT_IS_HTTPS = False


#BASE_URL = 'http://0.0.0.0'
BASE_URL_SECURE = 'https://0.0.0.0'

# use redis as queuing service
BROKER_TRANSPORT = "redis"
BROKER_HOST = "localhost"
BROKER_PORT = 6379
BROKER_VHOST = "0"

# send celery log to Python logging
CELERYD_HIJACK_ROOT_LOGGER = False

# a debug_toolbar setting
INTERNAL_IPS = ('127.0.0.1',)

# decide which of the period tasks to add to the schedule
#CELERYBEAT_SCHEDULE['send_test_email'] = SEND_TEST_EMAIL_JOB
#CELERYBEAT_SCHEDULE['refresh_acqs'] = REFRESH_ACQS_JOB

# if you're doing development work, you'll want this to be zero
IS_PREVIEW = False

# username, password to pass to LIVE_SERVER_TEST_URL

UNGLUEIT_TEST_USER = None
UNGLUEIT_TEST_PASSWORD = None

# local settings for maintenance mode
MAINTENANCE_MODE = False

# assume that CSS will get generated on dev
SASS_OUTPUT_STYLE = 'compressed'

