# HBase Read Result Formatter

A single-file CLI that runs `get`/`scan` queries through `hbase shell` and turns
the raw output into a readable table, CSV, or a vertical one-cell-per-line view.
Query templates live in a YAML config, so switching environments (namespaces),
row keys, and filter values is all done from the command line — no code or
config edits per run.

```
$ python hbase_format.py --table user_table --namespace prod --query scan_all

rowkey      col1    col2    col3
----------  ------  ------  ------
row_key_1   val11   val12   -
row_key_2   val21   -       val23
```

## Requirements

- Python 3.7+
- `hbase shell` on the PATH of the machine you run this on (an edge node)
- [PyYAML](https://pypi.org/project/PyYAML/) for YAML configs: `pip install pyyaml`
  (`.json` configs work with the standard library alone)

## Quick start

1. Describe your tables and queries in `config.yaml` (kept next to the script):

   ```yaml
   hbase_shell_cmd: hbase shell -n     # optional, this is the default
   default_namespace: default          # used when --namespace is not passed
   row_label: rowkey                   # heading for the row-key column

   tables:
     user_table:
       # Optional. Omit (or set to `auto`) to show whatever columns
       # come back. A whitespace-separated block needs no quotes,
       # commas, or dashes - convenient for very wide tables.
       columns: >
         cf1:col1 cf1:col2 cf1:col3
       # Optional named groups, selectable with --columns
       column_groups:
         basic: cf1:col1 cf1:col2
       queries:
         get_by_key: get '{namespace}:user_table', '{rowkey}'
         scan_all: scan '{namespace}:user_table'
   ```

2. Run a query:

   ```sh
   python hbase_format.py --table user_table --query get_by_key --param rowkey=row_key_1
   ```

## Query templates

Templates are ordinary hbase shell commands with `{name}` placeholders:

- `{namespace}` is filled from `--namespace`, falling back to
  `default_namespace` in the config. This lets one config serve every
  environment (`--namespace prod`, `--namespace uat`, ...).
- Any other `{name}` is filled from `--param name=value`.
- Literal braces in filters (e.g. `{FILTER=>"..."}`) are left alone — only
  `{identifier}`-shaped tokens are treated as placeholders.
- Parameter values are escaped for JRuby single-quoted strings, so quotes in
  a value cannot alter the query. Values placed inside a `regexstring:`
  comparator are still regexes: metacharacters change what the filter matches.

Example with a filter:

```yaml
scan_by_fileid: scan '{namespace}:file_table', {FILTER=>"SingleColumnValueFilter('cf1','fileId',=,'regexstring:.*{val}.*', true, true)"}
```

## Fetching multiple row keys

Repeat one `--param` name with different values to run the query once per
value:

```sh
python hbase_format.py --table user_table --query get_by_key \
    --param rowkey=rk1 --param rowkey=rk2 --param rowkey=rk3
```

All the queries are batched into a **single** `hbase shell` session (one JVM
startup instead of N) and the results are merged into one output, each `get`
result labeled with its own row key. This works for any single repeated
param — e.g. repeating `--param val=` runs one filtered scan per value.
Repeating two different params at once is rejected, as is combining repeated
values with `--from-file`. `--timeout` covers the whole batch.

## Choosing columns

- **Configured list**: the `columns` entry fixes which columns are shown and
  their order.
- **Auto-discovery**: omit `columns` (or set `columns: auto`) and the output
  shows whatever columns appear in the result, in first-seen order.
  `--all-columns` forces this even when a list is configured.
- **Per-run selection**: `--columns col1,cf1:col2,basic` takes a
  comma-separated mix of bare qualifiers (family prefix resolved
  automatically), full `cf:qualifier` names, and `column_groups` names.

## Output formats

| Flag         | Format                                                        |
|--------------|---------------------------------------------------------------|
| *(default)*  | Aligned text table, one row per row key                       |
| `--csv`      | CSV with a header row                                         |
| `--vertical` | One cell per line, one block per row key (like MySQL's `\G`); add `--skip-missing` to omit cells a row has no value for |

The row-key heading (`rowkey` by default) is set by `row_label` in the config.
Missing cells show as `-` in tables and empty fields in CSV.

## Parsing captured output

Already have the shell output in a file? Skip execution and just format it:

```sh
python hbase_format.py --table user_table --from-file result.txt
cat result.txt | python hbase_format.py --table user_table --from-file -
```

Both `scan` and `get` output are recognized, including values wrapped across
lines. Shell noise (headers, `Took ... seconds`, row counts, SLF4J/log4j
chatter) is filtered out — but only after a line has failed to parse as data,
so row keys that happen to start with words like `INFO` are never lost. Lines
that match no known format produce a warning on stderr so silent data loss
can't hide.

## Exit codes

| Code | Meaning                                          |
|------|--------------------------------------------------|
| 0    | Success (including a query that returned 0 rows) |
| 1    | Usage or configuration error                     |
| 2    | Query execution error (shell failure or timeout) |
