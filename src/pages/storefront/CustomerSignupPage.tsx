import { useState, type FormEvent } from 'react';
import { Link, Navigate, useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';

export function CustomerSignupPage() {
  const { user, register } = useAuth();
  const navigate = useNavigate();
  const [fullName, setFullName] = useState('');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [contactNumber, setContactNumber] = useState('');
  const [address, setAddress] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (user) {
    return <Navigate to={user.roleCode === 'CUSTOMER' ? '/' : '/dashboard'} replace />;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await register({ username, email, password, fullName, contactNumber, address });
      navigate('/', { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Registration failed.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-bg px-4 py-10">
      <div className="w-full max-w-[440px] rounded-card border border-surface-line bg-white p-8 shadow-card">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-[14px] bg-brand-500 text-2xl font-extrabold text-white">
            G
          </div>
          <div>
            <div className="text-xl font-bold text-ink-900">Gerlyn Variety Store</div>
            <div className="text-sm text-ink-400">Create a customer account</div>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {error && (
            <div className="rounded-lg bg-status-danger-bg px-3.5 py-2.5 text-sm font-medium text-status-danger-text">
              {error}
            </div>
          )}

          <label className="flex flex-col gap-1.5 text-[13.5px] font-semibold text-ink-500">
            Full name
            <input
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              required
              className="h-11 rounded-input border border-[#C9D4CE] px-3.5 text-base text-ink-900 focus:border-action-600 focus:outline-none focus:ring-[3px] focus:ring-action-50"
            />
          </label>

          <label className="flex flex-col gap-1.5 text-[13.5px] font-semibold text-ink-500">
            Username
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
              className="h-11 rounded-input border border-[#C9D4CE] px-3.5 text-base text-ink-900 focus:border-action-600 focus:outline-none focus:ring-[3px] focus:ring-action-50"
            />
          </label>

          <label className="flex flex-col gap-1.5 text-[13.5px] font-semibold text-ink-500">
            Email
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              className="h-11 rounded-input border border-[#C9D4CE] px-3.5 text-base text-ink-900 focus:border-action-600 focus:outline-none focus:ring-[3px] focus:ring-action-50"
            />
          </label>

          <label className="flex flex-col gap-1.5 text-[13.5px] font-semibold text-ink-500">
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              required
              className="h-11 rounded-input border border-[#C9D4CE] px-3.5 text-base text-ink-900 focus:border-action-600 focus:outline-none focus:ring-[3px] focus:ring-action-50"
            />
          </label>

          <label className="flex flex-col gap-1.5 text-[13.5px] font-semibold text-ink-500">
            Contact number
            <input
              value={contactNumber}
              onChange={(e) => setContactNumber(e.target.value)}
              className="h-11 rounded-input border border-[#C9D4CE] px-3.5 text-base text-ink-900 focus:border-action-600 focus:outline-none focus:ring-[3px] focus:ring-action-50"
            />
          </label>

          <label className="flex flex-col gap-1.5 text-[13.5px] font-semibold text-ink-500">
            Address
            <input
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              className="h-11 rounded-input border border-[#C9D4CE] px-3.5 text-base text-ink-900 focus:border-action-600 focus:outline-none focus:ring-[3px] focus:ring-action-50"
            />
          </label>

          <button
            type="submit"
            disabled={submitting}
            className="mt-1 h-11 rounded-input bg-action-600 text-[15px] font-semibold text-white transition-colors hover:bg-action-700 disabled:opacity-60"
          >
            {submitting ? 'Creating account…' : 'Create account'}
          </button>
        </form>

        <div className="mt-5 text-center text-sm text-ink-400">
          Already have an account?{' '}
          <Link to="/login" className="font-semibold text-action-600 hover:text-action-700">
            Sign in
          </Link>
        </div>
      </div>
    </div>
  );
}
