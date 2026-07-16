import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

interface ProtectedRouteProps {
  children: ReactNode;
  /** If set, user.roleCode must be one of these or they're redirected to `fallback`. */
  allowedRoles?: string[];
  /** Where to send an authenticated-but-wrong-role user. Defaults to the storefront home. */
  fallback?: string;
}

export function ProtectedRoute({ children, allowedRoles, fallback = '/' }: ProtectedRouteProps) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-surface-bg text-ink-600">
        Loading…
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }

  if (allowedRoles && !allowedRoles.includes(user.roleCode)) {
    return <Navigate to={fallback} replace />;
  }

  return <>{children}</>;
}
