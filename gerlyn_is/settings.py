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

DEBUG = env_bool('DJANGO_DEBUG', False)

# Add the server's LAN IP here so other branches can reach the system,
# e.g. ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50
ALLOWED_HOSTS = env_list(
    'DJANGO_ALLOWED_HOSTS', 'gerlynplus.up.railway.app,localhost,127.0.0.1'
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
    # Sets request.LANGUAGE for the {% t %} template tag, from the user's
    # saved Settings > Language preference (or session, for anonymous users).
    'accounts.middleware.LanguageMiddleware',
    # Keeps accounts.UserSession in sync with real Django sessions, for
    # Settings > Sessions (list/revoke active logins).
    'accounts.middleware.SessionTrackingMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    # Per-user module access (core.module_permissions) — after Auth (needs
    # request.user) and Message (denial shows a message) middleware.
    'accounts.middleware.ModulePermissionMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Backs the idle-timeout / back-button auth guard: blocks the browser
    # from serving a cached authenticated page after logout or session expiry.
    'accounts.middleware.NoCacheForAuthenticatedMiddleware',
]

# Trust the deployed origin for POSTs (set once the app has a real URL),
# e.g. DJANGO_CSRF_TRUSTED=https://gerlynplus.up.railway.app
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED', '')

# --- Idle-timeout: 30 minutes of inactivity signs the user out ------------
# SESSION_SAVE_EVERY_REQUEST re-saves (and re-stamps the expiry of) the
# session cookie on every request, so the 30-minute clock only ever measures
# time since the user's LAST request — i.e. idle time, not absolute session
# age. This is the backend backstop; idle_timeout.js (included in the base
# templates) additionally tracks client-side inactivity and proactively
# redirects to login the moment the 30 minutes elapse, rather than waiting
# for the user's next click to discover the session is gone.
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True

# --- Production hardening --------------------------------------------------
# Only applied when DEBUG is off, so local development over plain HTTP still
# works. Set DJANGO_SECURE_SSL=False if the host does not terminate TLS.
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL', False)
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
                'core.context_processors.active_nav',
                'core.context_processors.alerts',
                'core.context_processors.app_version',
                'core.context_processors.customer_nav',
                'core.context_processors.detail_modal',
                'core.context_processors.module_access',
                'core.context_processors.nav_branches',
                'core.context_processors.nav_categories',
                'core.context_processors.nav_selected_branch',
                'core.context_processors.store_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'gerlyn_is.wsgi.application'


# Database

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        # Read from the environment, fall back to a local XAMPP/MySQL default.
        # The defaults are what one machine happens to use; the environment is
        # what everyone else's machine and Railway use. Hardcoding these
        # without the os.environ.get() wrapper points every teammate and the
        # deploy at one person's local database, and .env stops being read at
        # all — which is what happened in a5e1786 and is why this is back.
        'NAME': os.environ.get('DB_NAME', 'gerlyn_db'),
        'USER': os.environ.get('DB_USER', 'root'),
        # No fallback default here on purpose — a committed password, even a
        # "local-only" one, is a credential sitting in git history. Set
        # DB_PASSWORD in your .env; there's no working default without it.
        'PASSWORD': os.environ['DB_PASSWORD'],
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '3306'),
        # Reuse the MySQL connection across requests instead of paying a full
        # TCP+auth handshake on every single page load (the main cause of
        # per-page slowness on Railway). Django verifies the connection is
        # still alive before reusing it, so a dropped connection can't cause
        # silent failures.
        'CONN_MAX_AGE': 60,
        'CONN_HEALTH_CHECKS': True,
    }
}


# Cache
# --------------------------------------------------------------------------
# The login lockout (accounts/rate_limit.py) counts failed attempts in the
# cache. With no CACHES set the default is LocMemCache — per-process and
# in-memory — so under Gunicorn (Procfile / railway.json run 2+ workers)
# each worker keeps its own count. The real cap becomes 5 x worker count,
# and every redeploy wipes it back to zero. DatabaseCache gives every
# worker one shared, durable store and needs no extra service — just a
# table in the database we already have.
#
# ONE-TIME SETUP, per environment (local dev AND production): this table is
# NOT created by `migrate`. Run once, alongside deploying this change:
#     python manage.py createcachetable
# It is idempotent (safe to re-run), which is why railway.json also runs it
# on every deploy so a fresh production database is never missing it.
# --------------------------------------------------------------------------
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'gerlyn_cache',
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
    {
        'NAME': 'accounts.validators.StrongPasswordValidator',
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

# --- User-uploaded media storage -------------------------------------------
# Storage is decided by the environment so no code (models/views/templates)
# ever changes:
#   * Local dev / no credentials  -> FileSystemStorage above (MEDIA_ROOT). The
#     /media/ route in gerlyn_is/urls.py serves it. Nothing extra to install.
#   * Production with Cloudinary  -> set CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET
#     and uploads (product images, GCash proofs) go to Cloudinary's CDN. This
#     survives redeploys on ephemeral hosts (Railway/Render) and scales the
#     media serving off the app server. `image.url` returns the CDN link.
USE_CLOUDINARY = bool(os.environ.get('CLOUDINARY_CLOUD_NAME'))
if USE_CLOUDINARY:
    INSTALLED_APPS += ['cloudinary', 'cloudinary_storage']
    STORAGES['default'] = {
        'BACKEND': 'cloudinary_storage.storage.MediaCloudinaryStorage',
    }
    CLOUDINARY_STORAGE = {
        'CLOUD_NAME': os.environ['CLOUDINARY_CLOUD_NAME'],
        'API_KEY': os.environ['CLOUDINARY_API_KEY'],
        'API_SECRET': os.environ['CLOUDINARY_API_SECRET'],
    }

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/profile/'

# --- Email -------------------------------------------------------------------
EMAIL_BACKEND       = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST          = 'smtp.gmail.com'
EMAIL_PORT          = 587
EMAIL_USE_TLS       = True
EMAIL_HOST_USER     = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL  = os.environ.get('EMAIL_HOST_USER', 'noreply@gerlynstore.com')

# Avatar upload validation
AVATAR_MAX_MB        = 2
AVATAR_ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/webp'}