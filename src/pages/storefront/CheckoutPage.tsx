import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { StorefrontLayout } from '../../layouts/StorefrontLayout';
import { Card } from '../../components/Card';
import { useCart } from '../../contexts/CartContext';
import { salesApi } from '../../lib/api';
import { formatCurrency } from '../../lib/format';

export function CheckoutPage() {
  const navigate = useNavigate();
  const { branchId, branchName, items, subtotal, clear } = useCart();
  const [deliveryAddress, setDeliveryAddress] = useState('');
  const [modeOfPayment, setModeOfPayment] = useState('Cash');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (items.length === 0) {
    return (
      <StorefrontLayout>
        <Card className="p-8 text-center">
          <div className="text-lg font-semibold text-ink-900">Your cart is empty</div>
          <Link
            to="/products"
            className="mt-4 inline-block rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700"
          >
            Browse products
          </Link>
        </Card>
      </StorefrontLayout>
    );
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!deliveryAddress.trim()) {
      setError('Delivery address is required.');
      return;
    }
    setSubmitting(true);
    try {
      const order = await salesApi.create({
        branchId: branchId ?? undefined,
        transactionType: 'Online',
        modeOfPayment,
        orderType: 'Retail',
        deliveryAddress,
        items: items.map((i) => ({ productId: i.productId, quantity: i.quantity })),
      });
      clear();
      navigate(`/orders/${order.id}`, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to place order.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <StorefrontLayout>
      <div className="mx-auto flex max-w-2xl flex-col gap-5">
        <div>
          <div className="text-lg font-semibold text-ink-900">Checkout</div>
          <div className="text-sm text-ink-500">Order from {branchName}</div>
        </div>

        <Card className="p-5">
          <div className="mb-2 text-sm font-semibold text-ink-600">Order summary</div>
          <div className="flex flex-col gap-1.5 text-sm">
            {items.map((item) => (
              <div key={item.productId} className="flex justify-between text-ink-600">
                <span>
                  {item.name} × {item.quantity}
                </span>
                <span className="font-semibold text-ink-900">
                  {formatCurrency(parseFloat(item.unitPrice) * item.quantity)}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 flex justify-between border-t border-surface-linelight pt-3">
            <span className="text-sm font-semibold text-ink-600">Total</span>
            <span className="text-xl font-bold text-ink-900">{formatCurrency(subtotal)}</span>
          </div>
        </Card>

        <Card className="p-5">
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            {error && (
              <div className="rounded-lg bg-status-danger-bg px-4 py-2.5 text-sm font-medium text-status-danger-text">
                {error}
              </div>
            )}

            <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
              Delivery address
              <textarea
                value={deliveryAddress}
                onChange={(e) => setDeliveryAddress(e.target.value)}
                placeholder="Street, barangay, city"
                rows={3}
                className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
              />
            </label>

            <label className="flex flex-col gap-1 text-sm font-semibold text-ink-600">
              Mode of payment
              <select
                value={modeOfPayment}
                onChange={(e) => setModeOfPayment(e.target.value)}
                className="rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900"
              >
                <option value="Cash">Cash on delivery</option>
                <option value="GCash">GCash</option>
                <option value="Card">Card</option>
                <option value="Bank Transfer">Bank Transfer</option>
              </select>
            </label>

            <button
              type="submit"
              disabled={submitting}
              className="rounded-lg bg-action-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-action-700 disabled:opacity-60"
            >
              {submitting ? 'Placing order…' : 'Place order'}
            </button>
          </form>
        </Card>
      </div>
    </StorefrontLayout>
  );
}
