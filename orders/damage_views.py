"""Customer side of Damaged Product Reports: "Report a Problem" on a received
order, and the photos of the customer's own reports.

The rules live in damage_reports.services (the staff pages call the same
service); this file only reads the form and shows the result. Every view looks
the order up by id AND customer=request.user, so another customer's order is a
404 - it does not confirm the order exists.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET

from audit import context as audit_context
from core.limits import MAX_DESCRIPTION_LENGTH
from damage_reports import services
from damage_reports.errors import DamageReportError, DamageReportForbidden
from damage_reports.models import DamageReport
from damage_reports.photos import MAX_PHOTO_MB, MAX_PHOTOS
from damage_reports.views import photo_response
from sales.models import SalesTransaction

from .customer_views import customer_required


def _own_order(request, transaction_id):
    return get_object_or_404(
        SalesTransaction.objects.select_related('branch'),
        id=transaction_id, customer=request.user,   # never someone else's
    )


def problem_panel(sale, customer):
    """What Track Order needs to draw its "Report a problem" card: whether the
    button is offered, until when, and the customer's reports so far."""
    reports = list(
        DamageReport.objects.filter(
            sales_transaction=sale, reporter_type=DamageReport.ReporterType.CUSTOMER,
        ).select_related('product').prefetch_related('photos').order_by('-created_at', '-pk')
    )
    block = services.customer_block_reason(sale, customer)
    lines = [] if block else services.reportable_lines(sale)
    return {
        'can_report': bool(not block and lines),
        'block': block,
        # Only worth saying once there is a window to talk about.
        'window_open': not block,
        'closes': services.report_window_closes(sale),
        'window_days': sale.REFUND_WINDOW.days,
        'all_reported': bool(not block and not lines),
        'reports': reports,
    }


def _form_context(sale, lines, *, posted=None, error=''):
    posted = posted or {}
    return {
        'sale': sale,
        'lines': lines,
        'closes': services.report_window_closes(sale),
        'window_days': sale.REFUND_WINDOW.days,
        'posted': posted,
        'error': error,
        'max_photos': MAX_PHOTOS,
        'max_photo_mb': MAX_PHOTO_MB,
        'max_description': MAX_DESCRIPTION_LENGTH,
    }


@customer_required
def report_problem(request, transaction_id):
    """GET: the form. POST: file the report (item, quantity, description, 1-5
    photos). Offered only on a Completed order inside the refund window."""
    sale = _own_order(request, transaction_id)
    block = services.customer_block_reason(sale, request.user)
    if block:
        messages.error(request, block)
        return redirect('orders:track_order', transaction_id=sale.pk)
    lines = services.reportable_lines(sale)
    if not lines:
        messages.info(
            request,
            f'Every item on {sale.order_number} has already been refunded or reported.',
        )
        return redirect('orders:track_order', transaction_id=sale.pk)

    if request.method != 'POST':
        return render(request, 'orders/report_problem.html', _form_context(sale, lines))

    posted = {
        'sales_item_id': request.POST.get('sales_item_id', ''),
        'quantity': request.POST.get('quantity', ''),
        'description': request.POST.get('description', ''),
    }
    audit_context.set_action_label('Reported a problem with an order')
    try:
        report = services.create_customer_report(
            request.user, sale.pk, sales_item_id=posted['sales_item_id'],
            quantity=posted['quantity'], description=posted['description'],
            photos=request.FILES.getlist('photos'),
        )
    except DamageReportForbidden as exc:
        raise PermissionDenied(str(exc))
    except DamageReportError as exc:
        return render(
            request, 'orders/report_problem.html',
            _form_context(sale, services.reportable_lines(sale), posted=posted, error=str(exc)),
        )

    messages.success(
        request,
        f'Thanks - we received your report ({report.report_number}). '
        f'{sale.branch.branch_name} will review it and let you know.',
    )
    return redirect('orders:track_order', transaction_id=sale.pk)


@customer_required
@require_GET
def problem_photo(request, transaction_id, report_id, photo_id):
    """Stream a photo from the customer's own report on their own order."""
    report = get_object_or_404(
        DamageReport,
        pk=report_id, sales_transaction_id=transaction_id,
        sales_transaction__customer=request.user,
        reporter_type=DamageReport.ReporterType.CUSTOMER,
    )
    return photo_response(get_object_or_404(report.photos, pk=photo_id))
