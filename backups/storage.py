"""Where encrypted backups are kept: Backblaze B2, or a plain folder.

A tiny interface — put / get / list — so the rest of the backup code never
knows which one it is talking to, and tests use the folder version with no
network. Backblaze is reached through its S3-compatible API (boto3).

The production key is write-only, so nothing in the *backup* path may need to
read or list the bucket: put() is all it uses. get() and list() are for the
restore test, which runs with the separate read-only key.
"""
import shutil
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings


class BackupStorageError(Exception):
    """The storage backend is missing or misconfigured."""


class LocalStorage:
    """A folder on disk. For trying things out and for tests — not off-site."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, key):
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise BackupStorageError(f'Refusing a key outside the backup folder: {key}')
        return path

    def put(self, key, source):
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def get(self, key, destination):
        shutil.copyfile(self._path(key), destination)

    def list(self, prefix=''):
        """[(key, size, modified)] for every stored file whose key starts with prefix."""
        found = []
        if not self.root.exists():
            return found
        for path in self.root.rglob('*'):
            if path.is_file():
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    stat = path.stat()
                    found.append((key, stat.st_size,
                                  datetime.fromtimestamp(stat.st_mtime, timezone.utc)))
        return sorted(found)


class B2Storage:
    """Backblaze B2 through its S3-compatible endpoint."""

    def __init__(self, endpoint, key_id, application_key, bucket):
        import boto3
        from botocore.config import Config

        # Backblaze's bucket page shows the endpoint without a scheme
        # (s3.eu-central-003.backblazeb2.com); boto3 insists on one.
        endpoint = endpoint.strip()
        if '://' not in endpoint:
            endpoint = f'https://{endpoint}'
        # https://s3.us-west-004.backblazeb2.com -> us-west-004
        host = endpoint.split('//')[-1]
        region = host.split('.')[1] if host.count('.') >= 3 else 'us-west-004'
        self.bucket = bucket
        self.client = boto3.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id=key_id,
            aws_secret_access_key=application_key,
            region_name=region,
            config=Config(
                signature_version='s3v4',
                retries={'max_attempts': 5, 'mode': 'standard'},
                # Newer boto3 adds checksum trailers by default that not every
                # S3-compatible service accepts; only send them when required.
                request_checksum_calculation='when_required',
                response_checksum_validation='when_required',
            ),
        )

    def put(self, key, source):
        self.client.upload_file(str(source), self.bucket, key)

    def get(self, key, destination):
        self.client.download_file(self.bucket, key, str(destination))

    def presigned_url(self, key, expires, filename=None):
        """A GET link for one private object that stops working after
        `expires` seconds. Signing is local arithmetic (no network call), but
        the key behind this client must have read access or B2 will refuse
        the link when it is opened."""
        params = {'Bucket': self.bucket, 'Key': key}
        if filename:
            safe = filename.replace('"', '').replace('\r', '').replace('\n', '')
            params['ResponseContentDisposition'] = f'attachment; filename="{safe}"'
        return self.client.generate_presigned_url('get_object', Params=params, ExpiresIn=expires)

    def list(self, prefix=''):
        found = []
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get('Contents', []):
                found.append((item['Key'], item['Size'], item['LastModified']))
        return sorted(found)


def get_storage():
    backend = settings.BACKUP_STORAGE
    if backend == 'local':
        return LocalStorage(settings.BACKUP_LOCAL_DIR)
    if backend == 'b2':
        missing = [
            name for name in ('B2_ENDPOINT', 'B2_KEY_ID', 'B2_APPLICATION_KEY', 'B2_BUCKET')
            if not getattr(settings, name)
        ]
        if missing:
            raise BackupStorageError('Backblaze is not configured: ' + ', '.join(missing) + ' not set.')
        try:
            return B2Storage(
                settings.B2_ENDPOINT, settings.B2_KEY_ID,
                settings.B2_APPLICATION_KEY, settings.B2_BUCKET,
            )
        except Exception as exc:
            raise BackupStorageError(f'Could not set up Backblaze: {exc}')
    raise BackupStorageError(f'Unknown BACKUP_STORAGE "{backend}" (use "b2" or "local").')
