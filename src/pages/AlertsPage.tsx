import { useEffect, useMemo, useState } from 'react';
import { InternalLayout } from '../layouts/InternalLayout';
import { StatusPill } from '../components/StatusPill';
import { Card } from '../components/Card';
import { useAuth } from '../contexts/AuthContext';
import {
  inventoryApi,
  warehouseStockApi,
  batchesApi,
  billingApi,
  type InventoryRow,
  type WarehouseStockRow,
  type Batch,
  type BillingRecord,
} from '../lib/api';
import { formatCurrency, formatDate } from '../lib/format';

function expiryTone(status: string): 'ok' | 'warn' | 'danger' {
  switch (status) {
    case 'Expired':
      return 'danger';
    case 'Expiring Soon':
      return 'warn';
    default:
      return 'ok';
  }
}

interface LowStockItem {
  key: string;
  location: 'Floor' | 'Warehouse';
  productName: string;
  productSku: string;
  branchName: string;
  quantityOnHand: number;
  reorderLevel: number;
}

function SectionHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-center justify-between border-b border-surface-linelight px-5 py-4">
      <span className="text-lg font-semibold">{title}</span>
      <span
        className={`flex h-6 min-w-[24px] items-center justify-center rounded-full px-2 text-[13px] font-bold ${
          count > 0 ? 'bg-status-danger-bg text-status-danger-text' : 'bg-ink-900/5 text-ink-500'
        }`}
      >
        {count}
      </span>
    </div>
  );
}

export function AlertsPage() {
  const { user } = useAuth();
  const [floor, setFloor] = useState<InventoryRow[]>([]);
  const [warehouse, setWarehouse] = useState<WarehouseStockRow[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [billing, setBilling] = useState<BillingRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const canConfirmPayments =
    user?.roleCode === 'OWNER' || user?.roleCode === 'BRANCH_MANAGER' || user?.roleCode === 'STAFF';

  function loadAll() {
    setLoading(true);
    setError(null);
    Promise.all([inventoryApi.list(), warehouseStockApi.list(), batchesApi.list(), billingApi.list()])
      .then(([floorRes, warehouseRes, batchesRes, billingRes]) => {
        setFloor(floorRes);
        setWarehouse(warehouseRes);
        setBatches(batchesRes);
        setBilling(billingRes);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load alerts.'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadAll();
  }, []);

  const lowStock: LowStockItem[] = useMemo(() => {
    const floorItems: LowStockItem[] = floor
      .filter((r) => r.restock)
      .map((r) => ({
        key: `floor-${r.id}`,
        location: 'Floor',
        productName: r.productName,
        productSku: r.productSku,
        branchName: r.branchName,
        quantityOnHand: r.quantityOnHand,
        reorderLevel: r.reorderLevel,
      }));
    const warehouseItems: LowStockItem[] = warehouse
      .filter((r) => r.restockFlag)
      .map((r) => ({
        key: `warehouse-${r.id}`,
        location: 'Warehouse',
        productName: r.productName,
        productSku: r.productSku,
        branchName: r.branchName,
        quantityOnHand: r.quantityOnHand,
        reorderLevel: r.reorderLevel,
      }));
    return [...floorItems, ...warehouseItems].sort((a, b) => a.quantityOnHand - b.quantityOnHand);
  }, [floor, warehouse]);

  const expiringBatches = useMemo(
    () =>
      batches
        .filter((b) => b.expiryStatus === 'Expired' || b.expiryStatus === 'Expiring Soon')
        .sort((a, b) => a.daysUntilExpiry - b.daysUntilExpiry),
    [batches]
  );

  const pendingPayments = useMemo(
    () => billing.filter((b) => b.status === 'Pending').sort((a, b) => a.dateRecorded.localeCompare(b.dateRecorded)),
    [billing]
  );

  const totalAlerts = lowStock.length + expiringBatches.length + pendingPayments.length;

  function replaceBillingRecord(updated: BillingRecord) {
    setBilling((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
    setActionError(null);
  }

  async function handleConfirm(record: BillingRecord, status: 'Paid' | 'Rejected') {
    setActionError(null);
    try {
      const updated = await billingApi.confirm(record.id, status);
      replaceBillingRecord(updated);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to update payment status.');
    }
  }

  return (
    <InternalLayout
      eyebrow={`Hello, ${user?.fullName?.split(' ')[0] ?? user?.username ?? ''}`}
      headline="Alerts"
      meta={`${totalAlerts} item${totalAlerts === 1 ? '' : 's'} need attention`}
    >
      {loading && <div className="text-ink-400">Loading alerts…</div>}

      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {!loading && !error && (
        <div className="flex flex-col gap-5">
          {actionError && (
            <div className="rounded-lg bg-status-danger-bg px-4 py-2.5 text-sm font-medium text-status-danger-text">
              {actionError}
            </div>
          )}

          <Card className="overflow-hidden">
            <SectionHeader title="Low stock" count={lowStock.length} />
            {lowStock.length === 0 && (
              <div className="px-5 py-6 text-sm text-ink-400">Nothing is low on stock right now.</div>
            )}
            {lowStock.map((item, i) => (
              <div
                key={item.key}
                className={`grid grid-cols-[1.6fr_0.9fr_1fr_0.8fr_0.8fr] items-center px-5 py-2.5 text-[14px] ${
                  i < lowStock.length - 1 ? 'border-b border-surface-linelight' : ''
                } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
              >
                <span className="min-w-0">
                  <span className="block truncate font-semibold">{item.productName}</span>
                  <span className="text-[12.5px] text-ink-400">{item.productSku}</span>
                </span>
                <span className="text-ink-600">{item.location}</span>
                <span className="truncate text-ink-600">{item.branchName}</span>
                <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                  {item.quantityOnHand} / {item.reorderLevel}
                </span>
                <span className="flex justify-end">
                  <StatusPill tone={item.quantityOnHand === 0 ? 'danger' : 'warn'}>
                    {item.quantityOnHand === 0 ? 'Out of stock' : 'Low stock'}
                  </StatusPill>
                </span>
              </div>
            ))}
          </Card>

          <Card className="overflow-hidden">
            <SectionHeader title="Expiring & expired batches" count={expiringBatches.length} />
            {expiringBatches.length === 0 && (
              <div className="px-5 py-6 text-sm text-ink-400">No batches are expiring soon.</div>
            )}
            {expiringBatches.map((b, i) => (
              <div
                key={b.id}
                className={`grid grid-cols-[1fr_1.2fr_1fr_0.9fr_0.9fr_1fr] items-center px-5 py-2.5 text-[14px] ${
                  i < expiringBatches.length - 1 ? 'border-b border-surface-linelight' : ''
                } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
              >
                <span className="font-semibold [font-variant-numeric:tabular-nums]">{b.batchNumber}</span>
                <span className="truncate">{b.productName}</span>
                <span className="truncate text-ink-600">{b.branchName}</span>
                <span className="text-right [font-variant-numeric:tabular-nums]">{b.quantity} pcs</span>
                <span
                  className={`text-right [font-variant-numeric:tabular-nums] ${
                    b.daysUntilExpiry < 0 ? 'font-semibold text-status-danger-dot' : ''
                  }`}
                >
                  {b.daysUntilExpiry < 0 ? `${Math.abs(b.daysUntilExpiry)}d ago` : `${b.daysUntilExpiry}d left`}
                </span>
                <span className="flex justify-end">
                  <StatusPill tone={expiryTone(b.expiryStatus)}>{b.expiryStatus}</StatusPill>
                </span>
              </div>
            ))}
          </Card>

          <Card className="overflow-hidden">
            <SectionHeader title="Pending payment confirmations" count={pendingPayments.length} />
            {pendingPayments.length === 0 && (
              <div className="px-5 py-6 text-sm text-ink-400">No payments waiting on confirmation.</div>
            )}
            {pendingPayments.map((record, i) => (
              <div
                key={record.id}
                className={`grid grid-cols-[1fr_1fr_1fr_0.9fr_1fr_auto] items-center gap-2 px-5 py-2.5 text-[14px] ${
                  i < pendingPayments.length - 1 ? 'border-b border-surface-linelight' : ''
                } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
              >
                <span className="font-semibold [font-variant-numeric:tabular-nums]">{record.orderNumber}</span>
                <span className="truncate text-ink-600">{record.branch ?? '—'}</span>
                <span className="text-[13px] text-ink-600">{formatDate(record.dateRecorded)}</span>
                <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                  {formatCurrency(record.amountDue)}
                </span>
                <span className="text-ink-600">{record.paymentMethod || '—'}</span>
                {canConfirmPayments ? (
                  <span className="flex justify-end gap-1.5">
                    <button
                      onClick={() => void handleConfirm(record, 'Paid')}
                      className="rounded-lg bg-status-ok-dot px-2.5 py-1 text-[12.5px] font-semibold text-white hover:opacity-90"
                    >
                      Mark paid
                    </button>
                    <button
                      onClick={() => void handleConfirm(record, 'Rejected')}
                      className="rounded-lg border border-status-danger-text px-2.5 py-1 text-[12.5px] font-semibold text-status-danger-text hover:bg-status-danger-bg"
                    >
                      Reject
                    </button>
                  </span>
                ) : (
                  <span />
                )}
              </div>
            ))}
          </Card>
        </div>
      )}
    </InternalLayout>
  );
}
