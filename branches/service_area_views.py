"""Delivery Areas - Owner-only administration of where each branch delivers.

Every add, edit, activate and deactivate is a save of a ``ServiceArea`` row, so
the Audit Log (audit.registry) records it with the actor; each view also names
the action so the grouped log reads "Deactivated a delivery area".

An area is identified by PSGC codes chosen from the same Province ->
Municipality -> Barangay lists the checkout uses. The name stored with it is
looked up from those codes here, never taken from the form, so what is shown
and what is matched cannot disagree. Once added, only the fee, minimum order,
notes and active flag change; a different place is a different entry.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts import geo
from accounts.models import User
from audit.context import set_action_label
from core.limits import MAX_DELIVERY_FEE, MAX_MINIMUM_ORDER, parse_money, parse_text
from decimal import Decimal

from .coverage import parent_label, sorted_areas
from .models import Branch, ServiceArea

MAX_NOTES_LENGTH = 255


def _owner_only(request):
    if request.user.role != User.Role.OWNER:
        messages.error(request, 'Only the Owner can manage delivery areas.')
        return redirect('accounts:home')
    return None


def _list_context(edit_area=None, form=None):
    by_branch = {}
    for area in ServiceArea.objects.all():
        by_branch.setdefault(area.branch_id, []).append(area)
    groups = []
    for branch in Branch.objects.order_by('branch_name'):
        rows = sorted_areas(by_branch.get(branch.pk, []))
        groups.append({
            'branch': branch,
            'configured': bool(rows),
            'rows': [{'area': a, 'parent': parent_label(a)} for a in rows],
        })
    return {
        'groups': groups,
        'provinces': geo.all_provinces(),
        'type_choices': ServiceArea.AreaType.choices,
        'branches': Branch.objects.order_by('branch_name'),
        'edit_area': edit_area,
        'form': form or {},
        'active_page': 'service_areas',
    }


@login_required(login_url='accounts:login')
def service_area_list(request):
    denied = _owner_only(request)
    if denied:
        return denied
    edit_area = None
    if request.GET.get('edit'):
        edit_area = ServiceArea.objects.filter(pk=request.GET['edit'] if request.GET['edit'].isdigit() else 0).first()
    return render(request, 'branches/service_area_list.html', _list_context(edit_area))


def _identify_place(area_type, province, city, barangay):
    """``(codes, name, error)`` for the place a new entry is about."""
    Type = ServiceArea.AreaType
    if area_type not in Type.values:
        return None, '', 'Choose whether this is a province, a municipality or a barangay.'
    if not geo.province_name(province):
        return None, '', 'Choose the province.'
    if area_type == Type.PROVINCE:
        return (province, '', ''), geo.province_name(province), None
    if not geo.city_name(city) or geo.province_of_city(city) != province:
        return None, '', 'Choose a municipality in that province.'
    if area_type == Type.MUNICIPALITY:
        return (province, city, ''), geo.city_name(city), None
    if barangay not in {b['code'] for b in geo.barangays(city)}:
        return None, '', 'Choose a barangay in that municipality.'
    return (province, city, barangay), geo.barangay_name(barangay), None


def _amounts(post):
    """``(fee, minimum, notes, errors)`` from the shared fields."""
    fee, e1 = parse_money(
        post.get('delivery_fee'), 'Delivery fee',
        minimum=Decimal('0'), maximum=MAX_DELIVERY_FEE, required=False)
    minimum, e2 = parse_money(
        post.get('minimum_order'), 'Minimum order',
        minimum=Decimal('0'), maximum=MAX_MINIMUM_ORDER, required=False)
    notes, e3 = parse_text(post.get('notes'), 'Notes', max_length=MAX_NOTES_LENGTH, required=False)
    return fee, minimum, notes or '', [e for e in (e1, e2, e3) if e]


def _refuse(request, errors, form=None, edit_area=None):
    for error in errors:
        messages.error(request, error)
    return render(request, 'branches/service_area_list.html', _list_context(edit_area, form))


@login_required(login_url='accounts:login')
@require_POST
def service_area_add(request):
    denied = _owner_only(request)
    if denied:
        return denied
    form = request.POST.dict()
    branch = Branch.objects.filter(
        pk=form.get('branch') if str(form.get('branch', '')).isdigit() else 0
    ).first()
    if branch is None:
        return _refuse(request, ['Choose the branch this area belongs to.'], form)

    area_type = form.get('area_type', '')
    codes, name, error = _identify_place(
        area_type, form.get('province', ''), form.get('city', ''), form.get('barangay', ''))
    fee, minimum, notes, errors = _amounts(request.POST)
    if error:
        errors.insert(0, error)
    if errors:
        return _refuse(request, errors, form)

    province, city, barangay = codes
    try:
        with transaction.atomic():
            set_action_label('Added a delivery area')
            ServiceArea.objects.create(
                branch=branch, area_type=area_type, area_name=name,
                province_code=province, city_code=city, barangay_code=barangay,
                is_active=bool(request.POST.get('is_active')),
                delivery_fee=fee, minimum_order=minimum, notes=notes,
            )
    except IntegrityError:
        return _refuse(
            request, [f'{name} is already listed for {branch.branch_name}. Edit that entry instead.'], form)
    messages.success(request, f'{name} added to {branch.branch_name}’s delivery areas.')
    return redirect('service_area_list')


@login_required(login_url='accounts:login')
@require_POST
def service_area_edit(request, pk):
    denied = _owner_only(request)
    if denied:
        return denied
    area = get_object_or_404(ServiceArea, pk=pk)
    fee, minimum, notes, errors = _amounts(request.POST)
    if errors:
        return _refuse(request, errors, request.POST.dict(), edit_area=area)
    set_action_label('Edited a delivery area')
    area.delivery_fee = fee
    area.minimum_order = minimum
    area.notes = notes
    area.is_active = bool(request.POST.get('is_active'))
    area.save()
    messages.success(request, f'{area.area_name} updated for {area.branch.branch_name}.')
    return redirect('service_area_list')


@login_required(login_url='accounts:login')
@require_POST
def service_area_toggle(request, pk):
    denied = _owner_only(request)
    if denied:
        return denied
    area = get_object_or_404(ServiceArea, pk=pk)
    area.is_active = not area.is_active
    set_action_label(f'{"Activated" if area.is_active else "Deactivated"} a delivery area')
    area.save(update_fields=['is_active', 'updated_at'])
    messages.success(
        request,
        f'{area.area_name} is now {"active" if area.is_active else "inactive"} for '
        f'{area.branch.branch_name}.',
    )
    return redirect('service_area_list')
