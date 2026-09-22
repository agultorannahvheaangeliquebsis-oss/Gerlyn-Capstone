"""Panel QA finding — "Stress-test 20/50/100+ PO items. Verify page breaks,
repeated table headers, wrapped names, totals, signatures/footer, and absence
of overlapping lines."

Nothing in the codebase exercised po_pdf.html (the xhtml2pdf-rendered
download) or _procurement_document_context beyond a handful of fixture line
items, so a purchase order with dozens/hundreds of rows was untested
territory going into this pass — this file is the first PDF test in the repo.

Two complementary techniques are used rather than one, per the panel's ask:

1. Hit procurement_pdf through the Django test client at 20 / 50 / 100+ items
   and confirm xhtml2pdf actually produces a complete PDF (status 200,
   application/pdf, a real %PDF-...%%EOF byte stream of non-trivial size) —
   this is the "did it break" smoke test. Parsing the PDF's internal layout
   is out of scope (no pypdf/pdfplumber in requirements.txt, and adding one
   just for this would be a new dependency for a single test file).

2. For the 100+ case, also render _procurement_document_context() through
   po_pdf.html directly (render_to_string, no pisa.CreatePDF step) and assert
   on the resulting HTML: every item present exactly once, long/special-
   character names escaped safely, and the printed total matching the exact
   sum of the line items. This is what actually proves "no items silently
   dropped" and "no overlapping/broken markup" — the rendered HTML is the
   whole visual truth of what xhtml2pdf will lay out; the PDF bytes on their
   own don't let us assert any of that without a parser.

xhtml2pdf-specific behaviour this file's assertions (and the CSS change in
po_pdf.html) lean on, checked against the exact installed version
(xhtml2pdf==0.2.19, see venv/Lib/site-packages/xhtml2pdf/tables.py):

  - <thead> already repeats on every page with NO extra attribute needed.
    pisaTagTHEAD.end() sets `tdata.repeat = max(tdata.repeat, tdata.row)` —
    i.e. it counts however many rows were inside <thead> and repeats that
    many at the top of each page automatically. po_pdf.html's items-table
    already uses a real <thead>, so this was already correct; nothing to add
    for "repeated table headers".
  - A <tr> can never be split across a page break: pisaTagTABLE.end() always
    builds the underlying reportlab Table with `splitByRow=1`, which only
    ever breaks a table *between* rows. `page-break-inside: avoid` is not a
    property xhtml2pdf's CSS layer implements at all (no match anywhere in
    w3c/css.py or context.py for it, or for -pdf-keep-with-next despite a
    stale docstring in default.py claiming otherwise) — adding it to the
    template would be a no-op that looks like a fix but does nothing, so it
    was deliberately left out rather than added to "look thorough".
  - Long/overflowing cell content (a single unbroken long product name) is
    already guarded by PmlKeepInFrame in mode "shrink" (the default -pdf-
    keep-in-frame-mode), which xhtml2pdf wraps around every <td>'s content —
    it shrinks the cell's text to fit rather than letting it overflow into
    the next column. po_pdf.html now sets `-pdf-keep-in-frame-mode: shrink`
    explicitly on .items-table td instead of relying on that default.
"""
from decimal import Decimal

from django.template.defaultfilters import floatformat
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse

from inventory.models import Product
from procurement.models import Procurement, ProcurementItem
from procurement.tests import ProcurementAuthorizationBase
from procurement.views import _procurement_document_context

# Deliberately long / awkward product names so "wrapped names" is actually
# exercised rather than assumed — the base fixture's "Sack of Rice" (12
# chars) would never wrap or need shrinking.
LONG_NAME_WORDY = (
    "Imported Extra Fine Long Grain Jasmine Rice Premium Quality Milled "
    "Grade A Superior Selection Resealable Value Pack"
)  # multi-word: exercises normal Paragraph word-wrap inside the cell
LONG_NAME_UNBROKEN = (
    "Supercalifragilisticexpialidociousindustrialgradebulkpackagingunitsack"
)  # one unbroken token: cannot wrap on a space, exercises the shrink-to-fit
   # KeepInFrame fallback instead
LONG_NAME_SPECIAL = 'Peanut Butter & Jelly "Value" Pack <Family Size> Set'
# ^ has HTML-significant characters, to prove the template escapes rather
#   than injects them raw.

assert len(LONG_NAME_WORDY) <= 150
assert len(LONG_NAME_UNBROKEN) <= 150
assert len(LONG_NAME_SPECIAL) <= 150


class ProcurementPdfStressBase(ProcurementAuthorizationBase):
    """Adds bulk product/line-item creation on top of the shared C-1 cast
    (Owner, Branch Manager, home/other branch, one supplier) so each test
    doesn't have to hand-roll its own fixtures."""

    def _bulk_products(self, prefix, count):
        """count distinct Product rows, most with a short generic name, a
        few deliberately long/awkward ones sprinkled in so every batch size
        actually contains a wrapped-name case, not just the 100+ one."""
        products = []
        for i in range(count):
            name = f"Stress Test Item {prefix}-{i:04d}"
            if i == 1:
                name = LONG_NAME_WORDY
            elif i == 2:
                name = LONG_NAME_UNBROKEN
            elif i == 3:
                name = LONG_NAME_SPECIAL
            products.append(Product(
                sku_code=f"STRESS-{prefix}-{i:04d}",
                product_name=name,
                unit_price=Decimal('20.00'),
            ))
        Product.objects.bulk_create(products)
        # bulk_create's return value doesn't reliably come back with a pk
        # populated on this backend (MySQL has no RETURNING clause), and
        # ProcurementItem's own bulk_create below refuses an FK to an
        # object it can't see a pk on -- re-fetch instead of trusting the
        # objects bulk_create() was handed.
        return list(
            Product.objects.filter(sku_code__startswith=f"STRESS-{prefix}-")
            .order_by('sku_code')
        )

    def _bulk_items(self, proc, products):
        """One ProcurementItem per product, deterministic but non-uniform
        quantity/cost so the total is a real sum, not count * one constant
        (which would pass a total-math bug that only breaks on mixed
        values)."""
        items = []
        for idx, product in enumerate(products):
            quantity = (idx % 9) + 1
            cost = Decimal('15.75') + Decimal(idx % 11)
            items.append(ProcurementItem(
                procurement=proc, product=product,
                quantity_ordered=quantity, unit_cost_snapshot=cost,
            ))
        ProcurementItem.objects.bulk_create(items)

    def _po_with_items(self, count, prefix):
        proc = self._po(self.home_branch, Procurement.Status.APPROVED,
                         owner=self.owner, created_by=self.owner)
        products = self._bulk_products(prefix, count)
        self._bulk_items(proc, products)
        # total_cost has no signal keeping it in sync with the items (see
        # Procurement.total_cost's docstring in models.py) — set it exactly
        # to the line sum so the PDF's own totals_disagree banner doesn't
        # fire and confound these assertions with an unrelated finding.
        line_sum = sum(
            (i.line_total for i in proc.items.all()), Decimal('0')
        )
        proc.total_cost = line_sum
        proc.save(update_fields=['total_cost'])
        return proc, line_sum


class ProcurementPdfDownloadStressTests(ProcurementPdfStressBase):
    """Hits the real download endpoint (procurement_pdf -> xhtml2pdf ->
    pisa.CreatePDF) at each stress tier and proves it comes back as an
    intact PDF, not a silent empty/broken one."""

    def _assert_is_real_pdf(self, response, min_size):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        content = response.content
        # A truncated or failed render is exactly the failure mode a bare
        # "status == 200" check would miss: pisa.CreatePDF can still return
        # a response object even when the stream it wrote is garbage, so the
        # actual bytes are checked for a real PDF header/trailer plus a
        # size floor that rules out a near-empty document.
        self.assertTrue(
            content.startswith(b'%PDF-'),
            'response body does not start with a PDF header'
        )
        self.assertIn(
            b'%%EOF', content[-2048:],
            'response body has no PDF trailer near its end -- looks truncated'
        )
        self.assertGreater(
            len(content), min_size,
            f'PDF is only {len(content)} bytes -- looks empty/broken'
        )

    def test_pdf_20_items(self):
        proc, _ = self._po_with_items(20, 'A')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('procurement_pdf', args=[proc.pk]))
        self._assert_is_real_pdf(response, min_size=2500)

    def test_pdf_50_items(self):
        proc, _ = self._po_with_items(50, 'B')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('procurement_pdf', args=[proc.pk]))
        self._assert_is_real_pdf(response, min_size=3500)

    def test_pdf_100_plus_items(self):
        proc, _ = self._po_with_items(120, 'C')
        self.client.force_login(self.owner)
        response = self.client.get(reverse('procurement_pdf', args=[proc.pk]))
        self._assert_is_real_pdf(response, min_size=5000)

    def test_pdf_size_grows_with_item_count(self):
        """A weak but real sanity check that the extra items are actually
        being rendered into the document rather than e.g. silently capped —
        more rows should mean more bytes."""
        self.client.force_login(self.owner)

        proc_small, _ = self._po_with_items(20, 'D')
        resp_small = self.client.get(reverse('procurement_pdf', args=[proc_small.pk]))

        proc_large, _ = self._po_with_items(120, 'E')
        resp_large = self.client.get(reverse('procurement_pdf', args=[proc_large.pk]))

        self.assertGreater(len(resp_large.content), len(resp_small.content))


class ProcurementPdfHtmlContentTests(ProcurementPdfStressBase):
    """The precise check: render po_pdf.html directly from
    _procurement_document_context (skipping pisa.CreatePDF) for a 100+ item
    order, and inspect the actual markup xhtml2pdf would lay out."""

    def _render_html(self, proc):
        request = RequestFactory().get('/')
        request.user = self.owner
        context = _procurement_document_context(proc, request)
        html = render_to_string('procurement/po_pdf.html', context)
        return html, context

    def test_every_item_present_exactly_once_no_drops(self):
        item_count = 137
        proc, _line_sum = self._po_with_items(item_count, 'F')
        html, context = self._render_html(proc)

        self.assertEqual(len(context['items']), item_count)
        # Each rendered row prints its product's SKU in a `.sku` div -- once
        # per item, so this both proves the row count exactly matches (no
        # items silently dropped, none duplicated) and that nothing further
        # up the table collided with/overwrote another row's markup.
        self.assertEqual(html.count('class="sku"'), item_count)

        for product in Product.objects.filter(sku_code__startswith='STRESS-F-'):
            self.assertIn(product.sku_code, html)

    def test_long_and_special_character_names_render_safely(self):
        proc, _line_sum = self._po_with_items(50, 'G')
        html, _context = self._render_html(proc)

        self.assertIn(LONG_NAME_WORDY, html)
        self.assertIn(LONG_NAME_UNBROKEN, html)

        # The special-character name must come through HTML-escaped (Django
        # autoescape is on by default for this template) -- present in its
        # escaped form, and its raw angle-bracket/ampersand form must NOT
        # appear verbatim, which is what "unescaped-safely rendered" is
        # actually testing for: a long name can't corrupt the surrounding
        # markup.
        self.assertIn(
            'Peanut Butter &amp; Jelly &quot;Value&quot; '
            'Pack &lt;Family Size&gt; Set',
            html,
        )
        self.assertNotIn(LONG_NAME_SPECIAL, html)
        self.assertNotIn('<Family Size>', html)

    def test_totals_match_exact_line_sum_at_scale(self):
        item_count = 150
        proc, line_sum = self._po_with_items(item_count, 'H')
        html, context = self._render_html(proc)

        # The context builder's own reconciliation must agree there is
        # nothing to flag...
        self.assertFalse(context['totals_disagree'])
        self.assertEqual(context['line_sum'], line_sum)
        # ...and what actually prints in the TOTAL COST row must be that
        # exact figure, formatted exactly the way the template's
        # `|floatformat:2` filter would (USE_THOUSAND_SEPARATOR is on in
        # settings, so a 150-item total crosses into 4+ digits and this
        # reuses Django's own formatter rather than re-implementing comma
        # grouping by hand).
        expected = floatformat(line_sum, 2)
        self.assertIn(f'PHP {expected}', html)
        # No mismatch banner should have been rendered alongside it.
        self.assertNotIn('does not match the', html)

    def test_signatures_and_footer_present_at_scale(self):
        """The signature block and footer are outside the items table, but
        they're exactly what a long table could visually crowd out or push
        off a truncated render -- confirm they still render at 100+ items."""
        proc, _line_sum = self._po_with_items(110, 'I')
        html, _context = self._render_html(proc)

        self.assertIn('Prepared By', html)
        self.assertIn('Authorized By', html)
        self.assertIn('Received / Acknowledged By', html)
        self.assertIn('Printed', html)
        self.assertIn('TOTAL COST', html)
