"""Operating expenses -- the money a branch spends that is not stock.

Procurement already records what the store pays suppliers for goods; this is
everything else that has to be paid for the branch to open its doors: rent,
utilities, salaries, transport, supplies, repairs. The Profitability report
subtracts these from gross profit to reach net profit.

Salaries are an amount someone types in. The system has no payroll, no
employee pay rates and no attendance, so there is nothing to compute one from;
it is recorded here like any other bill.
"""
import uuid
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from core.limits import MAX_AMOUNT, MAX_DESCRIPTION_LENGTH, MIN_PRICE, peso
from core.storage import PrivateMediaStorage

# A receipt is a photo of a bill: the supplier's name, an account number, a
# signature. As private as a payment proof, so it lives on the same non-public
# storage and is reachable only through expenses.views.expense_receipt.
receipt_storage = PrivateMediaStorage()


def receipt_upload_path(instance, filename):
    """expense_receipts/<yyyy>/<mm>/<random>.<jpg|png> -- never the uploader's
    own filename, which is just text they chose."""
    extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpg'
    now = timezone.now()
    return f'expense_receipts/{now:%Y}/{now:%m}/{uuid.uuid4().hex}.{extension}'


class Expense(models.Model):
    class Category(models.TextChoices):
        RENT = 'Rent', 'Rent'
        UTILITIES = 'Utilities', 'Utilities'
        SALARIES = 'Salaries', 'Salaries'
        TRANSPORTATION = 'Transportation', 'Transportation'
        SUPPLIES = 'Supplies', 'Supplies'
        MAINTENANCE = 'Maintenance', 'Maintenance'
        OTHER = 'Other', 'Other'

    # PROTECT: an expense is a financial record. Deleting a branch must not
    # quietly erase what it spent (branches.services lists this as a blocker).
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='expenses',
    )
    category = models.CharField(max_length=20, choices=Category.choices)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[
            MinValueValidator(MIN_PRICE, message=f'Must be at least {peso(MIN_PRICE)}.'),
            MaxValueValidator(MAX_AMOUNT, message=f'Cannot be more than {peso(MAX_AMOUNT)}.'),
        ],
        help_text='What was paid, in pesos. For Salaries this is a figure typed '
                  'in by hand -- the system has no payroll.',
    )
    # The day the cost belongs to (the month's rent is January's), not the day
    # someone got round to typing it in -- created_at holds that.
    date = models.DateField(db_index=True)
    description = models.CharField(max_length=MAX_DESCRIPTION_LENGTH, blank=True)
    recorded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['OWNER', 'BRANCH_MANAGER']},
        related_name='recorded_expenses',
    )
    receipt = models.ImageField(
        upload_to=receipt_upload_path, storage=receipt_storage, max_length=255,
        blank=True,
        help_text='Optional photo of the bill or receipt.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-pk']
        indexes = [
            # Every report reads "this branch, between these dates".
            models.Index(fields=['branch', '-date'], name='expense_branch_date_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=Decimal('0')),
                name='expense_amount_positive',
            ),
        ]

    def __str__(self):
        return f'{self.category} {self.amount} on {self.date}'
