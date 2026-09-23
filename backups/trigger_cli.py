"""What the Railway cron service runs: ask the web service to start a backup.

    python -m backups.trigger_cli

Standard library only — it must not import Django or the app, so the cron
service needs just two variables:

    BACKUP_TRIGGER_URL    https://<your-app>.up.railway.app/monitoring/backups/trigger/
    BACKUP_TRIGGER_TOKEN  the same secret set on the web service

Exits 0 when the web service accepted the request (HTTP 202) and non-zero
otherwise, so a failed trigger shows up as a failed cron run in Railway.
"""
import os
import sys
import time
import urllib.error
import urllib.request

ATTEMPTS = 4
WAIT_SECONDS = 20


def main(environ=None, opener=urllib.request.urlopen, sleep=time.sleep):
    environ = os.environ if environ is None else environ
    url = environ.get('BACKUP_TRIGGER_URL', '').strip()
    token = environ.get('BACKUP_TRIGGER_TOKEN', '').strip()
    if not url or not token:
        print('BACKUP_TRIGGER_URL and BACKUP_TRIGGER_TOKEN must both be set.', file=sys.stderr)
        return 2

    request = urllib.request.Request(url, method='POST', data=b'', headers={'Authorization': f'Bearer {token}'})
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with opener(request, timeout=60) as response:
                print(f'Backup trigger accepted (HTTP {response.status}).')
                return 0
        except urllib.error.HTTPError as exc:
            # 4xx means the request itself is wrong (bad token, backup already
            # running) — retrying won't change that.
            print(f'Backup trigger refused (HTTP {exc.code}).', file=sys.stderr)
            if exc.code < 500:
                return 1
        except (urllib.error.URLError, OSError) as exc:
            print(f'Could not reach the web service: {exc}', file=sys.stderr)
        if attempt < ATTEMPTS:
            sleep(WAIT_SECONDS)
    return 1


if __name__ == '__main__':
    sys.exit(main())
