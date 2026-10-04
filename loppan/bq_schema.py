"""Read deploy/bigquery/schema.sql and check NDJSON rows against it.

schema.sql is the contract between the fetchers and BigQuery. Rather than keep a
second copy of the column list here, which would drift, this parses the CREATE TABLE
statements themselves, then the `ALTER TABLE ... ADD COLUMN` statements appended
below them. It understands exactly the subset schema.sql uses: scalar types,
ARRAY<...>, STRUCT<...>, NOT NULL and OPTIONS (...).

What a row must look like for `bq load --source_format=NEWLINE_DELIMITED_JSON`, as
checked here:

  * every column present as a key, and no key that is not a column
  * NOT NULL columns non-null
  * INT64 a JSON integer, FLOAT64 a number, BOOL true/false, STRING a string
  * DATE "YYYY-MM-DD"; TIMESTAMP ISO-8601 with an explicit UTC zone (Z or +00:00)
  * ARRAY a JSON array (never null: BigQuery stores no null arrays), no null elements
  * STRUCT an object with exactly the struct's fields

    python loppan/bq_schema.py                       # print every table
    python loppan/bq_schema.py sweep_staging         # one table
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "deploy" / "bigquery" / "schema.sql"

SCALARS = {"STRING", "INT64", "FLOAT64", "BOOL", "DATE", "TIMESTAMP"}
INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1


@dataclass
class Column:
    name: str
    type: str                     # STRING, INT64, ..., or STRUCT
    mode: str                     # NULLABLE | REQUIRED | REPEATED
    fields: list["Column"] = field(default_factory=list)   # for STRUCT

    def describe(self) -> str:
        inner = ""
        if self.fields:
            inner = "<" + ", ".join(f"{f.name} {f.describe()}" for f in self.fields) + ">"
        return f"{self.type}{inner} {self.mode}"


# ---------------------------------------------------------------- parsing


def _strip_comments(sql: str) -> str:
    """Drop `-- ...` line comments, leaving string literals alone."""
    out, i, quote = [], 0, None
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(sql):
                out.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif sql.startswith("--", i):
            while i < len(sql) and sql[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _split_top(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` where it is not inside (), <> or a string literal."""
    parts, depth, quote, cur = [], 0, None, []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the bracket that closes the one at `start`."""
    depth, quote = 0, None
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError(f"unbalanced {open_ch}{close_ch} in schema.sql")


def _read_type(text: str) -> tuple[str, str]:
    """Split a column body into its type expression and whatever follows."""
    m = re.match(r"\s*([A-Za-z0-9_]+)", text)
    if not m:
        raise ValueError(f"no type in {text!r}")
    end = m.end()
    if end < len(text) and text[end:].lstrip().startswith("<"):
        lt = text.index("<", end)
        end = _balanced(text, lt, "<", ">")
    return text[:end].strip(), text[end:]


def _parse_type(name: str, type_expr: str, not_null: bool) -> Column:
    t = type_expr.strip()
    upper = t.upper()
    if upper.startswith("ARRAY<"):
        inner = _parse_type(name, t[t.index("<") + 1:t.rindex(">")], False)
        return Column(name, inner.type, "REPEATED", inner.fields)
    if upper.startswith("STRUCT<"):
        body = t[t.index("<") + 1:t.rindex(">")]
        fields = []
        for part in _split_top(body):
            fname, ftype = part.split(None, 1)
            fields.append(_parse_type(fname, ftype, False))
        return Column(name, "STRUCT", "REQUIRED" if not_null else "NULLABLE", fields)
    if upper not in SCALARS:
        raise ValueError(f"column {name}: type {t!r} is not one this parser knows")
    return Column(name, upper, "REQUIRED" if not_null else "NULLABLE")


def _parse_column(definition: str) -> Column:
    name, rest = definition.split(None, 1)
    type_expr, tail = _read_type(rest)
    # OPTIONS (...) may hold anything, including the words NOT NULL, so cut it first.
    o = re.search(r"\bOPTIONS\s*\(", tail, re.IGNORECASE)
    if o:
        close = _balanced(tail, tail.index("(", o.start()), "(", ")")
        tail = tail[:o.start()] + tail[close:]
    not_null = re.search(r"\bNOT\s+NULL\b", tail, re.IGNORECASE) is not None
    return _parse_type(name, type_expr, not_null)


# The tables the fetchers write. Only these must parse; anything else in schema.sql
# (items, parameter tables, later ALTERs) is the MERGE's business, not the loader's,
# and a type this parser does not know there must not break validation here.
CONTRACT = ("sweep_staging", "adjudication_staging", "circle_origin_staging", "runs",
            "brand_counts_staging")


def _statements(sql: str) -> list[str]:
    """Split comment-free SQL on the semicolons that end statements. Only quotes are
    tracked: `<` and `>` are comparisons outside a type, so _split_top would miscount."""
    parts, cur, quote, i = [], [], None, 0
    while i < len(sql):
        ch = sql[i]
        cur.append(ch)
        if quote:
            if ch == "\\" and i + 1 < len(sql):
                cur.append(sql[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == ";":
            parts.append("".join(cur[:-1]).strip())
            cur = []
        i += 1
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


_CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)\s*\(", re.IGNORECASE)
_ALTER = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.]+)\s+(.*)",
                    re.IGNORECASE | re.DOTALL)
_ADD = re.compile(r"ADD\s+COLUMN\s+(IF\s+NOT\s+EXISTS\s+)?(.*)", re.IGNORECASE | re.DOTALL)
# Actions that leave every column's name, type and mode as they were.
_HARMLESS = re.compile(r"(SET\s+OPTIONS\b|SET\s+DEFAULT\s+COLLATE\b|ALTER\s+COLUMN\s+"
                       r"(?:IF\s+EXISTS\s+)?\w+\s+(?:SET\s+OPTIONS|SET\s+DEFAULT|DROP\s+DEFAULT)\b)",
                       re.IGNORECASE)


def _alter(name: str, actions: str, columns: list[Column]) -> None:
    """Apply one contract table's ALTER TABLE to its column list, in place.

    ADD COLUMN appends, as BigQuery does. An action that only sets options or a
    default changes nothing a row must carry. Anything else (DROP, RENAME, SET DATA
    TYPE, DROP NOT NULL) is raised: guessing would validate rows against the wrong
    table.
    """
    for action in _split_top(actions):
        add = _ADD.match(action)
        if add:
            col = _parse_column(add.group(2))
            if any(c.name.lower() == col.name.lower() for c in columns):
                if add.group(1):
                    continue           # IF NOT EXISTS: already there, a no-op
                raise ValueError(f"ALTER TABLE {name}: column {col.name} already exists")
            columns.append(col)
        elif not _HARMLESS.match(action):
            raise ValueError(f"ALTER TABLE {name}: '{' '.join(action.split()[:3])} ...' "
                             f"is not an action this parser knows")


def load(path: pathlib.Path = SCHEMA_SQL) -> dict[str, list[Column]]:
    """Table name (without dataset) -> its columns, in declaration order.

    Reads CREATE TABLE statements, then ALTER TABLE statements on the contract
    tables, in file order. Every other statement (MERGE, UPDATE, an ALTER on another
    table) is ignored. A non-contract table that does not parse is left out rather
    than raised; a contract table that does not parse is an error.
    """
    tables: dict[str, list[Column]] = {}
    for stmt in _statements(_strip_comments(path.read_text(encoding="utf-8"))):
        m = _CREATE.match(stmt)
        if m:
            open_at = m.end() - 1
            body = stmt[open_at + 1:_balanced(stmt, open_at, "(", ")") - 1]
            name = m.group(1).split(".")[-1]
            try:
                tables[name] = [_parse_column(part) for part in _split_top(body)]
            except ValueError:
                if name in CONTRACT:
                    raise
            continue
        m = _ALTER.match(stmt)
        if m and m.group(1).split(".")[-1] in CONTRACT:
            name = m.group(1).split(".")[-1]
            if name not in tables:
                raise ValueError(f"schema.sql alters {name} before creating it")
            _alter(name, m.group(2), tables[name])
    missing = [t for t in CONTRACT if t not in tables]
    if missing:
        raise ValueError(f"schema.sql has no CREATE TABLE for {missing}")
    return tables


# ---------------------------------------------------------------- checking


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|\+00:00)$")


def _scalar_error(col: Column, v, where: str) -> str | None:
    t = col.type
    if t == "STRING":
        return None if isinstance(v, str) else f"{where}: STRING expected, got {type(v).__name__}"
    if t == "INT64":
        if isinstance(v, bool) or not isinstance(v, int):
            return f"{where}: INT64 expected, got {type(v).__name__} {v!r}"
        if not INT64_MIN <= v <= INT64_MAX:
            return f"{where}: {v} is outside INT64"
        return None
    if t == "FLOAT64":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"{where}: FLOAT64 expected, got {type(v).__name__} {v!r}"
        return None
    if t == "BOOL":
        return None if isinstance(v, bool) else f"{where}: BOOL expected, got {type(v).__name__} {v!r}"
    if t == "DATE":
        if not isinstance(v, str) or not _DATE.match(v):
            return f"{where}: DATE expected as YYYY-MM-DD, got {v!r}"
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            return f"{where}: {v!r} is not a real date"
        return None
    if t == "TIMESTAMP":
        if not isinstance(v, str) or not _TS.match(v):
            return f"{where}: TIMESTAMP expected as ISO-8601 UTC (…Z or …+00:00), got {v!r}"
        try:
            dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return f"{where}: {v!r} is not a real timestamp"
        return None
    return f"{where}: unsupported type {t}"


def _value_errors(col: Column, v, where: str) -> list[str]:
    if col.mode == "REPEATED":
        if not isinstance(v, list):
            return [f"{where}: ARRAY expected (use [] for none; BigQuery has no null "
                    f"arrays), got {type(v).__name__}"]
        errs = []
        for i, el in enumerate(v):
            if el is None:
                errs.append(f"{where}[{i}]: null element (BigQuery rejects these)")
            else:
                errs.extend(_one(col, el, f"{where}[{i}]"))
        return errs
    if v is None:
        return [f"{where}: NOT NULL column is null"] if col.mode == "REQUIRED" else []
    return _one(col, v, where)


def _one(col: Column, v, where: str) -> list[str]:
    if col.type == "STRUCT":
        if not isinstance(v, dict):
            return [f"{where}: STRUCT expected as an object, got {type(v).__name__}"]
        return _row_errors(col.fields, v, where + ".")
    e = _scalar_error(col, v, where)
    return [e] if e else []


def _row_errors(columns: list[Column], row: dict, prefix: str = "") -> list[str]:
    errs = []
    names = {c.name for c in columns}
    for key in row:
        if key not in names:
            errs.append(f"{prefix}{key}: not a column")
    for col in columns:
        if col.name not in row:
            errs.append(f"{prefix}{col.name}: missing")
            continue
        errs.extend(_value_errors(col, row[col.name], prefix + col.name))
    return errs


def row_errors(row: dict, columns: list[Column]) -> list[str]:
    """Everything wrong with one row; empty when it loads cleanly."""
    if not isinstance(row, dict):
        return [f"row is a {type(row).__name__}, not an object"]
    return _row_errors(columns, row)


def validate_file(path: pathlib.Path, table: str, max_errors: int = 20,
                  schema: dict[str, list[Column]] | None = None) -> dict:
    """Check every line of an NDJSON file. Returns a summary; `ok` says it all."""
    schema = schema or load()
    if table not in schema:
        raise SystemExit(f"no table {table!r} in schema.sql; have {sorted(schema)}")
    columns = schema[table]
    rows = bad_rows = 0
    errors: list[str] = []
    size = 0
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            size += len(raw)
            if not raw.strip():
                continue
            rows += 1
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                bad_rows += 1
                if len(errors) < max_errors:
                    errors.append(f"line {n}: not JSON ({exc})")
                continue
            errs = row_errors(row, columns)
            if errs:
                bad_rows += 1
                for e in errs:
                    if len(errors) < max_errors:
                        errors.append(f"line {n}: {e}")
    return {"file": str(path), "table": table, "rows": rows, "bad_rows": bad_rows,
            "bytes": size, "bytes_per_row": round(size / rows, 1) if rows else None,
            "ok": bad_rows == 0, "errors": errors}


def main() -> None:
    schema = load()
    wanted = sys.argv[1:] or list(schema)
    for name in wanted:
        print(name)
        for c in schema[name]:
            print(f"  {c.name:18s} {c.describe()}")


if __name__ == "__main__":
    main()
