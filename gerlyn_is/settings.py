import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

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
    """True only for an explicit 1/true/yes/on -- the string "False" is False.

    A blank value counts as unset (-> default): .env.example ships many `NAME=`
    lines, and copying one verbatim must not flip a default-on setting off.
    """
    raw = os.environ.get(name, '').strip().lower()
    if not raw:
        return default
    return raw in ('1', 'true', 'yes', 'on')


def env_str(name, default=''):
    """Like os.environ.get, but a blank value counts as unset.

    .env.example ships many `NAME=` lines; copied verbatim (locally or into
    Railway Variables) they must fall back to the default, not override it with "".
    """
    return (os.environ.get(name) or default).strip()


def env_list(name, default=''):
    return [v.strip() for v in os.environ.get(name, default).split(',') if v.strip()]


# SECURITY: never commit a real key. Falls back to the old dev key so the
# project still starts without a .env, but DEBUG must be off in production.
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-dev-only-key-change-me-in-dotenv',
)

# Railway injects these into every deployed service (never into a laptop), so
# their presence is a reliable "this is the hosted environment" signal.
RAILWAY_PUBLIC_DOMAIN = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '').strip()
ON_RAILWAY = bool(
    RAILWAY_PUBLIC_DOMAIN
    or os.environ.get('RAILWAY_ENVIRONMENT_NAME')
    or os.environ.get('RAILWAY_ENVIRONMENT')
)

# DEBUG comes from DJANGO_DEBUG and defaults to OFF. env_bool() compares against
# an explicit allow-list, so the string "False" is never truthy.
#
# On Railway, DEBUG is forced off regardless of the variable: Django's debug
# 404/500 pages print the full URL table, settings and tracebacks, and a
# copied-over DJANGO_DEBUG=True (the .env.example value) is exactly how that
# reached production once. Local development is unaffected.
DEBUG = env_bool('DJANGO_DEBUG', False)
if ON_RAILWAY and DEBUG:
    DEBUG = False
    print(
        'WARNING: DJANGO_DEBUG=True was ignored because this is a Railway '
        'deployment. Remove DJANGO_DEBUG (or set it to False) in Railway Variables.',
        file=sys.stderr,
    )

# Comma-separated hosts. Nothing here is hardcoded to one domain: Railway's own
# public domain is added automatically, plus the internal health-check host
# (Railway probes /healthz/ with Host: healthcheck.railway.app). Add a custom
# domain via DJANGO_ALLOWED_HOSTS. Add the LAN IP for on-premises use, e.g.
# DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50
ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]')
if RAILWAY_PUBLIC_DOMAIN and RAILWAY_PUBLIC_DOMAIN not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RAILWAY_PUBLIC_DOMAIN)
if ON_RAILWAY and 'healthcheck.railway.app' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('healthcheck.railway.app')

if not DEBUG and SECRET_KEY.startswith('django-insecure'):
    raise RuntimeError(
        'Refusing to start with DEBUG=False and the insecure default '
        'SECRET_KEY. Set DJANGO_SECRET_KEY in your .env file.'
    )

# --- Error tracking ----------------------------------------------------------
# Opt-in: no-ops entirely until SENTRY_DSN is set, so this stays a no-op for
# local dev and for anyone who hasn't created a (free-tier) Sentry project
# yet. Without this, a production 500 is only visible by someone happening to
# read Railway's log stream after the fact.
SENTRY_DSN = os.environ.get('SENTRY_DSN', '')
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        send_default_pii=False,
        traces_sample_rate=0.0,
        environment='production' if not DEBUG else 'development',
    )


# Application definition

INSTALLED_APPS = [
    # Superuser-only AdminSite (core.admin_site); the route itself is gated by
    # DJANGO_ADMIN_ENABLED below.
    'core.admin_site.SuperuserOnlyAdminConfig',
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
    # Damaged Product Reports: photo evidence, manager review, and the stock /
    # refund / supplier-return follow-through.
    'damage_reports',
    # Operating expenses (rent, utilities, salaries ...) feeding the
    # Profitability report. Owner and Branch Manager only.
    'expenses',
    # Ledger + endpoints behind the offline queue (static/js/offline-sync.js):
    # replays changes queued while offline, exactly once, through the real views.
    'offline_sync',
    # Append-only record of every data change (who, what, when, before/after).
    # Listed last so its signal handlers run after every other app's own
    # post_save handlers have finished adjusting the row.
    'audit',
    # Encrypted, off-site database + uploaded-file backups (Backblaze B2).
    'backups',
    # The DRF API (/api/) is served same-origin by this project, so no CORS
    # layer is needed. (The `frontend-react` branch, which calls it from a Vite
    # dev server on :5173, re-adds django-cors-headers.)
    "rest_framework",
]

# Query-count / SQL panel for finding N+1s during local development only —
# gated on DEBUG at every layer (app, middleware, urls.py) so it can never
# activate in production even though it stays in requirements.txt.
if DEBUG:
    INSTALLED_APPS += ['debug_toolbar']
    INTERNAL_IPS = ['127.0.0.1']

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
    # Remembers which request (and so which user) a data change belongs to,
    # so the audit log can name its actor. Needs request.user, so after Auth.
    'audit.middleware.AuditContextMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Backs the idle-timeout / back-button auth guard: blocks the browser
    # from serving a cached authenticated page after logout or session expiry.
    'accounts.middleware.NoCacheForAuthenticatedMiddleware',
]

if DEBUG:
    MIDDLEWARE.append('debug_toolbar.middleware.DebugToolbarMiddleware')

# --- Audit log --------------------------------------------------------------
# AUDIT_ENABLED=False switches change capture off entirely (emergencies only —
# nothing is recorded while it is off).
#
# AUDIT_DB_TRIGGERS installs MySQL triggers that refuse UPDATE/DELETE on the
# audit table, so the log stays append-only even against code or a database
# session that bypasses the app. Off while running the test suite (Django's
# TransactionTestCase flush issues DELETEs the trigger would reject); the
# trigger itself is covered by a dedicated test instead.
AUDIT_ENABLED = env_bool('AUDIT_ENABLED', True)
TESTING = len(sys.argv) > 1 and sys.argv[1] == 'test'
AUDIT_DB_TRIGGERS = env_bool('AUDIT_DB_TRIGGERS', not TESTING)

# --- Backups ------------------------------------------------------------------
# See docs/backup-and-restore.md. Every value below comes from the environment
# (Railway Variables in production, .env locally); none has a usable default,
# so an unconfigured install simply reports "not set up" on the Backup Status
# page instead of half-working.
#
# BACKUP_STORAGE: 'b2' (Backblaze, S3-compatible API) or 'local' (a folder —
# for trying it out and for the tests). Defaults to b2 once a bucket is set.
BACKUP_STORAGE = os.environ.get('BACKUP_STORAGE', 'b2' if os.environ.get('B2_BUCKET') else 'local')
BACKUP_LOCAL_DIR = Path(os.environ.get('BACKUP_LOCAL_DIR', str(BASE_DIR / 'tmp_backups')))
B2_KEY_ID = os.environ.get('B2_KEY_ID', '')
B2_APPLICATION_KEY = os.environ.get('B2_APPLICATION_KEY', '')
B2_BUCKET = os.environ.get('B2_BUCKET', '')
B2_ENDPOINT = os.environ.get('B2_ENDPOINT', '')
# 32 random bytes, url-safe base64. Backups are encrypted with a key derived
# from this before they leave the server; without it they cannot be read.
BACKUP_ENCRYPTION_KEY = os.environ.get('BACKUP_ENCRYPTION_KEY', '')
# The web service runs the daily backup itself (backups/scheduler.py) at this
# hour, UTC (19 = 03:00 Manila). BACKUP_SCHEDULER=False turns that off.
BACKUP_SCHEDULER = env_bool('BACKUP_SCHEDULER', True)
BACKUP_SCHEDULE_HOUR_UTC = int(os.environ.get('BACKUP_SCHEDULE_HOUR_UTC', '19'))
# OPTIONAL: lets an external cron start a backup over HTTP (not needed while
# the built-in schedule is on). Unset = that endpoint does not exist (404).
BACKUP_TRIGGER_TOKEN = os.environ.get('BACKUP_TRIGGER_TOKEN', '')
# The command-line tools, if they are not on PATH (e.g. on Windows).
BACKUP_MYSQLDUMP = os.environ.get('BACKUP_MYSQLDUMP', 'mysqldump')
BACKUP_MYSQL = os.environ.get('BACKUP_MYSQL', 'mysql')
# The status page flags a backup as overdue after this many hours.
BACKUP_STALE_HOURS = int(os.environ.get('BACKUP_STALE_HOURS', '26'))

# Overdue purchase-order deliveries (procurement.services.process_overdue_deliveries,
# run by dispatch_notifications): an Incoming / Partially Received order is
# flagged and the buyer + supplier told this many days after its delivery date,
# and escalated to the Owner after the second figure. Nothing is ever
# auto-cancelled — the Owner cancels or reschedules by hand.
PO_OVERDUE_GRACE_DAYS = int(os.environ.get('PO_OVERDUE_GRACE_DAYS', '1'))
PO_OVERDUE_ESCALATE_DAYS = int(os.environ.get('PO_OVERDUE_ESCALATE_DAYS', '3'))

# Trust the deployed HTTPS origin for POSTs. Railway's public domain is added
# automatically; add a custom domain with
# DJANGO_CSRF_TRUSTED=https://shop.example.com (comma-separated).
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED', '')
if RAILWAY_PUBLIC_DOMAIN and f'https://{RAILWAY_PUBLIC_DOMAIN}' not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(f'https://{RAILWAY_PUBLIC_DOMAIN}')

# --- Django admin --------------------------------------------------------------
# The business roles (Owner, Manager, Staff, Supplier, Customer) never use
# /admin/; the app has its own management screens. So the route only exists
# when explicitly switched on: always in local DEBUG, never on a hosted
# deployment unless DJANGO_ADMIN_ENABLED=True. Even when enabled it admits
# genuine superusers only (core.admin_site) -- a role, or is_staff alone, is
# not enough. DJANGO_ADMIN_URL moves it off the guessable /admin/ path.
ADMIN_ENABLED = env_bool('DJANGO_ADMIN_ENABLED', DEBUG)
ADMIN_URL = env_str('DJANGO_ADMIN_URL', 'admin/').strip('/') + '/'

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
# works. DJANGO_SECURE_SSL defaults to on for Railway (its edge terminates TLS
# and forwards X-Forwarded-Proto); set it False if some other host does not.
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL', ON_RAILWAY)
    # Railway's own health probe is plain HTTP to the container with no
    # forwarded-proto header; redirecting it would fail every deploy.
    SECURE_REDIRECT_EXEMPT = [r'^healthz/$']
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_SAMESITE = 'Lax'
    # Railway/Render sit behind a proxy that terminates TLS; without this
    # Django sees plain HTTP and redirect-loops forever.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_HSTS_SECONDS = 31536000          # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'

# --- Logging -------------------------------------------------------------------
# With DEBUG off, Django's default config sends django.request errors only to
# mail_admins (no ADMINS here), so a production 500 would leave NOTHING in
# Railway's log stream. Send them to stdout instead; the browser still gets the
# generic 500.html, never the traceback.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {'format': '%(asctime)s %(levelname)s %(name)s: %(message)s'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'standard'},
    },
    'root': {'handlers': ['console'], 'level': os.environ.get('DJANGO_LOG_LEVEL', 'WARNING')},
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        'django.security': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}



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
                'core.context_processors.input_limits',
                'core.context_processors.map_config',
                'core.context_processors.customer_nav',
                'core.context_processors.detail_modal',
                'core.context_processors.internal_notifications',
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

# Redis, once available, is strictly better than DatabaseCache: same
# cross-worker sharing (fixing the login-lockout bypass above), but a hit
# AND a miss both cost one round trip — DatabaseCache's miss path costs 5
# queries (a failed SELECT, a DELETE, a COUNT for culling, a re-check
# SELECT, then the INSERT), which matters most on core.refdata's short-TTL
# lookups that miss on every expiry. Purely additive: with no REDIS_URL set
# (local dev, or before the Railway Redis plugin is added), CACHES stays
# exactly as above — nothing about DatabaseCache's behavior changes.
#
# Railway's Redis plugin variable reference can resolve to a bare hostname
# (e.g. "redis.railway.internal") rather than a full "redis://host:port"
# URL, depending on which of the plugin's exposed variables gets pasted in.
# redis-py's parse_url() raises ValueError on anything without a scheme —
# on EVERY request, since this cache backs a context processor that renders
# on every page — so this normalizes a bare host/host:port into a real URL
# instead of taking the whole site down over a Railway Variables typo.
REDIS_URL = os.environ.get('REDIS_URL', '').strip()
if REDIS_URL and '://' not in REDIS_URL:
    REDIS_URL = f'redis://{REDIS_URL}'
    if not REDIS_URL.rsplit('/', 1)[-1].isdigit():
        REDIS_URL += ':6379/0' if REDIS_URL.count(':') < 2 else '/0'
if REDIS_URL:
    CACHES['default'] = {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': REDIS_URL,
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            # Keep credentials in LOCATION. django-redis delegates this URL to
            # redis-py's ConnectionPool.from_url(), which parses (and URL-
            # decodes) both ``redis://:password@...`` and
            # ``redis://username:password@...`` correctly.
            #
            # redis-py 8 defaults new connections to RESP3. Some managed Redis
            # endpoints/proxies reject its initial ``HELLO 3 AUTH ...`` even
            # though a normal AUTH command is accepted, producing:
            # "HELLO must be called with the client already authenticated".
            # Forcing RESP2 makes redis-py authenticate with AUTH first and
            # avoids HELLO entirely. CONNECTION_POOL_KWARGS is intentional:
            # these values are passed to every connection created by the pool.
            'CONNECTION_POOL_KWARGS': {
                'protocol': 2,
            },
        },
    }

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],

    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "core.permissions.HasModuleAccess",
    ],

    # No /api/ endpoint limited how often it could be called — login has its
    # own dedicated 5-attempt lockout (accounts.rate_limit) independent of
    # this, but everything else (including the AllowAny endpoints:
    # LoginView, RegisterCustomerView, VerifyTwoFactorView) had nothing.
    # These are a general backstop, not tuned per-endpoint.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/hour",
        "user": "1000/hour",
    },
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

# Django's password-reset tokens are invalid after this many seconds. They are
# also invalidated immediately when the password changes, making each link
# single-use even before the timeout is reached.
PASSWORD_RESET_TIMEOUT = 30 * 60


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
if TESTING:
    # Tests render templates without running collectstatic first. Production
    # keeps manifest hashing; the suite uses plain static URLs so missing or
    # newly added assets do not turn unrelated view tests into 500 errors.
    STORAGES['staticfiles'] = {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    }

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# --- User-uploaded media storage -------------------------------------------
# Storage is decided by the environment so no code (models/views/templates)
# ever changes:
#   * Local dev / no credentials  -> FileSystemStorage above (MEDIA_ROOT). The
#     /media/ route in gerlyn_is/urls.py serves it. Nothing extra to install.
#   * Production with Cloudinary  -> set CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET
#     and public product/avatar uploads go to Cloudinary's CDN. Payment proofs
#     deliberately use billing.storage.PaymentProofStorage instead, so they
#     cannot inherit a public CDN URL. Mount MEDIA_ROOT on persistent private
#     storage in production.
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
# Development prints messages to the terminal by default. In production the
# default is Brevo's HTTPS API when BREVO_API_KEY is set, otherwise SMTP.
# Railway blocks outbound SMTP (587/465) on Free/Trial/Hobby plans, so an HTTPS
# API is the only transport that works there; SMTP stays available for hosts
# that allow it. Every value comes from the environment -- nothing is committed.
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '').strip()
if DEBUG:
    _default_email_backend = 'django.core.mail.backends.console.EmailBackend'
elif BREVO_API_KEY:
    _default_email_backend = 'accounts.email_backends.BrevoApiEmailBackend'
else:
    _default_email_backend = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_BACKEND = env_str('EMAIL_BACKEND', _default_email_backend)
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', False)
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '').strip()
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '10'))
DEFAULT_FROM_EMAIL = (
    os.environ.get('DEFAULT_FROM_EMAIL', '').strip()
    or EMAIL_HOST_USER
    or 'noreply@localhost'
)

# A stable deployment origin prevents verification links from inheriting an
# internal proxy hostname. Blank is safe for local development and falls back
# to the current request's scheme/host. On Railway it defaults to the service's
# own public HTTPS domain; set PUBLIC_BASE_URL to override (custom domain).
PUBLIC_BASE_URL = env_str('PUBLIC_BASE_URL').rstrip('/')
if not PUBLIC_BASE_URL and ON_RAILWAY and RAILWAY_PUBLIC_DOMAIN:
    PUBLIC_BASE_URL = f'https://{RAILWAY_PUBLIC_DOMAIN}'

# Where staff download the Windows desktop app (desktop/README.md). The
# installer is a ~120 MB build artifact, so it lives on a file host or a GitHub
# release rather than in this repo or on Railway's ephemeral disk. Blank hides
# the download card; anything that is not an https:// URL is ignored.
DESKTOP_APP_DOWNLOAD_URL = env_str('DESKTOP_APP_DOWNLOAD_URL').strip()
if not DESKTOP_APP_DOWNLOAD_URL.startswith('https://'):
    DESKTOP_APP_DOWNLOAD_URL = ''

# Alternative to the public link above: keep the installer in a *private*
# Backblaze bucket and let only signed-in staff reach it. The card then points
# at /download/desktop/, which checks the role and redirects to a link that
# expires after a few minutes (core/desktop_download.py). Needs a bucket of its
# own and a READ key for it: the backup key above is write-only, so it cannot
# sign a download. Every value but the endpoint must be set to switch this on;
# the endpoint falls back to B2_ENDPOINT. When set, it wins over
# DESKTOP_APP_DOWNLOAD_URL.
DESKTOP_APP_B2_BUCKET = env_str('DESKTOP_APP_B2_BUCKET').strip()
DESKTOP_APP_B2_KEY_ID = env_str('DESKTOP_APP_B2_KEY_ID').strip()
DESKTOP_APP_B2_APPLICATION_KEY = env_str('DESKTOP_APP_B2_APPLICATION_KEY').strip()
DESKTOP_APP_B2_FILE = env_str('DESKTOP_APP_B2_FILE').strip().lstrip('/')
DESKTOP_APP_B2_ENDPOINT = env_str('DESKTOP_APP_B2_ENDPOINT').strip() or B2_ENDPOINT

# Server-side, Nominatim-compatible geocoding (the browser never talks to the
# provider; accounts/geo_views.py proxies, caches and rate-limits). Defaults to
# OpenStreetMap's public Nominatim + tile servers so "Locate Address" works with
# no extra accounts. Their usage policy requires a real contact in
# GEOCODING_USER_AGENT and light traffic (1 req/s is enforced below) -- switch to
# a hosted or self-hosted provider through the same variables if volume grows.
# The test suite keeps these blank so no test can reach the network.
_OSM_DEFAULTS = not TESTING
GEOCODING_SEARCH_URL = env_str(
    'GEOCODING_SEARCH_URL',
    'https://nominatim.openstreetmap.org/search' if _OSM_DEFAULTS else '',
)
GEOCODING_REVERSE_URL = env_str(
    'GEOCODING_REVERSE_URL',
    'https://nominatim.openstreetmap.org/reverse' if _OSM_DEFAULTS else '',
)
GEOCODING_API_KEY = os.environ.get('GEOCODING_API_KEY', '').strip()
GEOCODING_API_KEY_PARAM = os.environ.get('GEOCODING_API_KEY_PARAM', 'key').strip()
GEOCODING_USER_AGENT = env_str(
    'GEOCODING_USER_AGENT', 'GerlynPLUS/1.0 (server-side address picker)'
)
GEOCODING_COUNTRY_CODE = os.environ.get('GEOCODING_COUNTRY_CODE', 'ph').strip()
GEOCODING_TIMEOUT = int(os.environ.get('GEOCODING_TIMEOUT', '6'))
GEOCODING_CACHE_SECONDS = int(os.environ.get('GEOCODING_CACHE_SECONDS', '86400'))
GEOCODING_NEGATIVE_CACHE_SECONDS = int(os.environ.get('GEOCODING_NEGATIVE_CACHE_SECONDS', '900'))
GEOCODING_CLIENT_MAX_REQUESTS = int(os.environ.get('GEOCODING_CLIENT_MAX_REQUESTS', '30'))
GEOCODING_CLIENT_WINDOW_SECONDS = int(os.environ.get('GEOCODING_CLIENT_WINDOW_SECONDS', '60'))
GEOCODING_PROVIDER_MIN_INTERVAL_SECONDS = int(os.environ.get('GEOCODING_PROVIDER_MIN_INTERVAL_SECONDS', '1'))
MAP_TILE_URL_TEMPLATE = env_str(
    'MAP_TILE_URL_TEMPLATE',
    'https://tile.openstreetmap.org/{z}/{x}/{y}.png' if _OSM_DEFAULTS else '',
)
MAP_TILE_API_KEY = os.environ.get('MAP_TILE_API_KEY', '').strip()
MAP_TILE_ATTRIBUTION = env_str(
    'MAP_TILE_ATTRIBUTION',
    '© OpenStreetMap contributors' if _OSM_DEFAULTS else '',
)
MAP_TILE_CACHE_SECONDS = int(os.environ.get('MAP_TILE_CACHE_SECONDS', '604800'))

# Customer fulfillment fallback when browser/address coordinates are absent.
# Blank keeps the deterministic lowest-id active branch fallback. When set,
# this branch follows service-area matching and any still-valid automatic cart
# assignment, keeping fallback behavior stable across requests.
_default_fulfillment_branch = os.environ.get('DEFAULT_FULFILLMENT_BRANCH_ID', '').strip()
try:
    DEFAULT_FULFILLMENT_BRANCH_ID = (
        int(_default_fulfillment_branch) if _default_fulfillment_branch else None
    )
except ValueError as exc:
    raise ImproperlyConfigured(
        'DEFAULT_FULFILLMENT_BRANCH_ID must be a positive integer or blank.'
    ) from exc
if DEFAULT_FULFILLMENT_BRANCH_ID is not None and DEFAULT_FULFILLMENT_BRANCH_ID < 1:
    raise ImproperlyConfigured(
        'DEFAULT_FULFILLMENT_BRANCH_ID must be a positive integer or blank.'
    )

if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured(
        'EMAIL_USE_TLS and EMAIL_USE_SSL are mutually exclusive. '
        'Use TLS with port 587 or SSL with port 465, not both.'
    )
if PUBLIC_BASE_URL and not PUBLIC_BASE_URL.startswith(('http://', 'https://')):
    raise ImproperlyConfigured('PUBLIC_BASE_URL must start with http:// or https://.')
if not DEBUG and PUBLIC_BASE_URL and not PUBLIC_BASE_URL.startswith('https://'):
    raise ImproperlyConfigured('PUBLIC_BASE_URL must use https:// in production.')

# A console/dummy backend "succeeds" without delivering anything. .env.example
# ships EMAIL_BACKEND=console for local use, and copying it into Railway
# Variables would make every verification email silently vanish.
EMAIL_BACKEND_DELIVERS = not (ON_RAILWAY and EMAIL_BACKEND in (
    'django.core.mail.backends.console.EmailBackend',
    'django.core.mail.backends.dummy.EmailBackend',
    'django.core.mail.backends.locmem.EmailBackend',
    'django.core.mail.backends.filebased.EmailBackend',
))
if not EMAIL_BACKEND_DELIVERS:
    print(
        f'WARNING: EMAIL_BACKEND={EMAIL_BACKEND} does not deliver email. Remove '
        'EMAIL_BACKEND from Railway Variables (or set BREVO_API_KEY / SMTP).',
        file=sys.stderr,
    )

# Avatar upload validation
AVATAR_MAX_MB        = 2
AVATAR_ALLOWED_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
# GCash payment-proof upload validation — a phone screenshot/photo runs
# larger than an avatar, so this gets its own, more generous cap.
PROOF_OF_PAYMENT_MAX_MB = 5
