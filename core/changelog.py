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
