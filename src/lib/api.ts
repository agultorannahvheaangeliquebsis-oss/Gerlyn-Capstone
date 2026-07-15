const BASE_URL = 'http://127.0.0.1:8000';

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

  const response = await fetch(`${BASE_URL}${endpoint}`, {
    ...options,
    credentials: 'include', // sends session cookie + csrftoken cookie
    headers: {
      'Content-Type': 'application/json',
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

// ─── Product endpoints ───────────────────────────────────────────────────────

export interface Product {
  id: number;
  sku: string;
  name: string;
  category: string;
  unit: string;
  retailPrice: string;
  wholesalePrice: string | null;
  description: string;
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