import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { StorefrontLayout } from '../../layouts/StorefrontLayout';
import { Card } from '../../components/Card';
import { StatusPill } from '../../components/StatusPill';
import { PaymentForm } from '../../components/PaymentForm';
import { salesApi, billingApi, BASE_URL, type SalesTransaction, type BillingRecord } from '../../lib/api';
import { formatCurrency, formatDate } from '../../lib/format';

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

export function OrderDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [order, setOrder] = useState<SalesTransaction | null>(null);
  const [billingRecord, setBillingRecord] = useState<BillingRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    Promise.all([salesApi.get(Number(id)), billingApi.list()])
      .then(([orderRes, billingRes]) => {
        setOrder(orderRes);
        setBillingRecord(billingRes.find((b) => b.transactionId === orderRes.id) ?? null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load order.'))
      .finally(() => setLoading(false));
  }, [id]);

  if (loading) {
    return (
      <StorefrontLayout>
        <div className="text-ink-400">Loading order…</div>
      </StorefrontLayout>
    );
  }

  if (error || !order) {
    return (
      <StorefrontLayout>
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error ?? 'Order not found.'}
        </div>
      </StorefrontLayout>
    );
  }

  return (
    <StorefrontLayout>
      <div className="mx-auto flex max-w-2xl flex-col gap-5">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-lg font-semibold text-ink-900">Order {order.orderNumber}</div>
            <div className="text-sm text-ink-500">{formatDate(order.transactionDate)}</div>
          </div>
          <StatusPill tone={orderStatusTone(order.orderStatus)}>{order.orderStatus}</StatusPill>
        </div>

        <Card className="p-5">
          <div className="mb-2 text-[11.5px] font-bold uppercase tracking-wide text-ink-400">Items</div>
          <div className="flex flex-col gap-1.5">
            {order.items.map((item) => (
              <div key={item.id} className="flex justify-between text-[14px] text-ink-600">
                <span>
                  {item.productName} × {item.quantitySold}
                </span>
                <span className="font-semibold text-ink-900">{formatCurrency(item.subtotal)}</span>
              </div>
            ))}
          </div>
          <div className="mt-3 flex justify-between border-t border-surface-linelight pt-3">
            <span className="text-sm font-semibold text-ink-600">Total</span>
            <span className="text-xl font-bold text-ink-900">{formatCurrency(order.totalAmount)}</span>
          </div>
          {order.deliveryAddress && (
            <div className="mt-3 text-sm text-ink-600">
              <span className="font-semibold">Delivering to: </span>
              {order.deliveryAddress}
            </div>
          )}
        </Card>

        <Card className="p-5">
          <div className="mb-2 text-[11.5px] font-bold uppercase tracking-wide text-ink-400">Billing</div>
          {!billingRecord && <div className="text-sm text-ink-400">No billing record found.</div>}
          {billingRecord && (
            <div className="flex flex-col gap-3">
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[13px] text-ink-600">
                <span>Amount due</span>
                <span className="text-right font-semibold text-ink-900">
                  {formatCurrency(billingRecord.amountDue)}
                </span>
                <span>Method</span>
                <span className="text-right">{billingRecord.paymentMethod || '—'}</span>
                <span>Status</span>
                <span className="flex justify-end">
                  <StatusPill tone={paymentStatusTone(billingRecord.status)}>{billingRecord.status}</StatusPill>
                </span>
              </div>

              {billingRecord.proofOfPayment && (
                <a href={`${BASE_URL}${billingRecord.proofOfPayment}`} target="_blank" rel="noreferrer">
                  <img
                    src={`${BASE_URL}${billingRecord.proofOfPayment}`}
                    alt="Proof of payment"
                    className="h-28 w-28 rounded-lg border border-surface-line object-cover"
                  />
                </a>
              )}

              {billingRecord.status === 'Pending' && (
                <PaymentForm record={billingRecord} onSaved={setBillingRecord} />
              )}
            </div>
          )}
        </Card>

        <Link to="/orders" className="text-sm font-semibold text-action-700 hover:underline">
          ← Back to my orders
        </Link>
      </div>
    </StorefrontLayout>
  );
}
