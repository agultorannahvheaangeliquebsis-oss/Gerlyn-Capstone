"""Photo-evidence rules: how many, what kind, how big.

One report holds up to MAX_PHOTOS pictures, each a real JPEG or PNG of at most
MAX_PHOTO_MB. "Real" matters: the file's own header is read with Pillow, because
the name and the browser-declared content type are both just text the uploader
chose. A page saved as photo.jpg, an SVG, or a WebP renamed to .png is refused
rather than stored and later served back to a reviewer.
"""
from .errors import DamageReportError

MAX_PHOTOS = 5
MAX_PHOTO_MB = 5
MAX_PHOTO_BYTES = MAX_PHOTO_MB * 1024 * 1024
# A decoded picture is width x height x ~4 bytes. 40 megapixels is far past any
# phone camera and keeps a crafted, tiny-on-disk image from exhausting memory
# the first time something renders it.
MAX_PHOTO_PIXELS = 40_000_000

# Pillow's format name -> the extension the file is stored (and served) under.
_EXTENSIONS = {'jpeg': 'jpg', 'png': 'png'}


def validate_photos(files, *, minimum=0, already_attached=0):
    """Check an upload and return the files, renamed to what they really are.

    `minimum` is how many the caller needs (a customer report needs one).
    `already_attached` counts photos a report already holds, for callers that
    add to an existing report. Raises DamageReportError with one sentence.
    """
    from PIL import Image

    files = [f for f in (files or []) if f]
    if len(files) < minimum:
        raise DamageReportError(
            'Add at least one photo of the damage.' if minimum == 1
            else f'Add at least {minimum} photos of the damage.'
        )
    if len(files) + already_attached > MAX_PHOTOS:
        raise DamageReportError(
            f'A report can have at most {MAX_PHOTOS} photos.'
        )

    cleaned = []
    for upload in files:
        label = f'"{getattr(upload, "name", "photo")}"'
        size = getattr(upload, 'size', 0) or 0
        if size <= 0:
            raise DamageReportError(f'Photo {label} is empty.')
        if size > MAX_PHOTO_BYTES:
            raise DamageReportError(
                f'Photo {label} is larger than {MAX_PHOTO_MB} MB.'
            )
        try:
            upload.seek(0)
            image = Image.open(upload)
            detected = (image.format or '').lower()
            width, height = image.size
            if detected in _EXTENSIONS:
                # verify() walks the whole file's structure without decoding
                # every pixel, so a truncated or padded file fails here.
                image.verify()
        except Exception:
            raise DamageReportError(f'Photo {label} is not a valid image.')
        finally:
            upload.seek(0)
        if detected not in _EXTENSIONS:
            raise DamageReportError(f'Photo {label} must be a JPG or PNG picture.')
        if width * height > MAX_PHOTO_PIXELS:
            raise DamageReportError(f'Photo {label} has too many pixels to accept.')
        upload.name = f'damage-photo.{_EXTENSIONS[detected]}'
        cleaned.append(upload)
    return cleaned
