export const BASE_URL = 'http://localhost:8000';

/**
 * Gets the CSRF token from the cookie Django sets.
 * Django sets this automatically when any page or API is visited.
 */
function getCsrfToken(): string {
  const match = document.cookie.match(/csrftoken=([^;]+)/);
  return match ? match[1] : '';
}

/**
 * Base fetch wrapper that:
 * 1. Always includes credentials (sends session + CSRF cookies)
 * 2. Automatically attaches the CSRF token header on mutating requests
 * 3. Returns parsed JSON or throws an error
 */
async function apiFetch<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const method = options.method?.toUpperCase() ?? 'GET';
  const isMutating = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);
  // FormData bodies (file uploads) must let the browser set their own
  // multipart/form-data boundary — never force a Content-Type on them.
  const isFormData = options.body instanceof FormData;

  const response = await fetch(`${BASE_URL}${endpoint}`, {
    ...options,
    credentials: 'include', // sends session cookie + csrftoken cookie
    headers: {
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
      ...(isMutating ? { 'X-CSRFToken': getCsrfToken() } : {}),
      ...options.headers,
    },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({
      detail: 'Unknown error occurred.',
    }));
    throw new Error(error.detail ?? `HTTP ${response.status}`);
  }

  // Handle 204 No Content (logout returns no body)
  if (response.status === 204) return {} as T;
  return response.json();
}

// ─── Auth endpoints ──────────────────────────────────────────────────────────

export interface AuthUser {
  id: number;
  username: string;
  email: string;
  role: string;        // display name e.g. "Owner"
  roleCode: string;    // e.g. "OWNER", "STAFF" — use this for routing logic
  fullName: string;
  branch: string | null;
}

export const authApi = {
  login: (username: string, password: string) =>
    apiFetch<{ user: AuthUser; detail: string }>('/api/auth/login/', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  logout: () =>
    apiFetch<{ detail: string }>('/api/auth/logout/', {
      method: 'POST',
    }),

  me: () =>
    apiFetch<AuthUser>('/api/auth/me/'),

  register: (data: {
    username: string;
    email: string;
    password: string;
    fullName: string;
    contactNumber: string;
    address: string;
  }) =>
    apiFetch<{ user: AuthUser; detail: string }>('/api/auth/register/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
};

// ─── User administration endpoints (Owner only) ─────────────────────────────

export interface AdminUser extends AuthUser {
  branchId: number | null;
  contactNumber: string | null;
  isActive: boolean;
  dateJoined: string;
}

export interface UserCreateInput {
  username: string;
  password: string;
  role: string;
  email?: string;
  fullName?: string;
  contactNumber?: string;
  branchId?: number;
  address?: string;
}

export interface UserUpdateInput {
  fullName?: string;
  contactNumber?: string;
  branchId?: number;
  address?: string;
  isActive?: boolean;
}

export const usersApi = {
  list: () => apiFetch<AdminUser[]>('/api/auth/users/'),

  create: (data: UserCreateInput) =>
    apiFetch<AdminUser>('/api/auth/users/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  update: (id: number, data: UserUpdateInput) =>
    apiFetch<AdminUser>(`/api/auth/users/${id}/`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
};

// ─── Product endpoints ───────────────────────────────────────────────────────

export interface Product {
  id: number;
  sku: string;
  name: string;
  category: string;
  unit: string;
  retailPrice: string;
  wholesalePrice: string | null;
  availability: string[];
}

export const productsApi = {
  list: (params?: { search?: string; category?: string; branch?: string }) => {
    const query = new URLSearchParams(
      Object.entries(params ?? {}).filter(([, v]) => v != null) as [string, string][]
    ).toString();
    return apiFetch<Product[]>(`/api/products/${query ? `?${query}` : ''}`);
  },

  get: (id: number) =>
    apiFetch<Product>(`/api/products/${id}/`),

  create: (data: Partial<Product>) =>
    apiFetch<Product>('/api/products/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  update: (id: number, data: Partial<Product>) =>
    apiFetch<Product>(`/api/products/${id}/`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  delete: (id: number) =>
    apiFetch<void>(`/api/products/${id}/`, {
      method: 'DELETE',
    }),
};

// ─── Product category endpoints ─────────────────────────────────────────────

export interface ProductCategory {
  id: number;
  categoryName: string;
  description: string;
}

export const categoriesApi = {
  list: () => apiFetch<ProductCategory[]>('/api/categories/'),

  create: (data: { categoryName: string; description?: string }) =>
    apiFetch<ProductCategory>('/api/categories/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  update: (id: number, data: { categoryName?: string; description?: string }) =>
    apiFetch<ProductCategory>(`/api/categories/${id}/`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  remove: (id: number) =>
    apiFetch<void>(`/api/categories/${id}/`, {
      method: 'DELETE',
    }),
};

// ─── Branch endpoints ────────────────────────────────────────────────────────

export interface Branch {
  id: number;
  ownerId: number | null;
  ownerName: string | null;
  name: string;
  location: string;
}

export const branchesApi = {
  list: () => apiFetch<Branch[]>('/api/branches/'),

  create: (data: { name: string; location: string }) =>
    apiFetch<Branch>('/api/branches/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  update: (id: number, data: { name?: string; location?: string }) =>
    apiFetch<Branch>(`/api/branches/${id}/`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  remove: (id: number) =>
    apiFetch<void>(`/api/branches/${id}/`, {
      method: 'DELETE',
    }),
};

// ─── Reports endpoints ───────────────────────────────────────────────────────

export interface SalesByBranch {
  branch: string | null;
  total: string;
  count: number;
}

export interface TopProduct {
  productName: string;
  totalQty: number;
  totalRevenue: string;
}

export interface DashboardSummary {
  totalProducts: number;
  totalSales: string;
  totalTransactions: number;
  recentSales: string;
  salesByBranch: SalesByBranch[];
  topProducts: TopProduct[];
  lowStockCount: number;
  totalProcurementCost: string;
  pendingOrders: number;
  isOwner: boolean;
}

export interface InventoryRow {
  id: number;
  productId: number;
  productName: string;
  productSku: string;
  branchId: number;
  branchName: string;
  quantityOnHand: number;
  reorderLevel: number;
  restock: boolean;
  expiryDate: string | null;
  lastStockUpdate: string;
}

export const reportsApi = {
  dashboardSummary: () =>
    apiFetch<DashboardSummary>('/api/reports/dashboard-summary/'),

  inventoryReport: () =>
    apiFetch<InventoryRow[]>('/api/reports/inventory-report/'),

  // salesReport/procurementReport return types are declared further down
  // this file (SalesTransaction, Procurement) — see the bottom re-exports.
  salesReport: () => apiFetch<SalesTransaction[]>('/api/reports/sales-report/'),

  procurementReport: () => apiFetch<Procurement[]>('/api/reports/procurement-report/'),
};

// ─── Sales endpoints ─────────────────────────────────────────────────────────

export interface SalesItem {
  id: number;
  productId: number;
  productName: string;
  productSku: string;
  quantitySold: number;
  unitPrice: string;
  subtotal: string;
}

export interface SalesTransaction {
  id: number;
  orderNumber: string;
  branch: string | null;
  staff: string | null;
  customer: string | null;
  transactionDate: string;
  totalAmount: string;
  transactionType: string;
  modeOfPayment: string;
  orderType: string;
  orderStatus: string;
  deliveryAddress: string;
  items: SalesItem[];
}

export interface SalesTransactionInput {
  branchId?: number;
  transactionType: string;
  modeOfPayment: string;
  orderType: string;
  deliveryAddress?: string;
  items: { productId: number; quantity: number }[];
}

export const salesApi = {
  list: () => apiFetch<SalesTransaction[]>('/api/sales/'),

  get: (id: number) => apiFetch<SalesTransaction>(`/api/sales/${id}/`),

  create: (data: SalesTransactionInput) =>
    apiFetch<SalesTransaction>('/api/sales/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
};

// ─── Billing endpoints ───────────────────────────────────────────────────────

export interface BillingRecord {
  id: number;
  transactionId: number;
  orderNumber: string;
  branch: string | null;
  amountDue: string;
  paymentMethod: string;
  referenceNumber: string;
  proofOfPayment: string | null;
  status: string;
  confirmedBy: string | null;
  dateRecorded: string;
  dateConfirmed: string | null;
}

export const billingApi = {
  list: () => apiFetch<BillingRecord[]>('/api/billing/'),

  get: (id: number) => apiFetch<BillingRecord>(`/api/billing/${id}/`),

  pay: (id: number, data: { paymentMethod: string; referenceNumber?: string; proofOfPayment?: File }) => {
    const form = new FormData();
    form.append('paymentMethod', data.paymentMethod);
    if (data.referenceNumber) form.append('referenceNumber', data.referenceNumber);
    if (data.proofOfPayment) form.append('proofOfPayment', data.proofOfPayment);
    return apiFetch<BillingRecord>(`/api/billing/${id}/pay/`, {
      method: 'PATCH',
      body: form,
    });
  },

  confirm: (id: number, status: 'Paid' | 'Rejected') =>
    apiFetch<BillingRecord>(`/api/billing/${id}/confirm/`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
};

// ─── Inventory endpoints ─────────────────────────────────────────────────────
// Deliberately hit /api/inventory/, /api/warehouse-stock/, /api/batches/
// directly rather than the /api/reports/ equivalents — the report endpoints
// are gated to Owner/Branch Manager only, but floor and warehouse stock need
// to be visible to Staff too.

export const inventoryApi = {
  list: () => apiFetch<InventoryRow[]>('/api/inventory/'),
};

export interface WarehouseStockRow {
  id: number;
  warehouseId: number;
  warehouseName: string;
  branchName: string;
  productId: number;
  productName: string;
  productSku: string;
  quantityOnHand: number;
  reorderLevel: number;
  restockFlag: boolean;
  lastUpdated: string;
}

export const warehouseStockApi = {
  list: () => apiFetch<WarehouseStockRow[]>('/api/warehouse-stock/'),
};

export interface Batch {
  id: number;
  productId: number;
  productName: string;
  branchId: number;
  branchName: string;
  batchNumber: string;
  quantity: number;
  receivedDate: string;
  expiryDate: string;
  expiryStatus: 'Fresh' | 'Expiring Soon' | 'Expired';
  daysUntilExpiry: number;
  notes: string;
}

export const batchesApi = {
  list: () => apiFetch<Batch[]>('/api/batches/'),
};

// ─── Procurement endpoints ───────────────────────────────────────────────────

export interface ProcurementItem {
  id: number;
  productId: string | null;
  productName: string | null;
  productSku: string | null;
  quantityOrdered: number;
  quantityReceived: number;
  costPrice: string;
  subtotal: number;
}

export interface Procurement {
  id: number;
  poNumber: string;
  branch: string | null;
  supplierId: string | null;
  supplierName: string | null;
  authorizedBy: string | null;
  createdBy: string | null;
  orderDate: string;
  expectedDelivery: string | null;
  total: string;
  status: string;
  receiptFlag: string;
  items: ProcurementItem[];
}

export interface ProcurementInput {
  branchId: number;
  supplierId: number;
  orderDate: string;
  expectedDelivery?: string;
  items: { productId: number; quantityOrdered: number; costPrice: number }[];
}

export interface Supplier {
  id: number;
  name: string;
}

export const procurementApi = {
  list: () => apiFetch<Procurement[]>('/api/procurement/'),

  get: (id: number) => apiFetch<Procurement>(`/api/procurement/${id}/`),

  create: (data: ProcurementInput) =>
    apiFetch<Procurement>('/api/procurement/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  remove: (id: number) =>
    apiFetch<void>(`/api/procurement/${id}/`, {
      method: 'DELETE',
    }),

  updateStatus: (id: number, data: { status: string; receiptFlag?: string; deliveryDate?: string }) =>
    apiFetch<Procurement>(`/api/procurement/${id}/update_status/`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  suppliers: () => apiFetch<Supplier[]>('/api/procurement/suppliers/'),
};