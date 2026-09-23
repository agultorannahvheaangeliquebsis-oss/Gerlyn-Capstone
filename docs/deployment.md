# Deploying GerlynPLUS

**Target: live by early September 2026** — this is the gate before User
Evaluation. The three IT experts and the store staff cannot evaluate a system
that only runs on our laptops.

Recommended host: **Railway** (~$5/month). It gives MySQL in one click and
deploys straight from GitHub.

> ⚠️ **Do not follow old tutorials for PythonAnywhere's free tier.** It moved
> MySQL to a paid tier for new accounts in **January 2026**. Blog posts saying
> "free MySQL" are out of date.

The repo is already prepared: `Procfile`, `railway.json`, gunicorn, WhiteNoise,
`collectstatic`, and env-based settings are all in place and were tested in
production mode locally.

---

## Steps

### 1. Push to GitHub
Already done — `main` on `Gerlyn-Capstone`. Railway deploys from there.

### 2. Create the Railway project
1. railway.app → **New Project** → **Deploy from GitHub repo** → pick the repo.
2. Add **MySQL**: *New* → *Database* → *Add MySQL*. One click.

### 3. Set the environment variables

In the service → **Variables**:

| Variable | Value |
|---|---|
| `DJANGO_SECRET_KEY` | Generate a NEW one (never reuse the dev key) |
| `DJANGO_DEBUG` | `False` |
| `DJANGO_ALLOWED_HOSTS` | `<your-app>.up.railway.app` |
| `DJANGO_CSRF_TRUSTED` | `https://<your-app>.up.railway.app` |
| `DB_NAME` | `${{MySQL.MYSQL_DATABASE}}` |
| `DB_USER` | `${{MySQL.MYSQLUSER}}` |
| `DB_PASSWORD` | `${{MySQL.MYSQLPASSWORD}}` |
| `DB_HOST` | `${{MySQL.MYSQLHOST}}` |
| `DB_PORT` | `${{MySQL.MYSQLPORT}}` |

The `${{MySQL.*}}` syntax pulls values from the database service — don't paste
credentials by hand.

Generate the secret key with:
```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

> `DJANGO_CSRF_TRUSTED` is easy to forget. Without it every form POST — including
> **login** — fails with a CSRF error, and the site looks completely broken.

### 4. Add a volume for uploads (important)

Railway's filesystem is **ephemeral**: anything written at runtime is wiped on
every redeploy. GCash **payment proofs** are uploaded at runtime, so without a
volume they disappear — and those images are the evidence behind our billing
audit trail.

Service → **Settings** → **Volumes** → mount at **`/app/media`**.

### 5. Deploy + seed
Railway builds and runs migrations automatically (`railway.json`). Then, once:

```bash
railway run python manage.py seed_demo --reset
```

Or use Railway's shell. This creates the demo data and the logins.

### 5b. Create the cache table (one-time)

The login lockout (`accounts/rate_limit.py`) counts failed attempts in the
cache, and the cache backend is now `DatabaseCache` (`CACHES` in
`settings.py`) so all Gunicorn workers share one durable count. That needs a
table, and **`migrate` does not create it** — it is a separate command:

```bash
python manage.py createcachetable
```

- Run it **once in production** (Railway shell or `railway run …`). It is also
  wired into `railway.json`'s start command, which runs it on every deploy, so
  a brand-new production database can't come up without it. The command is
  idempotent — re-running it just prints "already exists".
- Run it **once locally too**, in every working copy / database you test
  against, or the lockout has nowhere to write and `manage.py test` will error
  on the cache table.

Without this the lockout silently degrades: each worker keeps its own count
(real cap = 5 × worker count) and every redeploy resets it to zero.

### 6. Test the live site — properly
- [ ] Open the URL. Log in as `gerlyn` / `demo1234`.
- [ ] **CSS loads** (if the page looks like plain text, `collectstatic` failed).
- [ ] Log in as `carlo` — he must see **Tarcan only**.
- [ ] Record a walk-in sale; check stock drops.
- [ ] **Open it on a phone using mobile data, not your wifi.** This is the only
      test that proves it's really reachable from outside.
- [ ] Upload a payment proof, redeploy, and confirm the image still loads
      (proves the volume works).

### 7. Write the URL into `README.md` and tell the team.

---

## Gotchas already handled in the code

Each of these was found by running the app in production mode locally, not by
guessing:

| Problem | Handled by |
|---|---|
| `DEBUG=False` serves **no CSS** — the whole site renders unstyled | WhiteNoise + `collectstatic` + `STATIC_ROOT` |
| Uploaded payment proofs **404** in production (`static()` is a no-op when `DEBUG=False`) | explicit media route in `gerlyn_is/urls.py` |
| Infinite redirect loop behind the host's TLS proxy | `SECURE_PROXY_SSL_HEADER` |
| App boots with an insecure default `SECRET_KEY` | startup guard raises `RuntimeError` |
| Django's own `check --deploy` raised 5 security warnings | HSTS, secure cookies, nosniff, `X_FRAME_OPTIONS` — now **0 warnings** |
| Login lockout counted attempts in per-process memory (LocMemCache), so each Gunicorn worker had its own count and redeploys reset it | `DatabaseCache` in `settings.py` — shared across workers + durable. Needs `manage.py createcachetable` once per environment (see **step 5b**); it's in `railway.json`'s start command for production |

Verify anytime with:
```bash
DJANGO_DEBUG=False DJANGO_SECRET_KEY=<a-real-key> python manage.py check --deploy
```

---

## Schedule daily inventory tasks

Create a Railway cron service from this repository with this command:

```bash
python manage.py dispatch_notifications
```

Run it once daily, ideally shortly after midnight Asia/Manila. The idempotent
job first removes batches whose expiry date has arrived from floor/warehouse
stock and records batch-linked expiry write-offs, then catches batches that
entered the 30-day notification window as time passed. Availability queries
also exclude due lots immediately, so a delayed cron run cannot make them
sellable.

---

## Test it in production mode locally first

Do this **before** deploying — it catches nearly everything:

```bash
python manage.py collectstatic --noinput
python manage.py createcachetable          # one-time; safe to re-run

export DJANGO_SECRET_KEY="$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')"
export DJANGO_DEBUG=False
export DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1"
export DJANGO_SECURE_SSL=False        # no TLS locally, so don't force the redirect

gunicorn gerlyn_is.wsgi --bind 127.0.0.1:8000
```

Open <http://127.0.0.1:8000/>. If the CSS loads and you can log in, the deploy
will almost certainly work.

---

## Always keep the local fallback

Keep `./run.sh` working. On defense day have **three** ways to demo:

1. the live URL,
2. a laptop running locally,
3. a screen recording.

Never depend on the venue's wifi.

---

## Cost

~$5/month ≈ ₱280. For September–November that's roughly **₱850 total**, about
**₱140 each** across six members. Don't spend a week trying to avoid it — the
deployment date is what protects the evaluation phase.
