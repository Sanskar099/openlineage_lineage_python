"""Parse a bounded SQL SELECT subset into source-to-target column mappings."""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as sqlglot_lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope


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


def _query_from_statement(statement: exp.Expression) -> exp.Query:
    if isinstance(statement, exp.Query):
        return statement
    if isinstance(statement, (exp.Insert, exp.Create)) and isinstance(statement.expression, exp.Query):
        return statement.expression
    raise LineageError(
        "Expected a SELECT/set-operation query, INSERT ... SELECT, or CREATE VIEW/TABLE AS SELECT"
    )


def _target_columns_for_statement(statement: exp.Expression, target_schema: TableSchema) -> tuple[str, ...]:
    if isinstance(statement, exp.Insert) and isinstance(statement.this, exp.Schema):
        columns = tuple(
            item.name
            for item in statement.this.expressions
            if isinstance(item, (exp.Column, exp.Identifier))
        )
        if columns:
            schema_columns = {column.casefold() for column in target_schema.columns}
            unknown = [column for column in columns if column.casefold() not in schema_columns]
            if unknown:
                raise LineageError(f"INSERT references target columns absent from target DDL: {unknown}")
            return columns
    return target_schema.columns


def _leftmost_select(query: exp.Query) -> exp.Select:
    while isinstance(query, exp.SetOperation):
        if not isinstance(query.this, exp.Query):
            raise LineageError("Could not find the left query branch of the set operation")
        query = query.this
    if not isinstance(query, exp.Select):
        raise LineageError(f"Unsupported query expression: {type(query).__name__}")
    return query


def _alias_query_outputs(query: exp.Query, target_columns: tuple[str, ...]) -> exp.Query:
    query = query.copy()
    select = _leftmost_select(query)
    projections = list(select.expressions)
    if any(isinstance(node, exp.Star) for projection in projections for node in projection.walk()):
        return query
    if len(projections) != len(target_columns):
        raise LineageError(
            f"Query has {len(projections)} output expressions but target mapping has "
            f"{len(target_columns)} columns"
        )

    for projection, target_column in zip(projections, target_columns, strict=True):
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        projection.replace(exp.alias_(expression, target_column, quoted=False))
    return query


def _is_direct_lineage(root: object) -> bool:
    for node in root.walk():
        expression = node.expression
        if isinstance(expression, exp.Table):
            continue
        if isinstance(expression, exp.Alias):
            expression = expression.this
        if not isinstance(expression, exp.Column):
            return False
    return True


def _validate_ambiguous_columns(query: exp.Query, schemas: dict[str, tuple[str, ...]]) -> None:
    for scope in traverse_scope(query):
        source_columns: list[set[str]] = []
        for _, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                try:
                    schema_key = _find_schema_name(_qualified_table_name(source), schemas)
                except LineageError:
                    continue
                source_columns.append({column.casefold() for column in schemas[schema_key]})
            else:
                source_query = source.expression
                source_columns.append(
                    {column.casefold() for column in source_query.named_selects if column != "*"}
                )

        for column in scope.unqualified_columns:
            name = column.name.casefold()
            if sum(name in available for available in source_columns) > 1:
                raise LineageError(f"ambiguous column reference: {column.name!r}")


def _source_columns_from_graph(
    root: object,
    schemas: dict[str, tuple[str, ...]],
    original_names: dict[str, str],
    dialect: str,
) -> tuple[SourceColumn, ...]:
    resolved: list[SourceColumn] = []
    seen: set[tuple[str, str]] = set()
    for node in root.walk():
        if node.downstream:
            continue
        if not isinstance(node.expression, exp.Table):
            continue

        table_name = _qualified_table_name(node.expression)
        schema_key = _find_schema_name(table_name, schemas)
        try:
            column_expression = sqlglot.parse_one(node.name, read=dialect or None)
        except sqlglot.errors.ParseError as error:
            raise LineageError(f"Could not parse source column name {node.name!r}") from error
        if not isinstance(column_expression, exp.Column):
            raise LineageError(f"Could not resolve source column name {node.name!r}")
        column_name = column_expression.name
        actual_column = next(
            (column for column in schemas[schema_key] if column.casefold() == column_name.casefold()),
            None,
        )
        if actual_column is None:
            raise LineageError(f"Lineage graph references unknown column {column_name!r} in {table_name!r}")

        identity = (schema_key, actual_column.casefold())
        if identity not in seen:
            resolved.append(SourceColumn(_actual_schema_name(schema_key, original_names), actual_column))
            seen.add(identity)
    return tuple(resolved)


def derive_column_lineage(
    sql: str,
    source_schemas: dict[str, tuple[str, ...] | list[str]],
    target_schema: TableSchema,
    dialect: str = "",
) -> LineageResult:
    """Map target columns through SQLGlot's query-scope lineage graph.

    Supports nested SELECTs, CTEs (including recursive CTE syntax), set operations,
    derived tables, window expressions, and query-backed INSERT/CREATE statements.
    Output columns are matched to target DDL by position unless an INSERT column
    list explicitly specifies the destination order.
    """
    try:
        statements = sqlglot.parse(sql, read=dialect or None)
    except sqlglot.errors.ParseError as error:
        raise LineageError(f"Could not parse SQL: {error}") from error

    if len(statements) != 1:
        raise LineageError("Provide one query or one query-backed INSERT/CREATE statement at a time")
    statement = statements[0]
    query = _query_from_statement(statement)
    original_query = query.copy()
    target_columns = _target_columns_for_statement(statement, target_schema)
    schemas_by_key = _normalize_schema_map(source_schemas)
    original_names = {name.casefold(): name for name in source_schemas}
    query = _alias_query_outputs(query, target_columns)
    _validate_ambiguous_columns(query, schemas_by_key)
    sqlglot_schema = {
        table_name: {column: "UNKNOWN" for column in columns}
        for table_name, columns in source_schemas.items()
    }

    try:
        query = qualify(
            query,
            schema=sqlglot_schema,
            dialect=dialect or None,
            validate_qualify_columns=True,
        )
        graphs = sqlglot_lineage(None, query, schema=sqlglot_schema, dialect=dialect or None)
    except (sqlglot.errors.SqlglotError, ValueError, TypeError) as error:
        if "ambiguous" in str(error).casefold():
            raise LineageError(f"ambiguous column reference: {error}") from error
        raise LineageError(f"Could not resolve SQL column lineage: {error}") from error

    if len(graphs) != len(target_columns):
        raise LineageError(
            f"Query resolves to {len(graphs)} output columns but target mapping has "
            f"{len(target_columns)} columns"
        )

    source_tables: list[str] = []
    mappings: list[ColumnMapping] = []
    query_select = _leftmost_select(original_query)
    projections = list(query_select.expressions)
    mappings: list[ColumnMapping] = []
    for index, (target_column, root) in enumerate(zip(target_columns, graphs.values(), strict=True)):
        projection = projections[index] if index < len(projections) else None
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if projection is None or isinstance(expression, exp.Star):
            expression_text = target_column
        else:
            expression_text = expression.sql(dialect=dialect or None)

        sources = _source_columns_from_graph(root, schemas_by_key, original_names, dialect)
        for source in sources:
            if source.table not in source_tables:
                source_tables.append(source.table)
        mappings.append(
            ColumnMapping(
                target_column=target_column,
                source_columns=sources,
                expression=expression_text,
                is_direct=_is_direct_lineage(root),
            )
        )

    return LineageResult(tuple(source_tables), tuple(mappings))