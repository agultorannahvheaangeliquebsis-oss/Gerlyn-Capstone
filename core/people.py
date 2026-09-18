"""core/people.py — shared person-display-name helper.

Sales' printed receipt and procurement's printed/PDF purchase order each
carried an identical `_person_name` and had started to drift apart in
their docstrings even though the logic was never different. One function,
used by both.
"""


def person_display_name(user):
    """A person's display name, or '' when there is nobody.

    Resolved here rather than in the template because a chained
    `{{ x.get_full_name|default:x.username }}` raises VariableDoesNotExist
    when `x` is null — a filter ARGUMENT is resolved strictly, unlike the
    main variable position, which quietly yields ''. Both callers have
    legitimately-null users: procurement's older purchase orders predate
    created_by, and a sale rung up by the Owner or a Branch Manager has no
    staff set (recorded_by is what's always populated).
    """
    if user is None:
        return ''
    return user.get_full_name() or user.username
