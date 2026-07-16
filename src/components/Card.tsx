import type { ReactNode } from 'react';

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-card border border-surface-line bg-white shadow-card ${className}`}>
      {children}
    </div>
  );
}
