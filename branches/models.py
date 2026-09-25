from django.db import models
from accounts.models import User


class Branch(models.Model):
    owner = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'OWNER'}, related_name='branches'
    )
    branch_name = models.CharField(max_length=100)
    location = models.TextField()
    region = models.CharField(max_length=10, blank=True)
    province = models.CharField(max_length=10, blank=True)
    city = models.CharField(max_length=10, blank=True)
    barangay = models.CharField(max_length=15, blank=True)
    street_detail = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    def __str__(self):
        return self.branch_name
