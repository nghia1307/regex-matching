"""
The transformation engine.

Every operation is expressed as a **native Spark SQL column expression** --
``regexp_replace``, ``regexp_extract``, ``rlike``. There is deliberately not a
single Python UDF here, and that is the most important performance decision in
the project:

* Native expressions are compiled by Catalyst and run inside the JVM, so rows
  never cross the JVM/Python boundary (which costs a serialise + pipe + deserialise
  per row and is the usual reason "Spark is slow").
* With no Python closures to ship, the executors need none of this application's
  code on their PYTHONPATH -- the standalone cluster works with no packaging step.
* The work is per-partition and embarrassingly parallel: no shuffle, no
  ``collect``, no driver-side iteration. Adding executors adds throughput
  linearly.

The whole transformation is built as a *single* ``select``, so match counting
reads the pre-transformation values while replacement writes the new ones in the
same pass over the data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F

from apps.llm.spec import Operation, RegexSpec

logger = logging.getLogger(__name__)

#: Bookkeeping column: how many target cells in this row the pattern affected.
MATCH_COUNT_COLUMN = "__matched"

#: Columns the UI never shows.
INTERNAL_PREFIX = "__"


class TransformError(ValueError):
    """Bad request that we can explain precisely to the user."""


@dataclass
class TransformPlan:
    """The result of planning: what to run and what the output looks like."""

    dataframe: DataFrame
    result_columns: list[str] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    description: str = ""


def _null_safe(column: str) -> Column:
    """Cell as a non-null string. Cast makes Parquet/numeric sources safe too."""
    return F.coalesce(F.col(f"`{column}`").cast("string"), F.lit(""))


def _match_flag(column: str, pattern: str, *, negate: bool = False) -> Column:
    """1 when the cell is affected by the operation, else 0. Null-safe."""
    hit = _null_safe(column).rlike(pattern)
    if negate:
        hit = ~hit
    return F.when(hit, F.lit(1)).otherwise(F.lit(0))


def validate_columns(df: DataFrame, columns: list[str]) -> list[str]:
    """
    Check requested columns against the real schema.

    Also the injection guard: column names are only ever used after being
    matched against the DataFrame's own schema, so a crafted name cannot become
    part of an expression.
    """
    if not columns:
        raise TransformError("select at least one target column")

    available = list(df.columns)
    lookup = {name.lower(): name for name in available}
    resolved: list[str] = []
    missing: list[str] = []

    for requested in columns:
        actual = lookup.get(str(requested).strip().lower())
        if actual is None:
            missing.append(str(requested))
        elif actual not in resolved:
            resolved.append(actual)

    if missing:
        raise TransformError(
            f"column(s) {missing} are not in the file. Available: {available[:25]}"
        )
    return resolved


def plan_transformation(
    df: DataFrame,
    spec: RegexSpec,
    target_columns: list[str],
    replacement_value: str = "",
) -> TransformPlan:
    """Build the output DataFrame for ``spec``. Lazy -- nothing runs yet."""
    targets = validate_columns(df, target_columns)
    pattern = spec.effective_pattern
    operation = spec.operation

    if operation not in Operation.ALL:
        raise TransformError(f"unsupported operation: {operation}")

    projections: list[Column] = []
    added: list[str] = []
    flags: list[Column] = []

    if operation in (Operation.REPLACE, Operation.MASK):
        replacement = (
            replacement_value if operation == Operation.REPLACE
            else spec.replacement_template
        )
        for name in df.columns:
            if name in targets:
                # regexp_replace keeps nulls as nulls, which is what we want:
                # an empty cell should not become the replacement value.
                projections.append(
                    F.regexp_replace(
                        F.col(f"`{name}`").cast("string"), pattern, replacement
                    ).alias(name)
                )
                flags.append(_match_flag(name, pattern))
            else:
                projections.append(F.col(f"`{name}`"))
        description = (
            f"{operation}: {len(targets)} column(s) -> "
            f"{replacement!r} where /{pattern}/ matches"
        )

    elif operation == Operation.EXTRACT:
        projections = [F.col(f"`{name}`") for name in df.columns]
        for name in targets:
            new_column = _unique_name(f"{name}_extracted", df.columns, added)
            projections.append(
                F.regexp_extract(_null_safe(name), pattern, spec.group).alias(new_column)
            )
            added.append(new_column)
            flags.append(_match_flag(name, pattern))
        description = (
            f"EXTRACT: group {spec.group} of /{pattern}/ into {added}"
        )

    else:  # Operation.VALIDATE
        projections = [F.col(f"`{name}`") for name in df.columns]
        for name in targets:
            new_column = _unique_name(f"{name}_valid", df.columns, added)
            projections.append(_null_safe(name).rlike(pattern).alias(new_column))
            added.append(new_column)
            # For VALIDATE the "affected" cells are the ones that FAIL.
            flags.append(_match_flag(name, pattern, negate=True))
        description = f"VALIDATE: flag cells not matching /{pattern}/ in {added}"

    total_flag = flags[0]
    for extra in flags[1:]:
        total_flag = total_flag + extra
    projections.append(total_flag.alias(MATCH_COUNT_COLUMN))

    out = df.select(*projections)
    result_columns = [c for c in out.columns if not c.startswith(INTERNAL_PREFIX)]

    logger.info("planned transformation: %s", description)
    return TransformPlan(
        dataframe=out,
        result_columns=result_columns,
        added_columns=added,
        description=description,
    )


def _unique_name(candidate: str, existing: list[str], already_added: list[str]) -> str:
    taken = {c.lower() for c in existing} | {c.lower() for c in already_added}
    if candidate.lower() not in taken:
        return candidate
    index = 2
    while f"{candidate}_{index}".lower() in taken:
        index += 1
    return f"{candidate}_{index}"


def sample_values(df: DataFrame, columns: list[str], limit: int = 5) -> list[str]:
    """
    A few non-empty example values, used to give the LLM format context.

    ``limit`` is applied inside Spark, so this reads a single partition rather
    than scanning the file.
    """
    if not columns:
        return []
    column = columns[0]
    rows = (
        df.select(F.col(f"`{column}`").alias("v"))
        .where(F.col("v").isNotNull() & (F.length(F.col("v")) > 0))
        .limit(limit)
        .collect()
    )
    return [str(row["v"])[:120] for row in rows]
