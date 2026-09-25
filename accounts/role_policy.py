"""Role boundaries for the Owner's internal User Management feature.

Customer accounts deliberately live outside this policy: they are created by
customer registration and managed through the Customer Accounts module.
"""

from .models import User


USER_MANAGEMENT_ROLES = frozenset({
    User.Role.OWNER,
    User.Role.BRANCH_MANAGER,
    User.Role.STAFF,
    User.Role.SUPPLIER,
})

USER_MANAGEMENT_ROLE_CHOICES = tuple(
    choice for choice in User.Role.choices
    if choice[0] in USER_MANAGEMENT_ROLES
)
