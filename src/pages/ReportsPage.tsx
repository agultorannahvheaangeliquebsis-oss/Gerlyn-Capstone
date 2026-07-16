import { useEffect, useMemo, useState } from 'react';
import { InternalLayout } from '../layouts/InternalLayout';
import { StatusPill } from '../components/StatusPill';
import { Card } from '../components/Card';
import { useAuth } from '../contexts/AuthContext';
import {
  reportsApi,
  type SalesTransaction,
  type InventoryRow,
  type Procurement,
} from '../lib/api';
import { formatCurrency, formatDate } from '../lib/format';

type Tab = 'sales' | 'inventory' | 'procurement';

function orderStatusTone(status: string): 'ok' | 'warn' | 'danger' | 'neutral' {
  switch (status) {
    case 'Completed':
      return 'ok';
    case 'Pending':
    case 'Approved':
    case 'Preparing':
      return 'warn';
    case 'Cancelled':
      return 'danger';
    default:
      return 'neutral';
  }
}

function procurementStatusTone(status: string): 'ok' | 'warn' | 'danger' | 'neutral' {
  switch (status) {
    case 'Received':
      return 'ok';
    case 'Ordered':
      return 'warn';
    case 'Cancelled':
      return 'danger';
    default:
      return 'neutral';
  }
}

function stockTone(quantityOnHand: number, restock: boolean): 'ok' | 'warn' | 'danger' {
  if (quantityOnHand === 0) return 'danger';
  if (restock) return 'warn';
  return 'ok';
}

function stockLabel(quantityOnHand: number, restock: boolean): string {
  if (quantityOnHand === 0) return 'Out of stock';
  if (restock) return 'Low stock';
  return 'OK';
}

const TABS: { key: Tab; label: string }[] = [
  { key: 'sales', label: 'Sales' },
  { key: 'inventory', label: 'Inventory' },
  { key: 'procurement', label: 'Procurement' },
];

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <Card className="flex items-center justify-between gap-2 px-4 py-3">
      <span className="text-[13.5px] text-ink-600">{label}</span>
      <span className="text-xl font-bold [font-variant-numeric:tabular-nums]">{value}</span>
    </Card>
  );
}

export function ReportsPage() {
  const { user } = useAuth();
  const allowed = user?.roleCode === 'OWNER' || user?.roleCode === 'BRANCH_MANAGER';

  const [tab, setTab] = useState<Tab>('sales');
  const [sales, setSales] = useState<SalesTransaction[]>([]);
  const [inventory, setInventory] = useState<InventoryRow[]>([]);
  const [procurement, setProcurement] = useState<Procurement[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');

  useEffect(() => {
    if (!allowed) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([reportsApi.salesReport(), reportsApi.inventoryReport(), reportsApi.procurementReport()])
      .then(([salesRes, inventoryRes, procurementRes]) => {
        if (cancelled) return;
        setSales(salesRes);
        setInventory(inventoryRes);
        setProcurement(procurementRes);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load reports.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allowed]);

  function inRange(dateStr: string): boolean {
    const d = dateStr.slice(0, 10);
    if (dateFrom && d < dateFrom) return false;
    if (dateTo && d > dateTo) return false;
    return true;
  }

  const filteredSales = useMemo(
    () => sales.filter((s) => inRange(s.transactionDate)),
    [sales, dateFrom, dateTo]
  );
  const filteredProcurement = useMemo(
    () => procurement.filter((p) => inRange(p.orderDate)),
    [procurement, dateFrom, dateTo]
  );

  const salesTotal = useMemo(
    () => filteredSales.reduce((sum, s) => sum + parseFloat(s.totalAmount), 0),
    [filteredSales]
  );
  const salesAvg = filteredSales.length ? salesTotal / filteredSales.length : 0;

  const procurementTotal = useMemo(
    () => filteredProcurement.reduce((sum, p) => sum + parseFloat(p.total), 0),
    [filteredProcurement]
  );
  const procurementReceivedCount = useMemo(
    () => filteredProcurement.filter((p) => p.status === 'Received').length,
    [filteredProcurement]
  );

  const inventoryLowCount = useMemo(() => inventory.filter((r) => r.restock).length, [inventory]);
  const inventoryUnits = useMemo(() => inventory.reduce((sum, r) => sum + r.quantityOnHand, 0), [inventory]);

  if (!allowed) {
    return (
      <InternalLayout eyebrow="Reports" headline="Reports">
        <div className="rounded-lg bg-status-warn-bg px-4 py-3 text-sm font-medium text-status-warn-text">
          Reports are only available to Owners and Branch Managers.
        </div>
      </InternalLayout>
    );
  }

  return (
    <InternalLayout
      eyebrow={`Hello, ${user?.fullName?.split(' ')[0] ?? user?.username ?? ''}`}
      headline="Reports"
      meta="Sales, inventory & procurement — filterable by date range"
    >
      {loading && <div className="text-ink-400">Loading reports…</div>}

      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {!loading && !error && (
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex gap-1 rounded-lg border border-surface-line bg-white p-1">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className={`rounded-md px-3.5 py-1.5 text-sm font-semibold ${
                    tab === t.key ? 'bg-action-50 text-action-700' : 'text-ink-500 hover:bg-surface-bg'
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {tab !== 'inventory' && (
              <div className="flex items-center gap-2">
                <label className="flex items-center gap-1.5 text-sm text-ink-600">
                  From
                  <input
                    type="date"
                    value={dateFrom}
                    onChange={(e) => setDateFrom(e.target.value)}
                    className="rounded-lg border border-surface-line bg-white px-2.5 py-1.5 text-sm text-ink-900"
                  />
                </label>
                <label className="flex items-center gap-1.5 text-sm text-ink-600">
                  To
                  <input
                    type="date"
                    value={dateTo}
                    onChange={(e) => setDateTo(e.target.value)}
                    className="rounded-lg border border-surface-line bg-white px-2.5 py-1.5 text-sm text-ink-900"
                  />
                </label>
                {(dateFrom || dateTo) && (
                  <button
                    onClick={() => {
                      setDateFrom('');
                      setDateTo('');
                    }}
                    className="rounded-lg px-2.5 py-1.5 text-sm font-semibold text-ink-500 hover:bg-surface-bg"
                  >
                    Clear
                  </button>
                )}
              </div>
            )}
          </div>

          {tab === 'sales' && (
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-3 gap-3">
                <StatTile label="Total revenue" value={formatCurrency(salesTotal)} />
                <StatTile label="Transactions" value={String(filteredSales.length)} />
                <StatTile label="Average order value" value={formatCurrency(salesAvg)} />
              </div>

              <Card className="overflow-hidden">
                <div className="grid grid-cols-[1fr_1fr_1.2fr_1fr_1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
                  <span>Order</span>
                  <span>Date</span>
                  <span>Customer / branch</span>
                  <span className="text-right">Total</span>
                  <span className="text-right">Status</span>
                </div>
                {filteredSales.length === 0 && (
                  <div className="px-5 py-6 text-sm text-ink-400">No transactions in this date range.</div>
                )}
                {filteredSales.map((s, i) => (
                  <div
                    key={s.id}
                    className={`grid grid-cols-[1fr_1fr_1.2fr_1fr_1fr] items-center px-5 py-2.5 text-[14px] ${
                      i < filteredSales.length - 1 ? 'border-b border-surface-linelight' : ''
                    } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
                  >
                    <span className="font-semibold [font-variant-numeric:tabular-nums]">{s.orderNumber}</span>
                    <span className="text-[13px] text-ink-600">{formatDate(s.transactionDate)}</span>
                    <span className="min-w-0">
                      <span className="block truncate">{s.customer ?? s.staff ?? 'Walk-in'}</span>
                      <span className="text-[12.5px] text-ink-400">{s.branch ?? '—'}</span>
                    </span>
                    <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                      {formatCurrency(s.totalAmount)}
                    </span>
                    <span className="flex justify-end">
                      <StatusPill tone={orderStatusTone(s.orderStatus)}>{s.orderStatus}</StatusPill>
                    </span>
                  </div>
                ))}
              </Card>
            </div>
          )}

          {tab === 'inventory' && (
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-3 gap-3">
                <StatTile label="SKUs tracked" value={String(inventory.length)} />
                <StatTile label="Low / out of stock" value={String(inventoryLowCount)} />
                <StatTile label="Total units on hand" value={inventoryUnits.toLocaleString()} />
              </div>

              <Card className="overflow-hidden">
                <div className="grid grid-cols-[1.6fr_1fr_0.8fr_0.8fr_1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
                  <span>Product</span>
                  <span>Branch</span>
                  <span className="text-right">On hand</span>
                  <span className="text-right">Reorder pt</span>
                  <span className="text-right">Status</span>
                </div>
                {inventory.length === 0 && (
                  <div className="px-5 py-6 text-sm text-ink-400">No inventory records found.</div>
                )}
                {inventory.map((row, i) => (
                  <div
                    key={row.id}
                    className={`grid grid-cols-[1.6fr_1fr_0.8fr_0.8fr_1fr] items-center px-5 py-2.5 text-[14px] ${
                      i < inventory.length - 1 ? 'border-b border-surface-linelight' : ''
                    } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-semibold">{row.productName}</span>
                      <span className="text-[12.5px] text-ink-400">{row.productSku}</span>
                    </span>
                    <span className="truncate text-ink-600">{row.branchName}</span>
                    <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                      {row.quantityOnHand}
                    </span>
                    <span className="text-right text-ink-600 [font-variant-numeric:tabular-nums]">
                      {row.reorderLevel}
                    </span>
                    <span className="flex justify-end">
                      <StatusPill tone={stockTone(row.quantityOnHand, row.restock)}>
                        {stockLabel(row.quantityOnHand, row.restock)}
                      </StatusPill>
                    </span>
                  </div>
                ))}
              </Card>
              <div className="text-xs text-ink-400">
                Inventory is a current snapshot, not a historical record — date filters don't apply here.
              </div>
            </div>
          )}

          {tab === 'procurement' && (
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-3 gap-3">
                <StatTile label="Total spend" value={formatCurrency(procurementTotal)} />
                <StatTile label="Purchase orders" value={String(filteredProcurement.length)} />
                <StatTile label="Received" value={String(procurementReceivedCount)} />
              </div>

              <Card className="overflow-hidden">
                <div className="grid grid-cols-[1fr_1fr_1.2fr_1fr_1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
                  <span>PO #</span>
                  <span>Order date</span>
                  <span>Supplier / branch</span>
                  <span className="text-right">Total</span>
                  <span className="text-right">Status</span>
                </div>
                {filteredProcurement.length === 0 && (
                  <div className="px-5 py-6 text-sm text-ink-400">No purchase orders in this date range.</div>
                )}
                {filteredProcurement.map((p, i) => (
                  <div
                    key={p.id}
                    className={`grid grid-cols-[1fr_1fr_1.2fr_1fr_1fr] items-center px-5 py-2.5 text-[14px] ${
                      i < filteredProcurement.length - 1 ? 'border-b border-surface-linelight' : ''
                    } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
                  >
                    <span className="font-semibold [font-variant-numeric:tabular-nums]">{p.poNumber}</span>
                    <span className="text-[13px] text-ink-600">{formatDate(p.orderDate)}</span>
                    <span className="min-w-0">
                      <span className="block truncate">{p.supplierName ?? '—'}</span>
                      <span className="text-[12.5px] text-ink-400">{p.branch ?? '—'}</span>
                    </span>
                    <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                      {formatCurrency(p.total)}
                    </span>
                    <span className="flex justify-end">
                      <StatusPill tone={procurementStatusTone(p.status)}>{p.status}</StatusPill>
                    </span>
                  </div>
                ))}
              </Card>
            </div>
          )}
        </div>
      )}
    </InternalLayout>
  );
}
