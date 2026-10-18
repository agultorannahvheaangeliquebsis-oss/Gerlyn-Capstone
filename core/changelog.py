# Static, hand-maintained changelog for the Download page. Move this to a
# DB-backed model later if it ever needs editing without a redeploy —
# not needed yet, so keeping it simple.
#
# Each item in a version's `changes` list is either a plain string (shown to
# everyone) or a dict `{'text': ..., 'internal': True}` (shown only to
# staff/branch managers/owners — see visible_changelog() below). Use
# `internal: True` for anything that's only meaningful to internal users
# (admin-only features) or would be unhelpful/sensitive to expose publicly
# (e.g. naming a bug that was actually a security-relevant crash). Customers
# still always see the version number and date, so they know an update
# shipped even when every change in it is internal-only.
CHANGELOG = [
    {
        'version': '1.36.4',
        'date': '2026-10-18',
        'changes': [
            {'text': 'Receive Delivery: Confirm Receipt and Record Failed / Rejected now sit in a single Record Receipt dropdown', 'internal': True},
        ],
    },
    {
        'version': '1.36.3',
        'date': '2026-10-13',
        'changes': [
            'The Download page no longer has a separate browser install/update button',
            {'text': 'The Download page now offers staff, branch managers, and owners a single download: GerlynPLUS for Windows', 'internal': True},
            {'text': 'Brought the What\'s New list up to date', 'internal': True},
        ],
    },
    {
        'version': '1.36.2',
        'date': '2026-10-13',
        'changes': [
            {'text': 'Offline mode now queues data entry too — new products, categories, write-offs, suppliers, supplier categories, and supplier products are saved on the device and sync automatically when the connection returns', 'internal': True},
            {'text': 'The offline banner and notices now name exactly which forms on the page can be saved offline', 'internal': True},
            {'text': 'Fixed: an offline change whose permission was removed before it synced now shows as Sync Failed instead of staying stuck on pending', 'internal': True},
        ],
    },
    {
        'version': '1.36.0',
        'date': '2026-10-12',
        'changes': [
            {'text': 'Added offline mode — walk-in sales, floor refills, stock count corrections, purchase orders, and delivery receiving made while offline are saved on the device and sync automatically when the connection returns', 'internal': True},
            {'text': 'Added GerlynPLUS for Windows, the same system as an installable desktop app', 'internal': True},
            {'text': 'Alerts Center: select several low-stock items and request their floor refills in one step', 'internal': True},
            {'text': 'Refill and purchase order forms now open pre-filled from the Alerts Center row you picked', 'internal': True},
            {'text': 'Fixed Move Stock to Floor scanning', 'internal': True},
            'Clearer, stricter checks on the registration and profile forms',
            {'text': 'Tighter access to private records and uploaded files', 'internal': True},
        ],
    },
    {
        'version': '1.35.0',
        'date': '2026-10-09',
        'changes': [
            'Added Forgot password — request a secure reset link by email from the sign-in page',
        ],
    },
    {
        'version': '1.34.1',
        'date': '2026-10-07',
        'changes': [
            {'text': 'Added input limits across the system (prices are capped at ₱250,000) and paginated long lists', 'internal': True},
            {'text': 'Fixed an error when the Owner requested a floor refill, and refill quantities are now checked before they are submitted', 'internal': True},
            {'text': 'Fixed the agreed unit cost being rejected as required on some purchase order items', 'internal': True},
            {'text': 'Reports are now limited to the Owner and Branch Managers, and the Django admin site is hidden on the live site', 'internal': True},
            'Verification emails now go out through a more reliable email service',
            'The browser tab now shows the GERLYN logo',
        ],
    },
    {
        'version': '1.34.0',
        'date': '2026-10-02',
        'changes': [
            'You can now cancel your own order from the order page while it is still Pending or Approved',
            'Added a "Use my saved address" shortcut when choosing a branch',
            {'text': 'Edit User now has its own page with the full permission matrix, and user and branch row actions are collapsed into a "..." menu', 'internal': True},
            {'text': 'The POS now captures payment evidence for GCash and Card sales, and the transaction list can be filtered by date range', 'internal': True},
            {'text': 'Purchase orders: added search and date-range filters, a progress stepper, and a status timeline', 'internal': True},
            {'text': 'Purchase orders can include manual (non-catalog) products, and supplier participation in procurement is now optional', 'internal': True},
        ],
    },
    {
        'version': '1.33.0',
        'date': '2026-09-30',
        'changes': [
            'GCash payments now require a 13-digit reference number',
            'The refund window is now 7 days, and its deadline is shown on the order',
            'Terms & Privacy consent is now recorded when you sign up',
            {'text': 'Billing Records can be filtered by channel, fulfilment, and date range, with secondary actions tucked into a More menu and the payment proof reachable from the Payment card', 'internal': True},
            {'text': 'The POS sales import now opens in a modal, and the order total stays in view on long orders', 'internal': True},
            {'text': 'The actual received cost now carries through deliveries, documents, and exports', 'internal': True},
        ],
    },
    {
        'version': '1.32.12',
        'date': '2026-09-28',
        'changes': [
            'Online orders are now fulfilled from the nearest branch automatically',
            'Added a refund approval workflow',
            'Added online requests for the PWD discount',
            {'text': 'Added supplier returns, supplier product offers that need Owner approval, and automatic handling of overdue deliveries', 'internal': True},
            {'text': 'Order status changes now ask for confirmation and are recorded in the audit trail', 'internal': True},
            {'text': 'Added a daily sales filter to Reports', 'internal': True},
            {'text': 'Create, read, update, and delete permissions are now enforced per module', 'internal': True},
        ],
    },
    {
        'version': '1.31.39',
        'date': '2026-09-23',
        'changes': [
            'The app is now branded GERLYN, and zero or negative prices are blocked',
            {'text': 'Added the Audit Log, plus encrypted cloud backups with a restore check', 'internal': True},
            {'text': 'Added the incoming delivery workflow and purchase order negotiation, with the audit log grouped by action', 'internal': True},
            {'text': 'Added refunds/returns, a non-performing and near-expiry products report, and a Sales by Category chart on Reports', 'internal': True},
        ],
    },
    {
        'version': '1.30.6',
        'date': '2026-09-13',
        'changes': [
            'Storefront categories are back, two levels deep',
            'Added recently viewed products, and items no longer available are cleared out of your cart',
        ],
    },
    {
        'version': '1.29.20',
        'date': '2026-09-05',
        'changes': [
            'Rebuilt the storefront header, filters, and product cards, and made the shop hold up on a phone',
            'Rebuilt the account screens and the About page',
            'Add to cart is now confirmed on the button itself',
            {'text': 'Cleaned up the sidebar, tabs, and status pills so each one means a single thing, and moved add/edit forms onto the lists they belong to', 'internal': True},
            {'text': 'Actions that cannot be undone now ask for confirmation first', 'internal': True},
        ],
    },
    {
        'version': '1.3.1',
        'date': '2026-08-07',
        'changes': [
            'Product search now updates instantly as you type, instead of waiting for you to press Search',
            {'text': "Added barcode scanning to the Walk-in POS and to the Inventory/Products search — scan with the device camera, or with a USB/Bluetooth hardware scanner (which just types the code, so no setup is needed)", 'internal': True},
            {'text': 'Users, Stock, Products, Suppliers, and Order Console now filter live as you type instead of needing a page reload', 'internal': True},
            {'text': "Added a My Profile section to the staff/branch manager/owner Settings page, so account info can be edited without leaving the admin panel", 'internal': True},
            {'text': 'Fixed a crash when requesting a second verification email too soon, and added a 30-minute cooldown between requests', 'internal': True},
            {'text': 'Fixed an account data issue that could silently prevent saving a mobile number on some older accounts', 'internal': True},
        ],
    },
    {
        'version': '1.3.0',
        'date': '2026-07-31',
        'changes': [
            'Fixed: the staff/branch manager/owner Settings page no longer bounces out to the customer profile page after changing your password, toggling two-factor authentication, or managing sessions',
            'Password and Sessions management on the staff/branch manager/owner Settings page are now inline instead of linking to a separate page',
            'Fixed: opening a new tab or window (or reopening the installed app) now correctly lands staff, branch managers, and owners on their own dashboard instead of the customer storefront home page',
        ],
    },
    {
        'version': '1.2.0',
        'date': '2026-07-31',
        'changes': [
            "Redesigned the staff/branch manager/owner sidebar into a collapsible section-based navigation with icons (from the team's GitHub update)",
            'Added Download and Settings to the staff/branch manager/owner sidebar, below View Store',
            'The Download page (app info, changelog, install/update) is now shared between the customer and staff views',
            'Added a dedicated Settings page for staff/branch managers/owners, covering account security (password, two-factor authentication, sessions)',
        ],
    },
    {
        'version': '1.1.0',
        'date': '2026-07-31',
        'changes': [
            'The store can now be installed as an app from this page — Download when it is your first time, Update afterwards',
            'Added this changelog to the Download page so you can see what changed in each update',
        ],
    },
    {
        'version': '1.0.0',
        'date': '2026-07-28',
        'changes': [
            'Added Sessions: see every device signed in to your account and log one out, or all of them, at once',
            'Added Language switching (English / Filipino)',
            'Added Privacy & Data settings, including account deactivation',
        ],
    },
    {
        'version': '0.9.0',
        'date': '2026-07-28',
        'changes': [
            'Added a map-based address picker — drop a pin for your delivery location instead of typing coordinates',
            'Redesigned the Settings hub (Profile, Addresses, Notifications, Help Center, About)',
            'Added a 30-minute idle auto-logout for security',
            "Fixed: pressing Back after logging out could show a cached page instead of redirecting to login",
        ],
    },
    {
        'version': '0.8.0',
        'date': '2026-07-26',
        'changes': [
            'Fixed the notifications view and its URL routing',
        ],
    },
    {
        'version': '0.7.0',
        'date': '2026-07-26',
        'changes': [
            'Various profile page fixes, front-end and back-end',
        ],
    },
]

# Shown for a version where every change is internal-only, so the audience
# still sees that something shipped on a given date without any specifics.
GENERIC_UPDATE_NOTE = 'General improvements and behind-the-scenes fixes.'


def visible_changelog(is_internal):
    """CHANGELOG filtered for the Download page's audience. Internal users
    (staff/branch managers/owners) see every change; everyone else only
    sees changes not flagged internal-only. A version keeps appearing (with
    its real version number and date) even if nothing in it is public —
    it just falls back to GENERIC_UPDATE_NOTE instead of specifics."""
    visible = []
    for entry in CHANGELOG:
        changes = []
        for change in entry['changes']:
            if isinstance(change, dict):
                if is_internal or not change.get('internal'):
                    changes.append(change['text'])
            else:
                changes.append(change)
        visible.append({
            'version': entry['version'],
            'date': entry['date'],
            'changes': changes or [GENERIC_UPDATE_NOTE],
        })
    return visible
