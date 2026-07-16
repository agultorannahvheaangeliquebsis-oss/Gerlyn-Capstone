import type { ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

interface NavItem {
  label: string;
  path?: string;
  badge?: number;
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Dashboard', path: '/dashboard' },
  { label: 'Sales & Orders', path: '/sales' },
  { label: 'Inventory', path: '/inventory' },
  { label: 'Procurement', path: '/procurement' },
  { label: 'Reports', path: '/reports' },
  { label: 'Alerts', path: '/alerts' },
  { label: 'Administration', path: '/administration' },
];

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return parts
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase() ?? '')
    .join('');
}

export function InternalLayout({
  headline,
  eyebrow,
  meta,
  actions,
  children,
}: {
  headline: string;
  eyebrow: string;
  meta?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const { user, logout } = useAuth();
  const location = useLocation();

  return (
    <div className="flex min-h-screen bg-surface-bg">
      {/* Sidebar */}
      <aside className="flex w-[236px] flex-none flex-col border-r border-surface-line bg-white">
        <div className="flex items-center gap-2.5 px-4 pb-3.5 pt-4.5">
          <div className="flex h-[38px] w-[38px] flex-none items-center justify-center rounded-[10px] bg-brand-500 text-[18px] font-extrabold text-white">
            G
          </div>
          <div className="min-w-0">
            <div className="text-[15px] font-bold leading-tight">Gerlyn Variety Store</div>
            <div className="text-xs text-ink-400">Baliuag, Bulacan</div>
          </div>
        </div>

        <nav className="flex flex-col gap-0.5 px-3 py-1.5">
          {NAV_ITEMS.map((item) => {
            const isActive = !!item.path && location.pathname === item.path;
            const content = (
              <>
                <span>{item.label}</span>
                {typeof item.badge === 'number' && item.badge > 0 && (
                  <span className="flex h-5 min-w-[20px] items-center justify-center rounded-full bg-status-danger-dot px-1.5 text-xs font-bold text-white">
                    {item.badge}
                  </span>
                )}
              </>
            );

            if (!item.path) {
              return (
                <div
                  key={item.label}
                  className="flex min-h-[44px] cursor-not-allowed items-center justify-between rounded-lg px-3 text-[15px] font-medium text-ink-500"
                  title="Not built yet in this pass"
                >
                  {content}
                </div>
              );
            }

            return (
              <Link
                key={item.label}
                to={item.path}
                className={`flex min-h-[44px] items-center justify-between rounded-lg px-3 text-[15px] ${
                  isActive ? 'bg-action-50 font-semibold text-action-700' : 'font-medium text-ink-500 hover:bg-surface-bg'
                }`}
              >
                {content}
              </Link>
            );
          })}
        </nav>

        <div className="mt-auto flex items-center gap-2.5 border-t border-surface-line px-4 py-3">
          <div className="flex h-9 w-9 flex-none items-center justify-center rounded-full bg-action-50 text-[13px] font-bold text-action-600">
            {user ? initials(user.fullName || user.username) : '—'}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold leading-tight">
              {user?.fullName || user?.username}
            </div>
            <div className="truncate text-xs text-ink-400">{user?.branch ?? user?.role}</div>
          </div>
          <button
            onClick={() => void logout()}
            className="flex-none rounded-lg px-2 py-1.5 text-xs font-semibold text-ink-500 hover:bg-surface-bg"
          >
            Log out
          </button>
        </div>
      </aside>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="relative overflow-hidden bg-gradient-to-br from-action-700 via-action-600 to-brand-500 px-7 py-5 text-white">
          <div className="pointer-events-none absolute -right-[70px] -top-[100px] h-[260px] w-[260px] rounded-full bg-white/10" />
          <div className="relative flex items-end justify-between gap-4">
            <div>
              <div className="text-[13px] font-semibold uppercase tracking-wide text-white/75">
                {eyebrow}
              </div>
              <div className="mt-0.5 text-[28px] font-bold tracking-tight">{headline}</div>
              {meta && <div className="mt-1 text-sm text-white/85 [font-variant-numeric:tabular-nums]">{meta}</div>}
            </div>
            {actions && <div className="flex items-center gap-2.5">{actions}</div>}
          </div>
        </div>

        <div className="flex-1 p-7">{children}</div>
      </div>
    </div>
  );
}
