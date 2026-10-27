"""Who may record an expense, for which branch, and what a valid one is.

The rules, kept out of the views so a test (or a future API) hits the same ones:

  * Only an Owner or a Branch Manager records, edits or deletes expenses. Staff
    never see this module, whatever grants their account carries.
  * An Owner works across every branch and must say which one an expense
    belongs to. A Branch Manager works on their own branch only: the branch they
    submit is ignored, they cannot open another branch's expense by id, and a
    manager with no branch assigned can do nothing at all (fails closed).
  * Every change is written to the audit log. The log is fed by the model
    signals (audit.registry); the action label set here is what the Audit Log
    page shows as the name of the action.
"""
from datetime import date

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from audit import context as audit_context
from branches.models import Branch
from core.limits import MAX_AMOUNT, MAX_DESCRIPTION_LENGTH, MIN_PRICE, parse_money, parse_text
from core.permissions import BRANCH_MANAGER, OWNER
from core.scoping import branch_ids_for_user, scope_queryset

from .models import Expense
from .receipts import validate_receipt

MANAGE_ROLES = (OWNER, BRANCH_MANAGER)

# Nobody is recording a 1990 electricity bill; a year typed as 0202 is a typo.
EARLIEST_DATE = date(2000, 1, 1)


class ExpenseError(Exception):
    """A rule was broken; the message is one sentence fit to show the user."""


class ExpenseForbidden(ExpenseError):
    """The user may not do this at all (as opposed to a field being wrong)."""


def ensure_can_manage(user):
    if not getattr(user, 'is_authenticated', False) or user.role not in MANAGE_ROLES:
        raise ExpenseForbidden('Only the Owner and Branch Managers can manage expenses.')


def visible_expenses(user, branch_id=None):
    """Expenses `user` may see, newest first. Fails closed for a manager with no
    branch (scope_queryset), and only an Owner's `branch_id` narrows anything."""
    qs = scope_queryset(Expense.objects.select_related('branch', 'recorded_by'), user, 'branch')
    if user.role == OWNER and branch_id:
        try:
            qs = qs.filter(branch_id=int(branch_id))
        except (TypeError, ValueError):
            pass
    return qs.order_by('-date', '-pk')


def get_manageable_expense(user, pk, *, lock=False):
    """The expense `pk` if `user` may act on it, else None -- the same answer
    for "no such expense" and "another branch's expense", so an id reveals
    nothing."""
    ensure_can_manage(user)
    qs = scope_queryset(Expense.objects.select_related('branch'), user, 'branch')
    if lock:
        qs = qs.select_for_update()
    return qs.filter(pk=pk).first()


def managed_branches(user):
    """Branches the user may record an expense against (a queryset)."""
    ids = branch_ids_for_user(user)
    branches = Branch.objects.order_by('branch_name')
    return branches if ids is None else branches.filter(pk__in=ids)


def _resolve_branch(user, raw_branch):
    """(branch, error). A Manager's branch is their own, whatever was posted."""
    if user.role == OWNER:
        text = str(raw_branch or '').strip()
        branch = Branch.objects.filter(pk=int(text)).first() if text.isascii() and text.isdigit() and len(text) <= 9 else None
        if branch is None:
            return None, 'Choose a branch.'
        return branch, None
    ids = branch_ids_for_user(user)
    if not ids:
        return None, 'No branch is assigned to your account, so you cannot record expenses.'
    return Branch.objects.filter(pk=ids[0]).first(), None


def clean_expense(user, post, files, *, instance=None):
    """Validate the Add / Edit Expense form. Returns `(cleaned, errors)`; nothing
    in `cleaned` is trusted until `errors` is empty.

    `cleaned['receipt']` is a validated upload, or None; `remove_receipt` is
    True when an edit asks to drop the current one.
    """
    ensure_can_manage(user)
    errors = []

    branch, err = _resolve_branch(user, post.get('branch'))
    if err:
        errors.append(err)

    category = post.get('category', '')
    if category not in Expense.Category.values:
        errors.append('Choose a category from the list.')

    amount, err = parse_money(post.get('amount'), 'Amount', minimum=MIN_PRICE, maximum=MAX_AMOUNT)
    if err:
        errors.append(err)

    raw_date = (post.get('date') or '').strip()
    try:
        expense_date = parse_date(raw_date) if raw_date else None
    except ValueError:
        expense_date = None
    if expense_date is None:
        errors.append('Enter the date this expense belongs to.')
    elif expense_date > timezone.localdate():
        errors.append('The date cannot be in the future.')
    elif expense_date < EARLIEST_DATE:
        errors.append('The date is too far in the past.')

    description, err = parse_text(
        post.get('description'), 'Description', max_length=MAX_DESCRIPTION_LENGTH,
    )
    if err:
        errors.append(err)
    elif category == Expense.Category.OTHER and not description:
        # "Other" says nothing about where the money went.
        errors.append('Describe what this expense was for.')

    receipt = None
    upload = files.get('receipt') if files is not None else None
    if upload:
        receipt, err = validate_receipt(upload)
        if err:
            errors.append(err)

    cleaned = {
        'branch': branch,
        'category': category,
        'amount': amount,
        'date': expense_date,
        'description': description,
        'receipt': receipt,
        'remove_receipt': bool(post.get('remove_receipt')) and instance is not None,
    }
    return cleaned, errors


def _discard_file(field_file):
    """Remove a stored receipt once the transaction that replaced it has
    committed -- never before, so a rollback cannot leave a row pointing at a
    file that is already gone."""
    if not field_file:
        return
    storage, name = field_file.storage, field_file.name

    def _remove():
        try:
            storage.delete(name)
        except OSError:
            pass

    transaction.on_commit(_remove)


def create_expense(user, cleaned):
    ensure_can_manage(user)
    with transaction.atomic():
        audit_context.set_action_label('Recorded an expense')
        expense = Expense(
            branch=cleaned['branch'], category=cleaned['category'],
            amount=cleaned['amount'], date=cleaned['date'],
            description=cleaned['description'], recorded_by=user,
        )
        if cleaned['receipt']:
            expense.receipt = cleaned['receipt']
        expense.save()
    return expense


def update_expense(user, pk, cleaned):
    """Apply an edit under a row lock, re-checking access against the locked
    row. Returns the expense, or None if `user` may not touch it."""
    with transaction.atomic():
        expense = get_manageable_expense(user, pk, lock=True)
        if expense is None:
            return None
        audit_context.set_action_label('Edited an expense')
        old_receipt = expense.receipt if (cleaned['receipt'] or cleaned['remove_receipt']) else None
        expense.branch = cleaned['branch']
        expense.category = cleaned['category']
        expense.amount = cleaned['amount']
        expense.date = cleaned['date']
        expense.description = cleaned['description']
        if cleaned['receipt']:
            expense.receipt = cleaned['receipt']
        elif cleaned['remove_receipt']:
            expense.receipt = ''
        expense.save()
        _discard_file(old_receipt)
    return expense


def delete_expense(user, pk):
    """Delete under a row lock. Returns True if it was deleted, False if `user`
    may not touch it. The audit log keeps the row as it stood."""
    with transaction.atomic():
        expense = get_manageable_expense(user, pk, lock=True)
        if expense is None:
            return False
        audit_context.set_action_label('Deleted an expense')
        receipt = expense.receipt
        expense.delete()
        _discard_file(receipt)
    return True
