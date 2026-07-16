import { useEffect, useMemo, useState } from 'react';
import { InternalLayout } from '../layouts/InternalLayout';
import { StatusPill } from '../components/StatusPill';
import { Card } from '../components/Card';
import { useAuth } from '../contexts/AuthContext';
import {
  inventoryApi,
  warehouseStockApi,
  batchesApi,
  type InventoryRow,
  type WarehouseStockRow,
  type Batch,
} from '../lib/api';
import { formatDate } from '../lib/format';

type Tab = 'floor' | 'warehouse' | 'batches';

function stockTone(quantityOnHand: number, restock: boolean): 'ok' | 'warn' | 'danger' {
  if (quantityOnHand === 0) return 'danger';
  if (restock) return 'warn';
  return 'ok';
}

function stockLabel(quantityOnHand: number, restock: boolean): string {
  if (quantityOnHand === 0) return 'Out of stock';
  if (restock) return 'Low stock';
  return 'OK';
}

function expiryTone(status: string): 'ok' | 'warn' | 'danger' {
  switch (status) {
    case 'Expired':
      return 'danger';
    case 'Expiring Soon':
      return 'warn';
    default:
      return 'ok';
  }
}

const TABS: { key: Tab; label: string }[] = [
  { key: 'floor', label: 'Floor stock' },
  { key: 'warehouse', label: 'Warehouse stock' },
  { key: 'batches', label: 'Batches · FEFO' },
];

const BATCH_FILTERS = ['All', 'Fresh', 'Expiring Soon', 'Expired'] as const;

export function InventoryPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>('floor');
  const [floor, setFloor] = useState<InventoryRow[]>([]);
  const [warehouse, setWarehouse] = useState<WarehouseStockRow[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [batchFilter, setBatchFilter] = useState<(typeof BATCH_FILTERS)[number]>('All');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([inventoryApi.list(), warehouseStockApi.list(), batchesApi.list()])
      .then(([floorRes, warehouseRes, batchesRes]) => {
        if (cancelled) return;
        setFloor(floorRes);
        setWarehouse(warehouseRes);
        setBatches(batchesRes);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load inventory.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const query = search.trim().toLowerCase();

  const filteredFloor = useMemo(
    () =>
      floor.filter(
        (row) =>
          !query || row.productName.toLowerCase().includes(query) || row.productSku.toLowerCase().includes(query)
      ),
    [floor, query]
  );

  const filteredWarehouse = useMemo(
    () =>
      warehouse.filter(
        (row) =>
          !query || row.productName.toLowerCase().includes(query) || row.productSku.toLowerCase().includes(query)
      ),
    [warehouse, query]
  );

  const filteredBatches = useMemo(
    () =>
      batches
        .filter((b) => batchFilter === 'All' || b.expiryStatus === batchFilter)
        .filter((b) => !query || b.productName.toLowerCase().includes(query) || b.batchNumber.toLowerCase().includes(query)),
    [batches, query, batchFilter]
  );

  const floorLowCount = useMemo(() => floor.filter((r) => r.restock).length, [floor]);
  const warehouseLowCount = useMemo(() => warehouse.filter((r) => r.restockFlag).length, [warehouse]);
  const expiringCount = useMemo(
    () => batches.filter((b) => b.expiryStatus === 'Expiring Soon' || b.expiryStatus === 'Expired').length,
    [batches]
  );

  return (
    <InternalLayout
      eyebrow={`Hello, ${user?.fullName?.split(' ')[0] ?? user?.username ?? ''}`}
      headline="Inventory"
      meta={`${floorLowCount} floor low · ${warehouseLowCount} warehouse low · ${expiringCount} expiring/expired`}
    >
      {loading && <div className="text-ink-400">Loading inventory…</div>}

      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {!loading && !error && (
        <div className="flex flex-col gap-4">
          <div className="flex items-center justify-between gap-3">
            <div className="flex gap-1 rounded-lg border border-surface-line bg-white p-1">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className={`rounded-md px-3.5 py-1.5 text-sm font-semibold ${
                    tab === t.key ? 'bg-action-50 text-action-700' : 'text-ink-500 hover:bg-surface-bg'
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search product or SKU…"
              className="w-64 rounded-lg border border-surface-line bg-white px-3 py-2 text-sm text-ink-900"
            />
          </div>

          {tab === 'floor' && (
            <Card className="overflow-hidden">
              <div className="grid grid-cols-[1.6fr_1fr_0.8fr_0.8fr_1fr_1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
                <span>Product</span>
                <span>Branch</span>
                <span className="text-right">On hand</span>
                <span className="text-right">Reorder pt</span>
                <span className="text-right">Status</span>
                <span className="text-right">Expiry</span>
              </div>
              {filteredFloor.length === 0 && (
                <div className="px-5 py-6 text-sm text-ink-400">No floor stock records found.</div>
              )}
              {filteredFloor.map((row, i) => (
                <div
                  key={row.id}
                  className={`grid grid-cols-[1.6fr_1fr_0.8fr_0.8fr_1fr_1fr] items-center px-5 py-2.5 text-[14px] ${
                    i < filteredFloor.length - 1 ? 'border-b border-surface-linelight' : ''
                  } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-semibold">{row.productName}</span>
                    <span className="text-[12.5px] text-ink-400">{row.productSku}</span>
                  </span>
                  <span className="truncate text-ink-600">{row.branchName}</span>
                  <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                    {row.quantityOnHand}
                  </span>
                  <span className="text-right text-ink-600 [font-variant-numeric:tabular-nums]">
                    {row.reorderLevel}
                  </span>
                  <span className="flex justify-end">
                    <StatusPill tone={stockTone(row.quantityOnHand, row.restock)}>
                      {stockLabel(row.quantityOnHand, row.restock)}
                    </StatusPill>
                  </span>
                  <span className="text-right text-[13px] text-ink-600">
                    {row.expiryDate ? formatDate(row.expiryDate) : '—'}
                  </span>
                </div>
              ))}
            </Card>
          )}

          {tab === 'warehouse' && (
            <Card className="overflow-hidden">
              <div className="grid grid-cols-[1.6fr_1.2fr_0.8fr_0.8fr_1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
                <span>Product</span>
                <span>Warehouse</span>
                <span className="text-right">On hand</span>
                <span className="text-right">Reorder pt</span>
                <span className="text-right">Status</span>
              </div>
              {filteredWarehouse.length === 0 && (
                <div className="px-5 py-6 text-sm text-ink-400">No warehouse stock records found.</div>
              )}
              {filteredWarehouse.map((row, i) => (
                <div
                  key={row.id}
                  className={`grid grid-cols-[1.6fr_1.2fr_0.8fr_0.8fr_1fr] items-center px-5 py-2.5 text-[14px] ${
                    i < filteredWarehouse.length - 1 ? 'border-b border-surface-linelight' : ''
                  } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
                >
                  <span className="min-w-0">
                    <span className="block truncate font-semibold">{row.productName}</span>
                    <span className="text-[12.5px] text-ink-400">{row.productSku}</span>
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate text-ink-600">{row.warehouseName}</span>
                    <span className="text-[12.5px] text-ink-400">{row.branchName}</span>
                  </span>
                  <span className="text-right font-semibold [font-variant-numeric:tabular-nums]">
                    {row.quantityOnHand}
                  </span>
                  <span className="text-right text-ink-600 [font-variant-numeric:tabular-nums]">
                    {row.reorderLevel}
                  </span>
                  <span className="flex justify-end">
                    <StatusPill tone={stockTone(row.quantityOnHand, row.restockFlag)}>
                      {stockLabel(row.quantityOnHand, row.restockFlag)}
                    </StatusPill>
                  </span>
                </div>
              ))}
            </Card>
          )}

          {tab === 'batches' && (
            <div className="flex flex-col gap-3">
              <div className="flex gap-2">
                {BATCH_FILTERS.map((f) => (
                  <button
                    key={f}
                    onClick={() => setBatchFilter(f)}
                    className={`rounded-full border px-3 py-1 text-[13px] font-semibold ${
                      batchFilter === f
                        ? 'border-action-600 bg-action-50 text-action-700'
                        : 'border-surface-line bg-white text-ink-500 hover:bg-surface-bg'
                    }`}
                  >
                    {f}
                  </button>
                ))}
              </div>

              <Card className="overflow-hidden">
                <div className="grid grid-cols-[1fr_1.4fr_1fr_0.7fr_1fr_1fr_1fr_1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
                  <span>Batch #</span>
                  <span>Product</span>
                  <span>Branch</span>
                  <span className="text-right">Qty</span>
                  <span className="text-right">Received</span>
                  <span className="text-right">Expiry</span>
                  <span className="text-right">Days left</span>
                  <span className="text-right">Status</span>
                </div>
                {filteredBatches.length === 0 && (
                  <div className="px-5 py-6 text-sm text-ink-400">No batches found for this filter.</div>
                )}
                {filteredBatches.map((b, i) => (
                  <div
                    key={b.id}
                    className={`grid grid-cols-[1fr_1.4fr_1fr_0.7fr_1fr_1fr_1fr_1fr] items-center px-5 py-2.5 text-[14px] ${
                      i < filteredBatches.length - 1 ? 'border-b border-surface-linelight' : ''
                    } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
                  >
                    <span className="font-semibold [font-variant-numeric:tabular-nums]">{b.batchNumber}</span>
                    <span className="truncate">{b.productName}</span>
                    <span className="truncate text-ink-600">{b.branchName}</span>
                    <span className="text-right [font-variant-numeric:tabular-nums]">{b.quantity}</span>
                    <span className="text-right text-[13px] text-ink-600">{formatDate(b.receivedDate)}</span>
                    <span className="text-right text-[13px] text-ink-600">{formatDate(b.expiryDate)}</span>
                    <span
                      className={`text-right [font-variant-numeric:tabular-nums] ${
                        b.daysUntilExpiry < 0 ? 'font-semibold text-status-danger-dot' : ''
                      }`}
                    >
                      {b.daysUntilExpiry < 0 ? `${Math.abs(b.daysUntilExpiry)}d ago` : `${b.daysUntilExpiry}d`}
                    </span>
                    <span className="flex justify-end">
                      <StatusPill tone={expiryTone(b.expiryStatus)}>{b.expiryStatus}</StatusPill>
                    </span>
                  </div>
                ))}
              </Card>
            </div>
          )}
        </div>
      )}
    </InternalLayout>
  );
}
