import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { StorefrontLayout } from '../../layouts/StorefrontLayout';
import { Card } from '../../components/Card';
import { useCart } from '../../contexts/CartContext';
import { productsApi, categoriesApi, type Product, type ProductCategory } from '../../lib/api';
import { formatCurrency } from '../../lib/format';

export function ProductCatalogPage() {
  const { branchId, branchName, addItem } = useCart();
  const [products, setProducts] = useState<Product[]>([]);
  const [categories, setCategories] = useState<ProductCategory[]>([]);
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [addedId, setAddedId] = useState<number | null>(null);

  useEffect(() => {
    if (!branchId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    Promise.all([
      productsApi.list({ branchId, search: search || undefined, category: category || undefined }),
      categoriesApi.list(),
    ])
      .then(([productsRes, categoriesRes]) => {
        setProducts(productsRes);
        setCategories(categoriesRes);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load products.'))
      .finally(() => setLoading(false));
  }, [branchId, search, category]);

  function handleAdd(product: Product) {
    addItem({
      productId: product.id,
      name: product.name,
      unit: product.unit,
      unitPrice: product.retailPrice,
    });
    setAddedId(product.id);
    setTimeout(() => setAddedId((current) => (current === product.id ? null : current)), 1200);
  }

  if (!branchId) {
    return (
      <StorefrontLayout>
        <Card className="p-8 text-center">
          <div className="text-lg font-semibold text-ink-900">Pick a branch first</div>
          <div className="mt-1 text-sm text-ink-500">
            Choose which branch you'd like to shop from to see products and prices.
          </div>
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
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-lg font-semibold text-ink-900">Shop {branchName}</div>
            <div className="text-sm text-ink-500">{products.length} products available</div>
          </div>
          <div className="flex gap-2">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search products…"
              className="rounded-lg border border-surface-line px-3.5 py-2 text-[15px] text-ink-900"
            />
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="rounded-lg border border-surface-line px-3.5 py-2 text-[15px] text-ink-900"
            >
              <option value="">All categories</option>
              {categories.map((c) => (
                <option key={c.id} value={c.categoryName}>
                  {c.categoryName}
                </option>
              ))}
            </select>
          </div>
        </div>

        {loading && <div className="text-ink-400">Loading products…</div>}
        {error && (
          <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
            {error}
          </div>
        )}

        {!loading && !error && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {products.map((product) => (
              <Card key={product.id} className="flex flex-col p-4">
                <Link to={`/products/${product.id}`} className="min-w-0">
                  <div className="truncate text-[15px] font-semibold text-ink-900">{product.name}</div>
                  <div className="text-xs text-ink-400">{product.category ?? 'Uncategorized'}</div>
                </Link>
                <div className="mt-2 flex items-baseline justify-between">
                  <span className="text-lg font-bold text-ink-900">{formatCurrency(product.retailPrice)}</span>
                  <span className="text-xs text-ink-400">per {product.unit}</span>
                </div>
                <div className="mt-1 text-xs text-ink-400">
                  {typeof product.stockAtBranch === 'number' && product.stockAtBranch > 0
                    ? `${product.stockAtBranch} in stock`
                    : 'Out of stock'}
                </div>
                <button
                  onClick={() => handleAdd(product)}
                  disabled={!product.stockAtBranch}
                  className="mt-3 rounded-lg bg-action-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-action-700 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {addedId === product.id ? 'Added ✓' : 'Add to cart'}
                </button>
              </Card>
            ))}
            {products.length === 0 && <div className="text-ink-400">No products match your search.</div>}
          </div>
        )}
      </div>
    </StorefrontLayout>
  );
}
