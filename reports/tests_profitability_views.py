"""The Profitability page, its PDF / Excel exports and who may open them."""
import io
import logging
from datetime import timedelta
from decimal import Decimal

from django.test import Client
from django.urls import reverse
from openpyxl import load_workbook
from pypdf import PdfReader

from accounts.models import User
from expenses.models import Expense
from reports import exports
from reports.testing_profit import D, ProfitFixture, make_user


def client_for(user):
    client = Client()
    client.force_login(user)
    return client


class PageTests(ProfitFixture):
    def get(self, user, **params):
        params.setdefault('range', 'custom')
        params.setdefault('date_from', self.start.isoformat())
        params.setdefault('date_to', self.today.isoformat())
        return client_for(user).get(reverse('profitability_report'), params)

    def test_owner_sees_the_verdict_the_statement_and_the_branch_comparison(self):
        response = self.get(self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['totals'].net_profit, D('310'))
        self.assertContains(response, 'pf-verdict--profit')
        self.assertContains(response, 'Profit &amp; Loss')
        self.assertContains(response, 'Branch Comparison')
        self.assertContains(response, 'Profit A')
        self.assertContains(response, 'Profit B')
        self.assertContains(response, '₱310.00')

    def test_the_incomplete_data_note_names_the_products(self):
        response = self.get(self.owner)
        self.assertContains(response, '1 product has no cost price; profit may be overstated.')
        self.assertContains(response, 'Mystery Tin')

    def test_a_loss_is_shown_as_a_loss_in_words_and_in_red(self):
        self.expense(self.branch_a, Expense.Category.MAINTENANCE, '5000')
        response = self.get(self.owner)
        self.assertContains(response, 'pf-verdict--loss')
        self.assertContains(response, '-₱4,690.00')
        self.assertContains(response, 'pf-verdict-word">Loss<')

    def test_a_manager_gets_their_own_branch_and_no_comparison(self):
        response = self.get(self.manager_a)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['totals'].net_profit, D('10'))
        self.assertNotContains(response, 'Branch Comparison')
        self.assertNotContains(response, 'Profit B')

    def test_a_manager_cannot_reach_another_branch_with_a_query_string(self):
        response = self.get(self.manager_a, branch=self.branch_b.pk)
        self.assertEqual(response.context['totals'].net_profit, D('10'))
        self.assertNotContains(response, 'Profit B')

    def test_an_owner_can_pick_a_branch(self):
        response = self.get(self.owner, branch=self.branch_b.pk)
        self.assertEqual(response.context['totals'].net_profit, D('300'))
        self.assertEqual(response.context['branch_filter'], str(self.branch_b.pk))

    def test_the_trend_chart_and_its_table_are_drawn(self):
        response = self.get(self.owner)
        self.assertContains(response, 'Monthly Trend')
        # Two panels (amounts, then net profit on its own axis) sharing the months.
        self.assertContains(response, 'Net sales, cost of goods sold and expenses')
        self.assertContains(response, 'class="pf-panel-title"', count=2)
        self.assertContains(response, 'data-net-profit="₱310.00"')
        self.assertIsNotNone(response.context['chart'])

    def test_an_empty_period_renders_without_error(self):
        response = self.get(self.owner, date_from='2001-01-01', date_to='2001-01-31')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Break-even')


class AccessTests(ProfitFixture):
    def test_staff_cannot_open_the_report_or_either_export(self):
        client = client_for(self.staff_a)
        for url in (
            reverse('profitability_report'),
            reverse('export_pdf', args=['profitability']),
            reverse('export_xlsx', args=['profitability']),
        ):
            self.assertEqual(client.get(url).status_code, 403, url)

    def test_granting_staff_the_reports_module_does_not_open_it(self):
        self.staff_a.allowed_modules = list(self.staff_a.allowed_modules) + ['reports', 'expenses']
        self.staff_a.module_actions = {'reports': ['add', 'edit', 'delete'], 'expenses': ['add', 'edit', 'delete']}
        self.staff_a.save()
        client = client_for(self.staff_a)
        for url in (
            reverse('profitability_report'),
            reverse('export_pdf', args=['profitability']),
            reverse('expense_list'),
        ):
            self.assertEqual(client.get(url).status_code, 403, url)

    def test_customers_and_suppliers_are_refused(self):
        supplier = User.objects.create_user(username='pf-sup-x', password='pw', role=User.Role.SUPPLIER)
        for user in (self.customer, supplier):
            self.assertEqual(client_for(user).get(reverse('profitability_report')).status_code, 403)

    def test_anonymous_visitors_are_sent_to_log_in(self):
        response = Client().get(reverse('profitability_report'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_a_manager_whose_reports_module_is_switched_off_is_refused(self):
        self.manager_a.allowed_modules = [m for m in self.manager_a.allowed_modules if m != 'reports']
        self.manager_a.save()
        self.assertEqual(client_for(self.manager_a).get(reverse('profitability_report')).status_code, 403)


def pdf_text(content):
    return '\n'.join((page.extract_text() or '') for page in PdfReader(io.BytesIO(content)).pages)


class ExportTests(ProfitFixture):
    def doc(self, user=None, **extra):
        return exports.build_document(
            user or self.owner, 'profitability', self.start, self.today, active_range='custom', **extra)

    def params(self, **extra):
        return {'range': 'custom', 'date_from': self.start.isoformat(), 'date_to': self.today.isoformat(), **extra}

    def test_the_summary_opens_with_the_figures_the_page_shows(self):
        doc = self.doc()
        headline = dict(doc.headline)
        self.assertEqual(headline['Net sales'], D('1800'))
        self.assertEqual(headline['Gross profit'], D('1230'))
        self.assertEqual(headline['Total expenses'], D('920'))
        self.assertEqual(headline['Net profit'], D('310'))
        self.assertTrue(headline['Result'].startswith('Profit'))
        pnl = {row[0]: row[1] for row in doc.summary[0].rows}
        self.assertEqual(pnl['Gross sales (completed orders)'], D('2100'))
        self.assertEqual(pnl['Less: refunds (approved)'], D('300'))
        self.assertEqual(pnl['Less: cost of goods sold'], D('570'))
        self.assertEqual(pnl['Net profit'], D('310'))

    def test_the_summary_blocks_cover_everything_asked_for(self):
        titles = [b.title for b in self.doc().summary]
        for title in ('Profit and loss', 'Expenses by category', 'For information', 'Branch comparison',
                      'Monthly trend', 'Most profitable products', 'Least profitable products'):
            self.assertIn(title, titles)

    def test_the_detail_rows_add_up_to_the_grand_total_and_the_headline(self):
        doc = self.doc()
        units, net, cogs, gross_profit, _margin, _note = doc.grand_total
        self.assertEqual(net, D('1800'))
        self.assertEqual(cogs, D('570'))
        self.assertEqual(gross_profit, D('1230'))
        self.assertEqual(sum(r[3] for g in doc.groups for r in g.rows), net)
        self.assertEqual(len(doc.groups), 2)
        self.assertEqual({g.title for g in doc.groups}, {'Profit A', 'Profit B'})

    def test_the_missing_cost_product_is_flagged_in_the_detail_and_the_notes(self):
        doc = self.doc()
        flagged = [r for g in doc.groups for r in g.rows if r[7] == 'No cost price']
        self.assertEqual([r[0] for r in flagged], ['PF-3'])
        self.assertTrue(any('1 product has no cost price' in n for n in doc.notes))
        self.assertTrue(any('Mystery Tin (PF-3)' in n for n in doc.notes))

    def test_a_manager_export_holds_only_their_branch(self):
        doc = self.doc(self.manager_a, branch_id=self.branch_b.pk)
        self.assertEqual(dict(doc.headline)['Net profit'], D('10'))
        self.assertEqual(doc.branch_label, 'Profit A')
        self.assertEqual([g.title for g in doc.groups], ['Profit A'])
        self.assertNotIn('Branch comparison', [b.title for b in doc.summary])

    def test_an_expenses_only_period_still_has_a_summary(self):
        quiet = exports.build_document(
            self.owner, 'profitability', self.start - timedelta(days=400),
            self.start - timedelta(days=380), active_range='custom')
        self.assertTrue(quiet.is_empty)        # nothing at all that month
        Expense.objects.create(
            branch=self.branch_a, category='Rent', amount=D('50'), date=self.start - timedelta(days=390),
            recorded_by=self.owner)
        doc = exports.build_document(
            self.owner, 'profitability', self.start - timedelta(days=400),
            self.start - timedelta(days=380), active_range='custom')
        self.assertFalse(doc.is_empty)
        self.assertEqual(doc.groups, [])
        self.assertEqual(dict(doc.headline)['Net loss'], D('-50'))

    def test_pdf_download_has_the_summary_before_the_detail(self):
        with self.assertNoLogs('xhtml2pdf', level=logging.WARNING):
            response = client_for(self.owner).get(reverse('export_pdf', args=['profitability']), self.params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('profitability-report', response['Content-Disposition'])
        text = pdf_text(response.content)
        self.assertIn('Profitability Report', text)
        self.assertLess(text.index('Summary'), text.index('Product profitability'))
        self.assertIn('Branch comparison', text)
        self.assertIn('PHP 310.00', text)
        self.assertIn('68.3%', text)
        self.assertIn('1 product has no cost price', text)

    def test_excel_download_is_typed_and_summary_first(self):
        response = client_for(self.owner).get(reverse('export_xlsx', args=['profitability']), self.params())
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(io.BytesIO(response.content))
        self.assertEqual(wb.sheetnames, ['Summary', 'Product Profitability'])
        cells = [[c.value for c in row] for row in wb['Summary'].iter_rows()]
        flat = [v for row in cells for v in row if v is not None]
        self.assertIn('Profit and loss', flat)
        # The headline tile also says "Net profit"; the P&L block's row has the % column.
        net_row = next(r for r in cells if r[0] == 'Net profit' and r[2] is not None)
        self.assertEqual(net_row[1], 310.0)                   # a number, not text
        self.assertAlmostEqual(net_row[2], 0.172, places=6)         # a real percentage (17.2%)
        detail = wb['Product Profitability']
        self.assertEqual([c.value for c in detail[4]][:3], ['SKU', 'Product', 'Units sold'])

    def test_staff_export_is_refused_but_a_manager_can_download(self):
        self.assertEqual(
            client_for(self.manager_a).get(reverse('export_pdf', args=['profitability']), self.params()).status_code,
            200)
        self.assertEqual(
            client_for(self.staff_a).get(reverse('export_xlsx', args=['profitability']), self.params()).status_code,
            403)

    def test_a_bad_range_redirects_back_instead_of_exporting_the_wrong_period(self):
        response = client_for(self.owner).get(
            reverse('export_pdf', args=['profitability']),
            {'range': 'custom', 'date_from': '2026-02-30', 'date_to': 'x'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('profitability_report'))


class NavigationTests(ProfitFixture):
    def test_the_report_tabs_link_to_profitability(self):
        response = client_for(self.owner).get(reverse('billing_report'))
        self.assertContains(response, reverse('profitability_report'))
        self.assertContains(response, 'Profitability')


class DashboardCardTests(ProfitFixture):
    """The "Net Profit (this month)" card on the Owner and Branch Manager dashboards."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Month-to-date needs every figure dated inside the current month; the
        # fixture's salary (6 days back) and day-before rent may fall in the
        # previous one, so replace them with two expenses dated today.
        Expense.objects.all().delete()
        cls.expense(cls.branch_a, Expense.Category.RENT, '500')
        cls.expense(cls.branch_b, Expense.Category.UTILITIES, '120')

    def card(self, user, view='accounts:owner_dashboard', **params):
        response = client_for(user).get(reverse(view), params)
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_owner_card_is_the_month_to_date_net_profit_of_every_branch(self):
        response = self.card(self.owner)
        card = response.context['net_profit_card']
        self.assertEqual(card['value'], D('610'))                 # 1,230 gross profit - 620 expenses
        self.assertEqual(card['status'], 'profit')
        self.assertContains(response, 'Net Profit (this month)')
        self.assertContains(response, '₱610.00')
        self.assertContains(response, 'Profit · 33.9% margin')    # 610 / 1,800
        self.assertEqual(card['month'], self.today.replace(day=1))

    def test_a_managers_card_is_their_own_branch_only(self):
        response = self.card(self.manager_a, 'accounts:manager_dashboard')
        self.assertEqual(response.context['net_profit_card']['value'], D('310'))     # 810 - 500
        self.assertContains(response, 'Net Profit (this month)')
        other = self.card(self.manager_b, 'accounts:manager_dashboard')
        self.assertEqual(other.context['net_profit_card']['value'], D('300'))        # 420 - 120

    def test_a_manager_cannot_widen_the_card_with_a_branch_parameter(self):
        response = self.card(self.manager_a, 'accounts:manager_dashboard', branch=self.branch_b.pk)
        self.assertEqual(response.context['net_profit_card']['value'], D('310'))

    def test_the_owner_card_follows_the_branch_switcher(self):
        response = self.card(self.owner, branch=self.branch_b.pk)
        self.assertEqual(response.context['net_profit_card']['value'], D('300'))
        self.assertIn(f'branch={self.branch_b.pk}', response.context['net_profit_card']['url'])

    def test_a_loss_tints_the_card_and_says_loss(self):
        self.expense(self.branch_a, Expense.Category.MAINTENANCE, '4000')
        response = self.card(self.owner)
        self.assertEqual(response.context['net_profit_card']['status'], 'loss')
        self.assertContains(response, 'kpi-card kpi-link danger')
        self.assertContains(response, 'Loss · ')
        self.assertContains(response, '-₱3,390.00')

    def test_the_card_links_to_the_report_for_the_same_month(self):
        url = self.card(self.owner).context['net_profit_card']['url']
        self.assertTrue(url.startswith(reverse('profitability_report')))
        page = client_for(self.owner).get(url)
        self.assertEqual(page.context['totals'].net_profit, D('610'))

    def test_staff_get_no_card(self):
        response = client_for(self.staff_a).get(reverse('accounts:staff_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Net Profit')
        self.assertNotIn('net_profit_card', response.context or {})

    def test_a_manager_without_the_reports_module_gets_no_card(self):
        self.manager_a.allowed_modules = [m for m in self.manager_a.allowed_modules if m != 'reports']
        self.manager_a.save()
        response = self.card(self.manager_a, 'accounts:manager_dashboard')
        self.assertIsNone(response.context['net_profit_card'])
        self.assertNotContains(response, 'Net Profit (this month)')
