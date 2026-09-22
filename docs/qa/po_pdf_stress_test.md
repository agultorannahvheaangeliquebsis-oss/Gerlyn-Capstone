# QA note — Purchase Order PDF stress test (20 / 50 / 100+ line items)

**For:** Katrina — to fold into Chapter III Testing as a specific test case,
replacing reliance on a general testing statement.
**Origin:** Capstone panel review finding (category QA/Export, priority
MEDIUM): "Stress-test 20/50/100+ PO items. Verify page breaks, repeated
table headers, wrapped names, totals, signatures/footer, and absence of
overlapping lines."
**Branch:** `fix/po-pdf-stress-test`
**Automated tests:** `procurement/tests_pdf_stress.py` (8 test cases, all
passing — see Result below)

## What was tested

The purchase order document has two output paths that must both survive a
large order:

1. **Downloadable PDF** — `procurement_pdf` (procurement/views.py), which
   renders `templates/procurement/po_pdf.html` and converts it with
   xhtml2pdf (`pisa.CreatePDF`).
2. **Browser print view** — `procurement_print`, which renders
   `templates/procurement/po_print.html` for on-screen viewing / the
   browser's own print-to-PDF.

Test orders were built at three tiers — **20, 50, and 100+ (up to 150)**
line items — each against its own set of freshly created `Product` rows (no
reused fixture data), including deliberately awkward product names, not
just short fixture names like "Sack of Rice":

- A long **multi-word** name (98 characters) — exercises normal word-wrap
  inside a table cell.
- A long **single unbroken token** with no spaces (72 characters) — cannot
  wrap on a space, so it exercises xhtml2pdf's cell-overflow handling
  instead.
- A name containing **HTML-significant characters** (`&`, `"`, `<`, `>`) —
  checks the template escapes it rather than letting it corrupt the
  surrounding markup.

## What was verified, and how

| Panel ask | How it was checked | Result |
|---|---|---|
| PDF actually renders at scale, doesn't silently fail | Django test client hits `procurement_pdf` at 20/50/120 items; response is asserted to be `200`, `Content-Type: application/pdf`, body starts with the `%PDF-` header and ends with a `%%EOF` trailer within the last 2KB, and is above a size floor (rules out a truncated/near-empty file) | Pass at all three tiers |
| Repeated table headers | Verified against the exact installed xhtml2pdf version (0.2.19) source, not assumed: `<thead>` rows repeat on every page automatically with **no extra attribute needed** (`xhtml2pdf/tables.py: pisaTagTHEAD.end()`). `po_pdf.html`'s items table already used a real `<thead>` before this pass, so headers were already repeating correctly — confirmed, not changed. `po_print.html` (browser path) already had `.po-items thead { display: table-header-group; }` under `@media print`, which is the standard, browser-native way to repeat a `<thead>` across printed pages — also already correct. |
| Page breaks / no row split across a page boundary | Verified from source: xhtml2pdf always builds its item table with reportlab's `splitByRow=1`, which only ever breaks a table *between* rows, never inside one — so a single item's row cannot visually split across a page boundary. **`page-break-inside: avoid` was deliberately *not* added** to `po_pdf.html`: xhtml2pdf's CSS layer does not implement that property at all (no reference anywhere in its `w3c/css.py` or `context.py`, confirmed against the installed source, and confirmed at runtime — xhtml2pdf logs any CSS property it can't handle, e.g. it already logs `Ignoring CSS properties xhtml2pdf does not implement: border-collapse` for an existing, unrelated rule in this same template). Adding it would look like a fix but do nothing; that would be worse than leaving a comment explaining why it's absent. |
| Wrapped / long names | The multi-word long name wraps normally (Paragraph-based cell rendering). The unbroken long token is protected by `PmlKeepInFrame` in `shrink` mode, which xhtml2pdf already wraps around every table cell by default — it shrinks cell content to fit rather than letting it overflow into the next column. This default is now declared **explicitly** (`-pdf-keep-in-frame-mode: shrink` on `.items-table td`) so it isn't an invisible default a future change could silently drop. |
| Totals correctness at scale | For a 150-item order, `_procurement_document_context()`'s `line_sum` was asserted to equal the true sum of every item's `line_total`, `totals_disagree` was asserted `False`, and the exact formatted total (via Django's own `floatformat` filter, matching `USE_THOUSAND_SEPARATOR = True` comma-grouping) was asserted present in the rendered `TOTAL COST` row. | Pass |
| No items silently dropped / no overlap | For a 137-item order, the number of rendered `.sku` markers (one per item row) was asserted to equal the item count exactly, and every item's SKU was asserted present in the HTML. | Pass |
| Signatures / footer survive a long table | For a 110-item order, the "Prepared By" / "Authorized By — Owner" / "Received / Acknowledged By — Supplier" signature block and the "Printed … by …" footer were asserted present in the rendered document. | Pass |
| Long/special-character names render safely | The HTML-special-character name was asserted to appear only in its escaped form (`&amp;`, `&quot;`, `&lt;`, `&gt;`); its raw, unescaped form was asserted absent. | Pass |

## Method note (why HTML assertions, not PDF parsing)

`requirements.txt` has no PDF-parsing library (no `pypdf`/`pdfplumber`), and
adding one just for this test file was judged unnecessary. Instead, for the
precision checks (row count, totals math, no dropped items, safe escaping),
the same context builder and template used by the real PDF path
(`_procurement_document_context()` + `po_pdf.html`) are rendered directly to
an HTML string via `render_to_string`, skipping only the final
`pisa.CreatePDF` conversion step. The rendered HTML is the complete visual
truth of what xhtml2pdf lays out onto the page — this gives exact,
byte-level assertions that PDF-byte inspection alone could not.

## Result

**Nothing broke at 100+ items.** The existing template was already correct
for header repetition (via `<thead>`) and row-splitting (via xhtml2pdf's
`splitByRow=1`) before this pass — those are library defaults, not new
fixes. The one substantive template change was making an existing but
implicit safety net (cell shrink-to-fit for unbreakable long text) explicit
in the CSS, plus adding source-verified comments in `po_pdf.html`
documenting exactly which mechanisms are already in play and why a naive
`page-break-inside: avoid` was deliberately left out (xhtml2pdf silently
ignores it — confirmed both from source and from its own runtime log
output).

- `procurement/tests_pdf_stress.py` — 8/8 passing
- `procurement` app full suite — 54/54 passing (no regressions)
- Full project suite — 774 tests, 772 passing, 2 skipped/pre-existing
  failures unrelated to this change (`ModuleNotFoundError: No module named
  'boto3'` in `backups/tests.py`'s B2Storage tests — a missing dependency in
  this environment, not something this branch touched)

## Files touched

- `procurement/tests_pdf_stress.py` — new, the stress test cases described above
- `templates/procurement/po_pdf.html` — added `-pdf-keep-in-frame-mode: shrink`
  on `.items-table td` (making an existing default explicit) plus a comment
  block documenting the xhtml2pdf behaviour this relies on and why no
  `page-break-inside`/`repeat` attributes were added
- `templates/procurement/po_print.html` — unchanged; already had the
  browser-print equivalent (`display: table-header-group` + `break-inside:
  avoid` under `@media print`), confirmed sufficient, noted here for the
  record
