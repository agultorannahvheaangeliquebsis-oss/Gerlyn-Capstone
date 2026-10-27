"""The optional receipt photo on an expense: what is accepted.

One JPG or PNG of at most MAX_RECEIPT_MB. "Real" matters -- the file's own
header is read with Pillow, because the name and the browser-declared content
type are both just text the uploader chose. A web page saved as receipt.jpg, an
SVG, or a WebP renamed to .png is refused rather than stored and later served
back to whoever opens the expense.
"""
MAX_RECEIPT_MB = 5
MAX_RECEIPT_BYTES = MAX_RECEIPT_MB * 1024 * 1024
# A decoded picture is width x height x ~4 bytes. 40 megapixels is far past any
# phone camera and keeps a crafted, tiny-on-disk image from exhausting memory
# the first time something renders it.
MAX_RECEIPT_PIXELS = 40_000_000

# Pillow's format name -> (extension the file is stored under, content type).
FORMATS = {'jpeg': ('jpg', 'image/jpeg'), 'png': ('png', 'image/png')}


def validate_receipt(upload):
    """Check an uploaded receipt. Returns `(upload, None)` with the file renamed
    to what it really is, or `(None, message)`."""
    from PIL import Image

    label = f'"{getattr(upload, "name", "receipt")}"'
    size = getattr(upload, 'size', 0) or 0
    if size <= 0:
        return None, f'The receipt {label} is empty.'
    if size > MAX_RECEIPT_BYTES:
        return None, f'The receipt {label} is larger than {MAX_RECEIPT_MB} MB.'
    try:
        upload.seek(0)
        image = Image.open(upload)
        detected = (image.format or '').lower()
        width, height = image.size
        if detected in FORMATS:
            # verify() walks the whole file's structure without decoding every
            # pixel, so a truncated or padded file fails here.
            image.verify()
    except Exception:
        return None, f'The receipt {label} is not a valid image.'
    finally:
        upload.seek(0)
    if detected not in FORMATS:
        return None, f'The receipt {label} must be a JPG or PNG picture.'
    if width * height > MAX_RECEIPT_PIXELS:
        return None, f'The receipt {label} has too many pixels to accept.'
    upload.name = f'receipt.{FORMATS[detected][0]}'
    return upload, None


def content_type_for(name):
    """The content type a stored receipt is served as, or '' if it is not one
    of the two accepted kinds (so it is never served as a page)."""
    extension = (name or '').rsplit('.', 1)[-1].lower()
    for ext, content_type in FORMATS.values():
        if extension == ext:
            return content_type
    return ''
