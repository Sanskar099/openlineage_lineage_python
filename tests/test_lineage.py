from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from lineage.openlineage import build_event
from lineage.parser import LineageError, derive_column_lineage, parse_create_table


ROOT = Path(__file__).parents[1]


def _example_result():
    target_ddl = (ROOT / "examples/schemas/target.sql").read_text(encoding="utf-8")
    sql = (ROOT / "examples/transform.sql").read_text(encoding="utf-8")
    source_schemas = {}
    for source_path in ("customers.sql", "orders.sql"):
        ddl = (ROOT / "examples/schemas" / source_path).read_text(encoding="utf-8")
        schema = parse_create_table(ddl, "postgres")
        source_schemas[schema.name] = schema.columns
    target_schema = parse_create_table(target_ddl, "postgres")
    return derive_column_lineage(sql, source_schemas, target_schema, "postgres"), target_schema


def test_maps_select_expressions_to_target_columns():
    result, _ = _example_result()

    assert result.source_tables == ("raw.customers", "raw.orders")
    assert [mapping.target_column for mapping in result.columns] == [
        "customer_id",
        "region",
        "lifetime_value",
    ]
    assert [(item.table, item.column) for item in result.columns[0].source_columns] == [
        ("raw.customers", "customer_id")
    ]
    assert [(item.table, item.column) for item in result.columns[2].source_columns] == [
        ("raw.orders", "amount")
    ]
    assert result.columns[2].is_direct is False


def test_builds_sdk_serialized_openlineage_event():
    result, target_schema = _example_result()
    event = build_event(
        result,
        target_schema.name,
        "demo.local",
        run_id="00000000-0000-4000-8000-000000000001",
        event_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    event = json.loads(json.dumps(event))

    assert event["eventType"] == "COMPLETE"
    assert event["outputs"][0]["name"] == "mart.customer_summary"
    lineage = event["outputs"][0]["facets"]["columnLineage"]
    assert "ColumnLineageDatasetFacet" in lineage["_schemaURL"]
    assert lineage["fields"]["lifetime_value"]["inputFields"][0] == {
        "field": "amount",
        "name": "raw.orders",
        "namespace": "demo.local",
        "transformations": [
            {"description": "SUM(o.amount)", "type": "DIRECT"}
        ],
    }


def test_rejects_ambiguous_unqualified_column():
    target = parse_create_table("CREATE TABLE out_table (id INTEGER)", "postgres")

    with pytest.raises(LineageError, match="ambiguous"):
        derive_column_lineage(
            "SELECT id FROM first_table a JOIN second_table b ON a.id = b.id",
            {"first_table": ("id",), "second_table": ("id",)},
            target,
            "postgres",
        )


def test_rejects_wildcard_projection():
    target = parse_create_table("CREATE TABLE out_table (id INTEGER)", "postgres")

    with pytest.raises(LineageError, match="Wildcard"):
        derive_column_lineage(
            "SELECT * FROM source_table",
            {"source_table": ("id",)},
            target,
            "postgres",
        )