"""Authenticated, streaming encryption for backup files (AES-256-GCM).

File layout:  b"GVB1" | 16-byte random salt | chunk*   where each chunk is a
4-byte big-endian length followed by that many bytes of ciphertext+tag.

  * The key actually used for a file is derived (HKDF-SHA256) from the master
    key and that file's own random salt, so no two files share a key.
  * The plaintext is cut into 1 MiB chunks, so a large database or media
    archive is never held in memory whole.
  * Each chunk's nonce is its counter plus a "final chunk" flag, and the header
    is bound in as associated data. Reordering, dropping or duplicating chunks,
    cutting the file short, or appending to it all fail authentication — a
    truncated backup is reported as broken, never silently accepted as smaller.
"""
import base64
import binascii
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b'GVB1'
SALT_LEN = 16
HEADER_LEN = len(MAGIC) + SALT_LEN
CHUNK_SIZE = 1024 * 1024
TAG_LEN = 16


class BackupCryptoError(Exception):
    """A backup file could not be encrypted, or could not be trusted."""


def generate_key():
    """A fresh master key, in the format BACKUP_ENCRYPTION_KEY expects."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def load_key(value):
    """Decode the BACKUP_ENCRYPTION_KEY setting into 32 raw bytes."""
    if not value or not value.strip():
        raise BackupCryptoError('BACKUP_ENCRYPTION_KEY is not set.')
    try:
        raw = base64.urlsafe_b64decode(value.strip().encode())
    except (binascii.Error, ValueError):
        raise BackupCryptoError('BACKUP_ENCRYPTION_KEY is not valid url-safe base64.')
    if len(raw) != 32:
        raise BackupCryptoError('BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes.')
    return raw


def _file_cipher(master_key, salt):
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt, info=b'gerlyn-backup-v1',
    ).derive(master_key)
    return AESGCM(derived)


def _nonce(counter, final):
    return counter.to_bytes(11, 'big') + (b'\x01' if final else b'\x00')


def encrypt_file(src, dst, master_key):
    salt = os.urandom(SALT_LEN)
    header = MAGIC + salt
    cipher = _file_cipher(master_key, salt)
    with open(src, 'rb') as fin, open(dst, 'wb') as fout:
        fout.write(header)
        counter = 0
        chunk = fin.read(CHUNK_SIZE)
        while True:
            following = fin.read(CHUNK_SIZE)
            final = not following
            sealed = cipher.encrypt(_nonce(counter, final), chunk, header)
            fout.write(struct.pack('>I', len(sealed)))
            fout.write(sealed)
            if final:
                return
            chunk, counter = following, counter + 1


def decrypt_file(src, dst, master_key):
    with open(src, 'rb') as fin:
        header = fin.read(HEADER_LEN)
        if len(header) != HEADER_LEN or header[:len(MAGIC)] != MAGIC:
            raise BackupCryptoError('Not a Gerlyn backup file (bad header).')
        cipher = _file_cipher(master_key, header[len(MAGIC):])
        counter = 0
        with open(dst, 'wb') as fout:
            while True:
                raw_length = fin.read(4)
                if len(raw_length) != 4:
                    raise BackupCryptoError('The backup file is truncated.')
                (length,) = struct.unpack('>I', raw_length)
                if length < TAG_LEN or length > CHUNK_SIZE + TAG_LEN:
                    raise BackupCryptoError('The backup file is corrupted.')
                sealed = fin.read(length)
                if len(sealed) != length:
                    raise BackupCryptoError('The backup file is truncated.')
                try:
                    plain, final = cipher.decrypt(_nonce(counter, False), sealed, header), False
                except InvalidTag:
                    try:
                        plain, final = cipher.decrypt(_nonce(counter, True), sealed, header), True
                    except InvalidTag:
                        raise BackupCryptoError(
                            'Decryption failed: wrong BACKUP_ENCRYPTION_KEY, or the file is '
                            'corrupted or has been tampered with.'
                        )
                fout.write(plain)
                if final:
                    if fin.read(1):
                        raise BackupCryptoError('Unexpected data after the end of the backup.')
                    return
                counter += 1
