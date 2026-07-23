#!/usr/bin/env python3
"""
HBase Read/Scan result formatter.

Executes get/scan queries defined in a config file via `hbase shell`,
parses the raw shell output, and renders one table row per HBase row key.

Usage:
    python hbase_format.py --table user_table --query scan_by_fileid --param val=xxx
    python hbase_format.py --table user_table --query get_by_key --param rowkey=row_key_1
    python hbase_format.py --table user_table --from-file result.txt
    cat result.txt | python hbase_format.py --table user_table --from-file -

Config file (default: config.json next to this script):
{
  "hbase_shell_cmd": "hbase shell -n",          // optional, this is the default
  "tables": {
    "user_table": {
      "columns": ["cf1:col1", "cf1:col2"],
      "queries": {
        "get_by_key": "get 'user_table', '{rowkey}'",
        "scan_all":   "scan 'user_table'"
      }
    }
  }
}

Parameter values are escaped for JRuby single-quoted strings before
substitution, so quotes in a value cannot alter the query structure.
Values substituted inside a regexstring comparator are still regexes:
metacharacters in the value change what the filter matches.

Stdlib only. Exit codes: 0 ok, 1 usage/config error, 2 query execution error.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Scan format:  row_key   column=cf1:col1, timestamp=..., value=val11
SCAN_RE = re.compile(
    r"^\s*(?P<row>\S+)\s+column=(?P<col>[^,]+),\s*timestamp=(?P<ts>[^,]+),\s*value=(?P<val>.*)$"
)

# Get format:   cf1:col1   timestamp=..., value=val1
# The qualifier may be empty (a get on a bare column family prints 'cf1:').
GET_RE = re.compile(
    r"^\s*(?P<col>[^\s:]+:[^\s,]*)\s+timestamp=(?P<ts>[^,]+),\s*value=(?P<val>.*)$"
)

# Lines to ignore entirely (headers, summaries, shell noise). Checked only
# AFTER a line has failed to parse as data, so a row key that happens to
# start with one of these words can never be discarded.
NOISE_RE = re.compile(
    r"^\s*(ROW\s+COLUMN\+CELL|COLUMN\s+CELL|\d+\s+row\(s\)|Took\s|hbase\S*[:>]"
    r"|SLF4J|log4j|(WARN|INFO)\b|ERROR:?(\s|$))",
    re.IGNORECASE,
)


def parse_result(text, default_rowkey="row"):
    """Parse raw HBase shell get/scan output.

    Returns (rows, row_order, unparsed) where rows = {rowkey: {column: value}},
    row_order preserves first-seen order of row keys, and unparsed counts
    non-empty lines that matched no known format (data, noise, continuation).
    Handles values wrapped across lines by appending continuation lines
    to the previously parsed cell.
    """
    rows = {}
    row_order = []
    unparsed = 0
    last_cell = None  # (rowkey, column) of the most recently parsed cell

    def put(rowkey, col, val):
        nonlocal last_cell
        if rowkey not in rows:
            rows[rowkey] = {}
            row_order.append(rowkey)
        rows[rowkey][col] = val
        last_cell = (rowkey, col)

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            last_cell = None
            continue

        m = SCAN_RE.match(line)
        if m:
            put(m.group("row"), m.group("col").strip(), m.group("val"))
            continue

        m = GET_RE.match(line)
        if m:
            put(default_rowkey, m.group("col").strip(), m.group("val"))
            continue

        if NOISE_RE.match(line):
            last_cell = None
            continue

        # Continuation of a wrapped value: indented line that matched
        # nothing above. Append to the last cell if we have one.
        if last_cell and raw_line.startswith((" ", "\t")):
            rk, col = last_cell
            rows[rk][col] += line.lstrip()
            continue

        unparsed += 1
        last_cell = None

    return rows, row_order, unparsed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def display_name(column):
    """Header label without the column family prefix: 'cf1:col1' -> 'col1'."""
    return column.split(":", 1)[1] if ":" in column else column


def render_table(rows, row_order, columns, missing="-"):
    """Render an aligned text table: first column ROW, then given columns."""
    headers = ["ROW"] + [display_name(c) for c in columns]
    body = [[rk] + [rows[rk].get(c, missing) for c in columns] for rk in row_order]

    widths = [len(h) for h in headers]
    for line in body:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells):
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

    out = [fmt(headers), fmt(["-" * w for w in widths])]
    out.extend(fmt(line) for line in body)
    return "\n".join(out)


def render_csv(rows, row_order, columns, missing=""):
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ROW"] + [display_name(c) for c in columns])
    for rk in row_order:
        w.writerow([rk] + [rows[rk].get(c, missing) for c in columns])
    return buf.getvalue().rstrip("\n")


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def escape_param(value):
    """Escape a value for interpolation inside a single-quoted JRuby string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_query(table_cfg, table_name, query_name, params):
    queries = table_cfg.get("queries", {})
    if not queries:
        die(f"No queries defined for table '{table_name}' in config.")

    if query_name is None:
        if len(queries) == 1:
            query_name = next(iter(queries))
        else:
            die(
                f"Table '{table_name}' has multiple queries; pick one with --query.\n"
                f"Available: {', '.join(queries)}"
            )
    if query_name not in queries:
        die(
            f"Query '{query_name}' not found for table '{table_name}'.\n"
            f"Available: {', '.join(queries)}"
        )

    query = queries[query_name]

    # Substitute {name} placeholders. Deliberately NOT str.format(): HBase
    # queries contain literal braces, e.g. {FILTER=>"..."}, which only match
    # the placeholder pattern if they look exactly like {identifier}.
    placeholder = re.compile(r"\{([A-Za-z_]\w*)\}")
    query = placeholder.sub(
        lambda m: escape_param(params[m.group(1)]) if m.group(1) in params else m.group(0),
        query,
    )

    unfilled = sorted(set(placeholder.findall(query)) - set(params))
    if unfilled:
        die(
            f"Query '{query_name}' needs parameter(s): {', '.join(unfilled)}. "
            f"Pass with --param name=value."
        )
    return query


def run_query(shell_cmd, query, timeout):
    cmd = shlex.split(shell_cmd)
    try:
        proc = subprocess.run(
            cmd,
            input=query + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        die(
            f"Could not find HBase shell command: {cmd[0]!r}.\n"
            f"Set \"hbase_shell_cmd\" in the config file to the correct path.",
            code=2,
        )
    except subprocess.TimeoutExpired:
        die(f"Query timed out after {timeout}s: {query}", code=2)

    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        die(f"hbase shell exited with code {proc.returncode} for query: {query}", code=2)
    return proc.stdout


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def die(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def load_config(path):
    p = Path(path)
    if not p.exists():
        die(f"Config file not found: {p}")
    try:
        cfg = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in {p}: {e}")
    if "tables" not in cfg:
        die('Config must have a top-level "tables" object.')
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Execute and format HBase get/scan results.")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"),
                    help="Path to config.json (default: next to this script)")
    ap.add_argument("--table", required=True, help="Table name as defined in the config")
    ap.add_argument("--query", help="Query name from the config (optional if only one)")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                    help="Placeholder value for the query template (repeatable)")
    ap.add_argument("--from-file", metavar="FILE",
                    help="Skip execution; parse a captured result file instead ('-' for stdin)")
    ap.add_argument("--rowkey", default="row",
                    help="Row key label for 'get' results, which don't include one")
    ap.add_argument("--csv", action="store_true", help="Output CSV instead of a text table")
    ap.add_argument("--timeout", type=int, default=120, help="Query timeout in seconds")
    args = ap.parse_args()

    cfg = load_config(args.config)
    tables = cfg["tables"]
    if args.table not in tables:
        die(f"Table '{args.table}' not in config. Available: {', '.join(tables)}")
    table_cfg = tables[args.table]

    columns = table_cfg.get("columns")
    if not columns:
        die(f"No \"columns\" defined for table '{args.table}' in config.")

    params = {}
    for item in args.param:
        if "=" not in item:
            die(f"--param must be NAME=VALUE, got: {item}")
        k, v = item.split("=", 1)
        params[k] = v

    # If the user passed a rowkey param, reuse it as the label for get results.
    rowkey_label = params.get("rowkey", args.rowkey)

    if args.from_file:
        if args.from_file == "-":
            raw = sys.stdin.read()
        else:
            p = Path(args.from_file)
            if not p.exists():
                die(f"Result file not found: {p}")
            raw = p.read_text()
    else:
        shell_cmd = cfg.get("hbase_shell_cmd", "hbase shell -n")
        query = build_query(table_cfg, args.table, args.query, params)
        raw = run_query(shell_cmd, query, args.timeout)

    rows, row_order, unparsed = parse_result(raw, default_rowkey=rowkey_label)
    if unparsed:
        sys.stderr.write(
            f"Warning: {unparsed} line(s) did not match any known format "
            f"and were skipped; the table may be incomplete.\n"
        )
    if not rows:
        sys.stderr.write("No cells found in the result. (0 rows, or unrecognized output format.)\n")
        sys.exit(0)

    if args.csv:
        print(render_csv(rows, row_order, columns))
    else:
        print(render_table(rows, row_order, columns))


if __name__ == "__main__":
    main()
