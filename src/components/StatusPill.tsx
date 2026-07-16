type Tone = 'ok' | 'warn' | 'danger' | 'neutral';

const TONE_CLASSES: Record<Tone, string> = {
  ok: 'bg-status-ok-bg text-status-ok-text',
  warn: 'bg-status-warn-bg text-status-warn-text',
  danger: 'bg-status-danger-bg text-status-danger-text',
  neutral: 'bg-ink-900/5 text-ink-500',
};

const DOT_CLASSES: Record<Tone, string> = {
  ok: 'bg-status-ok-dot',
  warn: 'bg-status-warn-dot',
  danger: 'bg-status-danger-dot',
  neutral: 'bg-ink-400',
};

export function StatusPill({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1 text-[12.5px] font-semibold ${TONE_CLASSES[tone]}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${DOT_CLASSES[tone]}`} />
      {children}
    </span>
  );
}
