# Account input validation audit

The audit covered customer HTML/API registration, Owner invitations and their
completion, internal account editing, Owner API creation/updates, supplier
creation/editing, self-service profiles, Django administration, saved addresses,
and password changes/resets. All five roles use the existing authorization rules.

## Findings and implemented corrections

- Customer names were not checked against their 50-character profile columns;
  invitations and profile edits could exceed the internal 100-character display
  name column. Shared validation now checks these limits before saving.
- Several REST fields had no bounds, username/mobile format checks, or consistent
  duplicate checks. API requests now receive field errors instead of bypassing
  the HTML rules. Registration also creates the default saved delivery address.
- Profile edits accepted malformed email addresses and usernames, and could save
  a contact number before another field failed. Input validation now happens
  before writes; profile/user saves are atomic and their names stay synchronized.
- Email comparisons differed between roles and missed case variants or another
  user's pending email. Checks now cover current and pending addresses across roles.
- Phone comparisons missed equivalent local/international numbers and separators.
  Checks cover all five profile tables, including legacy whitespace, while keeping
  the user's valid display formatting. Mobile digits must be ASCII.
- Long usernames were copied into shorter placeholder profile columns during
  creation. Only those temporary/fallback display values are bounded to their
  column sizes; a submitted real name is validated rather than truncated.
- Admin account forms and profile forms now use the same validation rules and
  browser limits. Customer account creation in Django admin includes name/email
  fields. Role changes in that form update the browser's name limits.
- Passwords had no application maximum and administrative/API creation could
  trim their whitespace. New passwords now have a 128-character maximum and
  preserve their exact content. Existing strength requirements remain in place.
- Browser attributes and friendly error messages now cover names, usernames,
  mobile numbers, password bounds/strength, combined names and supplier details.

Existing appropriate field widths, role policies, consent requirements, password
strength rules, supplier category selection, date/time fields and coordinate
validation are retained. No schema alteration or data migration is required;
the added application limits fit the existing MySQL columns. Existing accounts
and stored personal data are not rewritten.

## Final limits

Limits apply after trimming surrounding whitespace, except passwords, which are
never trimmed. Username normalization follows Django's Unicode NFKC behavior.

| Field | Limit | Rules |
| --- | --- | --- |
| Username | 1–150 | Django Unicode letters/numbers and `@ . + - _`; case-insensitive duplicate check; existing database uniqueness retained |
| Customer first/last name | 1–50 each | Required at creation and when supplied in an edit; Unicode letters/combining marks, spaces, hyphens, straight/curly apostrophes and periods; at least one letter |
| Internal first/last name | Up to 100 each; combined display name ≤100 including separating space | Same personal-name characters; first/last name required to complete an invitation; existing optional admin/edit fields retained |
| Owner/Manager/Staff display name | 1–100 | Same personal-name characters for supplied names |
| Supplier/business name | 1–100 | Business punctuation and numerals remain supported |
| Supplier contact person | 0–100 | Optional; same personal-name characters |
| Email | Up to 254 | Valid email format; required for customer accounts and invitations; existing optional internal/supplier fields retained; current and pending duplicates rejected across roles |
| New password | 8–128 | Existing similarity/common-password checks plus uppercase letter, ASCII digit and supported special character; exact whitespace preserved |
| Contact number | 0–20 input characters | Optional; normalized form must be `09` followed by 9 ASCII digits or `+639` followed by 9 ASCII digits; spaces/hyphens accepted; cross-role duplicates rejected |
| Delivery/supplier/profile address | Up to 500 | Required and nonempty at customer registration and for saved addresses; optional supplier/profile field remains optional |
| Street detail | 0–255 | Optional; existing address picker retained |
| Region/province/city code | 0–10 each | Existing structured address fields; limits enforced even for direct POSTs |
| Barangay code | 0–15 | Existing structured address field |
| Business/tax registration ID | 0–100 | Optional; begins with ASCII letter/digit; letters, digits, spaces, periods, slashes, underscores and hyphens; uppercase normalization and duplicate checks |
| Role, branch, category | Existing choices/record IDs | Server validates choices and referenced records; authorization remains server-enforced |

There is no separate middle-name field in this implementation. Compound given
names can be entered in the first-name field; this audit does not add new fields.
Legacy incomplete profiles can still receive partial edits that omit names.

## Validation and reproduction

`accounts/tests_input_validation.py` adds boundary, empty/whitespace, Unicode,
special-character, malformed JSON, duplicate, direct API, invitation, Django
admin and partial-write regression cases.

Commands run with process-local test environment variables (no `.env` edits):

```powershell
$env:DJANGO_DEBUG='False'
$env:DJANGO_SECRET_KEY='isolated-test-key-only-not-for-deployment'
$env:DB_PASSWORD='test-only'
.venv/Scripts/python.exe manage.py check
```

Result: no issues. MySQL test invocation attempted:

```powershell
.venv/Scripts/python.exe manage.py test accounts.tests_registration_consent --noinput
```

This could not create the test database because local MySQL refused connections.
To run the relevant suite without changing production settings, the following
isolated in-memory SQLite invocation was used:

```powershell
@'
import os
os.environ['DJANGO_SETTINGS_MODULE']='gerlyn_is.settings'
from django.conf import settings
settings.DATABASES={'default': {'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}
settings.STORAGES['staticfiles']={'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}
settings.EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'
settings.PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher']
from django.core.management import execute_from_command_line
execute_from_command_line(['manage.py','test','accounts.tests_input_validation','accounts.tests','accounts.tests_registration_consent','accounts.tests_password_reset','accounts.tests_module_permissions','accounts.tests_supplier_home','core.tests_security_remediation','procurement.tests_input_limits','procurement.tests_optional_supplier','procurement.tests_supplier_categories','--noinput'])
'@ | .venv/Scripts/python.exe -
```

Result: **228 tests passed**. Production password hashing and database settings
remain unchanged. Tests do not send external email.

Migration drift was checked against the isolated database:

```powershell
@'
import os
os.environ['DJANGO_SETTINGS_MODULE']='gerlyn_is.settings'
from django.conf import settings
settings.DATABASES={'default': {'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}
from django.core.management import execute_from_command_line
execute_from_command_line(['manage.py','makemigrations','--check','--dry-run'])
'@ | .venv/Scripts/python.exe -
```

Result: no changes detected. `git diff --check` also passed.

A broader isolated invocation using
`execute_from_command_line(['manage.py','test','accounts','core','procurement.tests_input_limits','--noinput'])`
ran 430 tests during development. It exposed failures in transfer discrepancy
status, modal navigation, and demo sale totals, and a MySQL-only seed test using
`AUTO_INCREMENT`. An account API password-error regression found in that run was
corrected and passes in the final targeted suite. The broader suite is not
reported as passing. The isolated run's seed test affected only its disposable
test database. Real MySQL integration and interactive browser verification have
not been run in this environment.

Username and business-ID database uniqueness constraints are retained. Email
and cross-profile phone uniqueness remain application checks, as in the existing
schema; this change does not add database constraints or clean up legacy duplicates.
