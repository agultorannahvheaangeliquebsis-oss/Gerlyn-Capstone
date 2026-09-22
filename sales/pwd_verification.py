"""In-person decision for a pickup order's PWD request."""
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from billing.models import BillingRecord
from core.scoping import branch_for_user, user_can_access_branch

from .models import DiscountVerification, SalesTransaction
from .pricing import price_cart


class VerificationError(ValueError):
    pass


def decide_pwd(order_id, actor, decision, *, id_number='', holder_name='',
               rejection_reason=''):
    if actor.role not in (User.Role.OWNER, User.Role.BRANCH_MANAGER, User.Role.STAFF):
        raise PermissionError('Only branch employees may verify a PWD request.')
    if decision not in ('verify', 'reject'):
        raise VerificationError('Choose verify or reject.')
    if not all(isinstance(value, str) for value in
               (id_number, holder_name, rejection_reason)):
        raise VerificationError('Enter valid verification details.')

    with transaction.atomic():
        order = SalesTransaction.objects.select_for_update().get(pk=order_id)
        if not user_can_access_branch(actor, order.branch_id):
            raise PermissionError('That order belongs to another branch.')
        assigned_branch = branch_for_user(actor)
        if actor.role != User.Role.OWNER and (
                assigned_branch is None or assigned_branch.pk != order.branch_id):
            raise PermissionError('An assigned branch is required.')
        if (order.pwd_verification_status != SalesTransaction.PwdVerificationStatus.PENDING
                or order.transaction_type != SalesTransaction.TransactionType.ONLINE
                or order.fulfillment_type != SalesTransaction.FulfillmentType.PICKUP
                or order.order_status == SalesTransaction.OrderStatus.CANCELLED):
            raise VerificationError('This order has no pending PWD request.')

        bill = BillingRecord.objects.select_for_update().filter(transaction=order).first()
        if bill and bill.status == BillingRecord.PaymentStatus.PAID:
            raise VerificationError('A paid order cannot be repriced.')

        verification = DiscountVerification.objects.select_for_update().filter(order=order).first()
        if not verification or verification.status != SalesTransaction.PwdVerificationStatus.PENDING:
            raise VerificationError('This request was already decided.')

        if decision == 'verify':
            id_number = id_number.strip()
            holder_name = holder_name.strip()
            if not id_number or not holder_name:
                raise VerificationError('Enter the PWD ID number and name on the ID.')
            if len(id_number) > 50 or len(holder_name) > 150:
                raise VerificationError('PWD ID details are too long.')
            items = list(order.items.select_related('product'))
            priced = price_cart(
                [(item.product, item.quantity_sold, item.unit_price) for item in items],
                discount_type=SalesTransaction.DiscountType.PWD,
            )
            for item, line in zip(items, priced['lines']):
                item.unit_price = line['unit_price']
                item.is_vat_exempt_line = line['is_vat_exempt_line']
                item.vat_amount = line['vat_amount']
                item.discount_amount = line['discount_amount']
                item.save()
            order.discount_type = SalesTransaction.DiscountType.PWD
            order.discount_id_number = id_number
            order.discount_holder_name = holder_name
            order.discount_applied_by = actor
            order.discount_applied_at = timezone.now()
            order.total_amount = priced['total_amount']
            order.total_discount_amount = priced['total_discount_amount']
            order.total_vat_amount = priced['total_vat_amount']
            order.pwd_verification_status = SalesTransaction.PwdVerificationStatus.VERIFIED
        else:
            rejection_reason = rejection_reason.strip()
            if not rejection_reason:
                raise VerificationError('Enter a reason for rejecting the request.')
            order.pwd_verification_status = SalesTransaction.PwdVerificationStatus.REJECTED

        order.save()
        if bill:
            bill.amount_due = order.total_amount
            bill.save(update_fields=['amount_due'])
        verification.status = order.pwd_verification_status
        verification.verified_by_employee = actor
        # Owners have global access and no assigned branch; record the order's branch.
        verification.branch = assigned_branch if assigned_branch else order.branch
        verification.verified_at = timezone.now()
        verification.rejection_reason = rejection_reason if decision == 'reject' else ''
        verification.save()
        return order
