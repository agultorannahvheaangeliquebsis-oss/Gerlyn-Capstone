import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { InternalLayout } from '../layouts/InternalLayout';
import { StatusPill } from '../components/StatusPill';
import { Card } from '../components/Card';
import { useAuth } from '../contexts/AuthContext';
import {
  procurementApi,
  productsApi,
  branchesApi,
  type Procurement,
  type Product,
  type Branch,
  type Supplier,
} from '../lib/api';
import { formatCurrency, formatDate } from '../lib/format';

function statusTone(status: string): 'ok' | 'warn' | 'danger' | 'neutral' {
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

function receiptFlagTone(flag: string): 'ok' | 'warn' | 'danger' {
  return flag === 'Discrepancy' ? 'danger' : 'warn';
}

const today = () => new Date().toISOString().slice(0, 10);

interface ItemRow {
  productId: string;
  quantityOrdered: string;
  costPrice: string;
}

function emptyRow(): ItemRow {
  return { productId: '', quantityOrdered: '1', costPrice: '' };
}

function NewPOForm({
  products,
  branches,
  suppliers,
  onCancel,
  onCreated,
}: {
  products: Product[];
  branches: Branch[];
  suppliers: Supplier[];
  onCancel: () => void;
  onCreated: () => void;
}) {
  const [branchId, setBranchId] = useState('');
  const [supplierId, setSupplierId] = useState('');
  const [orderDate, setOrderDate] = useState(today());
  const [expectedDelivery, setExpectedDelivery] = useState('');
  const [rows, setRows] = useState<ItemRow[]>([emptyRow()]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const previewTotal = useMemo(
    () =>
      rows.reduce((sum, row) => {
        const qty = parseInt(row.quantityOrdered, 10);
        const cost = parseFloat(row.costPrice);
        if (!Number.isFinite(qty) || qty <= 0 || !Number.isFinite(cost)) return sum;
        return sum + qty * cost;
      }, 0),
    [rows]
  );

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

    if (!branchId) {
      setError('Select a branch.');
      return;
    }
    if (!supplierId) {
      setError('Select a supplier.');
      return;
    }
    const items = rows
      .filter((row) => row.productId && parseInt(row.quantityOrdered, 10) > 0 && parseFloat(row.costPrice) >= 0)
      .map((row) => ({
        productId: Number(row.productId),
        quantityOrdered: parseInt(row.quantityOrdered, 10),
        costPrice: parseFloat(row.costPrice),
      }));
    if (items.length === 0) {
      setError('Add at least one item with a quantity and cost price.');
      return;
    }

    setSubmitting(true);
    try {
      await procurementApi.create({
        branchId: Number(branchId),
        supplierId: Number(supplierId),
        orderDate,
        expectedDelivery: expectedDelivery || undefined,
        items,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create purchase order.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card className="p-5">
      <div className="mb-4 text-lg font-semibold">New purchase order</div>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        {error && (
          <div className="rounded-lg bg-status-danger-bg px-4 py-2.5 text-sm font-medium text-status-danger-text">
            {error}
          </div>
        )}

        <div className="grid grid-cols-4 gap-3">
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
          <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
            Supplier
            <select
              value={supplierId}
              onChange={(e) => setSupplierId(e.target.value)}
              className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
            >
              <option value="">Select supplier…</option>
              {suppliers.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
            Order date
            <input
              type="date"
              value={orderDate}
              onChange={(e) => setOrderDate(e.target.value)}
              className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
            Expected delivery
            <input
              type="date"
              value={expectedDelivery}
              onChange={(e) => setExpectedDelivery(e.target.value)}
              className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
            />
          </label>
        </div>

        <div>
          <div className="mb-2 text-sm font-semibold text-ink-600">Items</div>
          <div className="flex flex-col gap-2">
            {rows.map((row, i) => {
              const qty = parseInt(row.quantityOrdered, 10);
              const cost = parseFloat(row.costPrice);
              const lineTotal = Number.isFinite(qty) && qty > 0 && Number.isFinite(cost) ? qty * cost : 0;
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
                        {p.name} ({p.sku})
                      </option>
                    ))}
                  </select>
                  <input
                    type="number"
                    min={1}
                    value={row.quantityOrdered}
                    onChange={(e) => updateRow(i, { quantityOrdered: e.target.value })}
                    placeholder="Qty"
                    className="w-20 flex-none rounded-lg border border-surface-line px-3 py-2 text-[15px] text-ink-900"
                  />
                  <input
                    type="number"
                    min={0}
                    step="0.01"
                    value={row.costPrice}
                    onChange={(e) => updateRow(i, { costPrice: e.target.value })}
                    placeholder="Cost price"
                    className="w-28 flex-none rounded-lg border border-surface-line px-3 py-2 text-[15px] text-ink-900"
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
            {submitting ? 'Saving…' : 'Save as draft'}
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

function ReceiveForm({ proc, onSaved }: { proc: Procurement; onSaved: (updated: Procurement) => void }) {
  const [receiptFlag, setReceiptFlag] = useState('None');
  const [deliveryDate, setDeliveryDate] = useState(proc.expectedDelivery ?? today());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const updated = await procurementApi.updateStatus(proc.id, {
        status: 'Received',
        receiptFlag,
        deliveryDate: deliveryDate || undefined,
      });
      onSaved(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to mark as received.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2.5 rounded-lg bg-surface-bg p-3.5">
      {error && <div className="text-sm font-medium text-status-danger-text">{error}</div>}
      <div className="grid grid-cols-2 gap-2.5">
        <label className="flex flex-col gap-1 text-xs font-semibold text-ink-600">
          Delivery date
          <input
            type="date"
            value={deliveryDate}
            onChange={(e) => setDeliveryDate(e.target.value)}
            className="rounded-lg border border-surface-line bg-white px-2.5 py-1.5 text-sm font-normal text-ink-900"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs font-semibold text-ink-600">
          Receipt flag
          <select
            value={receiptFlag}
            onChange={(e) => setReceiptFlag(e.target.value)}
            className="rounded-lg border border-surface-line bg-white px-2.5 py-1.5 text-sm font-normal text-ink-900"
          >
            <option value="None">None — matches order</option>
            <option value="Partial">Partial</option>
            <option value="Discrepancy">Discrepancy</option>
          </select>
        </label>
      </div>
      <button
        type="submit"
        disabled={submitting}
        className="self-start rounded-lg bg-status-ok-dot px-3.5 py-1.5 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-60"
      >
        {submitting ? 'Saving…' : 'Confirm received'}
      </button>
    </form>
  );
}

export function ProcurementPage() {
  const { user } = useAuth();
  const [orders, setOrders] = useState<Procurement[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const canCreate = user?.roleCode === 'OWNER' || user?.roleCode === 'BRANCH_MANAGER';
  const canTransition =
    user?.roleCode === 'OWNER' || user?.roleCode === 'BRANCH_MANAGER' || user?.roleCode === 'SUPPLIER';

  function loadAll() {
    setLoading(true);
    setError(null);
    Promise.all([procurementApi.list(), productsApi.list(), branchesApi.list(), procurementApi.suppliers()])
      .then(([ordersRes, productsRes, branchesRes, suppliersRes]) => {
        setOrders(ordersRes);
        setProducts(productsRes);
        setBranches(branchesRes);
        setSuppliers(suppliersRes);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load procurement orders.'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadAll();
  }, []);

  const sortedOrders = useMemo(() => [...orders].sort((a, b) => b.id - a.id), [orders]);

  function replaceOrder(updated: Procurement) {
    setOrders((prev) => prev.map((o) => (o.id === updated.id ? updated : o)));
    setActionError(null);
  }

  async function handleTransition(proc: Procurement, status: 'Ordered' | 'Cancelled') {
    setActionError(null);
    try {
      const updated = await procurementApi.updateStatus(proc.id, { status });
      replaceOrder(updated);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to update order status.');
    }
  }

  async function handleDeleteDraft(proc: Procurement) {
    setActionError(null);
    try {
      await procurementApi.remove(proc.id);
      setOrders((prev) => prev.filter((o) => o.id !== proc.id));
      setExpandedId(null);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to delete draft.');
    }
  }

  return (
    <InternalLayout
      eyebrow={`Hello, ${user?.fullName?.split(' ')[0] ?? user?.username ?? ''}`}
      headline="Procurement"
      meta={`${orders.length} purchase order${orders.length === 1 ? '' : 's'}`}
      actions={
        canCreate ? (
          <button
            onClick={() => setShowForm((v) => !v)}
            className="rounded-lg bg-white/15 px-4 py-2 text-sm font-semibold text-white hover:bg-white/25"
          >
            {showForm ? 'Close form' : '+ New purchase order'}
          </button>
        ) : undefined
      }
    >
      {loading && <div className="text-ink-400">Loading procurement orders…</div>}

      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {!loading && !error && (
        <div className="flex flex-col gap-5">
          {showForm && canCreate && (
            <NewPOForm
              products={products}
              branches={branches}
              suppliers={suppliers}
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
            <div className="grid grid-cols-[1fr_1.2fr_1fr_0.9fr_1fr_1fr_28px] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
              <span>PO #</span>
              <span>Supplier / branch</span>
              <span className="text-right">Order date</span>
              <span className="text-right">Total</span>
              <span className="text-right">Status</span>
              <span className="text-right">Receipt</span>
              <span />
            </div>

            {sortedOrders.length === 0 && (
              <div className="px-5 py-6 text-sm text-ink-400">No purchase orders recorded yet.</div>
            )}

            {sortedOrders.map((proc, i) => {
              const isOpen = expandedId === proc.id;
              return (
                <div key={proc.id} className="border-b border-surface-linelight last:border-b-0">
                  <button
                    onClick={() => setExpandedId(isOpen ? null : proc.id)}
                    className={`grid min-h-[56px] w-full grid-cols-[1fr_1.2fr_1fr_0.9fr_1fr_1fr_28px] items-center px-5 py-1.5 text-left hover:bg-[#FAFCFB] ${
                      i % 2 === 1 ? 'bg-[#FAFCFB]' : ''
                    }`}
                  >
                    <span className="text-[15px] font-semibold [font-variant-numeric:tabular-nums]">
                      {proc.poNumber}
                    </span>
                    <span className="min-w-0 pr-2">
                      <span className="block truncate text-[15px]">{proc.supplierName ?? '—'}</span>
                      <span className="text-[12.5px] text-ink-400">{proc.branch ?? '—'}</span>
                    </span>
                    <span className="text-right text-[13px] text-ink-600">{formatDate(proc.orderDate)}</span>
                    <span className="text-right text-[15px] font-semibold [font-variant-numeric:tabular-nums]">
                      {formatCurrency(proc.total)}
                    </span>
                    <span className="flex justify-end">
                      <StatusPill tone={statusTone(proc.status)}>{proc.status}</StatusPill>
                    </span>
                    <span className="flex justify-end">
                      {proc.receiptFlag !== 'None' ? (
                        <StatusPill tone={receiptFlagTone(proc.receiptFlag)}>{proc.receiptFlag}</StatusPill>
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
                          {proc.items.map((item) => (
                            <div key={item.id} className="grid grid-cols-[1fr_60px_70px_90px] gap-2 text-[14px]">
                              <span className="truncate">{item.productName}</span>
                              <span className="text-right text-ink-600">
                                {item.quantityReceived}/{item.quantityOrdered}
                              </span>
                              <span className="text-right text-ink-600">{formatCurrency(item.costPrice)}</span>
                              <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                                {formatCurrency(item.subtotal)}
                              </span>
                            </div>
                          ))}
                        </div>
                        <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[13px] text-ink-600">
                          <span>Expected delivery</span>
                          <span className="text-right">
                            {proc.expectedDelivery ? formatDate(proc.expectedDelivery) : '—'}
                          </span>
                          <span>Authorized by</span>
                          <span className="text-right">{proc.authorizedBy ?? '—'}</span>
                          <span>Created by</span>
                          <span className="text-right">{proc.createdBy ?? '—'}</span>
                        </div>
                      </div>

                      <div>
                        <div className="mb-2 text-[11.5px] font-bold uppercase tracking-wide text-ink-400">
                          Actions
                        </div>
                        {!canTransition && (
                          <div className="text-sm text-ink-400">
                            No actions available for your role on this order.
                          </div>
                        )}
                        {canTransition && proc.status === 'Draft' && (
                          <div className="flex flex-wrap gap-2">
                            <button
                              onClick={() => void handleTransition(proc, 'Ordered')}
                              className="rounded-lg bg-action-600 px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-action-700"
                            >
                              Submit to supplier
                            </button>
                            <button
                              onClick={() => void handleTransition(proc, 'Cancelled')}
                              className="rounded-lg border border-status-danger-text px-3.5 py-1.5 text-sm font-semibold text-status-danger-text hover:bg-status-danger-bg"
                            >
                              Cancel
                            </button>
                            {canCreate && (
                              <button
                                onClick={() => void handleDeleteDraft(proc)}
                                className="rounded-lg px-3.5 py-1.5 text-sm font-semibold text-ink-500 hover:bg-surface-bg"
                              >
                                Delete draft
                              </button>
                            )}
                          </div>
                        )}
                        {canTransition && proc.status === 'Ordered' && (
                          <div className="flex flex-col gap-2">
                            <ReceiveForm proc={proc} onSaved={replaceOrder} />
                            <button
                              onClick={() => void handleTransition(proc, 'Cancelled')}
                              className="self-start rounded-lg border border-status-danger-text px-3.5 py-1.5 text-sm font-semibold text-status-danger-text hover:bg-status-danger-bg"
                            >
                              Cancel order
                            </button>
                          </div>
                        )}
                        {(proc.status === 'Received' || proc.status === 'Cancelled') && (
                          <div className="text-sm text-ink-400">This order is {proc.status.toLowerCase()}.</div>
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
