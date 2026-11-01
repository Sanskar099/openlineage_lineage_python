"""Command-line interface for generating OpenLineage JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from lineage.openlineage import build_event
from lineage.parser import LineageError, derive_column_lineage, parse_create_table


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise LineageError(f"Could not read {path}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an OpenLineage event from SQL and table DDL")
    parser.add_argument("--sql", required=True, type=Path, help="SQL file containing one SELECT statement")
    parser.add_argument(
        "--source-ddl",
        required=True,
        action="append",
        type=Path,
        help="Source CREATE TABLE file; repeat for each source table",
    )
    parser.add_argument("--target-ddl", required=True, type=Path, help="Target CREATE TABLE file")
    parser.add_argument("--namespace", required=True, help="OpenLineage namespace for datasets and job")
    parser.add_argument("--output", type=Path, default=Path("lineage.json"), help="Output JSON path")
    parser.add_argument("--dialect", default="", help="SQLGlot dialect, such as postgres or snowflake")
    parser.add_argument("--job-name", default="sql-column-lineage", help="OpenLineage job name")
    args = parser.parse_args()

    try:
        source_schemas = {}
        for ddl_path in args.source_ddl:
            schema = parse_create_table(_read_text(ddl_path), args.dialect)
            source_schemas[schema.name] = schema.columns

        target_schema = parse_create_table(_read_text(args.target_ddl), args.dialect)
        result = derive_column_lineage(
            _read_text(args.sql),
            source_schemas,
            target_schema,
            args.dialect,
        )
        event = build_event(result, target_schema.name, args.namespace, args.job_name)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(event, indent=2) + "\n", encoding="utf-8")
    except (LineageError, OSError, ValueError) as error:
        print(f"lineage-generate: error: {error}", file=sys.stderr)
        return 2

    print(f"Wrote OpenLineage event to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())