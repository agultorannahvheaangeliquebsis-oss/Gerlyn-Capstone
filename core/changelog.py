# Static, hand-maintained changelog for the Download page. Move this to a
# DB-backed model later if it ever needs editing without a redeploy —
# not needed yet, so keeping it simple.
CHANGELOG = [
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
