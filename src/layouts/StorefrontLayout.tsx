import type { ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { useCart } from '../contexts/CartContext';

export function StorefrontLayout({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const { itemCount } = useCart();
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate('/');
  }

  return (
    <div className="flex min-h-screen flex-col bg-surface-bg">
      <header className="border-b border-surface-line bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-3.5">
          <Link to="/" className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 flex-none items-center justify-center rounded-[10px] bg-brand-500 text-base font-extrabold text-white">
              G
            </div>
            <div className="text-[15px] font-bold text-ink-900">Gerlyn Variety Store</div>
          </Link>

          <nav className="flex items-center gap-1">
            <Link
              to="/products"
              className="rounded-lg px-3 py-2 text-sm font-medium text-ink-500 hover:bg-surface-bg"
            >
              Shop
            </Link>

            <Link
              to="/cart"
              className="relative rounded-lg px-3 py-2 text-sm font-medium text-ink-500 hover:bg-surface-bg"
            >
              Cart
              {itemCount > 0 && (
                <span className="absolute -right-0.5 -top-0.5 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-status-danger-dot px-1 text-[11px] font-bold text-white">
                  {itemCount}
                </span>
              )}
            </Link>

            {user?.roleCode === 'CUSTOMER' ? (
              <>
                <Link
                  to="/orders"
                  className="rounded-lg px-3 py-2 text-sm font-medium text-ink-500 hover:bg-surface-bg"
                >
                  My Orders
                </Link>
                <button
                  onClick={() => void handleLogout()}
                  className="rounded-lg px-3 py-2 text-sm font-semibold text-ink-500 hover:bg-surface-bg"
                >
                  Log out
                </button>
              </>
            ) : user ? (
              <Link
                to="/dashboard"
                className="rounded-lg px-3 py-2 text-sm font-medium text-ink-500 hover:bg-surface-bg"
              >
                Staff Dashboard
              </Link>
            ) : (
              <>
                <Link
                  to="/login"
                  className="rounded-lg px-3 py-2 text-sm font-medium text-ink-500 hover:bg-surface-bg"
                >
                  Log in
                </Link>
                <Link
                  to="/signup"
                  className="rounded-input bg-action-600 px-3.5 py-2 text-sm font-semibold text-white transition-colors hover:bg-action-700"
                >
                  Sign up
                </Link>
              </>
            )}
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-7">{children}</main>

      <footer className="border-t border-surface-line bg-white px-6 py-5 text-center text-xs text-ink-400">
        © Gerlyn Variety Store — Baliuag, Bulacan
      </footer>
    </div>
  );
}
