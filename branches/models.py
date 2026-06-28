from django.db import models
from accounts.models import User


class Branch(models.Model):
    owner = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'OWNER'}, related_name='branches'
    )
    branch_name = models.CharField(max_length=100)
    location = models.TextField()

    def __str__(self):
        return self.branch_name