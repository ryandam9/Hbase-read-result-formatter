#!/usr/bin/env python3
"""
HBase Read/Scan result formatter.

Executes get/scan queries defined in a config file via `hbase shell`,
parses the raw shell output, and renders the result as a table, CSV,
or a vertical one-cell-per-line listing.

Usage:
    python hbase_format.py --table user_table --namespace prod --query scan_all
    python hbase_format.py --table user_table --query get_by_key --param rowkey=row_key_1
    python hbase_format.py --table user_table --query get_by_key \
        --param rowkey=rk1 --param rowkey=rk2 --param rowkey=rk3
    python hbase_format.py --table user_table --from-file result.txt --vertical
    cat result.txt | python hbase_format.py --table user_table --from-file -

Repeating one --param with different values (typically rowkey) runs the
query once per value, batched into a single hbase shell session, and
merges the results into one output; each 'get' result is labeled with
its own row key value.

Config file (default: config.yaml next to this script; JSON also accepted):

    hbase_shell_cmd: hbase shell -n        # optional, this is the default
    default_namespace: default             # used for '{namespace}' when
                                           # --namespace is not passed
    tables:
      user_table:
        # Columns: a YAML list, or a whitespace-separated block (handy
        # for tables with hundreds of columns - no quotes or commas):
        columns: >
          cf1:col1 cf1:col2
        queries:
          get_by_key: get '{namespace}:user_table', '{rowkey}'
          scan_all: scan '{namespace}:user_table'

'{namespace}' in a query template is filled from --namespace, falling
back to "default_namespace" in the config. Other '{name}' placeholders
are filled from --param name=value.

Parameter values are escaped for JRuby single-quoted strings before
substitution, so quotes in a value cannot alter the query structure.
Values substituted inside a regexstring comparator are still regexes:
metacharacters in the value change what the filter matches.

Requires PyYAML for YAML configs (pip install pyyaml); JSON configs
work with the stdlib alone.
Exit codes: 0 ok, 1 usage/config error, 2 query execution error.
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


def render_table(rows, row_order, columns, missing="-", row_label="ROW"):
    """Render an aligned text table: row-key column first, then given columns."""
    headers = [row_label] + [display_name(c) for c in columns]
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


def render_vertical(rows, row_order, columns, missing="-", row_label="ROW"):
    """Render one cell per line, one block per row key (like MySQL's \\G)."""
    names = [display_name(c) for c in columns]
    width = max(len(n) for n in names)
    blocks = []
    for rk in row_order:
        lines = [f"{row_label}: {rk}"]
        lines.extend(
            f"  {name.ljust(width)} : {rows[rk].get(col, missing)}"
            for col, name in zip(columns, names)
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_csv(rows, row_order, columns, missing="", row_label="ROW"):
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([row_label] + [display_name(c) for c in columns])
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
        hints = []
        if "namespace" in unfilled:
            hints.append("Pass --namespace, or set \"default_namespace\" in the config.")
            unfilled.remove("namespace")
        if unfilled:
            hints.append(f"Pass {', '.join(unfilled)} with --param name=value.")
        die(f"Query '{query_name}' needs parameter(s). " + " ".join(hints))
    return query


# Printed via `puts` between batched queries so the combined shell output
# can be split back into one segment per query. The label after the prefix
# becomes the row key label for that segment's 'get' results.
MARKER_PREFIX = "@@HBASE_FMT@@ "


def build_batch_script(labeled_queries):
    """Interleave marker `puts` lines with queries for one shell session."""
    lines = []
    for label, query in labeled_queries:
        lines.append(f"puts '{MARKER_PREFIX}{escape_param(label)}'")
        lines.append(query)
    return "\n".join(lines)


def split_on_markers(output, fallback_label):
    """Split combined shell output into [(label, text)] per marker.
    Text before the first marker (startup noise) gets the fallback label."""
    segments = []
    label, buf = fallback_label, []
    for line in output.splitlines():
        if line.startswith(MARKER_PREFIX):
            segments.append((label, "\n".join(buf)))
            label, buf = line[len(MARKER_PREFIX):], []
        else:
            buf.append(line)
    segments.append((label, "\n".join(buf)))
    return segments


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


def default_config_path():
    """First existing of config.yaml/.yml/.json next to the script,
    else config.yaml (so the not-found error names the preferred file)."""
    here = Path(__file__).parent
    for name in ("config.yaml", "config.yml", "config.json"):
        if (here / name).exists():
            return str(here / name)
    return str(here / "config.yaml")


def load_config(path):
    p = Path(path)
    if not p.exists():
        die(f"Config file not found: {p}")
    text = p.read_text()
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            die("YAML configs need PyYAML: pip install pyyaml "
                "(or use a .json config instead).")
        try:
            cfg = yaml.safe_load(text)
        except yaml.YAMLError as e:
            die(f"Invalid YAML in {p}: {e}")
    else:
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            die(f"Invalid JSON in {p}: {e}")
    if not isinstance(cfg, dict) or "tables" not in cfg:
        die('Config must have a top-level "tables" object.')
    return cfg


def normalize_columns(cols):
    """Accept a list of columns or a whitespace-separated string block."""
    if isinstance(cols, str):
        return cols.split()
    return list(cols)


def main():
    ap = argparse.ArgumentParser(description="Execute and format HBase get/scan results.")
    ap.add_argument("--config", default=default_config_path(),
                    help="Path to config.yaml/.json (default: next to this script)")
    ap.add_argument("--table", required=True, help="Table name as defined in the config")
    ap.add_argument("--query", help="Query name from the config (optional if only one)")
    ap.add_argument("--namespace", metavar="NAME",
                    help="Value for '{namespace}' in query templates "
                         "(default: \"default_namespace\" from the config)")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                    help="Placeholder value for the query template. Repeat one "
                         "name with different values (e.g. several rowkeys) to "
                         "run the query once per value in a single shell session.")
    ap.add_argument("--from-file", metavar="FILE",
                    help="Skip execution; parse a captured result file instead ('-' for stdin)")
    ap.add_argument("--rowkey", default="row",
                    help="Row key label for 'get' results, which don't include one")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--csv", action="store_true", help="Output CSV instead of a text table")
    fmt.add_argument("--vertical", action="store_true",
                     help="Output one cell per line, one block per row key")
    ap.add_argument("--timeout", type=int, default=120,
                    help="Timeout in seconds for the whole shell session "
                         "(covers all repeated-param queries)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    tables = cfg["tables"]
    if args.table not in tables:
        die(f"Table '{args.table}' not in config. Available: {', '.join(tables)}")
    table_cfg = tables[args.table]

    columns = normalize_columns(table_cfg.get("columns") or [])
    if not columns:
        die(f"No \"columns\" defined for table '{args.table}' in config.")

    param_values = {}
    for item in args.param:
        if "=" not in item:
            die(f"--param must be NAME=VALUE, got: {item}")
        k, v = item.split("=", 1)
        param_values.setdefault(k, []).append(v)

    multi = {k: vs for k, vs in param_values.items() if len(vs) > 1}
    if len(multi) > 1:
        die(f"Only one --param may be repeated with different values; "
            f"got multiple values for: {', '.join(multi)}")

    params = {k: vs[0] for k, vs in param_values.items()}
    namespace = args.namespace or cfg.get("default_namespace")
    if namespace is not None:
        params.setdefault("namespace", str(namespace))

    # One param set per execution: the repeated param (if any) varies,
    # everything else stays fixed.
    if multi:
        (vary_key, values), = multi.items()
        param_sets = [dict(params, **{vary_key: v}) for v in values]
    else:
        param_sets = [params]

    # If the user passed a rowkey param, reuse it as the label for get
    # results, which don't include a row key in the shell output.
    def rowkey_label(ps):
        return ps.get("rowkey", args.rowkey)

    if args.from_file:
        if multi:
            die("Repeated --param values cannot be combined with --from-file.")
        if args.from_file == "-":
            raw = sys.stdin.read()
        else:
            p = Path(args.from_file)
            if not p.exists():
                die(f"Result file not found: {p}")
            raw = p.read_text()
        segments = [(rowkey_label(params), raw)]
    else:
        shell_cmd = cfg.get("hbase_shell_cmd", "hbase shell -n")
        queries = [build_query(table_cfg, args.table, args.query, ps)
                   for ps in param_sets]
        if len(queries) == 1:
            raw = run_query(shell_cmd, queries[0], args.timeout)
            segments = [(rowkey_label(params), raw)]
        else:
            # Batch all queries into one shell session (one JVM startup),
            # with marker lines to attribute output to each query.
            labeled = list(zip((rowkey_label(ps) for ps in param_sets), queries))
            raw = run_query(shell_cmd, build_batch_script(labeled), args.timeout)
            segments = split_on_markers(raw, fallback_label=args.rowkey)

    rows, row_order, unparsed = {}, [], 0
    for label, text in segments:
        seg_rows, seg_order, seg_unparsed = parse_result(text, default_rowkey=label)
        unparsed += seg_unparsed
        for rk in seg_order:
            if rk not in rows:
                rows[rk] = {}
                row_order.append(rk)
            rows[rk].update(seg_rows[rk])

    if unparsed:
        sys.stderr.write(
            f"Warning: {unparsed} line(s) did not match any known format "
            f"and were skipped; the table may be incomplete.\n"
        )
    if not rows:
        sys.stderr.write("No cells found in the result. (0 rows, or unrecognized output format.)\n")
        sys.exit(0)

    row_label = str(cfg.get("row_label", "ROW"))
    if args.csv:
        print(render_csv(rows, row_order, columns, row_label=row_label))
    elif args.vertical:
        print(render_vertical(rows, row_order, columns, row_label=row_label))
    else:
        print(render_table(rows, row_order, columns, row_label=row_label))


if __name__ == "__main__":
    main()
