"""Shared fixture for the Profitability tests (not itself a test module).

Two branches, an Owner, a Manager and a Staff member on branch A, and a handful
of orders chosen so every figure has a hand-checkable value:

                          Branch A            Branch B
    sale lines            10 x P1 @100        8 x P1 @100
                          5  x P2 @20
                          4  x P3 @50
    frozen unit cost      P1 30               P1 35
                          P2 none (-> product cost 10, "estimated")
                          P3 none (and no product cost: "missing")
    approved refunds      2 x P1 sellable     1 x P1 damaged
                          (PHP 200)           (PHP 100)
    expenses              Rent 500            Utilities 120
                          Salaries 300

    gross sales           1,300               800
    refunds               200                 100
    net sales             1,100               700
    COGS                  300+50+0-60 = 290   280
    gross profit          810                 420
    expenses              800                 120
    net profit            10                  300

Product P1 has a current cost price of 40 even though its sold lines froze 30
and 35: the report must keep using the frozen figures.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from branches.models import Branch
from expenses.models import Expense
from inventory.models import Product, ProductCategory
from sales.models import Refund, RefundItem, SalesItem, SalesTransaction

D = Decimal
COMPLETED = SalesTransaction.OrderStatus.COMPLETED


def make_user(username, role, branch=None, **extra):
    user = User.objects.create_user(username=username, password='pw', role=role, **extra)
    if branch is not None:
        profile = user.manager_profile if role == User.Role.BRANCH_MANAGER else user.staff_profile
        profile.branch = branch
        profile.save(update_fields=['branch'])
        user.refresh_from_db()   # the cached profile still has no branch
    return user


class ProfitFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.start = cls.today - timedelta(days=6)
        cls.owner = User.objects.create_user(username='pf-owner', password='pw', role=User.Role.OWNER)
        cls.branch_a = Branch.objects.create(branch_name='Profit A', location='Baliuag', owner=cls.owner)
        cls.branch_b = Branch.objects.create(branch_name='Profit B', location='Pulilan', owner=cls.owner)
        cls.manager_a = make_user('pf-mgr-a', User.Role.BRANCH_MANAGER, cls.branch_a)
        cls.manager_b = make_user('pf-mgr-b', User.Role.BRANCH_MANAGER, cls.branch_b)
        cls.staff_a = make_user('pf-staff-a', User.Role.STAFF, cls.branch_a)
        cls.customer = User.objects.create_user(username='pf-cust', password='pw', role=User.Role.CUSTOMER)

        category = ProductCategory.objects.create(category_name='Profit goods')
        cls.p1 = Product.objects.create(
            category=category, sku_code='PF-1', product_name='Rice 25kg', unit_price=D('100'), cost_price=D('40'))
        cls.p2 = Product.objects.create(
            category=category, sku_code='PF-2', product_name='Sugar 1kg', unit_price=D('20'), cost_price=D('10'))
        cls.p3 = Product.objects.create(
            category=category, sku_code='PF-3', product_name='Mystery Tin', unit_price=D('50'))   # no cost anywhere

        cls._sales()
        cls._expenses()

    # --- builders -------------------------------------------------------------

    @classmethod
    def sale(cls, branch, lines, status=COMPLETED, when=None):
        """A Completed (by default) order. `lines` = [(product, qty, price, unit_cost)]."""
        total = sum(D(qty) * D(price) for _p, qty, price, _c in lines)
        txn = SalesTransaction.objects.create(
            branch=branch, total_amount=total, recorded_by=cls.owner,
            transaction_type='Physical', mode_of_payment='Cash',
        )
        updates = {'order_status': status}
        if when is not None:
            updates['transaction_date'] = when
        SalesTransaction.objects.filter(pk=txn.pk).update(**updates)
        txn.refresh_from_db()
        items = [
            SalesItem.objects.create(
                transaction=txn, product=product, quantity_sold=qty, unit_price=D(price),
                unit_cost=None if cost is None else D(cost),
            )
            for product, qty, price, cost in lines
        ]
        return txn, items

    @classmethod
    def refund(cls, txn, item, qty, amount, condition, status=Refund.Status.APPROVED):
        refund = Refund.objects.create(
            transaction=txn, status=status, reason='t', reason_code=Refund.ReasonCode.OTHER,
            refund_type=Refund.RefundType.PARTIAL, refund_method=Refund.RefundMethod.CASH,
            total_amount=D(amount), requested_by=cls.owner, refunded_by=cls.owner,
            refunded_at=timezone.now(),
        )
        RefundItem.objects.create(
            refund=refund, sales_item=item, quantity=qty, condition=condition, amount=D(amount))
        return refund

    @classmethod
    def expense(cls, branch, category, amount, when=None, description=''):
        return Expense.objects.create(
            branch=branch, category=category, amount=D(amount),
            date=when or cls.today, description=description, recorded_by=cls.owner,
        )

    # --- the fixture ----------------------------------------------------------

    @classmethod
    def _sales(cls):
        Condition = RefundItem.Condition
        # Branch A: three Completed orders...
        cls.a1, (cls.a1_item,) = cls.sale(cls.branch_a, [(cls.p1, 10, '100', '30')])
        cls.a2, (cls.a2_item,) = cls.sale(cls.branch_a, [(cls.p2, 5, '20', None)])
        cls.a3, (cls.a3_item,) = cls.sale(cls.branch_a, [(cls.p3, 4, '50', None)])
        # ...and one in each state that is not revenue, plus one outside the range.
        cls.a_cancelled, _ = cls.sale(
            cls.branch_a, [(cls.p1, 1, '100', '30')], status=SalesTransaction.OrderStatus.CANCELLED)
        cls.a_pending, _ = cls.sale(
            cls.branch_a, [(cls.p1, 1, '100', None)], status=SalesTransaction.OrderStatus.PENDING)
        cls.a_old, _ = cls.sale(
            cls.branch_a, [(cls.p1, 50, '100', '30')], when=timezone.now() - timedelta(days=40))
        # Branch B
        cls.b1, (cls.b1_item,) = cls.sale(cls.branch_b, [(cls.p1, 8, '100', '35')])

        cls.refund(cls.a1, cls.a1_item, 2, '200', Condition.SELLABLE)
        cls.refund(cls.b1, cls.b1_item, 1, '100', Condition.DAMAGED)
        # Requests that moved no money must reach nothing.
        cls.refund(cls.a1, cls.a1_item, 1, '100', Condition.SELLABLE, status=Refund.Status.PENDING)
        cls.refund(cls.a1, cls.a1_item, 1, '100', Condition.SELLABLE, status=Refund.Status.REJECTED)

    @classmethod
    def _expenses(cls):
        Category = Expense.Category
        cls.expense(cls.branch_a, Category.RENT, '500')
        cls.expense(cls.branch_a, Category.SALARIES, '300', when=cls.start)          # first day of the range
        cls.expense(cls.branch_a, Category.RENT, '999', when=cls.start - timedelta(days=1))   # day before: out
        cls.expense(cls.branch_b, Category.UTILITIES, '120')

    expected = {
        'a': dict(gross=D('1300'), refunds=D('200'), net=D('1100'), cogs=D('290'),
                  gross_profit=D('810'), expenses=D('800'), net_profit=D('10')),
        'b': dict(gross=D('800'), refunds=D('100'), net=D('700'), cogs=D('280'),
                  gross_profit=D('420'), expenses=D('120'), net_profit=D('300')),
        'all': dict(gross=D('2100'), refunds=D('300'), net=D('1800'), cogs=D('570'),
                    gross_profit=D('1230'), expenses=D('920'), net_profit=D('310')),
    }

    def assert_figures(self, figures, key):
        want = self.expected[key]
        got = dict(
            gross=figures.gross_sales, refunds=figures.refunds, net=figures.net_sales,
            cogs=figures.cogs, gross_profit=figures.gross_profit, expenses=figures.expenses,
            net_profit=figures.net_profit,
        )
        self.assertEqual(got, want)
