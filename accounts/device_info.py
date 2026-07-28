"""Turns a raw User-Agent string into a short "OS · Browser" label for the
Sessions page. Deliberately not a real parsing library (no new dependency)
— just enough string matching to be readable, not to be exhaustive.
"""

_OS_PATTERNS = [
    ('iPhone', 'iPhone'),
    ('iPad', 'iPad'),
    ('Android', 'Android'),
    ('Windows NT', 'Windows'),
    ('Mac OS X', 'macOS'),
    ('CrOS', 'ChromeOS'),
    ('Linux', 'Linux'),
]

_BROWSER_PATTERNS = [
    ('Edg/', 'Edge'),
    ('OPR/', 'Opera'),
    ('Firefox/', 'Firefox'),
    ('CriOS/', 'Chrome'),
    ('Chrome/', 'Chrome'),
    ('Safari/', 'Safari'),
]


def describe_user_agent(user_agent):
    if not user_agent:
        return 'Unknown device'

    os_name = next((label for token, label in _OS_PATTERNS if token in user_agent), 'Unknown OS')
    browser_name = next((label for token, label in _BROWSER_PATTERNS if token in user_agent), 'Unknown browser')

    return f"{os_name} · {browser_name}"
