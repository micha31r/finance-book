"""Turning a PDF page into rows of columns.

The banks we support emit real text PDFs, so there is no OCR here. The one
thing text extraction cannot tell us is which column an amount sits in, and
that is exactly what decides whether $70.00 is a credit or a debit. So every
amount is placed by comparing its right edge to the column header's right
edge, and column positions are read from each page rather than hardcoded.
"""
import pymupdf

from .money import is_money

# Both tolerances are in PDF points. Lines in these statements are ~14pt apart
# and the nearest two amount columns are ~59pt apart, so 4pt is loose enough to
# absorb sub-pixel drift and far too tight to reach a neighbour.
Y_TOLERANCE = 4.0
X_TOLERANCE = 4.0


def lines(page):
    """Group a page's words into visual lines of (x0, x1, text), left to right.

    Clustering by vertical centre rather than exact y matters: ANZ Plus
    transaction lists put credit amounts on a sub-line ~2pt below their row,
    and exact grouping silently drops them.
    """
    words = sorted(page.get_text("words"), key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    out, current, centre = [], [], None
    for x0, y0, x1, y1, text, *_ in words:
        c = (y0 + y1) / 2
        if centre is None:
            centre = c
        elif abs(c - centre) > Y_TOLERANCE:
            out.append((centre, sorted(current)))
            current, centre = [], c
        current.append((x0, x1, text))
    if current:
        out.append((centre, sorted(current)))
    return out


def find_header(page_lines, amount_columns):
    """Locate the transaction table header.

    Returns (index, {column: right_edge}, description_x0) or None. Amount
    columns are keyed by their right edge because the amounts under them are
    right-aligned.
    """
    for i, (_, items) in enumerate(page_lines):
        texts = [t for _, _, t in items]
        if "Date" not in texts or not all(c in texts for c in amount_columns):
            continue
        edges = {}
        for j, (_, x1, text) in enumerate(items):
            if text not in amount_columns or text in edges:
                continue
            # "Withdrawals ($)" spreads the label over two words; the unit
            # carries the true right edge.
            nxt = items[j + 1] if j + 1 < len(items) else None
            edges[text] = nxt[1] if nxt and nxt[2] == "($)" else x1
        desc_x = next((x0 for x0, _, t in items if t in ("Description", "Transaction")), None)
        if desc_x is None or len(edges) != len(amount_columns):
            continue
        return i, edges, desc_x
    return None


def split_row(items, edges, desc_x):
    """Split one line into (left-column tokens, description words, {column: token}).

    Anything left of the description column goes in the first list. That is
    usually the date, but it is also where page footers and end-of-period
    totals start, which is how those are told apart from a wrapped description.
    """
    left, description, cells = [], [], {}
    for x0, x1, text in items:
        if x0 < desc_x - X_TOLERANCE:
            left.append(text)
            continue
        if text == "blank":     # ANZ classic writes this word into empty cells
            continue
        hit = next((c for c, edge in edges.items()
                    if is_money(text) and abs(edge - x1) <= X_TOLERANCE), None)
        if hit:
            cells[hit] = text
        else:
            description.append(text)
    return left, description, cells


def table_rows(pages, column_sets, stop_at=None):
    """Yield (left tokens, description words, cells) for every transaction line.

    The header is located again on each page, because a page may use a
    different left margin, and skipped pages carry no table at all.
    """
    for page_lines in pages:
        header = next((h for h in (find_header(page_lines, c) for c in column_sets) if h), None)
        if not header:
            continue
        index, edges, desc_x = header
        for _, items in page_lines[index + 1:]:
            if stop_at and stop_at in " ".join(t for _, _, t in items):
                break
            yield split_row(items, edges, desc_x)


def is_continuation(left, description, cells):
    """True when a line is the tail of the description above it.

    A wrapped line sits entirely inside the description column. Footer text and
    totals rows always put something in the left column, so they never qualify.
    """
    return bool(description) and not left and not cells


def open_pdf(path):
    return pymupdf.open(path)
