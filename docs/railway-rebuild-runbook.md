# Railway runbook: rebuild the store records without touching pictures

Replaces the production products' stock, sales, billing and procurement history
with the day-by-day build from `rebuild_store_records`, leaving every product
picture (files and rows) exactly as it is.

```
python manage.py rebuild_store_records --yes --allow-remote --no-images \
    --start 2026-09-01 --end "2026-10-20 08:00"
```

Run it in the **web service shell** on Railway. It deletes transactional
records, so the backup in step 2 is not optional: the wipe commits before the
build starts, so a build that fails part-way leaves the database emptied.

## 0. Before you start

1. **The `--no-images` fix must be deployed.** Releases before it wipe every
   picture even with `--no-images`. In the shell:

   ```
   grep -n "if self.with_images" core/store_data/history.py
   ```

   It must print a match inside `Builder.wipe()`. If it prints nothing, stop.

2. **Pick a quiet window**, and stay clear of the nightly backup
   (19:00 UTC = 03:00 Manila). A backup taken mid-build captures a half-built
   database.

3. **Check there is room.** Everything the rebuild rewrites is about 25 MB on
   disk (measured locally for 1 Sep to 20 Oct). The new window is shorter than
   an Aug-1 history, so tables should not grow, but the single-transaction wipe
   needs undo space and, if binary logging is on, the log records every deleted
   and inserted row (roughly 50 to 100 MB, kept 30 days by default).
   About 200 MB free is comfortable; under about 100 MB is risky.

   In the MySQL shell (read-only):

   ```sql
   SET SESSION information_schema_stats_expiry = 0;   -- otherwise sizes can be a day stale
   SELECT table_name, table_rows AS est_rows,
          ROUND(data_length/1048576,1)  AS data_mb,
          ROUND(index_length/1048576,1) AS index_mb,
          ROUND(data_free/1048576,1)    AS free_in_file_mb
   FROM information_schema.tables
   WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'
   ORDER BY data_length + index_length DESC LIMIT 15;
   SHOW BINARY LOGS;                                  -- add up File_size
   SHOW VARIABLES LIKE 'binlog_expire_logs_seconds';
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
# products, pictures, a fingerprint of (id, product, path), products without a primary picture
python manage.py shell -c "
import hashlib
from inventory.models import Product, ProductImage as I
rows = sorted(I.objects.values_list('id','product_id','image'))
print(Product.objects.count(), len(rows), hashlib.sha256(repr(rows).encode()).hexdigest()[:16],
      Product.objects.exclude(images__is_primary=True).count())"

# which storage is live
python manage.py shell -c "
from django.conf import settings as s; from django.core.files.storage import default_storage as d
print(s.USE_CLOUDINARY, s.MEDIA_ROOT, type(d).__name__)"

# files on the volume (skip if Cloudinary)
find /app/media/products -type f | wc -l; du -sb /app/media/products
```

The last number of the fingerprint line must be `0`. A product without a
primary picture fails the build's final check *after* the data is committed.

Also run the picture-load check from step 5 now, so a picture that already
404s is not blamed on the rebuild.

## 2. Back up

```bash
python manage.py run_backup --trigger manual
```

Confirm a SUCCESS run on the Backups page. That archive is encrypted and goes
to `/app/tmp_backups` (container disk, lost on redeploy) unless `B2_BUCKET` is
set. So also take a plain dump from a workstation with the MySQL 9.x client,
against the MySQL service's `MYSQL_PUBLIC_URL`:

```
mysqldump --single-transaction --no-tablespaces --result-file=before_rebuild.sql <db>
```

Do not use XAMPP's client: it cannot do `caching_sha2_password`.

## 3. Run it, detached

A dropped shell between the wipe and the build would leave the database empty,
so detach the process:

```bash
cd /app
nohup python manage.py rebuild_store_records --yes --allow-remote --no-images \
    --start 2026-09-01 --end "2026-10-20 08:00" > /tmp/rebuild.log 2>&1 &
tail -f /tmp/rebuild.log
```

A local run took 15 minutes; allow two to three times that on Railway. Do not
start a second rebuild or a backup while it runs.

## 4. Check the log

- 16 lines marked `[ok]`, none marked `[FAIL]`, and no
  "consistency check(s) failed".
- It ends with
  `Built through 2026-10-20 08:00: 4125 walk-in sales, 216 online orders (22 cancelled), 551 purchase orders (543 receipts), 530 restock slips, 7 write-offs, 14 supplier returns, 11 refunds.`
  These are the local figures for the same seed. A small difference means
  production holds accounts the local database does not.

## 5. Post-run checks

**Products and pictures unchanged.** Re-run the fingerprint from step 1. All
four values must be identical to before, and the last must be `0`.

**Pictures load, nothing was re-uploaded:**

```bash
python manage.py shell -c "
from django.core.files.storage import default_storage as d
from inventory.models import ProductImage as I
print('missing files:', sum(not d.exists(i.image.name) for i in I.objects.all()))
for i in I.objects.order_by('?')[:3]: print(i.image.url)"

curl -s -o /dev/null -w '%{http_code} %{content_type} %{size_download}\n' \
    https://$RAILWAY_PUBLIC_DOMAIN<url>          # expect 200 image/jpeg
find /app/media/products -type f | wc -l; du -sb /app/media/products   # same as step 1
```

Then open the storefront catalogue and confirm the pictures render.

**Sales window:**

```bash
python manage.py shell -c "
import datetime as dt
from django.utils import timezone as tz
from core.store_data.history import MANILA
from sales.models import SalesTransaction as T
cut = dt.datetime(2026,10,20,8,0,tzinfo=MANILA)
print(T.objects.count(), tz.localtime(T.objects.latest('transaction_date').transaction_date),
      T.objects.filter(transaction_date__gt=cut).count())"
```

Expect `4341`, a latest date on 20 Oct before 08:00, and `0` after the cutoff.
The Sales list sorted newest first should show 20 Oct at the top.

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

With `--no-images`, neither the files nor the rows are touched in either case.
One exception: products that are not in the master list are still deleted by
the build, and their picture rows go with them (the files stay on disk).
