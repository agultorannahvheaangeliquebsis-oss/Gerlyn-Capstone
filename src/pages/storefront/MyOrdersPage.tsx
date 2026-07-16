import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { StorefrontLayout } from '../../layouts/StorefrontLayout';
import { Card } from '../../components/Card';
import { StatusPill } from '../../components/StatusPill';
import { salesApi, billingApi, type SalesTransaction, type BillingRecord } from '../../lib/api';
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

export function MyOrdersPage() {
  const [orders, setOrders] = useState<SalesTransaction[]>([]);
  const [billing, setBilling] = useState<BillingRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([salesApi.list(), billingApi.list()])
      .then(([ordersRes, billingRes]) => {
        setOrders(ordersRes);
        setBilling(billingRes);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load orders.'))
      .finally(() => setLoading(false));
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

  return (
    <StorefrontLayout>
      <div className="flex flex-col gap-5">
        <div className="text-lg font-semibold text-ink-900">My Orders</div>

        {loading && <div className="text-ink-400">Loading orders…</div>}
        {error && (
          <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
            {error}
          </div>
        )}

        {!loading && !error && sortedOrders.length === 0 && (
          <Card className="p-8 text-center">
            <div className="text-ink-500">You haven't placed any orders yet.</div>
            <Link
              to="/products"
              className="mt-4 inline-block rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700"
            >
              Start shopping
            </Link>
          </Card>
        )}

        {!loading && !error && sortedOrders.length > 0 && (
          <Card className="overflow-hidden">
            {sortedOrders.map((order, i) => {
              const record = billingByTxnId.get(order.id);
              return (
                <Link
                  key={order.id}
                  to={`/orders/${order.id}`}
                  className={`grid grid-cols-[1fr_1fr_1fr_1fr] items-center gap-2 border-b border-surface-linelight px-5 py-4 last:border-b-0 hover:bg-[#FAFCFB] ${
                    i % 2 === 1 ? 'bg-[#FAFCFB]' : ''
                  }`}
                >
                  <div>
                    <div className="text-[15px] font-semibold text-ink-900">{order.orderNumber}</div>
                    <div className="text-xs text-ink-400">{formatDate(order.transactionDate)}</div>
                  </div>
                  <div className="text-sm text-ink-600">{order.branch ?? '—'}</div>
                  <div className="text-right text-[15px] font-semibold text-ink-900">
                    {formatCurrency(order.totalAmount)}
                  </div>
                  <div className="flex justify-end gap-2">
                    <StatusPill tone={orderStatusTone(order.orderStatus)}>{order.orderStatus}</StatusPill>
                    {record && (
                      <StatusPill tone={paymentStatusTone(record.status)}>{record.status}</StatusPill>
                    )}
                  </div>
                </Link>
              );
            })}
          </Card>
        )}
      </div>
    </StorefrontLayout>
  );
}
