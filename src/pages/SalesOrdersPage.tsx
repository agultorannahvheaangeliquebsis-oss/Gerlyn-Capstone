import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { InternalLayout } from '../layouts/InternalLayout';
import { StatusPill } from '../components/StatusPill';
import { Card } from '../components/Card';
import { PaymentForm } from '../components/PaymentForm';
import { useAuth } from '../contexts/AuthContext';
import {
  salesApi,
  billingApi,
  productsApi,
  branchesApi,
  BASE_URL,
  type SalesTransaction,
  type BillingRecord,
  type Product,
  type Branch,
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

function paymentStatusTone(status: string): 'ok' | 'warn' | 'danger' | 'neutral' {
  switch (status) {
    case 'Paid':
      return 'ok';
    case 'Pending':
      return 'warn';
    case 'Rejected':
      return 'danger';
    default:
      return 'neutral';
  }
}

interface ItemRow {
  productId: string;
  quantity: string;
}

function emptyRow(): ItemRow {
  return { productId: '', quantity: '1' };
}

function NewSaleForm({
  products,
  branches,
  needsBranch,
  onCancel,
  onCreated,
}: {
  products: Product[];
  branches: Branch[];
  needsBranch: boolean;
  onCancel: () => void;
  onCreated: () => void;
}) {
  const [branchId, setBranchId] = useState('');
  const [transactionType, setTransactionType] = useState('Physical');
  const [modeOfPayment, setModeOfPayment] = useState('Cash');
  const [orderType, setOrderType] = useState('Retail');
  const [deliveryAddress, setDeliveryAddress] = useState('');
  const [rows, setRows] = useState<ItemRow[]>([emptyRow()]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function priceFor(product: Product): number {
    if (orderType === 'Wholesale' && product.wholesalePrice) {
      return parseFloat(product.wholesalePrice);
    }
    return parseFloat(product.retailPrice);
  }

  const previewTotal = useMemo(() => {
    return rows.reduce((sum, row) => {
      const product = products.find((p) => String(p.id) === row.productId);
      const qty = parseInt(row.quantity, 10);
      if (!product || !Number.isFinite(qty) || qty <= 0) return sum;
      return sum + priceFor(product) * qty;
    }, 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, products, orderType]);

  function updateRow(index: number, patch: Partial<ItemRow>) {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  function addRow() {
    setRows((prev) => [...prev, emptyRow()]);
  }

  function removeRow(index: number) {
    setRows((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== index) : prev));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    if (needsBranch && !branchId) {
      setError('Select a branch for this order.');
      return;
    }
    const items = rows
      .filter((row) => row.productId && parseInt(row.quantity, 10) > 0)
      .map((row) => ({ productId: Number(row.productId), quantity: parseInt(row.quantity, 10) }));
    if (items.length === 0) {
      setError('Add at least one item.');
      return;
    }
    if (transactionType === 'Online' && !deliveryAddress.trim()) {
      setError('Delivery address is required for Online orders.');
      return;
    }

    setSubmitting(true);
    try {
      await salesApi.create({
        branchId: needsBranch ? Number(branchId) : undefined,
        transactionType,
        modeOfPayment,
        orderType,
        deliveryAddress: transactionType === 'Online' ? deliveryAddress : undefined,
        items,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to record sale.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card className="p-5">
      <div className="mb-4 text-lg font-semibold">Record a sale</div>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        {error && (
          <div className="rounded-lg bg-status-danger-bg px-4 py-2.5 text-sm font-medium text-status-danger-text">
            {error}
          </div>
        )}

        <div className="grid grid-cols-4 gap-3">
          {needsBranch && (
            <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
              Branch
              <select
                value={branchId}
                onChange={(e) => setBranchId(e.target.value)}
                className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
              >
                <option value="">Select branch…</option>
                {branches.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
            Transaction type
            <select
              value={transactionType}
              onChange={(e) => setTransactionType(e.target.value)}
              className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
            >
              <option value="Physical">Physical</option>
              <option value="Online">Online</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
            Order type
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value)}
              className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
            >
              <option value="Retail">Retail</option>
              <option value="Wholesale">Wholesale</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
            Mode of payment
            <select
              value={modeOfPayment}
              onChange={(e) => setModeOfPayment(e.target.value)}
              className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
            >
              <option value="Cash">Cash</option>
              <option value="GCash">GCash</option>
              <option value="Card">Card</option>
            </select>
          </label>
        </div>

        {transactionType === 'Online' && (
          <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
            Delivery address
            <input
              value={deliveryAddress}
              onChange={(e) => setDeliveryAddress(e.target.value)}
              className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
              placeholder="Street, barangay, city"
            />
          </label>
        )}

        <div>
          <div className="mb-2 text-sm font-semibold text-ink-600">Items</div>
          <div className="flex flex-col gap-2">
            {rows.map((row, i) => {
              const product = products.find((p) => String(p.id) === row.productId);
              const qty = parseInt(row.quantity, 10);
              const lineTotal = product && Number.isFinite(qty) && qty > 0 ? priceFor(product) * qty : 0;
              return (
                <div key={i} className="flex items-center gap-2">
                  <select
                    value={row.productId}
                    onChange={(e) => updateRow(i, { productId: e.target.value })}
                    className="min-w-0 flex-1 rounded-lg border border-surface-line px-3 py-2 text-[15px] text-ink-900"
                  >
                    <option value="">Select product…</option>
                    {products.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} ({p.sku}) · {formatCurrency(orderType === 'Wholesale' && p.wholesalePrice ? p.wholesalePrice : p.retailPrice)}
                      </option>
                    ))}
                  </select>
                  <input
                    type="number"
                    min={1}
                    value={row.quantity}
                    onChange={(e) => updateRow(i, { quantity: e.target.value })}
                    className="w-24 flex-none rounded-lg border border-surface-line px-3 py-2 text-[15px] text-ink-900"
                  />
                  <span className="w-28 flex-none text-right text-[15px] font-semibold [font-variant-numeric:tabular-nums]">
                    {formatCurrency(lineTotal)}
                  </span>
                  <button
                    type="button"
                    onClick={() => removeRow(i)}
                    disabled={rows.length === 1}
                    className="flex-none rounded-lg px-2 py-2 text-sm font-semibold text-status-danger-text hover:bg-status-danger-bg disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    Remove
                  </button>
                </div>
              );
            })}
          </div>
          <button
            type="button"
            onClick={addRow}
            className="mt-2 rounded-lg px-3 py-1.5 text-sm font-semibold text-action-700 hover:bg-action-50"
          >
            + Add item
          </button>
        </div>

        <div className="flex items-center justify-between border-t border-surface-linelight pt-3">
          <span className="text-sm font-semibold text-ink-600">Estimated total</span>
          <span className="text-xl font-bold [font-variant-numeric:tabular-nums]">{formatCurrency(previewTotal)}</span>
        </div>

        <div className="flex gap-2">
          <button
            type="submit"
            disabled={submitting}
            className="rounded-lg bg-action-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-action-700 disabled:opacity-60"
          >
            {submitting ? 'Saving…' : 'Save transaction'}
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-surface-line px-4 py-2.5 text-sm font-semibold text-ink-600 hover:bg-surface-bg"
          >
            Cancel
          </button>
        </div>
      </form>
    </Card>
  );
}

export function SalesOrdersPage() {
  const { user } = useAuth();
  const [orders, setOrders] = useState<SalesTransaction[]>([]);
  const [billing, setBilling] = useState<BillingRecord[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const needsBranch = user?.roleCode === 'OWNER' || user?.roleCode === 'CUSTOMER';
  const canConfirmPayments = user?.roleCode === 'OWNER' || user?.roleCode === 'BRANCH_MANAGER' || user?.roleCode === 'STAFF';

  function loadAll() {
    setLoading(true);
    setError(null);
    const requests: [Promise<SalesTransaction[]>, Promise<BillingRecord[]>, Promise<Product[]>, Promise<Branch[]>] = [
      salesApi.list(),
      billingApi.list(),
      productsApi.list(),
      needsBranch ? branchesApi.list() : Promise.resolve([]),
    ];
    Promise.all(requests)
      .then(([ordersRes, billingRes, productsRes, branchesRes]) => {
        setOrders(ordersRes);
        setBilling(billingRes);
        setProducts(productsRes);
        setBranches(branchesRes);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load sales & orders.'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const billingByTxnId = useMemo(() => {
    const map = new Map<number, BillingRecord>();
    for (const record of billing) map.set(record.transactionId, record);
    return map;
  }, [billing]);

  const sortedOrders = useMemo(
    () => [...orders].sort((a, b) => new Date(b.transactionDate).getTime() - new Date(a.transactionDate).getTime()),
    [orders]
  );

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
      headline="Sales & Orders"
      meta={`${orders.length} transaction${orders.length === 1 ? '' : 's'}`}
      actions={
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded-lg bg-white/15 px-4 py-2 text-sm font-semibold text-white hover:bg-white/25"
        >
          {showForm ? 'Close form' : '+ Record sale'}
        </button>
      }
    >
      {loading && <div className="text-ink-400">Loading sales & orders…</div>}

      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {!loading && !error && (
        <div className="flex flex-col gap-5">
          {showForm && (
            <NewSaleForm
              products={products}
              branches={branches}
              needsBranch={needsBranch}
              onCancel={() => setShowForm(false)}
              onCreated={() => {
                setShowForm(false);
                loadAll();
              }}
            />
          )}

          {actionError && (
            <div className="rounded-lg bg-status-danger-bg px-4 py-2.5 text-sm font-medium text-status-danger-text">
              {actionError}
            </div>
          )}

          <Card className="overflow-hidden">
            <div className="grid grid-cols-[1fr_1.3fr_1fr_1fr_1fr_28px] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
              <span>Order</span>
              <span>Customer / branch</span>
              <span className="text-right">Total</span>
              <span className="text-right">Order status</span>
              <span className="text-right">Payment</span>
              <span />
            </div>

            {sortedOrders.length === 0 && (
              <div className="px-5 py-6 text-sm text-ink-400">No transactions recorded yet.</div>
            )}

            {sortedOrders.map((order, i) => {
              const record = billingByTxnId.get(order.id);
              const isOpen = expandedId === order.id;
              return (
                <div key={order.id} className="border-b border-surface-linelight last:border-b-0">
                  <button
                    onClick={() => setExpandedId(isOpen ? null : order.id)}
                    className={`grid min-h-[56px] w-full grid-cols-[1fr_1.3fr_1fr_1fr_1fr_28px] items-center px-5 py-1.5 text-left hover:bg-[#FAFCFB] ${
                      i % 2 === 1 ? 'bg-[#FAFCFB]' : ''
                    }`}
                  >
                    <span className="text-[15px] font-semibold [font-variant-numeric:tabular-nums]">
                      {order.orderNumber}
                    </span>
                    <span className="min-w-0 pr-2">
                      <span className="block truncate text-[15px]">{order.customer ?? order.staff ?? 'Walk-in'}</span>
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
                    <span className="flex justify-end">
                      {record ? (
                        <StatusPill tone={paymentStatusTone(record.status)}>{record.status}</StatusPill>
                      ) : (
                        <span className="text-[13px] text-ink-400">—</span>
                      )}
                    </span>
                    <span className="text-right text-[13px] text-ink-400">{isOpen ? '▾' : '▸'}</span>
                  </button>

                  {isOpen && (
                    <div className="grid grid-cols-2 gap-5 bg-[#FAFCFB] px-5 pb-5 pt-1">
                      <div>
                        <div className="mb-2 text-[11.5px] font-bold uppercase tracking-wide text-ink-400">
                          Items
                        </div>
                        <div className="flex flex-col gap-1.5">
                          {order.items.map((item) => (
                            <div key={item.id} className="grid grid-cols-[1fr_60px_90px] gap-2 text-[14px]">
                              <span className="truncate">{item.productName}</span>
                              <span className="text-right text-ink-600">×{item.quantitySold}</span>
                              <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                                {formatCurrency(item.subtotal)}
                              </span>
                            </div>
                          ))}
                        </div>
                        <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[13px] text-ink-600">
                          <span>Date</span>
                          <span className="text-right">{formatDate(order.transactionDate)}</span>
                          <span>Mode of payment</span>
                          <span className="text-right">{order.modeOfPayment}</span>
                          <span>Order type</span>
                          <span className="text-right">{order.orderType}</span>
                          {order.deliveryAddress && (
                            <>
                              <span>Delivery to</span>
                              <span className="text-right">{order.deliveryAddress}</span>
                            </>
                          )}
                        </div>
                      </div>

                      <div>
                        <div className="mb-2 text-[11.5px] font-bold uppercase tracking-wide text-ink-400">
                          Billing
                        </div>
                        {!record && <div className="text-sm text-ink-400">No billing record found.</div>}
                        {record && (
                          <div className="flex flex-col gap-3">
                            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[13px] text-ink-600">
                              <span>Amount due</span>
                              <span className="text-right font-semibold text-ink-900">
                                {formatCurrency(record.amountDue)}
                              </span>
                              <span>Method</span>
                              <span className="text-right">{record.paymentMethod || '—'}</span>
                              <span>Reference #</span>
                              <span className="text-right">{record.referenceNumber || '—'}</span>
                              <span>Status</span>
                              <span className="flex justify-end">
                                <StatusPill tone={paymentStatusTone(record.status)}>{record.status}</StatusPill>
                              </span>
                              {record.confirmedBy && (
                                <>
                                  <span>Confirmed by</span>
                                  <span className="text-right">{record.confirmedBy}</span>
                                </>
                              )}
                            </div>

                            {record.proofOfPayment && (
                              <a href={`${BASE_URL}${record.proofOfPayment}`} target="_blank" rel="noreferrer">
                                <img
                                  src={`${BASE_URL}${record.proofOfPayment}`}
                                  alt="Proof of payment"
                                  className="h-28 w-28 rounded-lg border border-surface-line object-cover"
                                />
                              </a>
                            )}

                            {record.status === 'Pending' && (
                              <PaymentForm record={record} onSaved={replaceBillingRecord} />
                            )}

                            {record.status === 'Pending' && canConfirmPayments && (
                              <div className="flex gap-2">
                                <button
                                  onClick={() => void handleConfirm(record, 'Paid')}
                                  className="rounded-lg bg-status-ok-dot px-3.5 py-1.5 text-sm font-semibold text-white hover:opacity-90"
                                >
                                  Mark paid
                                </button>
                                <button
                                  onClick={() => void handleConfirm(record, 'Rejected')}
                                  className="rounded-lg border border-status-danger-text px-3.5 py-1.5 text-sm font-semibold text-status-danger-text hover:bg-status-danger-bg"
                                >
                                  Reject
                                </button>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </Card>
        </div>
      )}
    </InternalLayout>
  );
}
