import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Configuration
# Secrets come from the environment, loaded from a .env file that is NOT
# committed. Copy .env.example to .env and fill it in. No extra dependency —
# the loader below is deliberately tiny so `pip install -r requirements.txt`
# stays enough to run the project.
# --------------------------------------------------------------------------
def _load_dotenv(path):
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        # Existing env vars win, so a real deployment can override the file.
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def env_list(name, default=''):
    return [v.strip() for v in os.environ.get(name, default).split(',') if v.strip()]


# SECURITY: never commit a real key. Falls back to the old dev key so the
# project still starts without a .env, but DEBUG must be off in production.
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-dev-only-key-change-me-in-dotenv',
)

DEBUG = env_bool('DJANGO_DEBUG', True)

# Add the server's LAN IP here so other branches can reach the system,
# e.g. ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50
ALLOWED_HOSTS = env_list(
    'DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver'
)

if not DEBUG and SECRET_KEY.startswith('django-insecure'):
    raise RuntimeError(
        'Refusing to start with DEBUG=False and the insecure default '
        'SECRET_KEY. Set DJANGO_SECRET_KEY in your .env file.'
    )


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core', 
    'accounts',
    'branches',
    'inventory',
    'sales',
    'procurement',
    'billing',
    'reports',
    'orders',
    # The DRF API (/api/) is served same-origin by this project, so no CORS
    # layer is needed. (The `frontend-react` branch, which calls it from a Vite
    # dev server on :5173, re-adds django-cors-headers.)
    "rest_framework",
]

AUTH_USER_MODEL = 'accounts.User'

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serves static files in production. Without it, DEBUG=False
    # means Django serves NO CSS and the whole site renders unstyled.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Trust the deployed origin for POSTs (set once the app has a real URL),
# e.g. DJANGO_CSRF_TRUSTED=https://gerlynplus.up.railway.app
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED', '')

# --- Production hardening --------------------------------------------------
# Only applied when DEBUG is off, so local development over plain HTTP still
# works. Set DJANGO_SECURE_SSL=False if the host does not terminate TLS.
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Railway/Render sit behind a proxy that terminates TLS; without this
    # Django sees plain HTTP and redirect-loops forever.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_HSTS_SECONDS = 31536000          # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'



ROOT_URLCONF = 'gerlyn_is.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.alerts',
            ],
        },
    },
]

WSGI_APPLICATION = 'gerlyn_is.wsgi.application'


# Database

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('DB_NAME', 'gerlyn_db'),
        'USER': os.environ.get('DB_USER', 'root'),
        'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '3306'),
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],

    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# Password validation


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
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

# The store is in Baliuag, Bulacan — reports and "today's sales" must reflect
# local time, not UTC, or a late-evening sale lands on the wrong day.
TIME_ZONE = 'Asia/Manila'

USE_I18N = True

USE_TZ = True

# Money reads as ₱253,529.00 (per the design system), not ₱253529.
USE_THOUSAND_SEPARATOR = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
# collectstatic gathers everything here for WhiteNoise to serve.
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        # Compresses + fingerprints filenames for long-lived caching.
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

LOGIN_URL = '/login/'