"""Reading custodian files: text decoding, header matching, and the number/date spellings custodians use."""

import csv
import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


class ParseError(ValueError):
    pass


def decode(content: bytes | str) -> str:
    if isinstance(content, str):
        return content
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def norm_account(value: str) -> str:
    """Account numbers compare without spaces, dashes or case: '4BX-10 4417' == '4bx104417'."""
    return re.sub(r"[\s\-./]", "", value).upper()


def norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


_NEG_PARENS = re.compile(r"^\((.*)\)$")


def parse_decimal(raw: str) -> Decimal | None:
    """'1,234.56', '$1,234.56', '(1,234.56)', '1,234.56-', '-1234.56' -> Decimal. Blank -> None."""
    text = raw.strip().replace(",", "").replace("$", "").replace(" ", "")
    if text in ("", "-"):
        return None
    negative = False
    if m := _NEG_PARENS.match(text):
        text, negative = m.group(1), True
    elif text.endswith("-"):
        text, negative = text[:-1], True
    try:
        value = Decimal(text)
    except InvalidOperation as e:
        raise ParseError(f"{raw.strip()!r} is not a number") from e
    if not value.is_finite():
        raise ParseError(f"{raw.strip()!r} is not a number")
    return -value if negative else value


def parse_date(raw: str, formats: tuple[str, ...]) -> date | None:
    text = raw.strip()
    if not text:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"{text!r} is not a date in the expected format ({', '.join(formats)})")


@dataclass
class Table:
    headers: dict[str, int]  # normalized header -> column index
    rows: list[tuple[int, list[str]]]  # (line number in the file, cells); blank lines dropped


def read_table(text: str, *, delimiter: str, skip_rows: int) -> Table:
    lines = text.splitlines()[skip_rows:]
    reader: Iterator[list[str]] = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return Table({}, [])
    headers: dict[str, int] = {}
    for i, h in enumerate(header):
        headers.setdefault(norm_header(h), i)
    rows = []
    for cells in reader:
        if any(c.strip() for c in cells):
            rows.append((reader.line_num + skip_rows, cells))  # type: ignore[attr-defined]
    return Table(headers, rows)
