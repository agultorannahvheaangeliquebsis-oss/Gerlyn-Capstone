import { useEffect, useMemo, useState } from 'react';
import { InternalLayout } from '../layouts/InternalLayout';
import { StatusPill } from '../components/StatusPill';
import { Card } from '../components/Card';
import { useAuth } from '../contexts/AuthContext';
import {
  reportsApi,
  salesApi,
  type DashboardSummary,
  type InventoryRow,
  type SalesTransaction,
} from '../lib/api';
import { formatCurrency, formatDate } from '../lib/format';

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

interface RestockGroup {
  productName: string;
  productSku: string;
  rows: InventoryRow[];
}

function groupRestockSuggestions(rows: InventoryRow[]): RestockGroup[] {
  const byProduct = new Map<string, RestockGroup>();
  for (const row of rows) {
    if (!row.restock) continue;
    const existing = byProduct.get(row.productSku);
    if (existing) {
      existing.rows.push(row);
    } else {
      byProduct.set(row.productSku, {
        productName: row.productName,
        productSku: row.productSku,
        rows: [row],
      });
    }
  }
  return Array.from(byProduct.values()).sort((a, b) => a.productName.localeCompare(b.productName));
}

function isExpiringSoon(row: InventoryRow): boolean {
  if (!row.expiryDate) return false;
  const days = (new Date(row.expiryDate).getTime() - Date.now()) / 86_400_000;
  return days >= 0 && days <= 30;
}

export function DashboardPage() {
  const { user } = useAuth();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [inventory, setInventory] = useState<InventoryRow[]>([]);
  const [orders, setOrders] = useState<SalesTransaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([reportsApi.dashboardSummary(), reportsApi.inventoryReport(), salesApi.list()])
      .then(([summaryRes, inventoryRes, ordersRes]) => {
        if (cancelled) return;
        setSummary(summaryRes);
        setInventory(inventoryRes);
        setOrders(ordersRes);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load dashboard.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const restockGroups = useMemo(() => groupRestockSuggestions(inventory), [inventory]);
  const expiringSoonCount = useMemo(() => inventory.filter(isExpiringSoon).length, [inventory]);
  const recentOrders = useMemo(
    () =>
      [...orders]
        .sort((a, b) => new Date(b.transactionDate).getTime() - new Date(a.transactionDate).getTime())
        .slice(0, 5),
    [orders]
  );

  const today = new Date().toISOString().slice(0, 10);
  const weekday = new Date().toLocaleDateString('en-US', { weekday: 'long' });

  return (
    <InternalLayout
      eyebrow={`Hello, ${user?.fullName?.split(' ')[0] ?? user?.username ?? ''}`}
      headline="Dashboard"
      meta={`${today} · ${weekday}`}
    >
      {loading && <div className="text-ink-400">Loading dashboard…</div>}

      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {summary && !loading && !error && (
        <div className="flex flex-col gap-5">
          {/* Hero row */}
          <div className="grid grid-cols-[1.5fr_1fr] gap-4">
            <Card className="flex justify-between gap-6 p-5">
              <div>
                <div className="text-sm font-semibold text-ink-600">Sales · last 30 days</div>
                <div className="mt-1.5 text-metric leading-tight [font-variant-numeric:tabular-nums]">
                  {formatCurrency(summary.recentSales)}
                </div>
                <div className="mt-2 text-sm font-semibold text-action-700">
                  {formatCurrency(summary.totalSales)} all-time · {summary.totalTransactions} transactions
                </div>
              </div>
              {summary.isOwner && summary.salesByBranch.length > 0 && (
                <div className="flex min-w-[220px] flex-col justify-center gap-1.5 border-l border-surface-linelight pl-6">
                  {summary.salesByBranch.map((row) => (
                    <div key={row.branch ?? 'unassigned'} className="flex justify-between gap-4 text-sm">
                      <span className="text-ink-600">{row.branch ?? 'Unassigned'}</span>
                      <span className="font-semibold [font-variant-numeric:tabular-nums]">
                        {formatCurrency(row.total)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <Card className="p-5">
              <div className="text-sm font-semibold text-ink-600">Top product</div>
              {summary.topProducts[0] ? (
                <>
                  <div className="mt-1.5 text-2xl font-bold leading-tight">
                    {summary.topProducts[0].productName}
                  </div>
                  <div className="mt-2 text-sm font-semibold text-ink-600 [font-variant-numeric:tabular-nums]">
                    {summary.topProducts[0].totalQty} pcs sold · {formatCurrency(summary.topProducts[0].totalRevenue)}
                  </div>
                </>
              ) : (
                <div className="mt-1.5 text-ink-400">No sales recorded yet.</div>
              )}
            </Card>
          </div>

          {/* Secondary strip */}
          <div className="grid grid-cols-4 gap-3">
            <Card className="flex items-center justify-between gap-2 px-4 py-3">
              <span className="text-[13.5px] text-ink-600">Low stock items</span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-status-warn-dot" />
                <span className="text-xl font-bold [font-variant-numeric:tabular-nums]">
                  {summary.lowStockCount}
                </span>
              </span>
            </Card>
            <Card className="flex items-center justify-between gap-2 px-4 py-3">
              <span className="text-[13.5px] text-ink-600">Expiring ≤ 30 days</span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-status-warn-dot" />
                <span className="text-xl font-bold [font-variant-numeric:tabular-nums]">
                  {expiringSoonCount}
                </span>
              </span>
            </Card>
            <Card className="flex items-center justify-between gap-2 px-4 py-3">
              <span className="text-[13.5px] text-ink-600">Open purchase orders</span>
              <span className="text-xl font-bold [font-variant-numeric:tabular-nums]">
                {summary.pendingOrders}
              </span>
            </Card>
            <Card className="flex items-center justify-between gap-2 px-4 py-3">
              <span className="text-[13.5px] text-ink-600">Procurement cost · all time</span>
              <span className="text-xl font-bold [font-variant-numeric:tabular-nums]">
                {formatCurrency(summary.totalProcurementCost)}
              </span>
            </Card>
          </div>

          {/* Two-column: restock + recent orders */}
          <div className="grid grid-cols-[1.1fr_1fr] items-start gap-4">
            <Card className="overflow-hidden">
              <div className="flex items-center justify-between border-b border-surface-linelight px-5 py-4">
                <div className="flex items-baseline gap-2.5">
                  <span className="text-lg font-semibold">Restock suggestions</span>
                  <span className="text-[13px] text-ink-400">tap a row for detail</span>
                </div>
                <span className="text-sm font-semibold text-ink-400">{restockGroups.length} products</span>
              </div>

              {restockGroups.length === 0 && (
                <div className="px-5 py-6 text-sm text-ink-400">Nothing needs restocking right now.</div>
              )}

              {restockGroups.map((group) => {
                const isOpen = expanded === group.productSku;
                const anyOut = group.rows.some((r) => r.quantityOnHand === 0);
                return (
                  <div key={group.productSku} className="border-b border-surface-linelight last:border-b-0">
                    <button
                      onClick={() => setExpanded(isOpen ? null : group.productSku)}
                      className="flex min-h-[56px] w-full items-center gap-3 px-5 py-2 text-left hover:bg-[#FAFCFB]"
                    >
                      <span className="w-4 flex-none text-[13px] text-ink-400">{isOpen ? '▾' : '▸'}</span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-[16px] font-semibold">{group.productName}</span>
                        <span className="text-[13px] text-ink-600">
                          Low at {group.rows.length} {group.rows.length === 1 ? 'branch' : 'branches'}
                        </span>
                      </span>
                      <StatusPill tone={anyOut ? 'danger' : 'warn'}>
                        {anyOut ? 'Out of Stock' : 'Low Stock'}
                      </StatusPill>
                    </button>
                    {isOpen && (
                      <div className="bg-[#FAFCFB] px-5 pb-4 pl-12">
                        <div className="grid grid-cols-3 gap-2 pb-2 text-[11.5px] font-bold uppercase tracking-wide text-ink-400">
                          <span>Branch</span>
                          <span className="text-right">On hand</span>
                          <span className="text-right">Reorder pt</span>
                        </div>
                        {group.rows.map((row) => (
                          <div
                            key={row.id}
                            className="grid grid-cols-3 gap-2 border-t border-surface-linelight py-1.5 text-[15px]"
                          >
                            <span>{row.branchName}</span>
                            <span
                              className={`text-right [font-variant-numeric:tabular-nums] ${
                                row.quantityOnHand === 0 ? 'font-semibold text-status-danger-dot' : 'font-semibold text-status-warn-dot'
                              }`}
                            >
                              {row.quantityOnHand}
                            </span>
                            <span className="text-right [font-variant-numeric:tabular-nums]">
                              {row.reorderLevel}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </Card>

            <Card className="overflow-hidden">
              <div className="flex items-center justify-between border-b border-surface-linelight px-5 py-4">
                <span className="text-lg font-semibold">Recent orders</span>
              </div>
              <div className="grid grid-cols-[1fr_1.4fr_1fr_1.1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
                <span>Order</span>
                <span>Customer</span>
                <span className="text-right">Total</span>
                <span className="text-right">Status</span>
              </div>
              {recentOrders.length === 0 && (
                <div className="px-5 py-6 text-sm text-ink-400">No orders recorded yet.</div>
              )}
              {recentOrders.map((order, i) => (
                <div
                  key={order.id}
                  className={`grid min-h-[52px] grid-cols-[1fr_1.4fr_1fr_1.1fr] items-center px-5 py-1.5 ${
                    i < recentOrders.length - 1 ? 'border-b border-surface-linelight' : ''
                  } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
                >
                  <span className="text-[15px] font-semibold [font-variant-numeric:tabular-nums]">
                    {order.orderNumber}
                  </span>
                  <span className="min-w-0 text-[15px]">
                    <span className="block truncate">{order.customer ?? order.staff ?? 'Walk-in'}</span>
                    <span className="text-[12.5px] text-ink-400">
                      {order.transactionType} · {order.branch ?? '—'}
                    </span>
                  </span>
                  <span className="text-right text-[15px] font-semibold [font-variant-numeric:tabular-nums]">
                    {formatCurrency(order.totalAmount)}
                  </span>
                  <span className="flex justify-end">
                    <StatusPill tone={orderStatusTone(order.orderStatus)}>{order.orderStatus}</StatusPill>
                  </span>
                </div>
              ))}
            </Card>
          </div>

          <div className="text-xs text-ink-400">
            Snapshot as of {formatDate(new Date().toISOString())}. Sales & Orders, Inventory, Procurement,
            Reports, Alerts and Administration are wired on the backend but not yet built in this pass.
          </div>
        </div>
      )}
    </InternalLayout>
  );
}
