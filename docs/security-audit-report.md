# Gerlyn IS Capstone Security Audit Report

**Review date:** September 16, 2026  
**Review type:** Read-only source-code security assessment  
**Scope:** Authentication, authorization, API endpoints, middleware, input handling, database access, uploaded media, and client-side disclosure risks

## Executive Summary

The review identified **seven actionable security findings**:

| Severity | Count |
|---|---:|
| Critical | 0 |
| High | 4 |
| Medium | 3 |
| Low | 0 |

The highest-priority risks are broken authorization controls in procurement and warehouse APIs, API routes bypassing granular module permissions, and public access to uploaded payment proofs. These issues could expose cross-branch business data or sensitive customer payment evidence.

No exploitable SQL injection, confirmed XSS, CSRF bypass, committed production credential, or sensitive client-side logging was found in the reviewed scope.

> This report is read-only. No application files were changed as part of the audit.

## Recommended Remediation Order

1. Protect payment-proof uploads from public access.
2. Fix the procurement customer data leak.
3. Enforce branch ownership when creating procurement orders.
4. apply module permissions consistently to API endpoints.
5. Scope warehouse endpoints by role and branch.
6. Enforce the configured password policy during API registration.
7. Restrict the supplier directory to purchasing roles.

---

## SEC-01 — Customers Can Enumerate All Procurement Orders

**Vulnerability level:** High  
**Category:** Broken access control / information disclosure  
**Location:** `procurement/views.py:1140-1154`

### Risk

`ProcurementListCreateAPIView.get_queryset()` restricts managers, staff, and suppliers, but all other roles fall through to the unrestricted queryset. A self-registered customer can request `GET /api/procurement/` and receive all purchase orders, including supplier names, branch details, costs, and line items.

### Remediation Diff

```diff
diff --git a/procurement/views.py b/procurement/views.py
@@
     def get_queryset(self, user):
         qs = Procurement.objects.select_related(
             'branch', 'supplier', 'supplier__supplier_profile',
             'owner', 'owner__owner_profile',
             'created_by', 'created_by__staff_profile',
             'created_by__manager_profile',
         ).prefetch_related('items__product').order_by('-id')
 
-        if user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
+        if user.role == User.Role.OWNER:
+            return qs
+        if user.role in [User.Role.BRANCH_MANAGER, User.Role.STAFF]:
             return scope_queryset(qs, user)
-        elif user.role == User.Role.SUPPLIER:
+        if user.role == User.Role.SUPPLIER:
             return qs.filter(supplier=user)
-        # OWNER sees all
-        return qs
+        return qs.none()
```

---

## SEC-02 — Managers Can Create Procurement Orders for Other Branches

**Vulnerability level:** High  
**Category:** Broken object-level authorization  
**Location:** `procurement/serializers.py:173-176`

### Risk

The API permits branch managers to create purchase orders, but `branchId` is checked only for existence. A manager can submit another branch's ID and create unauthorized orders and financial obligations for that branch.

### Remediation Diff

```diff
diff --git a/procurement/serializers.py b/procurement/serializers.py
@@
 from branches.models import Branch
+from core.scoping import user_can_access_branch
@@
     def validate_branchId(self, value):
         if not Branch.objects.filter(id=value).exists():
             raise serializers.ValidationError("Branch not found.")
+
+        request = self.context.get('request')
+        if not request or not user_can_access_branch(request.user, value):
+            raise serializers.ValidationError(
+                "You do not have access to this branch."
+            )
         return value
```

---

## SEC-03 — API Routes Bypass Granular Module Permissions

**Vulnerability level:** High  
**Category:** Broken RBAC  
**Locations:** `accounts/middleware.py:49-69`, `core/module_permissions.py:39-83`

### Risk

`ModulePermissionMiddleware` resolves URL names against `MODULE_REGISTRY`, but the registry primarily contains template-view URL names. API names such as `api-sales-list`, `api-billing-list`, and `api-stock-transfer-list` are absent. Staff or branch managers whose access to a module was revoked can therefore call the corresponding API directly if their broad role is accepted by that endpoint.

### Remediation Diff

Add every relevant API URL name to the same module mapping and enforce the same rule through a DRF permission class.

```diff
diff --git a/core/permissions.py b/core/permissions.py
@@
+from rest_framework.permissions import BasePermission
+from core.module_permissions import URL_NAME_TO_MODULE, user_can_access_module
+
+class HasModuleAccess(BasePermission):
+    def has_permission(self, request, view):
+        match = getattr(request, 'resolver_match', None)
+        module = URL_NAME_TO_MODULE.get(match.url_name if match else None)
+        return module is None or user_can_access_module(request.user, module)
```

```diff
diff --git a/gerlyn_is/settings.py b/gerlyn_is/settings.py
@@
     "DEFAULT_PERMISSION_CLASSES": [
         "rest_framework.permissions.IsAuthenticated",
+        "core.permissions.HasModuleAccess",
     ],
```

Representative registry additions:

```diff
diff --git a/core/module_permissions.py b/core/module_permissions.py
@@
     SALES_POS: (
         'Sales / Order Console',
         ['transaction_list', 'transaction_add', 'transaction_detail',
-         'transaction_set_status'],
+         'transaction_set_status', 'api-sales-list', 'api-sales-detail'],
     ),
@@
     BILLING: (
         'Billing',
-        ['billing_list', 'billing_detail', 'billing_create', 'billing_confirm'],
+        ['billing_list', 'billing_detail', 'billing_create', 'billing_confirm',
+         'api-billing-list', 'api-billing-detail', 'api-billing-pay',
+         'api-billing-confirm'],
     ),
```

All API URL names should be mapped, including router-generated list and detail names.

---

## SEC-04 — Payment-Proof Images Are Publicly Retrievable

**Vulnerability level:** High  
**Category:** Sensitive information disclosure  
**Locations:** `gerlyn_is/urls.py:60-63`, `billing/models.py:38-41`

### Risk

The wildcard `/media/<path>` route serves every uploaded file without authentication. Payment screenshots can contain names, phone numbers, account identifiers, balances, and transaction references. Billing API authorization does not protect the image after its URL becomes known or guessed.

### Remediation Diff

```diff
diff --git a/gerlyn_is/urls.py b/gerlyn_is/urls.py
@@
-from django.views.static import serve
+from billing.views import payment_proof_download
@@
-    re_path(
-        r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}
-    ),
+    path(
+        'billing/<int:pk>/payment-proof/',
+        payment_proof_download,
+        name='payment_proof_download',
+    ),
 ]
```

```diff
diff --git a/billing/views.py b/billing/views.py
@@
+from django.http import FileResponse, Http404
+from django.views.decorators.http import require_GET
+from billing.api_views import user_can_access_billing_record
+
+@login_required(login_url='accounts:login')
+@require_GET
+def payment_proof_download(request, pk):
+    record = get_object_or_404(
+        BillingRecord.objects.select_related(
+            'transaction', 'transaction__branch'
+        ),
+        pk=pk,
+    )
+    if not user_can_access_billing_record(request.user, record):
+        raise Http404
+    if not record.proof_of_payment:
+        raise Http404
+    return FileResponse(
+        record.proof_of_payment.open('rb'),
+        content_type='application/octet-stream',
+    )
```

Production object storage must also use private assets or short-lived signed URLs. Removing the Django media route alone does not make publicly hosted Cloudinary objects private.

---

## SEC-05 — API Registration Bypasses Password Policy

**Vulnerability level:** Medium  
**Category:** Weak authentication controls  
**Location:** `accounts/api_views.py:100-148`

### Risk

The HTML registration path invokes Django's configured password validators, but the API passes the password directly to `create_user()`. The user manager hashes the password but does not automatically run password validation. Customers can therefore create accounts with weak or commonly used passwords.

### Remediation Diff

```diff
diff --git a/accounts/api_views.py b/accounts/api_views.py
@@
 from django.contrib.auth import authenticate, login, logout
+from django.contrib.auth import password_validation
+from django.core.exceptions import ValidationError as DjangoValidationError
+from django.db import transaction
@@
+        candidate = User(username=username, email=email)
+        try:
+            password_validation.validate_password(password, user=candidate)
+        except DjangoValidationError as exc:
+            return Response(
+                {'password': exc.messages},
+                status=status.HTTP_400_BAD_REQUEST,
+            )
+
-        user = User.objects.create_user(
-            username=username,
-            email=email,
-            password=password,
-            role=User.Role.CUSTOMER,
-        )
+        with transaction.atomic():
+            user = User.objects.create_user(
+                username=username,
+                email=email,
+                password=password,
+                role=User.Role.CUSTOMER,
+            )
```

The profile update should remain inside the transaction, and broad `except Exception: pass` handling should be removed so partial registrations cannot fail silently.

---

## SEC-06 — Warehouse Endpoint Has Cross-Branch Access Weaknesses

**Vulnerability level:** Medium  
**Category:** Broken object-level authorization / information disclosure  
**Location:** `inventory/api_views.py:186-212`

### Risk

`WarehouseViewSet` uses an unrestricted class-level queryset. Customers and suppliers can enumerate warehouse metadata, while a branch manager can retrieve, update, or delete a warehouse belonging to another branch because write authorization checks only the role.

### Remediation Diff

```diff
diff --git a/inventory/api_views.py b/inventory/api_views.py
@@
+from rest_framework.exceptions import PermissionDenied
@@
 class WarehouseViewSet(viewsets.ModelViewSet):
-    queryset = Warehouse.objects.select_related('branch').all()
     serializer_class = WarehouseSerializer
     permission_classes = [IsAuthenticated]
+
+    def get_queryset(self):
+        if self.request.user.role not in INTERNAL_ROLES:
+            return Warehouse.objects.none()
+        qs = Warehouse.objects.select_related('branch').all()
+        return scope_queryset(qs, self.request.user, 'branch')
+
+    def perform_create(self, serializer):
+        branch = serializer.validated_data['branch']
+        if not user_can_access_branch(self.request.user, branch.id):
+            raise PermissionDenied('You do not have access to that branch.')
+        serializer.save()
```

The current serializer exposes `branch` as read-only. Warehouse creation should either be disabled or use a writable `branchId` that performs the same authorization check.

---

## SEC-07 — Supplier Directory Is Available to Unrelated Roles

**Vulnerability level:** Medium  
**Category:** Information disclosure / excessive privilege  
**Location:** `procurement/views.py:1306-1330`

### Risk

Any authenticated customer, supplier, or staff account can enumerate supplier IDs and names. The endpoint exists as a purchase-order picker, but it does not enforce the Owner or Branch Manager role required to create purchase orders.

### Remediation Diff

```diff
diff --git a/procurement/views.py b/procurement/views.py
@@
 class SupplierListAPIView(APIView):
     permission_classes = [IsAuthenticated]
 
     def get(self, request):
+        if request.user.role not in [
+            User.Role.OWNER,
+            User.Role.BRANCH_MANAGER,
+        ]:
+            return Response(
+                {'detail': 'Permission denied.'},
+                status=status.HTTP_403_FORBIDDEN,
+            )
         suppliers = User.objects.filter(
             role=User.Role.SUPPLIER
         ).select_related('supplier_profile')
```

---

## Controls Reviewed Without a Confirmed Finding

### SQL Injection

No exploitable raw SQL or user-controlled SQL interpolation was found in the reviewed request paths. Django ORM filters are parameterized. The interpolated `ALTER TABLE` statement in `accounts/management/commands/seed_demo.py` uses an internal table-name list and is not remotely reachable.

### Cross-Site Request Forgery

Django's CSRF middleware is enabled, and DRF uses `SessionAuthentication`, which enforces CSRF validation for authenticated unsafe requests.

### Cross-Site Scripting

No confirmed user-controlled path into an unescaped HTML sink was found in the reviewed scope. Django template escaping and the examined JavaScript escaping helpers mitigate the identified HTML-writing operations.

### Secrets and Client Logs

The `.env` file is ignored and is not tracked by Git. No tracked production credential or client-side logging of passwords, session identifiers, or authentication tokens was found.

## Verification Checklist After Remediation

- A customer receives no procurement records from `GET /api/procurement/`.
- A manager cannot create, view, update, approve, or delete objects belonging to another branch.
- Revoking a module from a staff or manager account blocks both template pages and related API endpoints.
- Direct requests to payment-proof file URLs fail without an authorized session.
- API registration rejects passwords that fail `AUTH_PASSWORD_VALIDATORS`.
- Customer and supplier accounts cannot enumerate warehouses or the supplier picker.
- Existing owner workflows and public storefront product/branch reads continue to function.
- Automated tests cover each authorization boundary with at least two branches and every user role.

## Conclusion

The application already has useful foundations: session authentication, CSRF middleware, centralized branch-scoping helpers, ORM-based database access, and production secret guards. The main weakness is inconsistent application of authorization rules between template views and API routes. Centralizing API authorization and adding cross-role, cross-branch regression tests should be treated as the primary security improvement.
