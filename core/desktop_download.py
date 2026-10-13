"""Where the Windows desktop installer comes from (desktop/README.md).

Two ways to offer it, chosen by environment variables:

* a public https link  - DESKTOP_APP_DOWNLOAD_URL; the card links straight to it.
* a private B2 bucket  - DESKTOP_APP_B2_*; the card links to /download/desktop/,
  which only signed-in staff can open and which redirects to a signed link that
  expires after SIGNED_LINK_SECONDS. The file is never public and never passes
  through this server (it is ~120 MB; Railway would stream it for nothing).
"""
from django.conf import settings
from django.urls import reverse

# Long enough to start a ~120 MB download on a slow connection, short enough
# that a link pasted into a chat is dead by the time anyone else tries it.
SIGNED_LINK_SECONDS = 300

_PRIVATE_SETTINGS = (
    'DESKTOP_APP_B2_ENDPOINT',
    'DESKTOP_APP_B2_BUCKET',
    'DESKTOP_APP_B2_KEY_ID',
    'DESKTOP_APP_B2_APPLICATION_KEY',
    'DESKTOP_APP_B2_FILE',
)


def private_installer_configured():
    return all(getattr(settings, name, '') for name in _PRIVATE_SETTINGS)


def public_installer_url():
    return getattr(settings, 'DESKTOP_APP_DOWNLOAD_URL', '')


def card_href():
    """What the Download page's card should link to; '' hides the card."""
    if private_installer_configured():
        return reverse('desktop_download')
    return public_installer_url()


def signed_installer_url():
    """A short-lived link to the private installer. Raises if B2 cannot be set up."""
    from backups.storage import B2Storage

    storage = B2Storage(
        settings.DESKTOP_APP_B2_ENDPOINT,
        settings.DESKTOP_APP_B2_KEY_ID,
        settings.DESKTOP_APP_B2_APPLICATION_KEY,
        settings.DESKTOP_APP_B2_BUCKET,
    )
    key = settings.DESKTOP_APP_B2_FILE
    return storage.presigned_url(key, SIGNED_LINK_SECONDS, filename=key.rsplit('/', 1)[-1])
