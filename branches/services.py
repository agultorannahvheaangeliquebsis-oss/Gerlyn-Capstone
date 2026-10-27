from django.db import transaction
from django.db.models import Q

from core.refdata import bust_branches_cache

from .models import Branch


class BranchDeletionBlocked(Exception):
    def __init__(self, blockers):
        self.blockers = tuple(blockers)
        super().__init__(
            'Cannot delete this branch while it has: ' + ', '.join(self.blockers) + '.'
        )


def branch_deletion_blockers(branch):
    """Return the records that make hard deletion unsafe.

    An unused branch owns an automatically-created empty Warehouse, so that
    row alone is not a blocker. Stock rows, assigned people, and operational
    history are blockers because deleting the branch would erase stock or
    detach business records from the branch that produced them.
    """
    from accounts.models import BranchManagerProfile, StaffProfile, SupplierProfile
    from inventory.models import (
        Batch, Inventory, OpeningStockImportRow, StockTransfer, WarehouseStock,
    )
    from expenses.models import Expense
    from procurement.models import Procurement, SupplierReturn
    from sales.models import PosSalesImport, SalesTransaction

    checks = (
        ('floor stock records', Inventory.objects.filter(branch=branch)),
        ('warehouse stock records', WarehouseStock.objects.filter(warehouse__branch=branch)),
        ('stock batch history', Batch.objects.filter(branch=branch)),
        ('assigned managers', BranchManagerProfile.objects.filter(branch=branch)),
        ('assigned staff', StaffProfile.objects.filter(branch=branch)),
        ('assigned suppliers', SupplierProfile.objects.filter(branch=branch)),
        (
            'stock transfer or write-off history',
            StockTransfer.objects.filter(
                Q(source_branch=branch) |
                Q(destination_branch=branch) |
                Q(warehouse__branch=branch)
            ),
        ),
        ('sales history', SalesTransaction.objects.filter(branch=branch)),
        ('POS import history', PosSalesImport.objects.filter(branch=branch)),
        ('procurement history', Procurement.objects.filter(branch=branch)),
        ('supplier return history', SupplierReturn.objects.filter(branch=branch)),
        ('expense records', Expense.objects.filter(branch=branch)),
        ('opening-stock history', OpeningStockImportRow.objects.filter(branch=branch)),
    )
    return [label for label, queryset in checks if queryset.exists()]


def delete_branch_if_safe(branch_id):
    """Hard-delete an unused branch under a lock, or raise with blockers."""
    with transaction.atomic():
        branch = Branch.objects.select_for_update().get(pk=branch_id)
        blockers = branch_deletion_blockers(branch)
        if blockers:
            raise BranchDeletionBlocked(blockers)
        name = branch.branch_name
        branch.delete()
        transaction.on_commit(bust_branches_cache)
    return name
