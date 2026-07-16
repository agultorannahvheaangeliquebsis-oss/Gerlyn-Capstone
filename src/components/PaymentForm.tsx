import { useState, type FormEvent } from 'react';
import { billingApi, type BillingRecord } from '../lib/api';

export function PaymentForm({
  record,
  onSaved,
}: {
  record: BillingRecord;
  onSaved: (updated: BillingRecord) => void;
}) {
  const [paymentMethod, setPaymentMethod] = useState(record.paymentMethod || 'Cash');
  const [referenceNumber, setReferenceNumber] = useState('');
  const [proofFile, setProofFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const updated = await billingApi.pay(record.id, {
        paymentMethod,
        referenceNumber: referenceNumber || undefined,
        proofOfPayment: proofFile ?? undefined,
      });
      onSaved(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to record payment.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2.5 rounded-lg bg-surface-bg p-3.5">
      {error && <div className="text-sm font-medium text-status-danger-text">{error}</div>}
      <div className="grid grid-cols-2 gap-2.5">
        <label className="flex flex-col gap-1 text-xs font-semibold text-ink-600">
          Payment method
          <select
            value={paymentMethod}
            onChange={(e) => setPaymentMethod(e.target.value)}
            className="rounded-lg border border-surface-line bg-white px-2.5 py-1.5 text-sm font-normal text-ink-900"
          >
            <option value="Cash">Cash</option>
            <option value="GCash">GCash</option>
            <option value="Card">Card</option>
            <option value="Bank Transfer">Bank Transfer</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-semibold text-ink-600">
          Reference number
          <input
            value={referenceNumber}
            onChange={(e) => setReferenceNumber(e.target.value)}
            placeholder="Optional"
            className="rounded-lg border border-surface-line bg-white px-2.5 py-1.5 text-sm font-normal text-ink-900"
          />
        </label>
      </div>
      <label className="flex flex-col gap-1 text-xs font-semibold text-ink-600">
        Proof of payment
        <input
          type="file"
          accept="image/*"
          onChange={(e) => setProofFile(e.target.files?.[0] ?? null)}
          className="text-sm text-ink-600"
        />
      </label>
      <button
        type="submit"
        disabled={submitting}
        className="self-start rounded-lg bg-action-600 px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-action-700 disabled:opacity-60"
      >
        {submitting ? 'Saving…' : 'Submit payment'}
      </button>
    </form>
  );
}
