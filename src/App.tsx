import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AuthProvider } from './contexts/AuthContext';
import { CartProvider } from './contexts/CartContext';
import { ProtectedRoute } from './components/ProtectedRoute';
import { LoginPage } from './pages/LoginPage';
import { DashboardPage } from './pages/DashboardPage';
import { SalesOrdersPage } from './pages/SalesOrdersPage';
import { InventoryPage } from './pages/InventoryPage';
import { ProcurementPage } from './pages/ProcurementPage';
import { ReportsPage } from './pages/ReportsPage';
import { AlertsPage } from './pages/AlertsPage';
import { AdministrationPage } from './pages/AdministrationPage';
import { StorefrontHomePage } from './pages/storefront/StorefrontHomePage';
import { ProductCatalogPage } from './pages/storefront/ProductCatalogPage';
import { ProductDetailPage } from './pages/storefront/ProductDetailPage';
import { CartPage } from './pages/storefront/CartPage';
import { CheckoutPage } from './pages/storefront/CheckoutPage';
import { MyOrdersPage } from './pages/storefront/MyOrdersPage';
import { OrderDetailPage } from './pages/storefront/OrderDetailPage';
import { CustomerSignupPage } from './pages/storefront/CustomerSignupPage';

// Every internal-system role except Customer — Customers get bounced to the storefront.
const STAFF_ROLES = ['OWNER', 'BRANCH_MANAGER', 'STAFF', 'SUPPLIER'];

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <CartProvider>
          <Routes>
            {/* Public storefront */}
            <Route path="/" element={<StorefrontHomePage />} />
            <Route path="/products" element={<ProductCatalogPage />} />
            <Route path="/products/:id" element={<ProductDetailPage />} />
            <Route path="/cart" element={<CartPage />} />
            <Route path="/signup" element={<CustomerSignupPage />} />
            <Route path="/login" element={<LoginPage />} />

            {/* Customer-only storefront */}
            <Route
              path="/checkout"
              element={
                <ProtectedRoute allowedRoles={['CUSTOMER']}>
                  <CheckoutPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/orders"
              element={
                <ProtectedRoute allowedRoles={['CUSTOMER']}>
                  <MyOrdersPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/orders/:id"
              element={
                <ProtectedRoute allowedRoles={['CUSTOMER']}>
                  <OrderDetailPage />
                </ProtectedRoute>
              }
            />

            {/* Internal system */}
            <Route
              path="/dashboard"
              element={
                <ProtectedRoute allowedRoles={STAFF_ROLES}>
                  <DashboardPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/sales"
              element={
                <ProtectedRoute allowedRoles={STAFF_ROLES}>
                  <SalesOrdersPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/inventory"
              element={
                <ProtectedRoute allowedRoles={STAFF_ROLES}>
                  <InventoryPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/procurement"
              element={
                <ProtectedRoute allowedRoles={STAFF_ROLES}>
                  <ProcurementPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/reports"
              element={
                <ProtectedRoute allowedRoles={STAFF_ROLES}>
                  <ReportsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/alerts"
              element={
                <ProtectedRoute allowedRoles={STAFF_ROLES}>
                  <AlertsPage />
                </ProtectedRoute>
              }
            />
            <Route
              path="/administration"
              element={
                <ProtectedRoute allowedRoles={STAFF_ROLES}>
                  <AdministrationPage />
                </ProtectedRoute>
              }
            />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </CartProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
