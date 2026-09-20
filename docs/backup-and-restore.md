# Backup, retention and recovery

What is backed up, where it goes, who can touch it, how long it is kept, and how
to get it back. Owner-facing status lives at **System & Monitoring → Backups**.

## 1. What is backed up

| Part | What | Notes |
|---|---|---|
| Database | Every table (`mysqldump`, consistent snapshot) | Includes the Audit Log and its triggers |
| Uploaded files | Everything under `MEDIA_ROOT` (`/app/media` — payment proofs) | Lives on the Railway Volume |
| Erased-accounts list | IDs and dates of accounts whose personal data was erased | IDs only, no personal data. See §7 |
| Manifest | What the run produced, SHA-256 checksums, row counts, file fingerprint | Used by the restore test |

**Not backed up** (know these):

- **Railway Variables** (secrets, keys, tokens). Keep an up-to-date copy in the
  password manager. Without `BACKUP_ENCRYPTION_KEY` no backup can be read.
- **Cloudinary images** (product photos, avatars) when Cloudinary is enabled —
  they live in Cloudinary, not on the Volume.
- The application code (that is git).

Every file is **encrypted on the server before it leaves it** (AES-256-GCM, a
different key per file derived from `BACKUP_ENCRYPTION_KEY`). Backblaze only ever
holds ciphertext.

## 2. Schedule, retention, targets

| | |
|---|---|
| Runs | Daily at **03:00 Manila time** (19:00 UTC), by the Railway cron service |
| `daily/` | kept **14 days** |
| `weekly/` (Sundays, UTC) | kept **8 weeks** |
| `monthly/` (the 1st, UTC) | kept **12 months** |
| `erasures/` | kept **12 months** (Backblaze lifecycle rule; the app writes the list into every folder above) |
| **RPO** (most data you can lose) | **24 hours** — the time since the last backup |
| **RTO** (time to be running again) | **about 4 hours**, once the steps in §6 are practised |

Retention is enforced by **Backblaze lifecycle rules** on the bucket
(`fileNamePrefix` → *Days Till Hide* / *Days Till Delete = 1*), not by the app —
the app's key cannot delete anything. Rules: `daily/` 14, `weekly/` 56,
`monthly/` 365, `erasures/` 365.

## 3. Storage and access control

- **Where:** Backblaze B2, private bucket `gerlyn-backups`, default encryption on.
  Off Railway on purpose: losing the Railway project must not lose the backups.
- **Writer key** (`gerlyn-backup-writer`): *Write Only*, this bucket only, no
  expiry. Lives in Railway Variables. Can add files; cannot read, list or delete.
- **Restorer key** (`gerlyn-backup-restorer`): *Read Only*, this bucket only.
  Lives in the Owner's password manager. Used only for restore tests and real
  restores — never left on Railway.
- **Encryption key** (`BACKUP_ENCRYPTION_KEY`): on Railway as a Variable **and**
  two offline copies (password manager + printed, stored safely).
- **Backblaze account:** two-factor authentication on. Nobody else has the login.
- **App access:** Backup Status and "Run backup now" are Owner (and Django
  superuser) only. The daily trigger URL needs the secret `BACKUP_TRIGGER_TOKEN`
  (sent as a Bearer header, never in the URL) and does not exist when it is unset.
- **Rotation:** rotate both Backblaze keys and the trigger token once a year, or
  immediately if a laptop or a Railway seat is lost. Rotating the *encryption*
  key makes older backups unreadable with the new key — keep the old key with the
  backups it belongs to.

## 4. Setting it up

### Backblaze (one time)
Bucket `gerlyn-backups` (private), the four lifecycle rules above, and the two
application keys. Note the bucket **endpoint** (Buckets → your bucket, e.g.
`s3.us-west-004.backblazeb2.com`).

### Railway — web service, Variables

| Variable | Value |
|---|---|
| `B2_ENDPOINT` | `https://s3.<region>.backblazeb2.com` |
| `B2_KEY_ID` / `B2_APPLICATION_KEY` | the **writer** key |
| `B2_BUCKET` | `gerlyn-backups` |
| `BACKUP_ENCRYPTION_KEY` | from `python -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"` — save the offline copies first |
| `BACKUP_TRIGGER_TOKEN` | any long random string, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

`mysqldump` comes from `nixpacks.toml` (installs the MySQL client on deploy).

### Railway — the cron service
1. Project → **New** → **GitHub Repo** → pick the same repo. Name it `backup-cron`.
2. **Settings → Config-as-code → Railway Config File**: `/railway.cron.json`
   (holds the schedule `0 19 * * *` = 03:00 Manila, and the start command).
   *Do not* add a cron schedule to the web service itself.
3. **Variables** (only these two):
   - `BACKUP_TRIGGER_URL` = `https://<your-app>.up.railway.app/monitoring/backups/trigger/`
   - `BACKUP_TRIGGER_TOKEN` = the same value as on the web service
4. Deploy. It runs at the scheduled time, asks the web service to start a backup
   (the web service has the Volume) and exits. A failed trigger shows as a failed
   cron run in Railway.

### First check
Open **System & Monitoring → Backups**. Every row of the *Setup check* should be
**OK** (in particular *mysqldump*). Press **Run backup now**, refresh after a
minute: four rows should show *Succeeded*, and `daily/` in the Backblaze bucket
should hold four `.enc` files.

## 5. Monitoring

- The Backups page shows the last successful database and file backup and turns
  **Overdue** after 26 hours (`BACKUP_STALE_HOURS`).
- A failed run, a run that couldn't start (missing key/bucket), and a run that
  finds the previous backup overdue each send the Owner a **Notification**.
- Backup activity is not in the Audit Log (it is not a data change); the status
  page is the record. Failures also reach Sentry.

## 6. Restoring

Always start with the checks in §8 (a restore test) so the first time you do this
is not during an emergency.

Everything below uses **`manage.py verify_backup`**, which downloads, verifies
checksums, decrypts and restores into a database **you name**. It never touches
the database in `DB_NAME`.

### Restore the database and files to Railway (disaster recovery)
1. On the web service, temporarily replace `B2_KEY_ID` / `B2_APPLICATION_KEY` with
   the **restorer** key (the writer key cannot read). Keep the writer key noted.
2. Open the web service **Shell** (`/opt/venv/bin/python` is the interpreter) and run:
   ```
   /opt/venv/bin/python manage.py verify_backup --keep \
       --scratch-db gerlyn_restored \
       --media-dir /app/media \
       --ledger-out /tmp/erasures.json
   ```
   Add `--folder weekly` / `monthly` or `--stamp 20260922-190000Z` to pick an older
   backup. It prints `RESTORE CHECK PASSED` when the restored tables and files
   match the manifest.
3. Re-apply erasures (§7) **before** switching, against the restored database:
   ```
   DB_NAME=gerlyn_restored /opt/venv/bin/python manage.py reapply_erasures --ledger /tmp/erasures.json
   ```
4. Set the web service's `DB_NAME` variable to `gerlyn_restored` and redeploy.
5. Put the **writer** key back in `B2_KEY_ID` / `B2_APPLICATION_KEY`.
6. Log in, spot-check recent orders and a payment proof, and run **Run backup now**.

### Restore only the uploaded files
Same command with `--media-dir /app/media` (the database part is restored to the
scratch database, which you can drop with the SQL `DROP DATABASE gerlyn_restored`).

## 7. Erased accounts must stay erased

A hard delete of personal data (`hard_delete_personal_data`) only changes the
live database. An **older backup still contains that person**, so restoring it
would bring them back. Each backup therefore stores the list of erased account
IDs (no personal data). After *any* restore, run:

```
python manage.py reapply_erasures --ledger erasures.json     # from --ledger-out
python manage.py reapply_erasures --dry-run --ledger erasures.json   # preview
```

It erases every account on the list that is still present, and is safe to repeat.
Backups themselves also age out under the retention rules above, so an erased
person's data is gone from Backblaze within 12 months at the latest.

## 8. The restore test

Do one after setup, then at least **every three months** and after any change to
the backup system. The status page turns amber when the last one is over 100 days old.

On your **laptop** (needs the MySQL command-line tools; set `BACKUP_MYSQLDUMP` /
`BACKUP_MYSQL` in `.env` if they are not on your PATH):

1. In `.env` (git-ignored) set `BACKUP_STORAGE=b2`, the `B2_*` values with the
   **restorer** key, and `BACKUP_ENCRYPTION_KEY`. Point `DB_*` at your **local**
   MySQL (any database — only a scratch database is created and dropped).
2. Run:
   ```
   python manage.py verify_backup --replace --media-dir restored_media
   ```
3. It must end with `RESTORE CHECK PASSED`. It reports tables checked, the file
   count/bytes, and whether the files match the manifest. Busy tables
   (sessions, notifications, audit log, backup runs) may differ slightly because
   they change while a backup is taken; every business table must match exactly.
4. Delete `restored_media` afterwards (it holds real payment proofs).
5. On **Backups → Restore tests**, record the date, backup used, result and notes.

### Restore test log

| # | Date | Backup used | Where | Result | Notes |
|---|---|---|---|---|---|
| 0 | 2026-09-20 | `daily/…-20260920-164409Z` (local folder, not Backblaze) | Developer laptop, MariaDB 10.6 | **PASSED** | Rehearsal of the tooling: 50 tables matched, 53 files (171,226 bytes) byte-identical, scratch database dropped, wrong key correctly refused. Not a Backblaze test. |
| 1 | _pending_ | _first real Backblaze backup_ | Owner's laptop | | Do this after the first scheduled backup. |

## 9. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Setup check: *mysqldump* not found | The deploy hasn't picked up `nixpacks.toml` yet, or the base image lacks the package. Check the build log; run `mysqldump --version` in the Shell. |
| Setup check: Backblaze storage not set | A `B2_*` Variable is missing on the **web** service. |
| Backup fails with `AccessDenied` | Wrong/expired writer key, or the key isn't restricted to this bucket. |
| Cron run fails `HTTP 403` | `BACKUP_TRIGGER_TOKEN` differs between the two services. |
| Cron run fails `HTTP 404` | `BACKUP_TRIGGER_TOKEN` isn't set on the web service, or the URL is wrong. |
| Cron run fails `HTTP 409` | A backup was already running (normal if triggered twice). |
| Restore: "Decryption failed: wrong BACKUP_ENCRYPTION_KEY" | Different key than the one the backup was made with. Use the offline copy. |
| Restore: "checksum does not match" | The download was corrupted or the file was altered; try again, or use another day's backup. |
| Backup stays "Running" for hours | The process was killed (redeploy). It is marked failed automatically after 3 hours. |
