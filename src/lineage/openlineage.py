"""Build and serialize OpenLineage run events using the Python SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from openlineage.client.event_v2 import InputDataset, Job, OutputDataset, Run, RunEvent, RunState
from openlineage.client.facet_v2 import column_lineage_dataset
from openlineage.client.serde import Serde

from lineage.parser import LineageResult


def build_event(
    result: LineageResult,
    target_table: str,
    namespace: str,
    job_name: str = "sql-column-lineage",
    run_id: str | None = None,
    event_time: datetime | None = None,
) -> dict:
    """Create a complete OpenLineage event with the output column-lineage facet."""
    event_time = event_time or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        raise ValueError("event_time must include a timezone")

    fields = {}
    for mapping in result.columns:
        inputs = [
            column_lineage_dataset.InputField(
                namespace=namespace,
                name=source.table,
                field=source.column,
                transformations=[
                    column_lineage_dataset.Transformation(
                        type="DIRECT" if mapping.is_direct else "INDIRECT",
                        description=mapping.expression,
                    )
                ],
            )
            for source in mapping.source_columns
        ]
        fields[mapping.target_column] = column_lineage_dataset.Fields(
            inputFields=inputs,
            transformationDescription=mapping.expression,
            transformationType="IDENTITY" if mapping.is_direct else None,
        )

    facet = column_lineage_dataset.ColumnLineageDatasetFacet(fields=fields)
    event = RunEvent(
        eventTime=event_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        run=Run(runId=run_id or str(uuid4())),
        job=Job(namespace=namespace, name=job_name),
        eventType=RunState.COMPLETE,
        inputs=[InputDataset(namespace=namespace, name=table) for table in result.source_tables],
        outputs=[
            OutputDataset(
                namespace=namespace,
                name=target_table,
                facets={"columnLineage": facet},
            )
        ],
    )
    return Serde.to_dict(event)


def event_to_json(event: dict) -> str:
    """Serialize an SDK event dictionary as readable JSON."""
    return Serde.to_json(event, indent=2)