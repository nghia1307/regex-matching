"""
The Spark transformation engine.

These run against a real ``local[2]`` SparkSession, so what is asserted here is
what Catalyst actually does -- including the Java regex flavour, null semantics
and multi-partition behaviour.
"""
from __future__ import annotations

import pytest

from apps.llm.spec import Operation, RegexSpec
from apps.sparkeng.engine import (
    MATCH_COUNT_COLUMN,
    TransformError,
    plan_transformation,
    sample_values,
    validate_columns,
)

pytestmark = pytest.mark.django_db

EMAIL = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b"


def rows_by_id(df):
    return {row["ID"]: row.asDict() for row in df.collect()}


# --------------------------------------------------------------------------- #
# column validation
# --------------------------------------------------------------------------- #
def test_validate_columns_is_case_insensitive(customers_df):
    assert validate_columns(customers_df, ["email"]) == ["Email"]


def test_validate_columns_rejects_unknown_names(customers_df):
    with pytest.raises(TransformError, match="not in the file"):
        validate_columns(customers_df, ["Emial"])


def test_validate_columns_requires_at_least_one(customers_df):
    with pytest.raises(TransformError, match="at least one"):
        validate_columns(customers_df, [])


def test_validate_columns_deduplicates(customers_df):
    assert validate_columns(customers_df, ["Email", "email", "Name"]) == ["Email", "Name"]


# --------------------------------------------------------------------------- #
# REPLACE -- the worked example from the brief
# --------------------------------------------------------------------------- #
def test_replace_redacts_emails(customers_df, email_spec):
    plan = plan_transformation(customers_df, email_spec, ["Email"], "REDACTED")
    result = rows_by_id(plan.dataframe)

    assert result["1"]["Email"] == "REDACTED"
    assert result["2"]["Email"] == "REDACTED"
    assert result["3"]["Email"] == "REDACTED"
    # Untouched columns stay untouched.
    assert result["1"]["Name"] == "John Doe"
    # Notes was not a target, so an email in it survives.
    assert "bob@x.io" in result["2"]["Notes"]


def test_replace_leaves_non_matching_and_null_cells_alone(customers_df, email_spec):
    plan = plan_transformation(customers_df, email_spec, ["Email"], "REDACTED")
    result = rows_by_id(plan.dataframe)

    assert result["5"]["Email"] == "not-an-email"
    # An empty CSV field reads as null and must not become the replacement value.
    assert result["4"]["Email"] is None


def test_match_count_reflects_affected_cells_only(customers_df, email_spec):
    plan = plan_transformation(customers_df, email_spec, ["Email"], "REDACTED")
    total = plan.dataframe.selectExpr(f"sum({MATCH_COUNT_COLUMN}) AS n").collect()[0]["n"]
    assert total == 3


def test_replace_across_several_columns_counts_each_cell(customers_df, email_spec):
    plan = plan_transformation(customers_df, email_spec, ["Email", "Notes"], "X")
    total = plan.dataframe.selectExpr(f"sum({MATCH_COUNT_COLUMN}) AS n").collect()[0]["n"]
    # 3 emails in Email + 1 in Notes
    assert total == 4


def test_internal_columns_are_hidden_from_result_columns(customers_df, email_spec):
    plan = plan_transformation(customers_df, email_spec, ["Email"], "REDACTED")
    assert MATCH_COUNT_COLUMN not in plan.result_columns
    assert plan.result_columns == ["ID", "Name", "Email", "Notes"]


# --------------------------------------------------------------------------- #
# MASK
# --------------------------------------------------------------------------- #
def test_mask_keeps_the_pieces_the_template_captures(customers_df):
    spec = RegexSpec(
        pattern=r"([A-Za-z0-9])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,7})",
        operation=Operation.MASK,
        replacement_template="$1***$2",
    )
    plan = plan_transformation(customers_df, spec, ["Email"])
    result = rows_by_id(plan.dataframe)

    assert result["1"]["Email"] == "j***@example.com"
    assert result["3"]["Email"] == "a***@website.org"
    assert result["5"]["Email"] == "not-an-email"


# --------------------------------------------------------------------------- #
# EXTRACT
# --------------------------------------------------------------------------- #
def test_extract_adds_a_column_and_preserves_the_original(customers_df):
    spec = RegexSpec(
        pattern=r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,7})",
        operation=Operation.EXTRACT,
        group=1,
    )
    plan = plan_transformation(customers_df, spec, ["Email"])
    result = rows_by_id(plan.dataframe)

    assert plan.added_columns == ["Email_extracted"]
    assert result["1"]["Email"] == "john.doe@example.com"
    assert result["1"]["Email_extracted"] == "example.com"
    # regexp_extract returns "" when nothing matches, never null.
    assert result["5"]["Email_extracted"] == ""


def test_extract_does_not_collide_with_an_existing_column_name(spark):
    df = spark.createDataFrame(
        [("a@b.com", "keep")], ["Email", "Email_extracted"]
    )
    spec = RegexSpec(pattern=r"@(\w+)", operation=Operation.EXTRACT, group=1)
    plan = plan_transformation(df, spec, ["Email"])
    assert plan.added_columns == ["Email_extracted_2"]


# --------------------------------------------------------------------------- #
# VALIDATE
# --------------------------------------------------------------------------- #
def test_validate_flags_invalid_values(customers_df):
    spec = RegexSpec(
        pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}$",
        operation=Operation.VALIDATE,
    )
    plan = plan_transformation(customers_df, spec, ["Email"])
    result = rows_by_id(plan.dataframe)

    assert result["1"]["Email_valid"] is True
    assert result["5"]["Email_valid"] is False
    assert result["4"]["Email_valid"] is False  # empty is not valid
    # For VALIDATE the counter tracks failures, not matches.
    total = plan.dataframe.selectExpr(f"sum({MATCH_COUNT_COLUMN}) AS n").collect()[0]["n"]
    assert total == 2


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def test_case_insensitive_pattern_is_honoured(spark):
    df = spark.createDataFrame([("HELLO@EXAMPLE.COM",)], ["Email"])
    spec = RegexSpec(pattern=r"[a-z]+@[a-z.]+", case_insensitive=True)
    plan = plan_transformation(df, spec, ["Email"], "X")
    assert plan.dataframe.collect()[0]["Email"] == "X"


def test_column_names_with_spaces_and_dots_work(spark):
    df = spark.createDataFrame([("a@b.com",)], ["Work Email.Primary"])
    spec = RegexSpec(pattern=r"@b\.com")
    plan = plan_transformation(df, spec, ["Work Email.Primary"], "@redacted")
    assert plan.dataframe.collect()[0]["Work Email.Primary"] == "a@redacted"


def test_unsupported_operation_is_refused(customers_df, email_spec):
    email_spec.operation = "DELETE_EVERYTHING"
    with pytest.raises(TransformError, match="unsupported operation"):
        plan_transformation(customers_df, email_spec, ["Email"], "x")


def test_sample_values_skips_blanks_and_bounds_the_read(customers_df):
    values = sample_values(customers_df, ["Email"], limit=3)
    assert len(values) <= 3
    assert all(value.strip() for value in values)


def test_transformation_survives_many_partitions(spark, email_spec):
    """Correctness must not depend on partition count."""
    rows = [(str(i), f"user{i}@example.com") for i in range(200)]
    df = spark.createDataFrame(rows, ["ID", "Email"]).repartition(7)
    plan = plan_transformation(df, email_spec, ["Email"], "REDACTED")
    collected = plan.dataframe.collect()
    assert len(collected) == 200
    assert {row["Email"] for row in collected} == {"REDACTED"}
