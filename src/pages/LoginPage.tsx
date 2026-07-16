import { useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

export function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (user) {
    const from = (location.state as { from?: string } | null)?.from ?? '/';
    return <Navigate to={from} replace />;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(username, password);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-bg px-4">
      <div className="w-full max-w-[400px] rounded-card border border-surface-line bg-white p-8 shadow-card">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-[14px] bg-brand-500 text-2xl font-extrabold text-white">
            G
          </div>
          <div>
            <div className="text-xl font-bold text-ink-900">Gerlyn Variety Store</div>
            <div className="text-sm text-ink-400">Sign in to the staff dashboard</div>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="username" className="text-[13.5px] font-semibold text-ink-500">
              Username
            </label>
            <input
              id="username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              className="h-11 rounded-input border border-[#C9D4CE] px-3.5 text-base text-ink-900 focus:border-action-600 focus:outline-none focus:ring-[3px] focus:ring-action-50"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="password" className="text-[13.5px] font-semibold text-ink-500">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="h-11 rounded-input border border-[#C9D4CE] px-3.5 text-base text-ink-900 focus:border-action-600 focus:outline-none focus:ring-[3px] focus:ring-action-50"
            />
          </div>

          {error && (
            <div className="rounded-lg bg-status-danger-bg px-3.5 py-2.5 text-sm font-medium text-status-danger-text">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="mt-1 h-11 rounded-input bg-action-600 text-[15px] font-semibold text-white transition-colors hover:bg-action-700 disabled:opacity-60"
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
