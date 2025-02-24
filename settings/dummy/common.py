import os

# all the COMMON_KEYS
# copy this file to settings/keys/ and replace the dummy values with real ones
BOOXTREAM_API_KEY = os.environ.get('BOOXTREAM_API_KEY', '012345678901234567890123456789')
BOOXTREAM_API_USER = os.environ.get('BOOXTREAM_API_USER', 'user')
DROPBOX_KEY = os.environ.get('DROPBOX_KEY', '012345678901234')
GITHUB_PUBLIC_TOKEN = os.environ.get('GITHUB_PUBLIC_TOKEN', None) # 40 chars; null has lower limit
MAILCHIMP_API_KEY = os.environ.get('MAILCHIMP_API_KEY', '-us2') # [32chars]-xx#
MAILCHIMP_NEWS_ID = os.environ.get('MAILCHIMP_NEWS_ID', '0123456789')
MOBIGEN_PASSWORD = os.environ.get('MOBIGEN_PASSWORD', '012345678901234')
MOBIGEN_URL = os.environ.get('MOBIGEN_URL', '') # https://host/mobigen
MOBIGEN_USER_ID = os.environ.get('MOBIGEN_USER_ID', 'user')
TEST_STRIPE_PK = 'pk_0EajXPn195ZdF7Gt7pCxsqRhNN5BF'
TEST_STRIPE_SK = 'sk_0EajIO4Dnh646KPIgLWGcO10f9qnH'

