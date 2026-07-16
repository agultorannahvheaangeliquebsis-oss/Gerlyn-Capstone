import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { InternalLayout } from '../layouts/InternalLayout';
import { StatusPill } from '../components/StatusPill';
import { Card } from '../components/Card';
import { useAuth } from '../contexts/AuthContext';
import {
  usersApi,
  branchesApi,
  categoriesApi,
  type AdminUser,
  type Branch,
  type ProductCategory,
} from '../lib/api';

type Tab = 'users' | 'branches' | 'categories';

const TABS: { key: Tab; label: string }[] = [
  { key: 'users', label: 'Users' },
  { key: 'branches', label: 'Branches' },
  { key: 'categories', label: 'Product categories' },
];

const ROLE_OPTIONS = [
  { value: 'OWNER', label: 'Owner' },
  { value: 'BRANCH_MANAGER', label: 'Branch Manager' },
  { value: 'STAFF', label: 'Store Staff' },
  { value: 'SUPPLIER', label: 'Supplier' },
  { value: 'CUSTOMER', label: 'Customer' },
];

const inputClass = 'rounded-lg border border-surface-line px-3 py-2 text-[15px] font-normal text-ink-900';
const labelClass = 'flex flex-col gap-1 text-sm font-semibold text-ink-600';

// ─── Users tab ────────────────────────────────────────────────────────────

function NewUserForm({
  branches,
  onCancel,
  onCreated,
}: {
  branches: Branch[];
  onCancel: () => void;
  onCreated: () => void;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [role, setRole] = useState('STAFF');
  const [fullName, setFullName] = useState('');
  const [contactNumber, setContactNumber] = useState('');
  const [branchId, setBranchId] = useState('');
  const [address, setAddress] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const needsBranch = role === 'BRANCH_MANAGER' || role === 'STAFF';
  const needsAddress = role === 'CUSTOMER';

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!username || !password) {
      setError('Username and password are required.');
      return;
    }
    setSubmitting(true);
    try {
      await usersApi.create({
        username,
        password,
        role,
        email: email || undefined,
        fullName: fullName || undefined,
        contactNumber: contactNumber || undefined,
        branchId: needsBranch && branchId ? Number(branchId) : undefined,
        address: needsAddress ? address : undefined,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create user.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card className="p-5">
      <div className="mb-4 text-lg font-semibold">New user account</div>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        {error && (
          <div className="rounded-lg bg-status-danger-bg px-4 py-2.5 text-sm font-medium text-status-danger-text">
            {error}
          </div>
        )}
        <div className="grid grid-cols-4 gap-3">
          <label className={labelClass}>
            Username
            <input value={username} onChange={(e) => setUsername(e.target.value)} className={inputClass} />
          </label>
          <label className={labelClass}>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={inputClass}
            />
          </label>
          <label className={labelClass}>
            Email (optional)
            <input value={email} onChange={(e) => setEmail(e.target.value)} className={inputClass} />
          </label>
          <label className={labelClass}>
            Role
            <select value={role} onChange={(e) => setRole(e.target.value)} className={inputClass}>
              {ROLE_OPTIONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          <label className={labelClass}>
            Full name
            <input value={fullName} onChange={(e) => setFullName(e.target.value)} className={inputClass} />
          </label>
          <label className={labelClass}>
            Contact number
            <input
              value={contactNumber}
              onChange={(e) => setContactNumber(e.target.value)}
              className={inputClass}
            />
          </label>
          {needsBranch && (
            <label className={labelClass}>
              Branch
              <select value={branchId} onChange={(e) => setBranchId(e.target.value)} className={inputClass}>
                <option value="">Select branch…</option>
                {branches.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {needsAddress && (
            <label className={labelClass}>
              Address
              <input value={address} onChange={(e) => setAddress(e.target.value)} className={inputClass} />
            </label>
          )}
        </div>
        <div className="flex gap-2">
          <button
            type="submit"
            disabled={submitting}
            className="rounded-lg bg-action-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-action-700 disabled:opacity-60"
          >
            {submitting ? 'Creating…' : 'Create user'}
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-surface-line px-4 py-2.5 text-sm font-semibold text-ink-600 hover:bg-surface-bg"
          >
            Cancel
          </button>
        </div>
      </form>
    </Card>
  );
}

function UsersTab({ branches }: { branches: Branch[] }) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  function load() {
    setLoading(true);
    setError(null);
    usersApi
      .list()
      .then(setUsers)
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load users.'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  const sorted = useMemo(() => [...users].sort((a, b) => a.role.localeCompare(b.role) || a.username.localeCompare(b.username)), [users]);

  async function toggleActive(u: AdminUser) {
    setActionError(null);
    try {
      const updated = await usersApi.update(u.id, { isActive: !u.isActive });
      setUsers((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Failed to update user.');
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700"
        >
          {showForm ? 'Close form' : '+ New user'}
        </button>
      </div>

      {showForm && (
        <NewUserForm
          branches={branches}
          onCancel={() => setShowForm(false)}
          onCreated={() => {
            setShowForm(false);
            load();
          }}
        />
      )}

      {loading && <div className="text-ink-400">Loading users…</div>}
      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}
      {actionError && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-2.5 text-sm font-medium text-status-danger-text">
          {actionError}
        </div>
      )}

      {!loading && !error && (
        <Card className="overflow-hidden">
          <div className="grid grid-cols-[1fr_1fr_1fr_1fr_1fr_0.8fr_0.8fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
            <span>Username</span>
            <span>Full name</span>
            <span>Role</span>
            <span>Branch</span>
            <span>Contact</span>
            <span className="text-right">Status</span>
            <span className="text-right">Action</span>
          </div>
          {sorted.map((u, i) => (
            <div
              key={u.id}
              className={`grid grid-cols-[1fr_1fr_1fr_1fr_1fr_0.8fr_0.8fr] items-center px-5 py-2.5 text-[14px] ${
                i < sorted.length - 1 ? 'border-b border-surface-linelight' : ''
              } ${i % 2 === 1 ? 'bg-[#FAFCFB]' : ''}`}
            >
              <span className="truncate font-semibold">{u.username}</span>
              <span className="truncate">{u.fullName}</span>
              <span className="text-ink-600">{u.role}</span>
              <span className="truncate text-ink-600">{u.branch ?? '—'}</span>
              <span className="truncate text-ink-600">{u.contactNumber || '—'}</span>
              <span className="flex justify-end">
                <StatusPill tone={u.isActive ? 'ok' : 'neutral'}>{u.isActive ? 'Active' : 'Inactive'}</StatusPill>
              </span>
              <span className="flex justify-end">
                <button
                  onClick={() => void toggleActive(u)}
                  className={`rounded-lg px-2.5 py-1 text-[12.5px] font-semibold ${
                    u.isActive
                      ? 'border border-status-danger-text text-status-danger-text hover:bg-status-danger-bg'
                      : 'bg-status-ok-dot text-white hover:opacity-90'
                  }`}
                >
                  {u.isActive ? 'Deactivate' : 'Activate'}
                </button>
              </span>
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}

// ─── Branches tab ─────────────────────────────────────────────────────────

function BranchRow({
  branch,
  onSaved,
  onDeleted,
}: {
  branch: Branch;
  onSaved: (b: Branch) => void;
  onDeleted: (id: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(branch.name);
  const [location, setLocation] = useState(branch.location);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setError(null);
    setSubmitting(true);
    try {
      const updated = await branchesApi.update(branch.id, { name, location });
      onSaved(updated);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update branch.');
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete() {
    setError(null);
    try {
      await branchesApi.remove(branch.id);
      onDeleted(branch.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete branch.');
    }
  }

  if (editing) {
    return (
      <div className="flex flex-col gap-2 border-b border-surface-linelight bg-[#FAFCFB] px-5 py-3">
        {error && <div className="text-sm font-medium text-status-danger-text">{error}</div>}
        <div className="flex items-center gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)} className={`${inputClass} flex-1`} />
          <input
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            className={`${inputClass} flex-1`}
          />
          <button
            onClick={() => void handleSave()}
            disabled={submitting}
            className="rounded-lg bg-action-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-action-700 disabled:opacity-60"
          >
            Save
          </button>
          <button
            onClick={() => setEditing(false)}
            className="rounded-lg border border-surface-line px-3 py-1.5 text-sm font-semibold text-ink-600 hover:bg-white"
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-[1.2fr_2fr_1fr_1fr] items-center border-b border-surface-linelight px-5 py-2.5 text-[14px] last:border-b-0">
      <span className="font-semibold">{branch.name}</span>
      <span className="truncate text-ink-600">{branch.location}</span>
      <span className="text-ink-600">{branch.ownerName ?? '—'}</span>
      <span className="flex justify-end gap-2">
        <button
          onClick={() => setEditing(true)}
          className="rounded-lg px-2.5 py-1 text-[12.5px] font-semibold text-action-700 hover:bg-action-50"
        >
          Edit
        </button>
        <button
          onClick={() => void handleDelete()}
          className="rounded-lg px-2.5 py-1 text-[12.5px] font-semibold text-status-danger-text hover:bg-status-danger-bg"
        >
          Delete
        </button>
      </span>
      {error && <div className="col-span-4 pt-1 text-[12.5px] font-medium text-status-danger-text">{error}</div>}
    </div>
  );
}

function BranchesTab({
  branches,
  setBranches,
  loading,
  error,
}: {
  branches: Branch[];
  setBranches: (b: Branch[]) => void;
  loading: boolean;
  error: string | null;
}) {
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState('');
  const [location, setLocation] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!name || !location) {
      setFormError('Name and location are required.');
      return;
    }
    setSubmitting(true);
    try {
      const created = await branchesApi.create({ name, location });
      setBranches([...branches, created]);
      setName('');
      setLocation('');
      setShowForm(false);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to create branch.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700"
        >
          {showForm ? 'Close form' : '+ New branch'}
        </button>
      </div>

      {showForm && (
        <Card className="p-5">
          <form onSubmit={handleCreate} className="flex flex-col gap-3">
            {formError && <div className="text-sm font-medium text-status-danger-text">{formError}</div>}
            <div className="flex gap-3">
              <label className={`${labelClass} flex-1`}>
                Name
                <input value={name} onChange={(e) => setName(e.target.value)} className={inputClass} />
              </label>
              <label className={`${labelClass} flex-1`}>
                Location
                <input value={location} onChange={(e) => setLocation(e.target.value)} className={inputClass} />
              </label>
            </div>
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={submitting}
                className="self-start rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700 disabled:opacity-60"
              >
                {submitting ? 'Creating…' : 'Create branch'}
              </button>
            </div>
          </form>
        </Card>
      )}

      {loading && <div className="text-ink-400">Loading branches…</div>}
      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {!loading && !error && (
        <Card className="overflow-hidden">
          <div className="grid grid-cols-[1.2fr_2fr_1fr_1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
            <span>Name</span>
            <span>Location</span>
            <span>Owner</span>
            <span className="text-right">Actions</span>
          </div>
          {branches.length === 0 && <div className="px-5 py-6 text-sm text-ink-400">No branches yet.</div>}
          {branches.map((b) => (
            <BranchRow
              key={b.id}
              branch={b}
              onSaved={(updated) => setBranches(branches.map((x) => (x.id === updated.id ? updated : x)))}
              onDeleted={(id) => setBranches(branches.filter((x) => x.id !== id))}
            />
          ))}
        </Card>
      )}
    </div>
  );
}

// ─── Categories tab ───────────────────────────────────────────────────────

function CategoryRow({
  category,
  onSaved,
  onDeleted,
}: {
  category: ProductCategory;
  onSaved: (c: ProductCategory) => void;
  onDeleted: (id: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [categoryName, setCategoryName] = useState(category.categoryName);
  const [description, setDescription] = useState(category.description);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setError(null);
    setSubmitting(true);
    try {
      const updated = await categoriesApi.update(category.id, { categoryName, description });
      onSaved(updated);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update category.');
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete() {
    setError(null);
    try {
      await categoriesApi.remove(category.id);
      onDeleted(category.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete category.');
    }
  }

  if (editing) {
    return (
      <div className="flex flex-col gap-2 border-b border-surface-linelight bg-[#FAFCFB] px-5 py-3">
        {error && <div className="text-sm font-medium text-status-danger-text">{error}</div>}
        <div className="flex items-center gap-2">
          <input
            value={categoryName}
            onChange={(e) => setCategoryName(e.target.value)}
            className={`${inputClass} flex-1`}
          />
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Description"
            className={`${inputClass} flex-1`}
          />
          <button
            onClick={() => void handleSave()}
            disabled={submitting}
            className="rounded-lg bg-action-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-action-700 disabled:opacity-60"
          >
            Save
          </button>
          <button
            onClick={() => setEditing(false)}
            className="rounded-lg border border-surface-line px-3 py-1.5 text-sm font-semibold text-ink-600 hover:bg-white"
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-[1.2fr_2fr_1fr] items-center border-b border-surface-linelight px-5 py-2.5 text-[14px] last:border-b-0">
      <span className="font-semibold">{category.categoryName}</span>
      <span className="truncate text-ink-600">{category.description || '—'}</span>
      <span className="flex justify-end gap-2">
        <button
          onClick={() => setEditing(true)}
          className="rounded-lg px-2.5 py-1 text-[12.5px] font-semibold text-action-700 hover:bg-action-50"
        >
          Edit
        </button>
        <button
          onClick={() => void handleDelete()}
          className="rounded-lg px-2.5 py-1 text-[12.5px] font-semibold text-status-danger-text hover:bg-status-danger-bg"
        >
          Delete
        </button>
      </span>
      {error && <div className="col-span-3 pt-1 text-[12.5px] font-medium text-status-danger-text">{error}</div>}
    </div>
  );
}

function CategoriesTab() {
  const [categories, setCategories] = useState<ProductCategory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [categoryName, setCategoryName] = useState('');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  function load() {
    setLoading(true);
    setError(null);
    categoriesApi
      .list()
      .then(setCategories)
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load categories.'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!categoryName) {
      setFormError('Category name is required.');
      return;
    }
    setSubmitting(true);
    try {
      const created = await categoriesApi.create({ categoryName, description });
      setCategories((prev) => [...prev, created]);
      setCategoryName('');
      setDescription('');
      setShowForm(false);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to create category.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700"
        >
          {showForm ? 'Close form' : '+ New category'}
        </button>
      </div>

      {showForm && (
        <Card className="p-5">
          <form onSubmit={handleCreate} className="flex flex-col gap-3">
            {formError && <div className="text-sm font-medium text-status-danger-text">{formError}</div>}
            <div className="flex gap-3">
              <label className={`${labelClass} flex-1`}>
                Category name
                <input
                  value={categoryName}
                  onChange={(e) => setCategoryName(e.target.value)}
                  className={inputClass}
                />
              </label>
              <label className={`${labelClass} flex-1`}>
                Description (optional)
                <input
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  className={inputClass}
                />
              </label>
            </div>
            <button
              type="submit"
              disabled={submitting}
              className="self-start rounded-lg bg-action-600 px-4 py-2 text-sm font-semibold text-white hover:bg-action-700 disabled:opacity-60"
            >
              {submitting ? 'Creating…' : 'Create category'}
            </button>
          </form>
        </Card>
      )}

      {loading && <div className="text-ink-400">Loading categories…</div>}
      {error && (
        <div className="rounded-lg bg-status-danger-bg px-4 py-3 text-sm font-medium text-status-danger-text">
          {error}
        </div>
      )}

      {!loading && !error && (
        <Card className="overflow-hidden">
          <div className="grid grid-cols-[1.2fr_2fr_1fr] bg-surface-bg px-5 py-2.5 text-[11.5px] font-bold uppercase tracking-wide text-ink-600">
            <span>Category</span>
            <span>Description</span>
            <span className="text-right">Actions</span>
          </div>
          {categories.length === 0 && <div className="px-5 py-6 text-sm text-ink-400">No categories yet.</div>}
          {categories.map((c) => (
            <CategoryRow
              key={c.id}
              category={c}
              onSaved={(updated) => setCategories((prev) => prev.map((x) => (x.id === updated.id ? updated : x)))}
              onDeleted={(id) => setCategories((prev) => prev.filter((x) => x.id !== id))}
            />
          ))}
        </Card>
      )}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────

export function AdministrationPage() {
  const { user } = useAuth();
  const allowed = user?.roleCode === 'OWNER';
  const [tab, setTab] = useState<Tab>('users');
  const [branches, setBranches] = useState<Branch[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(true);
  const [branchesError, setBranchesError] = useState<string | null>(null);

  useEffect(() => {
    if (!allowed) return;
    branchesApi
      .list()
      .then(setBranches)
      .catch((err) => setBranchesError(err instanceof Error ? err.message : 'Failed to load branches.'))
      .finally(() => setBranchesLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allowed]);

  if (!allowed) {
    return (
      <InternalLayout eyebrow="Administration" headline="Administration">
        <div className="rounded-lg bg-status-warn-bg px-4 py-3 text-sm font-medium text-status-warn-text">
          Administration is only available to the Owner.
        </div>
      </InternalLayout>
    );
  }

  return (
    <InternalLayout
      eyebrow={`Hello, ${user?.fullName?.split(' ')[0] ?? user?.username ?? ''}`}
      headline="Administration"
      meta="Users, branches & product categories"
    >
      <div className="flex flex-col gap-4">
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

        {tab === 'users' && <UsersTab branches={branches} />}
        {tab === 'branches' && (
          <BranchesTab
            branches={branches}
            setBranches={setBranches}
            loading={branchesLoading}
            error={branchesError}
          />
        )}
        {tab === 'categories' && <CategoriesTab />}
      </div>
    </InternalLayout>
  );
}
