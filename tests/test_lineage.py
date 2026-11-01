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
            {"description": "SUM(o.amount)", "type": "INDIRECT"}
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


def test_expands_wildcard_projection_from_source_schema():
    target = parse_create_table("CREATE TABLE out_table (id INTEGER)", "postgres")

    result = derive_column_lineage(
        "SELECT * FROM source_table",
        {"source_table": ("id",)},
        target,
        "postgres",
    )

    assert result.columns[0].target_column == "id"
    assert [(source.table, source.column) for source in result.columns[0].source_columns] == [
        ("source_table", "id")
    ]


def test_traces_cte_nested_derived_table_and_window_dependencies():
    target = parse_create_table("CREATE TABLE totals (running_total DECIMAL)", "postgres")

    result = derive_column_lineage(
        """
        WITH base AS (
            SELECT region, amount
            FROM orders
        )
        SELECT SUM(amount) OVER (PARTITION BY region) AS running_total
        FROM (
            SELECT region, amount
            FROM base
        ) AS nested
        """,
        {"orders": ("region", "amount")},
        target,
        "postgres",
    )

    assert {(source.table, source.column) for source in result.columns[0].source_columns} == {
        ("orders", "amount"),
        ("orders", "region"),
    }
    assert result.columns[0].is_direct is False


def test_traces_union_all_to_both_source_tables():
    target = parse_create_table("CREATE TABLE combined (amount DECIMAL)", "postgres")

    result = derive_column_lineage(
        "SELECT amount FROM first_source UNION ALL SELECT amount FROM second_source",
        {"first_source": ("amount",), "second_source": ("amount",)},
        target,
        "postgres",
    )

    assert {(source.table, source.column) for source in result.columns[0].source_columns} == {
        ("first_source", "amount"),
        ("second_source", "amount"),
    }
    assert result.columns[0].is_direct is True


def test_traces_recursive_cte_to_base_table():
    target = parse_create_table("CREATE TABLE ids (id INTEGER)", "postgres")

    result = derive_column_lineage(
        """
        WITH RECURSIVE walk AS (
            SELECT id, parent_id FROM nodes WHERE parent_id IS NULL
            UNION ALL
            SELECT n.id, n.parent_id
            FROM nodes AS n
            JOIN walk AS previous ON n.parent_id = previous.id
        )
        SELECT id FROM walk
        """,
        {"nodes": ("id", "parent_id")},
        target,
        "postgres",
    )

    assert [(source.table, source.column) for source in result.columns[0].source_columns] == [
        ("nodes", "id")
    ]


def test_maps_insert_select_using_insert_target_column_order():
    target = parse_create_table(
        "CREATE TABLE report (id INTEGER, total DECIMAL)",
        "postgres",
    )

    result = derive_column_lineage(
        "INSERT INTO report (total, id) SELECT amount, id FROM source_table",
        {"source_table": ("id", "amount")},
        target,
        "postgres",
    )

    assert [mapping.target_column for mapping in result.columns] == ["total", "id"]
    assert [(source.table, source.column) for source in result.columns[0].source_columns] == [
        ("source_table", "amount")
    ]
    assert [(source.table, source.column) for source in result.columns[1].source_columns] == [
        ("source_table", "id")
    ]


def test_traces_create_view_as_select():
    target = parse_create_table("CREATE TABLE customer_ids (customer_id INTEGER)", "postgres")

    result = derive_column_lineage(
        "CREATE VIEW customer_ids AS SELECT customer_id FROM customers",
        {"customers": ("customer_id",)},
        target,
        "postgres",
    )

    assert [(source.table, source.column) for source in result.columns[0].source_columns] == [
        ("customers", "customer_id")
    ]