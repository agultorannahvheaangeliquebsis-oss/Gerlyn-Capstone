# Railway runbook: rebuild the store records without touching pictures

Replaces the production products' stock, sales, billing and procurement history
with the day-by-day build from `rebuild_store_records`, leaving every product
picture (files and rows) exactly as it is.

```
/opt/venv/bin/python manage.py rebuild_store_records --yes --allow-remote --no-images \
    --start 2026-09-01 --end "2026-10-20 08:00"
```

Run it in the **web service shell** on Railway. It deletes transactional
records, so the backup in step 2 is not optional: the wipe commits before the
build starts, so a build that fails part-way leaves the database emptied.

## 0. Before you start

1. **The `--no-images` fixes must be deployed.** Releases before them wipe every
   picture even with `--no-images`, and then fail the final report. In the
   shell:

   ```
   grep -n "if self.with_images" core/store_data/history.py
   ```

   It must print **three** matches: the 8-space-indented guard in
   `Builder.wipe()` (about line 243), the one in `catalog()` (about line 439)
   and the one in `verify()` (about line 1412). Two matches, or only the
   `catalog()` one, means an older release: stop. A single match at line 436 is
   the original code. (Use `/opt/venv/bin/python` for Python commands in the
   service shell; plain `python` has no Django.)

2. **Pick a quiet window**, and stay clear of the nightly backup
   (19:00 UTC = 03:00 Manila). A backup taken mid-build captures a half-built
   database.

3. **Check there is room.** Everything the rebuild rewrites is about 25 MB on
   disk (measured locally for 1 Sep to 20 Oct). The new window is shorter than
   an Aug-1 history, so tables should not grow, but the single-transaction wipe
   needs undo space (roughly 20 to 30 MB) and, if binary logging is on, the log
   records every deleted and inserted row (roughly 50 to 100 MB, kept 30 days by
   default). The MySQL volume's used/free figure is on the MySQL service's
   Metrics tab in Railway; the web shell cannot see that disk (`/app/media` is
   a different volume). With binary logging off, about 100 MB free is enough;
   with it on, about 200 MB. Under about 60 MB free: stop and grow the volume.

   On 2026-10-16 the Railway MySQL volume was 500 MB with about 390 MB used
   (redo log 100 MB, undo 2 x 16 MB, the `mysql` schema 44 MB, table files
   padded above their data), binary logging was **off**, and the whole
   application schema was about 40 MB.

   Read-only, from the web shell (no `mysql` client needed):

   ```bash
   cd /app
   /opt/venv/bin/python manage.py shell <<'EOF'
   from django.db import connection
   cur = connection.cursor()
   def q(sql):
       cur.execute(sql)
       return cur.fetchall()
   q("SET SESSION information_schema_stats_expiry = 0")   # otherwise sizes can be a day stale
   print("schemas (MB):", q("SELECT table_schema, ROUND(SUM(data_length+index_length)/1048576,1) FROM information_schema.tables GROUP BY table_schema ORDER BY 2 DESC"))
   print("top tables (name, est_rows, data_mb, index_mb):")
   for r in q("SELECT table_name, table_rows, ROUND(data_length/1048576,1), ROUND(index_length/1048576,1) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type='BASE TABLE' ORDER BY data_length+index_length DESC LIMIT 15"):
       print("  ", r)
   print(q("SHOW VARIABLES LIKE 'log_bin'"), q("SHOW VARIABLES LIKE 'binlog_expire_logs_seconds'"))
   try:
       logs = q("SHOW BINARY LOGS")
       print("binlogs:", len(logs), "files,", round(sum(int(r[1]) for r in logs)/1048576, 1), "MB")
   except Exception as e:
       print("SHOW BINARY LOGS failed:", e)    # error 1381 = binary logging is off
   EOF
   ```

   Expected biggest tables (local, after a build): `inventory_stockmovement`
   (about a third of the data), then `sales_salestransaction`, `inventory_batch`,
   `sales_salesitembatchconsumption`, `sales_salesitem`. `audit_auditlog` and
   `django_session` are untouched by the rebuild and nothing purges them, so
   only real use makes them large. `Notification` is emptied by the rebuild and
   the cache tables are negligible.

   If it will not fit, in this order: grow the volume in Railway; purge old
   binary logs (`PURGE BINARY LOGS BEFORE NOW() - INTERVAL 1 DAY`) and lower the
   retention; run `python manage.py clearsessions`. Do not reuse
   `copy_to_railway.ps1` unchanged as a fallback: it also reloads
   `inventory_product` and `inventory_productimage` with local filenames and
   carries no media files.

## 1. Record the before-state

```bash
cd /app
# products, pictures, a fingerprint of (id, product, path), products without a primary picture
/opt/venv/bin/python manage.py shell -c "
import hashlib
from inventory.models import Product, ProductImage as I
rows = sorted(I.objects.values_list('id','product_id','image'))
print(Product.objects.count(), len(rows), hashlib.sha256(repr(rows).encode()).hexdigest()[:16],
      Product.objects.exclude(images__is_primary=True).count())"

# which storage is live: read the FIRST value (True = Cloudinary). The class
# name is always DefaultStorage, Django's lazy wrapper, so it tells you nothing.
/opt/venv/bin/python manage.py shell -c "
from django.conf import settings as s; from django.core.files.storage import default_storage as d
print(s.USE_CLOUDINARY, s.MEDIA_ROOT, type(d).__name__)"

# files on the volume (only meaningful if USE_CLOUDINARY is False)
ls -la /app/media; find /app/media/products -type f 2>/dev/null | wc -l
```

The last number is how many products have no picture flagged primary. It is
informational: with `--no-images` the build skips that check, so a catalogue
with unflagged or uploaded pictures is fine. (An older release without the
`verify()` guard would fail its final report *after* committing the data.)

Also run the picture-load check from step 5 now, so a picture that already
404s is not blamed on the rebuild. On Cloudinary an empty `/app/media/products`
is expected; the volume then holds only `payment_proofs`.

## 2. Back up

```bash
cd /app
/opt/venv/bin/python manage.py run_backup --trigger manual
```

Confirm a SUCCESS run on the Backups page. That archive is encrypted and goes
to `/app/tmp_backups` (container disk, lost on redeploy) unless `B2_BUCKET` is
set. So also take a plain dump on a workstation with the MySQL 9.x client,
against the MySQL service's **public** URL (`MYSQL_PUBLIC_URL`; the
`*.railway.internal` host only works from inside Railway). PowerShell:

```powershell
$u = [uri]"PASTE_MYSQL_PUBLIC_URL_HERE"
$user, $pass = $u.UserInfo -split ':', 2
$env:MYSQL_PWD = [uri]::UnescapeDataString($pass)
$file = "$HOME\railway_before_rebuild_$(Get-Date -Format yyyy-MM-dd).sql"
& "C:\Program Files\MySQL\MySQL Server 9.7\bin\mysqldump.exe" `
  -h $u.Host -P $u.Port -u ([uri]::UnescapeDataString($user)) `
  --single-transaction --no-tablespaces --set-gtid-purged=OFF `
  --result-file=$file $u.AbsolutePath.TrimStart('/')
Remove-Item Env:MYSQL_PWD
(Get-Item $file).Length / 1MB; Get-Content $file -Tail 1   # expect -- Dump completed
```

Do not use XAMPP's client: it cannot do `caching_sha2_password`. If it hangs
for a couple of minutes, press Ctrl+C and run it again; Railway's public proxy
sometimes stalls a TLS session. Do not leave the connection URL in an
environment variable afterwards; it contains the database password.

## 3. Run it, detached

A dropped shell between the wipe and the build would leave the database empty,
so detach the process:

```bash
cd /app
nohup /opt/venv/bin/python manage.py rebuild_store_records --yes --allow-remote --no-images \
    --start 2026-09-01 --end "2026-10-20 08:00" > /tmp/rebuild.log 2>&1 &
```

Then wait for it. This blocks until the process exits and prints the result;
Ctrl+C or a closed tab ends only the wait, never the rebuild:

```bash
while pgrep -f rebuild_store_records > /dev/null; do sleep 20; done; tail -n 30 /tmp/rebuild.log
```

To look without waiting: `pgrep -af rebuild_store_records || echo finished`.

**The log stays empty until the end.** Python buffers output written to a file,
so the progress lines appear only when the process exits. The log containing
just `nohup: ignoring input` is normal; a real failure writes a traceback at
once. Judge progress by whether the process is alive and by the MySQL volume
graph, which should stay under about 470 MB.

A local run took 15 minutes; allow two to three times that on Railway. Do not
start a second rebuild or a backup while it runs, and do not push to the
branch Railway deploys from: a deploy restarts the container and kills the
process mid-build.

## 4. Check the log

- 15 lines marked `[ok]` (16 on a build that generates pictures; with
  `--no-images` the "every product has a primary image" line is not
  printed), none marked `[FAIL]`, and no "consistency check(s) failed".
- It ends with
  `Built through 2026-10-20 08:00: 4125 walk-in sales, 216 online orders (22 cancelled), 551 purchase orders (543 receipts), 530 restock slips, 7 write-offs, 14 supplier returns, 11 refunds.`
  These are the local figures for the same seed. A small difference means
  production holds accounts the local database does not.

## 5. Post-run checks

**Products and pictures unchanged.** Re-run the fingerprint from step 1. All
four values must be identical to before.

**Pictures load, nothing was re-uploaded.** Request every picture's URL. This
works for both storages and needs no extra tools (on Cloudinary
`default_storage.exists()` costs an API call per file, so do not use it):

```bash
/opt/venv/bin/python manage.py shell -c "
import urllib.request
from inventory.models import ProductImage as I
bad = []
for i in I.objects.all():
    try:
        ok = urllib.request.urlopen(urllib.request.Request(i.image.url, method='HEAD'), timeout=20).status == 200
    except Exception:
        ok = False
    if not ok: bad.append((i.id, i.image.name))
print('checked', I.objects.count(), 'bad', len(bad), bad[:5])"
```

Expect `checked 105 bad 0 []` (the count is however many picture rows there
were in step 1). On filesystem storage also compare
`find /app/media/products -type f | wc -l` with step 1. Then open the
storefront catalogue and confirm the pictures render.

**Sales window:**

```bash
/opt/venv/bin/python manage.py shell <<'EOF'
import datetime as dt
from django.utils import timezone as tz
from core.store_data.history import MANILA
from sales.models import SalesTransaction as T
cut = dt.datetime(2026, 10, 20, 8, 0, tzinfo=MANILA)
first = T.objects.earliest('transaction_date').transaction_date
last = T.objects.latest('transaction_date').transaction_date
print("count", T.objects.count(), "| first", tz.localtime(first), "| latest", tz.localtime(last), "| after cutoff", T.objects.filter(transaction_date__gt=cut).count())
EOF
```

Expect `count 4341`, `first` on 2026-09-01, `latest` on 20 Oct before 08:00,
and `after cutoff 0`. The Sales list sorted newest first should show 20 Oct at
the top.

"No future-dated sales" means nothing after `--end`, not nothing after the
server clock. While the server clock is earlier than the `--end` moment, the
build contains sales that are in the future relative to it; that is by design.

**Space.** Re-run the size query from step 0 and look at the volume metric.

## 6. Roll back

- **Build failed or a check failed:** restore the plain dump. `mysqldump`
  drops and recreates every table, so this is a complete rollback:

  ```
  mysql -h <host> -P <port> -u <user> -p <db> < before_rebuild.sql
  ```

  The encrypted `run_backup` copy restores through
  [backup-and-restore.md](backup-and-restore.md).
- **Pictures** need no rollback; `--no-images` never touches them.
- **Data is fine but `--end` was wrong:** re-run the command with the right
  `--end`. It wipes and rebuilds, so no restore is needed.

## Where pictures live

The repository cannot say which storage production uses; it depends on the
environment.

- `CLOUDINARY_CLOUD_NAME` set: `settings.py` switches to
  `MediaCloudinaryStorage` and pictures are on Cloudinary.
- Not set: `FileSystemStorage` at `MEDIA_ROOT` (`BASE_DIR/media`, `/app/media`
  on Railway), which must be a mounted volume or the files vanish on every
  redeploy. `/media/` serves only `products`, `categories` and `store` paths.

Production was observed on 2026-10-16 with `USE_CLOUDINARY=True`: pictures on
Cloudinary, and the `/app/media` volume holding only `payment_proofs`.

With `--no-images`, neither the files nor the rows are touched in either case.
One exception: products that are not in the master list are still deleted by
the build, and their picture rows go with them (the files stay on disk).
