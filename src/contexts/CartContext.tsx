import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

export interface CartItem {
  productId: number;
  name: string;
  unit: string;
  unitPrice: string;
  quantity: number;
}

interface CartState {
  branchId: number | null;
  branchName: string | null;
  items: CartItem[];
}

interface CartContextValue extends CartState {
  itemCount: number;
  subtotal: number;
  setBranch: (branchId: number, branchName: string) => void;
  addItem: (item: Omit<CartItem, 'quantity'>, quantity?: number) => void;
  updateQuantity: (productId: number, quantity: number) => void;
  removeItem: (productId: number) => void;
  clear: () => void;
}

const STORAGE_KEY = 'gerlyn_cart';

const CartContext = createContext<CartContextValue | null>(null);

function loadState(): CartState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    // ignore corrupt/unavailable storage, start fresh
  }
  return { branchId: null, branchName: null, items: [] };
}

export function CartProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<CartState>(loadState);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }, [state]);

  function setBranch(branchId: number, branchName: string) {
    setState((prev) => {
      if (prev.branchId === branchId) return { ...prev, branchName };
      // Switching branches invalidates the cart — orders are single-branch.
      if (prev.items.length > 0) {
        const confirmed = window.confirm(
          'Changing branch will clear your current cart. Continue?'
        );
        if (!confirmed) return prev;
      }
      return { branchId, branchName, items: [] };
    });
  }

  function addItem(item: Omit<CartItem, 'quantity'>, quantity = 1) {
    setState((prev) => {
      const existing = prev.items.find((i) => i.productId === item.productId);
      const items = existing
        ? prev.items.map((i) =>
            i.productId === item.productId ? { ...i, quantity: i.quantity + quantity } : i
          )
        : [...prev.items, { ...item, quantity }];
      return { ...prev, items };
    });
  }

  function updateQuantity(productId: number, quantity: number) {
    setState((prev) => ({
      ...prev,
      items:
        quantity <= 0
          ? prev.items.filter((i) => i.productId !== productId)
          : prev.items.map((i) => (i.productId === productId ? { ...i, quantity } : i)),
    }));
  }

  function removeItem(productId: number) {
    setState((prev) => ({ ...prev, items: prev.items.filter((i) => i.productId !== productId) }));
  }

  function clear() {
    setState((prev) => ({ ...prev, items: [] }));
  }

  const itemCount = state.items.reduce((sum, i) => sum + i.quantity, 0);
  const subtotal = state.items.reduce((sum, i) => sum + parseFloat(i.unitPrice) * i.quantity, 0);

  return (
    <CartContext.Provider
      value={{ ...state, itemCount, subtotal, setBranch, addItem, updateQuantity, removeItem, clear }}
    >
      {children}
    </CartContext.Provider>
  );
}

export function useCart() {
  const ctx = useContext(CartContext);
  if (!ctx) throw new Error('useCart must be used within a CartProvider');
  return ctx;
}
