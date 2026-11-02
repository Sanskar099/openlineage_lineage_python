# SQL Column Lineage for OpenLineage

Derive column-level lineage from a SQL `SELECT` query and explicit source and target DDL schemas. The generator parses SQL with SQLGlot and builds a `COMPLETE` event with the OpenLineage Python SDK, including the output dataset's `columnLineage` facet.

## Requirements

- Python 3.11 or newer
- SQL supported by the selected SQLGlot dialect

## Install

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Generate an event

The example inputs use PostgreSQL syntax. Source DDL can be supplied more than once for a query with multiple source tables.

```powershell
lineage-generate `
	--sql examples/transform.sql `
	--source-ddl examples/schemas/customers.sql `
	--source-ddl examples/schemas/orders.sql `
	--target-ddl examples/schemas/target.sql `
	--namespace demo.local `
	--output examples/lineage.json `
	--dialect postgres
```

Run the focused tests with:

```powershell
python -m pytest
```

## Scope and assumptions

- The first version accepts one `SELECT` statement with physical tables in `FROM` and `JOIN`, and a `CREATE TABLE` DDL for each source and target.
- Output columns are matched to the target DDL by select-list position. Keep the select list in target-column order.
- Qualified references are resolved through table aliases. Unqualified columns must resolve to exactly one source schema.
- `SELECT *`, CTEs, subqueries, set operations, and dynamic SQL are rejected instead of producing guessed lineage.
- The facet maps columns used by each output expression. Join, filter, grouping, and ordering dependencies are not yet represented as indirect column dependencies.
- `lineage.json` is a complete OpenLineage run event. Its `columnLineage` facet describes direct expression inputs; it is not a database connection or SQL execution tool.

The tests check the supported mapping behavior and serialize the event through the SDK. Before using the generated event in production, validate it against the OpenLineage schema version used by your backend and add cases for the SQL dialect and transformations used in your environment.
