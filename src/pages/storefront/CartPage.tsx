import { Link, useNavigate } from 'react-router-dom';
import { StorefrontLayout } from '../../layouts/StorefrontLayout';
import { Card } from '../../components/Card';
import { useCart } from '../../contexts/CartContext';
import { formatCurrency } from '../../lib/format';

export function CartPage() {
  const navigate = useNavigate();
  const { branchName, items, subtotal, updateQuantity, removeItem } = useCart();

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

  return (
    <StorefrontLayout>
      <div className="flex flex-col gap-5">
        <div>
          <div className="text-lg font-semibold text-ink-900">Your cart</div>
          <div className="text-sm text-ink-500">Shopping at {branchName}</div>
        </div>

        <Card className="overflow-hidden">
          <div className="grid grid-cols-[1fr_120px_120px_28px] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
            <span>Product</span>
            <span className="text-right">Quantity</span>
            <span className="text-right">Subtotal</span>
            <span />
          </div>

          {items.map((item) => (
            <div
              key={item.productId}
              className="grid grid-cols-[1fr_120px_120px_28px] items-center border-b border-surface-linelight px-5 py-3 last:border-b-0"
            >
              <div>
                <div className="text-[15px] font-semibold text-ink-900">{item.name}</div>
                <div className="text-xs text-ink-400">
                  {formatCurrency(item.unitPrice)} per {item.unit}
                </div>
              </div>
              <input
                type="number"
                min={1}
                value={item.quantity}
                onChange={(e) => updateQuantity(item.productId, Math.max(1, parseInt(e.target.value, 10) || 1))}
                className="w-20 justify-self-end rounded-lg border border-surface-line px-2.5 py-1.5 text-right text-[15px] text-ink-900"
              />
              <span className="text-right text-[15px] font-semibold [font-variant-numeric:tabular-nums]">
                {formatCurrency(parseFloat(item.unitPrice) * item.quantity)}
              </span>
              <button
                onClick={() => removeItem(item.productId)}
                className="justify-self-end rounded-lg px-2 py-1.5 text-sm font-semibold text-status-danger-text hover:bg-status-danger-bg"
              >
                ✕
              </button>
            </div>
          ))}
        </Card>

        <Card className="flex items-center justify-between p-5">
          <span className="text-sm font-semibold text-ink-600">Subtotal</span>
          <span className="text-xl font-bold text-ink-900">{formatCurrency(subtotal)}</span>
        </Card>

        <div className="flex justify-end gap-3">
          <Link
            to="/products"
            className="rounded-lg border border-surface-line px-4 py-2.5 text-sm font-semibold text-ink-600 hover:bg-surface-bg"
          >
            Continue shopping
          </Link>
          <button
            onClick={() => navigate('/checkout')}
            className="rounded-lg bg-action-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-action-700"
          >
            Proceed to checkout
          </button>
        </div>
      </div>
    </StorefrontLayout>
  );
}
