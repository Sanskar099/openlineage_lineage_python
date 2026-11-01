"""Parse a bounded SQL SELECT subset into source-to-target column mappings."""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp


class LineageError(ValueError):
    """Raised when SQL or schemas cannot be resolved without guessing."""


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class SourceColumn:
    table: str
    column: str


@dataclass(frozen=True)
class ColumnMapping:
    target_column: str
    source_columns: tuple[SourceColumn, ...]
    expression: str
    is_direct: bool


@dataclass(frozen=True)
class LineageResult:
    source_tables: tuple[str, ...]
    columns: tuple[ColumnMapping, ...]


def _qualified_table_name(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def parse_create_table(ddl: str, dialect: str = "") -> TableSchema:
    """Read a simple CREATE TABLE DDL and return its name and ordered columns."""
    try:
        statement = sqlglot.parse_one(ddl, read=dialect or None)
    except sqlglot.errors.ParseError as error:
        raise LineageError(f"Could not parse table DDL: {error}") from error

    if not isinstance(statement, exp.Create) or not isinstance(statement.this, exp.Schema):
        raise LineageError("Each schema input must be a CREATE TABLE statement with explicit columns")

    table = statement.this.this
    if not isinstance(table, exp.Table):
        raise LineageError("Could not determine the table name from CREATE TABLE DDL")

    columns = tuple(
        definition.this.name
        for definition in statement.this.expressions
        if isinstance(definition, exp.ColumnDef)
    )
    if not columns:
        raise LineageError(f"No explicit columns found for table {_qualified_table_name(table)!r}")

    return TableSchema(_qualified_table_name(table), columns)


def _normalize_schema_map(schemas: dict[str, tuple[str, ...] | list[str]]) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for table_name, columns in schemas.items():
        key = table_name.casefold()
        if key in normalized:
            raise LineageError(f"Duplicate source schema for table {table_name!r}")
        normalized[key] = tuple(columns)
    return normalized


def _find_schema_name(table_name: str, schemas: dict[str, tuple[str, ...]]) -> str:
    key = table_name.casefold()
    if key in schemas:
        return next(name for name in schemas if name == key)

    matching_names = [name for name in schemas if name.rsplit(".", 1)[-1] == key.rsplit(".", 1)[-1]]
    if len(matching_names) == 1:
        return matching_names[0]
    if len(matching_names) > 1:
        raise LineageError(f"Table {table_name!r} matches multiple supplied schemas")
    raise LineageError(f"No source DDL was supplied for table {table_name!r}")


def _actual_schema_name(normalized_name: str, original_names: dict[str, str]) -> str:
    return original_names[normalized_name]


def _projection_columns(
    projection: exp.Expression,
    aliases: dict[str, str],
    schemas: dict[str, tuple[str, ...]],
    original_names: dict[str, str],
) -> tuple[SourceColumn, ...]:
    if any(isinstance(node, exp.Star) for node in projection.walk()):
        raise LineageError("Wildcard projections such as SELECT * are not supported; list target columns explicitly")

    resolved: list[SourceColumn] = []
    seen: set[tuple[str, str]] = set()
    for column in projection.find_all(exp.Column):
        column_name = column.name
        if column.table:
            source_key = aliases.get(column.table.casefold())
            if source_key is None:
                source_key = next(
                    (key for key in schemas if key == column.table.casefold()),
                    None,
                )
            if source_key is None:
                raise LineageError(f"Could not resolve table qualifier {column.table!r} for column {column_name!r}")
            candidates = [source_key] if any(name.casefold() == column_name.casefold() for name in schemas[source_key]) else []
        else:
            candidates = [
                table_key
                for table_key, columns in schemas.items()
                if any(name.casefold() == column_name.casefold() for name in columns)
            ]

        if len(candidates) != 1:
            reason = "ambiguous" if candidates else "unknown"
            raise LineageError(f"Column {column_name!r} is {reason}; qualify it or correct the supplied schemas")

        table_key = candidates[0]
        actual_column = next(name for name in schemas[table_key] if name.casefold() == column_name.casefold())
        identity = (table_key, actual_column.casefold())
        if identity not in seen:
            resolved.append(SourceColumn(_actual_schema_name(table_key, original_names), actual_column))
            seen.add(identity)

    return tuple(resolved)


def derive_column_lineage(
    sql: str,
    source_schemas: dict[str, tuple[str, ...] | list[str]],
    target_schema: TableSchema,
    dialect: str = "",
) -> LineageResult:
    """Map each target column to columns referenced by its SELECT expression.

    Output columns are matched to the target DDL by select-list position.
    The supported query form is one SELECT from physical tables with optional joins.
    """
    try:
        statements = sqlglot.parse(sql, read=dialect or None)
    except sqlglot.errors.ParseError as error:
        raise LineageError(f"Could not parse SQL: {error}") from error

    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise LineageError("Provide exactly one SELECT statement")
    select = statements[0]

    if select.args.get("with") or select.args.get("with_") or any(
        isinstance(node, (exp.Subquery, exp.CTE)) for node in select.walk()
    ):
        raise LineageError("CTEs and subqueries are not supported yet")

    schemas_by_key = _normalize_schema_map(source_schemas)
    original_names = {name.casefold(): name for name in source_schemas}
    from_clause = select.args.get("from_")
    if from_clause is None:
        raise LineageError("The SELECT must read from at least one source table")

    relations = [from_clause.this, *(join.this for join in select.args.get("joins", []))]
    aliases: dict[str, str] = {}
    source_tables: list[str] = []
    for relation in relations:
        if not isinstance(relation, exp.Table):
            raise LineageError("Only physical tables in FROM and JOIN are supported")
        parsed_name = _qualified_table_name(relation)
        schema_key = _find_schema_name(parsed_name, schemas_by_key)
        aliases[relation.alias_or_name.casefold()] = schema_key
        aliases[relation.name.casefold()] = schema_key
        canonical_name = _actual_schema_name(schema_key, original_names)
        if canonical_name not in source_tables:
            source_tables.append(canonical_name)

    projections = list(select.expressions)
    if len(projections) != len(target_schema.columns):
        raise LineageError(
            f"SELECT has {len(projections)} output expressions but target DDL has "
            f"{len(target_schema.columns)} columns; counts must match"
        )

    mappings: list[ColumnMapping] = []
    for target_column, projection in zip(target_schema.columns, projections, strict=True):
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        sources = _projection_columns(projection, aliases, schemas_by_key, original_names)
        mappings.append(
            ColumnMapping(
                target_column=target_column,
                source_columns=sources,
                expression=expression.sql(dialect=dialect or None),
                is_direct=isinstance(expression, exp.Column),
            )
        )

    return LineageResult(tuple(source_tables), tuple(mappings))