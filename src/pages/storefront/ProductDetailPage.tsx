import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { StorefrontLayout } from '../../layouts/StorefrontLayout';
import { Card } from '../../components/Card';
import { useCart } from '../../contexts/CartContext';
import { productsApi, type Product } from '../../lib/api';
import { formatCurrency } from '../../lib/format';

export function ProductDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { branchId, branchName, addItem } = useCart();
  const [product, setProduct] = useState<Product | null>(null);
  const [related, setRelated] = useState<Product[]>([]);
  const [quantity, setQuantity] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [added, setAdded] = useState(false);

  useEffect(() => {
    if (!id || !branchId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setAdded(false);
    setQuantity(1);
    productsApi
      .get(Number(id), { branchId })
      .then((p) => {
        setProduct(p);
        return productsApi.list({ branchId, category: p.category ?? undefined });
      })
      .then((list) => setRelated(list.filter((p) => p.id !== Number(id)).slice(0, 6)))
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load product.'))
      .finally(() => setLoading(false));
  }, [id, branchId]);

  function handleAdd() {
    if (!product) return;
    addItem(
      { productId: product.id, name: product.name, unit: product.unit, unitPrice: product.retailPrice },
      quantity
    );
    setAdded(true);
  }

  if (!branchId) {
    return (
      <StorefrontLayout>
        <Card className="p-8 text-center">
          <div className="text-lg font-semibold text-ink-900">Pick a branch first</div>
          <Link
            to="/"
            className="mt-4 inline-block rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700"
          >
            Choose a branch
          </Link>
        </Card>
      </StorefrontLayout>
    );
  }

  return (
    <StorefrontLayout>
      {loading && <div className="text-ink-400">Loading product…</div>}
      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {!loading && !error && product && (
        <div className="flex flex-col gap-8">
          <Card className="p-6">
            <div className="text-xs text-ink-400">{product.category ?? 'Uncategorized'}</div>
            <div className="mt-1 text-2xl font-bold text-ink-900">{product.name}</div>
            <div className="mt-1 text-sm text-ink-400">SKU {product.sku} · at {branchName}</div>

            <div className="mt-4 flex items-baseline gap-2">
              <span className="text-3xl font-bold text-ink-900">{formatCurrency(product.retailPrice)}</span>
              <span className="text-sm text-ink-400">per {product.unit}</span>
            </div>

            <div className="mt-2 text-sm text-ink-500">
              {typeof product.stockAtBranch === 'number' && product.stockAtBranch > 0
                ? `${product.stockAtBranch} in stock at this branch`
                : 'Out of stock at this branch'}
            </div>

            <div className="mt-5 flex items-center gap-3">
              <input
                type="number"
                min={1}
                max={product.stockAtBranch ?? undefined}
                value={quantity}
                onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value, 10) || 1))}
                className="w-24 rounded-lg border border-surface-line px-3 py-2 text-[15px] text-ink-900"
              />
              <button
                onClick={handleAdd}
                disabled={!product.stockAtBranch}
                className="rounded-lg bg-action-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-action-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {added ? 'Added ✓' : 'Add to cart'}
              </button>
              {added && (
                <button
                  onClick={() => navigate('/cart')}
                  className="text-sm font-semibold text-action-700 hover:underline"
                >
                  View cart →
                </button>
              )}
            </div>
          </Card>

          {related.length > 0 && (
            <div>
              <div className="mb-3 text-lg font-semibold text-ink-900">Related products</div>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                {related.map((p) => (
                  <Link key={p.id} to={`/products/${p.id}`}>
                    <Card className="p-4">
                      <div className="truncate text-[15px] font-semibold text-ink-900">{p.name}</div>
                      <div className="mt-1 text-sm font-bold text-ink-900">{formatCurrency(p.retailPrice)}</div>
                    </Card>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </StorefrontLayout>
  );
}
