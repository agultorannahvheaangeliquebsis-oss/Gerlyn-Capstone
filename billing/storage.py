from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class PaymentProofStorage(FileSystemStorage):
    """Keep payment evidence out of the public/default media backend.

    Product images and avatars may use the configured Cloudinary backend, but
    payment proofs stay behind Django's authorization-checked download view.
    Deployments should mount MEDIA_ROOT on persistent private storage.
    """

    def __init__(self):
        super().__init__(location=settings.MEDIA_ROOT, base_url=None)
