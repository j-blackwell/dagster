import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import polars as pl
import pytest
from dagster import (
    AssetExecutionContext,
    AssetIn,
    AssetKey,
    DailyPartitionsDefinition,
    Definitions,
    DynamicPartitionsDefinition,
    IOManagerDefinition,
    MetadataValue,
    MultiPartitionKey,
    MultiPartitionsDefinition,
    Out,
    StaticPartitionsDefinition,
    TableColumn,
    TableSchema,
    TimeWindowPartitionMapping,
    asset,
    build_input_context,
    build_output_context,
    fs_io_manager,
    instance_for_test,
    job,
    materialize,
    op,
)
from dagster._core.storage.db_io_manager import TableSlice
from dagster_snowflake import build_snowflake_io_manager
from dagster_snowflake.resources import SnowflakeResource
from dagster_snowflake_polars import (
    SnowflakePolarsIOManager,
    SnowflakePolarsTypeHandler,
    snowflake_polars_io_manager,
)

if TYPE_CHECKING:
    from dagster._core.definitions.metadata.metadata_value import IntMetadataValue

resource_config = {
    "database": "database_abc",
    "account": "account_abc",
    "user": "user_abc",
    "password": "password_abc",
    "warehouse": "warehouse_abc",
}

IS_BUILDKITE = os.getenv("BUILDKITE") is not None


SHARED_BUILDKITE_SNOWFLAKE_CONF: Mapping[str, Any] = {
    "account": os.getenv("SNOWFLAKE_ACCOUNT", ""),
    "user": "BUILDKITE",
    "password": os.getenv("SNOWFLAKE_BUILDKITE_PASSWORD", ""),
}

DATABASE = "TEST_SNOWFLAKE_IO_MANAGER"
SCHEMA = "SNOWFLAKE_IO_MANAGER_SCHEMA"

pythonic_snowflake_io_manager = SnowflakePolarsIOManager(
    database=DATABASE, **SHARED_BUILDKITE_SNOWFLAKE_CONF
)
old_snowflake_io_manager = snowflake_polars_io_manager.configured(
    {**SHARED_BUILDKITE_SNOWFLAKE_CONF, "database": DATABASE}
)


@contextmanager
def temporary_snowflake_table(schema_name: str, db_name: str) -> Iterator[str]:
    table_name = "test_io_manager_" + str(uuid.uuid4()).replace("-", "_")
    with SnowflakeResource(
        database=db_name, **SHARED_BUILDKITE_SNOWFLAKE_CONF
    ).get_connection() as conn:
        try:
            yield table_name
        finally:
            conn.cursor().execute(f"drop table {schema_name}.{table_name}")


def test_handle_output():
    handler = SnowflakePolarsTypeHandler()
    connection = MagicMock()
    # Mock the connection attributes needed for ADBC URI
    connection.account = "account_abc"
    connection.user = "user_abc"
    connection.password = "password_abc"
    connection.database = "database_abc"
    connection.schema = "schema_abc"
    connection.warehouse = "warehouse_abc"

    df = pl.DataFrame({"col1": ["a"], "col2": [1]})
    output_context = build_output_context(
        resource_config={**resource_config, "time_data_to_string": False}
    )

    # Mock the write_database method on the DataFrame
    with patch.object(pl.DataFrame, "write_database", MagicMock()):
        metadata = handler.handle_output(
            output_context,
            TableSlice(
                table="my_table",
                schema="my_schema",
                database="my_db",
                columns=None,
                partition_dimensions=[],
            ),
            df,
            connection,
        )

    assert metadata == {
        "dataframe_columns": MetadataValue.table_schema(
            TableSchema(columns=[TableColumn("col1", "String"), TableColumn("col2", "Int64")])
        ),
        "dagster/row_count": 1,
    }


def test_load_input():
    handler = SnowflakePolarsTypeHandler()
    connection = MagicMock()

    # Mock cursor and its methods
    cursor_mock = MagicMock()
    connection.cursor.return_value = cursor_mock
    cursor_mock.fetchall.return_value = [("a", 1)]
    cursor_mock.description = [
        (column, None, None, None, None, None, None) for column in ["COL1", "COL2"]
    ]

    input_context = build_input_context(
        resource_config={**resource_config, "time_data_to_string": False}
    )

    df = handler.load_input(
        input_context,
        TableSlice(
            table="my_table",
            schema="my_schema",
            database="my_db",
            columns=None,
            partition_dimensions=[],
        ),
        connection,
    )

    cursor_mock.execute.assert_called_once_with("SELECT * FROM my_db.my_schema.my_table")
    assert df.equals(pl.DataFrame({"col1": ["a"], "col2": [1]}))


def test_build_snowflake_polars_io_manager():
    assert isinstance(
        build_snowflake_io_manager([SnowflakePolarsTypeHandler()]), IOManagerDefinition
    )
    # test wrapping decorator to make sure that works as expected
    assert isinstance(snowflake_polars_io_manager, IOManagerDefinition)


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.parametrize(
    "io_manager", [(pythonic_snowflake_io_manager), (old_snowflake_io_manager)]
)
@pytest.mark.integration
def test_io_manager_with_snowflake_polars(io_manager):
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:
        # Create a job with the temporary table name as an output, so that it will write to that table
        # and not interfere with other runs of this test

        @op(out={table_name: Out(io_manager_key="snowflake", metadata={"schema": SCHEMA})})
        def emit_polars_df(_):
            return pl.DataFrame({"foo": ["bar", "baz"], "quux": [1, 2]})

        @op
        def read_polars_df(df: pl.DataFrame):
            assert set(df.columns) == {"foo", "quux"}
            assert len(df) == 2

        @job(
            resource_defs={"snowflake": io_manager},
        )
        def io_manager_test_job():
            read_polars_df(emit_polars_df())

        res = io_manager_test_job.execute_in_process()
        assert res.success


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.integration
def test_io_manager_asset_metadata() -> None:
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:

        @asset(key_prefix=SCHEMA, name=table_name)
        def my_polars_df():
            return pl.DataFrame({"foo": ["bar", "baz"], "quux": [1, 2]})

        defs = Definitions(
            assets=[my_polars_df], resources={"io_manager": pythonic_snowflake_io_manager}
        )

        res = defs.resolve_implicit_global_asset_job_def().execute_in_process()
        assert res.success

        mats = res.get_asset_materialization_events()
        assert len(mats) == 1
        mat = mats[0]

        assert mat.materialization.metadata["dagster/table_name"] == MetadataValue.text(
            f"{DATABASE}.{SCHEMA}.{table_name}"
        )


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.parametrize(
    "io_manager", [(snowflake_polars_io_manager), (SnowflakePolarsIOManager.configure_at_launch())]
)
@pytest.mark.integration
def test_io_manager_with_snowflake_polars_timestamp_data(io_manager):
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:
        from datetime import datetime

        time_df = pl.DataFrame(
            {
                "foo": ["bar", "baz"],
                "date": [
                    datetime(2017, 1, 1, 12, 30, 45, 350000),
                    datetime(2017, 2, 1, 12, 30, 45, 350000),
                ],
            }
        )

        @op(out={table_name: Out(io_manager_key="snowflake", metadata={"schema": SCHEMA})})
        def emit_time_df(_):
            return time_df

        @op
        def read_time_df(df: pl.DataFrame):
            assert set(df.columns) == {"foo", "date"}
            # Check that dates are preserved (allowing for timezone differences)
            assert df["date"].dtype == pl.Datetime

        @job(
            resource_defs={"snowflake": io_manager},
            config={
                "resources": {
                    "snowflake": {
                        "config": {**SHARED_BUILDKITE_SNOWFLAKE_CONF, "database": DATABASE}
                    }
                }
            },
        )
        def io_manager_timestamp_test_job():
            read_time_df(emit_time_df())

        res = io_manager_timestamp_test_job.execute_in_process()
        assert res.success


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.parametrize(
    "io_manager", [(pythonic_snowflake_io_manager), (old_snowflake_io_manager)]
)
@pytest.mark.integration
def test_time_window_partitioned_asset(io_manager):
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:
        partitions_def = DailyPartitionsDefinition(start_date="2022-01-01")

        @asset(
            partitions_def=partitions_def,
            metadata={"partition_expr": "time"},
            config_schema={"value": str},
            key_prefix=SCHEMA,
            name=table_name,
        )
        def daily_partitioned(context: AssetExecutionContext) -> pl.DataFrame:
            from datetime import datetime

            partition = datetime.strptime(context.partition_key, "%Y-%m-%d")
            value = context.op_execution_context.op_config["value"]

            return pl.DataFrame(
                {
                    "TIME": [partition, partition, partition],
                    "A": [value, value, value],
                    "B": [4, 5, 6],
                }
            )

        @asset(
            partitions_def=partitions_def,
            key_prefix=SCHEMA,
            ins={"df": AssetIn([SCHEMA, table_name])},
            io_manager_key="fs_io",
        )
        def downstream_partitioned(df) -> None:
            # assert that we only get the columns created in daily_partitioned
            assert len(df) == 3

        asset_full_name = f"{SCHEMA}__{table_name}"
        snowflake_table_path = f"{SCHEMA}.{table_name}"

        snowflake_conn = SnowflakeResource(database=DATABASE, **SHARED_BUILDKITE_SNOWFLAKE_CONF)

        resource_defs = {"io_manager": io_manager, "fs_io": fs_io_manager}
        result = materialize(
            [daily_partitioned, downstream_partitioned],
            partition_key="2022-01-01",
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "1"}}}},
        )
        materialization = next(
            event
            for event in result.all_events
            if event.event_type_value == "ASSET_MATERIALIZATION"
        )
        meta = materialization.materialization.metadata["dagster/partition_row_count"]
        assert cast("IntMetadataValue", meta).value == 3

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}").fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            )
            assert out_df["A"].tolist() == ["1", "1", "1"]

        materialize(
            [daily_partitioned, downstream_partitioned],
            partition_key="2022-01-02",
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "2"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}").fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            )
            assert sorted(out_df["A"].tolist()) == ["1", "1", "1", "2", "2", "2"]

        materialize(
            [daily_partitioned, downstream_partitioned],
            partition_key="2022-01-01",
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "3"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}").fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            )
            assert sorted(out_df["A"].tolist()) == ["2", "2", "2", "3", "3", "3"]


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.parametrize(
    "io_manager", [(pythonic_snowflake_io_manager), (old_snowflake_io_manager)]
)
@pytest.mark.integration
def test_static_partitioned_asset(io_manager):
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:
        partitions_def = StaticPartitionsDefinition(["red", "yellow", "blue"])

        @asset(
            partitions_def=partitions_def,
            key_prefix=[SCHEMA],
            metadata={"partition_expr": "color"},
            config_schema={"value": str},
            name=table_name,
        )
        def static_partitioned(context: AssetExecutionContext) -> pl.DataFrame:
            partition = context.partition_key
            value = context.op_execution_context.op_config["value"]
            return pl.DataFrame(
                {
                    "COLOR": [partition, partition, partition],
                    "A": [value, value, value],
                    "B": [4, 5, 6],
                }
            )

        @asset(
            partitions_def=partitions_def,
            key_prefix=SCHEMA,
            ins={"df": AssetIn([SCHEMA, table_name])},
            io_manager_key="fs_io",
        )
        def downstream_partitioned(df) -> None:
            # assert that we only get the columns created in static_partitioned
            assert len(df) == 3

        asset_full_name = f"{SCHEMA}__{table_name}"
        snowflake_table_path = f"{SCHEMA}.{table_name}"

        snowflake_conn = SnowflakeResource(database=DATABASE, **SHARED_BUILDKITE_SNOWFLAKE_CONF)

        resource_defs = {"io_manager": io_manager, "fs_io": fs_io_manager}
        materialize(
            [static_partitioned, downstream_partitioned],
            partition_key="red",
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "1"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}")
            ).fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            assert out_df["A"].tolist() == ["1", "1", "1"]

        materialize(
            [static_partitioned, downstream_partitioned],
            partition_key="blue",
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "2"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}")
            ).fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            assert sorted(out_df["A"].tolist()) == ["1", "1", "1", "2", "2", "2"]

        materialize(
            [static_partitioned, downstream_partitioned],
            partition_key="red",
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "3"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}")
            ).fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            assert sorted(out_df["A"].tolist()) == ["2", "2", "2", "3", "3", "3"]


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.parametrize(
    "io_manager", [(pythonic_snowflake_io_manager), (old_snowflake_io_manager)]
)
@pytest.mark.integration
def test_multi_partitioned_asset(io_manager):
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:
        partitions_def = MultiPartitionsDefinition(
            {
                "time": DailyPartitionsDefinition(start_date="2022-01-01"),
                "color": StaticPartitionsDefinition(["red", "yellow", "blue"]),
            }
        )

        @asset(
            partitions_def=partitions_def,
            key_prefix=[SCHEMA],
            metadata={"partition_expr": {"time": "CAST(time as TIMESTAMP)", "color": "color"}},
            config_schema={"value": str},
            name=table_name,
        )
        def multi_partitioned(context) -> pl.DataFrame:
            partition = context.partition_key.keys_by_dimension
            value = context.op_execution_context.op_config["value"]
            return pl.DataFrame(
                {
                    "color": [partition["color"], partition["color"], partition["color"]],
                    "time": [partition["time"], partition["time"], partition["time"]],
                    "a": [value, value, value],
                }
            )

        @asset(
            partitions_def=partitions_def,
            key_prefix=SCHEMA,
            ins={"df": AssetIn([SCHEMA, table_name])},
            io_manager_key="fs_io",
        )
        def downstream_partitioned(df) -> None:
            # assert that we only get the columns created in multi_partitioned
            assert len(df) == 3

        asset_full_name = f"{SCHEMA}__{table_name}"
        snowflake_table_path = f"{SCHEMA}.{table_name}"

        snowflake_conn = SnowflakeResource(database=DATABASE, **SHARED_BUILDKITE_SNOWFLAKE_CONF)

        resource_defs = {"io_manager": io_manager, "fs_io": fs_io_manager}

        materialize(
            [multi_partitioned, downstream_partitioned],
            partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "red"}),
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "1"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}")
            ).fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            assert out_df["A"].tolist() == ["1", "1", "1"]

        materialize(
            [multi_partitioned, downstream_partitioned],
            partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "blue"}),
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "2"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}")
            ).fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            assert sorted(out_df["A"].tolist()) == ["1", "1", "1", "2", "2", "2"]

        materialize(
            [multi_partitioned, downstream_partitioned],
            partition_key=MultiPartitionKey({"time": "2022-01-02", "color": "red"}),
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "3"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}")
            ).fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            assert sorted(out_df["A"].tolist()) == ["1", "1", "1", "2", "2", "2", "3", "3", "3"]

        materialize(
            [multi_partitioned, downstream_partitioned],
            partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "red"}),
            resources=resource_defs,
            run_config={"ops": {asset_full_name: {"config": {"value": "4"}}}},
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}")
            ).fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            assert sorted(out_df["A"].tolist()) == ["2", "2", "2", "3", "3", "3", "4", "4", "4"]


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.parametrize(
    "io_manager", [(pythonic_snowflake_io_manager), (old_snowflake_io_manager)]
)
@pytest.mark.integration
def test_dynamic_partitions(io_manager):
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:
        dynamic_fruits = DynamicPartitionsDefinition(name="dynamic_fruits")

        @asset(
            partitions_def=dynamic_fruits,
            key_prefix=[SCHEMA],
            metadata={"partition_expr": "FRUIT"},
            config_schema={"value": str},
            name=table_name,
        )
        def dynamic_partitioned(context: AssetExecutionContext) -> pl.DataFrame:
            partition = context.partition_key
            value = context.op_execution_context.op_config["value"]
            return pl.DataFrame(
                {
                    "fruit": [partition, partition, partition],
                    "a": [value, value, value],
                }
            )

        @asset(
            partitions_def=dynamic_fruits,
            key_prefix=SCHEMA,
            ins={"df": AssetIn([SCHEMA, table_name])},
            io_manager_key="fs_io",
        )
        def downstream_partitioned(df) -> None:
            # assert that we only get the columns created in dynamic_partitioned
            assert len(df) == 3

        asset_full_name = f"{SCHEMA}__{table_name}"
        snowflake_table_path = f"{SCHEMA}.{table_name}"

        snowflake_conn = SnowflakeResource(database=DATABASE, **SHARED_BUILDKITE_SNOWFLAKE_CONF)

        resource_defs = {"io_manager": io_manager, "fs_io": fs_io_manager}

        with instance_for_test() as instance:
            instance.add_dynamic_partitions(dynamic_fruits.name, ["apple"])  # pyright: ignore[reportArgumentType]

            materialize(
                [dynamic_partitioned, downstream_partitioned],
                partition_key="apple",
                resources=resource_defs,
                instance=instance,
                run_config={"ops": {asset_full_name: {"config": {"value": "1"}}}},
            )

            with snowflake_conn.get_connection() as conn:
                out_df = (
                    conn.cursor()
                    .execute(
                        f"SELECT * FROM {snowflake_table_path}",
                    )
                    .fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
                )
                assert out_df["A"].tolist() == ["1", "1", "1"]

            instance.add_dynamic_partitions(dynamic_fruits.name, ["orange"])  # pyright: ignore[reportArgumentType]

            materialize(
                [dynamic_partitioned, downstream_partitioned],
                partition_key="orange",
                resources=resource_defs,
                instance=instance,
                run_config={"ops": {asset_full_name: {"config": {"value": "2"}}}},
            )

            with snowflake_conn.get_connection() as conn:
                out_df = (
                    conn.cursor()
                    .execute(
                        f"SELECT * FROM {snowflake_table_path}",
                    )
                    .fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
                )
            assert sorted(out_df["A"].tolist()) == ["1", "1", "1", "2", "2", "2"]

            materialize(
                [dynamic_partitioned, downstream_partitioned],
                partition_key="apple",
                resources=resource_defs,
                instance=instance,
                run_config={"ops": {asset_full_name: {"config": {"value": "3"}}}},
            )

            with snowflake_conn.get_connection() as conn:
                out_df = (
                    conn.cursor()
                    .execute(
                        f"SELECT * FROM {snowflake_table_path}",
                    )
                    .fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
                )
                assert sorted(out_df["A"].tolist()) == ["2", "2", "2", "3", "3", "3"]


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.parametrize(
    "io_manager", [(pythonic_snowflake_io_manager), (old_snowflake_io_manager)]
)
@pytest.mark.integration
def test_self_dependent_asset(io_manager):
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:
        daily_partitions = DailyPartitionsDefinition(start_date="2023-01-01")

        @asset(
            partitions_def=daily_partitions,
            key_prefix=SCHEMA,
            ins={
                "self_dependent_asset": AssetIn(
                    key=AssetKey([SCHEMA, table_name]),
                    partition_mapping=TimeWindowPartitionMapping(start_offset=-1, end_offset=-1),
                ),
            },
            metadata={
                "partition_expr": "TO_TIMESTAMP(key)",
            },
            config_schema={"value": str, "last_partition_key": str},
            name=table_name,
        )
        def self_dependent_asset(
            context: AssetExecutionContext, self_dependent_asset: pl.DataFrame
        ) -> pl.DataFrame:
            key = context.partition_key

            if not self_dependent_asset.is_empty():
                assert len(self_dependent_asset) == 3
                assert (
                    self_dependent_asset["key"]
                    == context.op_execution_context.op_config["last_partition_key"]
                ).all()
            else:
                assert context.op_execution_context.op_config["last_partition_key"] == "NA"
            value = context.op_execution_context.op_config["value"]
            pl_df = pl.DataFrame(
                {
                    "key": [key, key, key],
                    "a": [value, value, value],
                }
            )

            return pl_df

        asset_full_name = f"{SCHEMA}__{table_name}"
        snowflake_table_path = f"{SCHEMA}.{table_name}"

        snowflake_conn = SnowflakeResource(database=DATABASE, **SHARED_BUILDKITE_SNOWFLAKE_CONF)

        resource_defs = {"io_manager": io_manager}

        materialize(
            [self_dependent_asset],
            partition_key="2023-01-01",
            resources=resource_defs,
            run_config={
                "ops": {asset_full_name: {"config": {"value": "1", "last_partition_key": "NA"}}}
            },
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}").fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            )
            assert sorted(out_df["A"].tolist()) == ["1", "1", "1"]

        materialize(
            [self_dependent_asset],
            partition_key="2023-01-02",
            resources=resource_defs,
            run_config={
                "ops": {
                    asset_full_name: {"config": {"value": "2", "last_partition_key": "2023-01-01"}}
                }
            },
        )

        with snowflake_conn.get_connection() as conn:
            out_df = (
                conn.cursor().execute(f"SELECT * FROM {snowflake_table_path}").fetch_pandas_all()  # pyright: ignore[reportOptionalMemberAccess]
            )
            assert sorted(out_df["A"].tolist()) == ["1", "1", "1", "2", "2", "2"]


@pytest.mark.skipif(not IS_BUILDKITE, reason="Requires access to the BUILDKITE snowflake DB")
@pytest.mark.parametrize(
    "io_manager", [(pythonic_snowflake_io_manager), (old_snowflake_io_manager)]
)
@pytest.mark.integration
def test_quoted_identifiers_asset(io_manager):
    with temporary_snowflake_table(
        schema_name=SCHEMA,
        db_name=DATABASE,
    ) as table_name:

        @asset(
            key_prefix=SCHEMA,
            name=table_name,
        )
        def illegal_column_name(context: AssetExecutionContext) -> pl.DataFrame:
            return pl.DataFrame(
                {
                    "5foo": [1, 2, 3],  # columns that start with numbers need to be quoted
                    "column with a space": [1, 2, 3],
                    "column_with_punctuation!": [1, 2, 3],
                    "by": [1, 2, 3],  # reserved
                }
            )

        resource_defs = {"io_manager": io_manager}
        res = materialize(
            [illegal_column_name],
            resources=resource_defs,
        )

        assert res.success
