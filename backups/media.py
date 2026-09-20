"""Archiving the uploaded-files folder (MEDIA_ROOT) and proving it survived.

Payment proofs live here on a Railway Volume. The archive is fingerprinted from
its own contents, so a restore test can extract it and check that every file
came back byte for byte, without ever needing the live folder.
"""
import hashlib
import tarfile
from pathlib import Path


def archive_media(root, destination_tgz):
    """Tar+gzip every file under `root` (an empty archive if it doesn't exist)."""
    root = Path(root)
    with tarfile.open(destination_tgz, 'w:gz') as tar:
        if root.is_dir():
            for path in sorted(root.rglob('*')):
                if not path.is_file():
                    continue
                try:
                    tar.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
                except FileNotFoundError:
                    # Removed while the archive was being written.
                    continue
    return fingerprint_archive(destination_tgz)


def fingerprint_archive(archive_tgz):
    """{'files', 'bytes', 'tree_sha256'} computed from the archive's contents."""
    tree = hashlib.sha256()
    files = total = 0
    with tarfile.open(archive_tgz, 'r:gz') as tar:
        for member in sorted((m for m in tar.getmembers() if m.isfile()), key=lambda m: m.name):
            digest = hashlib.sha256()
            handle = tar.extractfile(member)
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
            tree.update(f'{member.name}\0{digest.hexdigest()}\n'.encode())
            files += 1
            total += member.size
    return {'files': files, 'bytes': total, 'tree_sha256': tree.hexdigest()}


def extract_archive(archive_tgz, destination):
    with tarfile.open(archive_tgz, 'r:gz') as tar:
        tar.extractall(destination, filter='data')
