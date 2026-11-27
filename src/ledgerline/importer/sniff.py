"""Encoding, delimiter, table-region and header detection."""

from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass

from .amounts import looks_numeric
from .dates import CANDIDATE_FORMATS, parse_date

DELIMITERS = (",", ";", "\t", "|")


def detect_encoding(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        data.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        return "latin-1"


def decode(data: bytes) -> tuple[str, str]:
    enc = detect_encoding(data)
    text = data.decode(enc)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc


def _parse(text: str, delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"', skipinitialspace=True)
    rows = []
    for row in reader:
        rows.append([c.strip() for c in row])
    return rows


@dataclass
class TableRegion:
    delimiter: str
    rows: list[list[str]]  # all parsed rows of the file
    start: int  # first row index of the table (inclusive)
    end: int  # last row index (exclusive)
    width: int

    @property
    def body(self) -> list[list[str]]:
        return self.rows[self.start : self.end]


def _longest_run(rows: list[list[str]], width: int) -> tuple[int, int]:
    best = (0, 0)
    start = None
    for i, row in enumerate(rows + [[]]):
        fits = len(row) == width and any(c for c in row)
        if fits and start is None:
            start = i
        elif not fits and start is not None:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    return best


def find_table(text: str) -> TableRegion:
    """Pick the delimiter whose longest run of equal-width rows is the largest."""
    candidates: list[tuple[int, int, TableRegion]] = []
    for delim in DELIMITERS:
        rows = _parse(text, delim)
        widths = Counter(len(r) for r in rows if any(c for c in r) and len(r) > 1)
        if not widths:
            continue
        # Evaluate every plausible width, not only the mode (preambles can dominate small files).
        for width, _ in widths.most_common(4):
            s, e = _longest_run(rows, width)
            candidates.append((e - s, width, TableRegion(delim, rows, s, e, width)))
    if not candidates:
        rows = _parse(text, ",")
        return TableRegion(",", rows, 0, len(rows), max((len(r) for r in rows), default=1))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    region = candidates[0][2]
    _trim_sparse_edges(region)
    return region


def _trim_sparse_edges(region: TableRegion) -> None:
    """Drop totals/footer rows like ``Summe;;;;;`` that happen to have the table width."""
    body = region.body
    if len(body) < 4:
        return
    counts = sorted(sum(1 for c in r if c) for r in body)
    median = counts[len(counts) // 2]
    while region.end - region.start > 2 and sum(1 for c in region.rows[region.end - 1] if c) < 0.5 * median:
        region.end -= 1


def _is_typed(cell: str) -> bool:
    if not cell:
        return False
    if looks_numeric(cell):
        return True
    return any(parse_date(cell, f) for f in CANDIDATE_FORMATS[:12])


def detect_header(region: TableRegion) -> bool:
    """A row is a header if its cells are untyped where the body below is typed."""
    body = region.body
    if len(body) < 2:
        return False
    first, rest = body[0], body[1:51]
    if any(_is_typed(c) for c in first):
        return False
    typed_cols = 0
    for j in range(region.width):
        col = [r[j] for r in rest if r[j]]
        if col and sum(_is_typed(c) for c in col) / len(col) >= 0.8:
            typed_cols += 1
    if typed_cols:
        return True
    # All-text tables: fall back to "header cells are unique and non-empty".
    return len(set(first)) == len(first) and all(first)


@dataclass
class SniffResult:
    encoding: str
    delimiter: str
    header: list[str]
    rows: list[list[str]]
    has_header: bool
    preamble_lines: int
    footer_lines: int


def sniff(data: bytes) -> SniffResult:
    text, enc = decode(data)
    region = find_table(text)
    has_header = detect_header(region)
    body = region.body
    if has_header:
        header, rows = body[0], body[1:]
    else:
        header, rows = [f"col_{i}" for i in range(region.width)], body
    # Merge a header row that sits directly above the run but is a different width (rare)
    footer = len([r for r in region.rows[region.end :] if any(c for c in r)])
    preamble = len([r for r in region.rows[: region.start] if any(c for c in r)])
    return SniffResult(enc, region.delimiter, header, rows, has_header, preamble, footer)
