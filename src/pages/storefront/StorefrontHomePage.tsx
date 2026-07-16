import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { StorefrontLayout } from '../../layouts/StorefrontLayout';
import { Card } from '../../components/Card';
import { useCart } from '../../contexts/CartContext';
import { branchesApi, type Branch } from '../../lib/api';

export function StorefrontHomePage() {
  const navigate = useNavigate();
  const { branchId, branchName, setBranch } = useCart();
  const [branches, setBranches] = useState<Branch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    branchesApi
      .list()
      .then(setBranches)
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load branches.'))
      .finally(() => setLoading(false));
  }, []);

  function chooseBranch(branch: Branch) {
    setBranch(branch.id, branch.name);
    navigate('/products');
  }

  return (
    <StorefrontLayout>
      <div className="flex flex-col gap-8">
        <div className="rounded-card bg-gradient-to-br from-action-700 via-action-600 to-brand-500 px-8 py-10 text-white shadow-card">
          <div className="text-[13px] font-semibold uppercase tracking-wide text-white/75">
            Gerlyn Variety Store
          </div>
          <div className="mt-1 text-[32px] font-bold tracking-tight">
            Everyday goods, delivered from your neighborhood branch.
          </div>
          <div className="mt-2 max-w-xl text-[15px] text-white/85">
            Pick a branch to start shopping — pricing and stock are shown for the branch you choose.
          </div>
        </div>

        {branchId && (
          <div className="flex items-center justify-between rounded-card border border-surface-line bg-white px-5 py-3.5 shadow-card">
            <div className="text-sm text-ink-600">
              Currently shopping at <span className="font-semibold text-ink-900">{branchName}</span>
            </div>
            <button
              onClick={() => navigate('/products')}
              className="rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700"
            >
              Continue shopping
            </button>
          </div>
        )}

        <div>
          <div className="mb-3 text-lg font-semibold text-ink-900">Choose a branch</div>

          {loading && <div className="text-ink-400">Loading branches…</div>}
          {error && (
            <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
              {error}
            </div>
          )}

          {!loading && !error && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {branches.map((branch) => (
                <Card
                  key={branch.id}
                  className={`p-5 ${branch.id === branchId ? 'ring-2 ring-action-600' : ''}`}
                >
                  <div className="text-[17px] font-bold text-ink-900">{branch.name}</div>
                  <div className="mt-1 text-sm text-ink-500">{branch.location}</div>
                  <button
                    onClick={() => chooseBranch(branch)}
                    className="mt-4 rounded-lg border border-action-600 px-3.5 py-1.5 text-sm font-semibold text-action-600 hover:bg-action-50"
                  >
                    {branch.id === branchId ? 'Shop here' : 'Shop this branch'}
                  </button>
                </Card>
              ))}
              {branches.length === 0 && <div className="text-ink-400">No branches available yet.</div>}
            </div>
          )}
        </div>
      </div>
    </StorefrontLayout>
  );
}
