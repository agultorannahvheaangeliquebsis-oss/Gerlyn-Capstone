from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from branches.models import Branch
from inventory.models import (
    Product, StockTransfer, StockTransferEvent, StockTransferItem,
)


class TransferDetailSummaryTests(TestCase):
    """The shared detail fragment behind the Stock Transfer and Write-off modals."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='pw', role=User.Role.OWNER,
        )
        self.branch_a = Branch.objects.create(
            owner=self.owner, branch_name='Branch A', location='A',
        )
        self.branch_b = Branch.objects.create(
            owner=self.owner, branch_name='Branch B', location='B',
        )
        self.product = Product.objects.create(
            sku_code='SKU-D', product_name='Detail Product', unit_price='10.00',
        )
        self.client.force_login(self.owner)

    def _slip(self, purpose, status, **extra):
        transfer = StockTransfer.objects.create(
            warehouse=self.branch_a.warehouse,
            source_branch=self.branch_a,
            destination_branch=(
                self.branch_b if purpose == StockTransfer.Purpose.BRANCH_RESTOCK else None
            ),
            purpose=purpose, status=status, requested_by=self.owner, **extra,
        )
        StockTransferItem.objects.create(
            transfer=transfer, product=self.product,
            quantity_requested=10, quantity_approved=8, quantity_released=6,
        )
        return transfer

    def _get(self, transfer):
        return self.client.get(reverse('transfer_detail', args=[transfer.pk]))

    def test_quantity_stages_flag_each_shortfall(self):
        transfer = self._slip(
            StockTransfer.Purpose.BRANCH_RESTOCK, StockTransfer.Status.ACKNOWLEDGED,
            received_qty=5, acknowledged_by=self.owner, approved_by=self.owner,
        )
        stages = {s['label']: s for s in self._get(transfer).context['qty_stages']}
        self.assertEqual(
            [(k, s['value']) for k, s in stages.items()],
            [('Requested', 10), ('Approved', 8), ('Picked', 6), ('Received', 5)],
        )
        self.assertEqual(stages['Requested']['tone'], '')
        self.assertEqual(stages['Approved']['tone'], 'warn')
        self.assertEqual(stages['Picked']['tone'], 'warn')
        self.assertEqual(stages['Received']['tone'], 'danger')

    def test_pending_slip_hides_stages_it_has_not_reached(self):
        transfer = self._slip(
            StockTransfer.Purpose.BRANCH_RESTOCK, StockTransfer.Status.PENDING,
        )
        response = self._get(transfer)
        self.assertEqual([s['label'] for s in response.context['qty_stages']], ['Requested'])
        self.assertNotContains(response, '<th class="num">Approved</th>')
        self.assertNotContains(response, '<th class="num">Picked</th>')

    def test_write_off_uses_write_off_wording(self):
        transfer = self._slip(
            StockTransfer.Purpose.DAMAGED_SPOILED, StockTransfer.Status.ACKNOWLEDGED,
            received_qty=6, acknowledged_by=self.owner, approved_by=self.owner,
        )
        StockTransferEvent.objects.create(
            transfer=transfer, from_status='Approved', to_status='Acknowledged',
            actor=self.owner, note='Write-off completed.',
        )
        response = self._get(transfer)
        labels = [s['label'] for s in response.context['qty_stages']]
        self.assertEqual(labels, ['Requested', 'Approved', 'Removed'])
        self.assertContains(response, 'Completed by')
        self.assertContains(response, 'Reason for Write-off')
        self.assertContains(response, 'Damaged or Spoiled')
        self.assertContains(response, 'Write-off History')
        self.assertNotContains(response, 'Received qty')

    def test_write_off_pending_pill_matches_the_list(self):
        transfer = self._slip(
            StockTransfer.Purpose.OTHER, StockTransfer.Status.PENDING,
        )
        self.assertContains(self._get(transfer), 'Pending Approval')

    def test_rejected_slip_names_the_rejecter(self):
        transfer = self._slip(
            StockTransfer.Purpose.BRANCH_RESTOCK, StockTransfer.Status.REJECTED,
            approved_by=self.owner,
        )
        response = self._get(transfer)
        self.assertContains(response, 'Rejected by')
        self.assertNotContains(response, 'Approved by')

    def test_history_lists_actor_timestamp_and_note(self):
        transfer = self._slip(
            StockTransfer.Purpose.BRANCH_RESTOCK, StockTransfer.Status.APPROVED,
        )
        StockTransferEvent.objects.create(
            transfer=transfer, from_status='', to_status='Pending',
            actor=self.owner, note='Request created.',
        )
        StockTransferEvent.objects.create(
            transfer=transfer, from_status='Pending', to_status='Approved',
            actor=None, note='Approved by system.',
        )
        response = self._get(transfer)
        history = response.context['history']
        self.assertEqual([h['from_label'] for h in history], ['Created', 'Pending'])
        self.assertEqual([h['actor'] for h in history], ['owner', 'System'])
        self.assertContains(response, 'Transfer History')
        self.assertContains(response, 'Request created.')
        self.assertContains(response, 'Approved by system.')

    def test_notes_and_discrepancy_reason_are_shown(self):
        transfer = self._slip(
            StockTransfer.Purpose.BRANCH_RESTOCK, StockTransfer.Status.DISCREPANCY,
            notes='Deliver to back door', flag_reason='Two boxes crushed',
        )
        response = self._get(transfer)
        self.assertContains(response, 'Deliver to back door')
        self.assertContains(response, 'Discrepancy: Two boxes crushed')

    def test_receiving_branch_still_gets_receipt_and_flag_forms(self):
        transfer = self._slip(
            StockTransfer.Purpose.BRANCH_RESTOCK, StockTransfer.Status.RELEASED,
        )
        response = self._get(transfer)
        self.assertContains(response, 'Confirm Received on Floor')
        self.assertContains(response, 'Flag Discrepancy')
        self.assertContains(response, 'name="quantity_received"')
        self.assertContains(response, 'Moved to the floor')
