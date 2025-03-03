import datetime
from abc import ABC, abstractmethod, abstractproperty
from typing import TYPE_CHECKING, Hashable, List, Optional, Tuple

import pendulum
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from prefect.orion import schemas
from prefect.orion.utilities.database import UUID as UUIDTypeDecorator
from prefect.orion.utilities.database import json_has_any_key

if TYPE_CHECKING:
    from prefect.orion.database.interface import OrionDBInterface


class BaseQueryComponents(ABC):
    """
    Abstract base class used to inject dialect-specific SQL operations into Orion.
    """

    def _unique_key(self) -> Tuple[Hashable, ...]:
        """
        Returns a key used to determine whether to instantiate a new DB interface.
        """
        return (self.__class__,)

    # --- dialect-specific SqlAlchemy bindings

    @abstractmethod
    def insert(self, obj):
        """dialect-specific insert statement"""

    @abstractmethod
    def greatest(self, *values):
        """dialect-specific SqlAlchemy binding"""

    # --- dialect-specific JSON handling

    @abstractproperty
    def uses_json_strings(self) -> bool:
        """specifies whether the configured dialect returns JSON as strings"""

    @abstractmethod
    def cast_to_json(self, json_obj):
        """casts to JSON object if necessary"""

    @abstractmethod
    def build_json_object(self, *args):
        """builds a JSON object from sequential key-value pairs"""

    @abstractmethod
    def json_arr_agg(self, json_array):
        """aggregates a JSON array"""

    # --- dialect-optimized subqueries

    @abstractmethod
    def make_timestamp_intervals(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        interval: datetime.timedelta,
    ):
        ...

    @abstractmethod
    def set_state_id_on_inserted_flow_runs_statement(
        self,
        fr_model,
        frs_model,
        inserted_flow_run_ids,
        insert_flow_run_states,
    ):
        ...

    @abstractmethod
    async def get_flow_run_notifications_from_queue(
        self, session: AsyncSession, db: "OrionDBInterface", limit: int
    ):
        """Database-specific implementation of reading notifications from the queue and deleting them"""

    async def queue_flow_run_notifications(
        self,
        session: sa.orm.session,
        flow_run: schemas.core.FlowRun,
        db: "OrionDBInterface",
    ):
        """Database-specific implementation of queueing notifications for a flow run"""
        # insert a <policy, state> pair into the notification queue
        stmt = (await db.insert(db.FlowRunNotificationQueue)).from_select(
            [
                db.FlowRunNotificationQueue.flow_run_notification_policy_id,
                db.FlowRunNotificationQueue.flow_run_state_id,
            ],
            # ... by selecting from any notification policy that matches the criteria
            sa.select(
                db.FlowRunNotificationPolicy.id,
                sa.cast(sa.literal(str(flow_run.state_id)), UUIDTypeDecorator),
            )
            .select_from(db.FlowRunNotificationPolicy)
            .where(
                sa.and_(
                    # the policy is active
                    db.FlowRunNotificationPolicy.is_active.is_(True),
                    # the policy state names aren't set or match the current state name
                    sa.or_(
                        db.FlowRunNotificationPolicy.state_names == [],
                        json_has_any_key(
                            db.FlowRunNotificationPolicy.state_names,
                            [flow_run.state_name],
                        ),
                    ),
                    # the policy tags aren't set, or the tags match the flow run tags
                    sa.or_(
                        db.FlowRunNotificationPolicy.tags == [],
                        json_has_any_key(
                            db.FlowRunNotificationPolicy.tags, flow_run.tags
                        ),
                    ),
                )
            ),
            # don't send python defaults as part of the insert statement, because they are
            # evaluated once per statement and create unique constraint violations on each row
            include_defaults=False,
        )
        await session.execute(stmt)

    async def read_block_documents(
        self,
        session: sa.orm.Session,
        db: "OrionDBInterface",
        block_document_filter: Optional[schemas.filters.BlockDocumentFilter] = None,
        block_type_filter: Optional[schemas.filters.BlockTypeFilter] = None,
        block_schema_filter: Optional[schemas.filters.BlockSchemaFilter] = None,
        include_secrets: bool = False,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ):

        # if no filter is provided, one is created that excludes anonymous blocks
        if block_document_filter is None:
            block_document_filter = schemas.filters.BlockDocumentFilter(
                is_anonymous=schemas.filters.BlockDocumentFilterIsAnonymous(eq_=False)
            )

        # --- Query for Parent Block Documents
        # begin by building a query for only those block documents that are selected
        # by the provided filters
        filtered_block_documents_query = sa.select(db.BlockDocument.id).where(
            block_document_filter.as_sql_filter(db)
        )

        if block_type_filter is not None:
            block_type_exists_clause = sa.select(db.BlockType).where(
                db.BlockType.id == db.BlockDocument.block_type_id,
                block_type_filter.as_sql_filter(db),
            )
            filtered_block_documents_query = filtered_block_documents_query.where(
                block_type_exists_clause.exists()
            )

        if block_schema_filter is not None:
            block_schema_exists_clause = sa.select(db.BlockSchema).where(
                db.BlockSchema.id == db.BlockDocument.block_schema_id,
                block_schema_filter.as_sql_filter(db),
            )
            filtered_block_documents_query = filtered_block_documents_query.where(
                block_schema_exists_clause.exists()
            )

        if offset is not None:
            filtered_block_documents_query = filtered_block_documents_query.offset(
                offset
            )

        if limit is not None:
            filtered_block_documents_query = filtered_block_documents_query.limit(limit)

        # apply database-specific handling of the filtered parent block documents
        filtered_block_document_ids = await self._handle_filtered_block_document_ids(
            session=session,
            filtered_block_documents_query=filtered_block_documents_query,
        )

        # --- Query for Referenced Block Documents
        # next build a recursive query for (potentially nested) block documents
        # that reference the filtered block documents
        block_document_references_query = (
            sa.select(db.BlockDocumentReference)
            .filter(
                db.BlockDocumentReference.parent_block_document_id.in_(
                    filtered_block_document_ids
                )
            )
            .cte("block_document_references", recursive=True)
        )
        block_document_references_join = sa.select(db.BlockDocumentReference).join(
            block_document_references_query,
            db.BlockDocumentReference.parent_block_document_id
            == block_document_references_query.c.reference_block_document_id,
        )
        recursive_block_document_references_cte = (
            block_document_references_query.union_all(block_document_references_join)
        )

        # --- Final Query for All Block Documents
        # build a query that unions:
        # - the filtered block documents
        # - with any block documents that are discovered as (potentially nested) references
        all_block_documents_query = sa.union_all(
            # first select the parent block
            sa.select(
                [
                    db.BlockDocument,
                    sa.null().label("reference_name"),
                    sa.null().label("reference_parent_block_document_id"),
                ]
            )
            .select_from(db.BlockDocument)
            .where(db.BlockDocument.id.in_(filtered_block_document_ids)),
            #
            # then select any referenced blocks
            sa.select(
                [
                    db.BlockDocument,
                    recursive_block_document_references_cte.c.name,
                    recursive_block_document_references_cte.c.parent_block_document_id,
                ]
            )
            .select_from(db.BlockDocument)
            .join(
                recursive_block_document_references_cte,
                db.BlockDocument.id
                == recursive_block_document_references_cte.c.reference_block_document_id,
            ),
        ).cte("all_block_documents_query")

        # the final union query needs to be `aliased` for proper ORM unpacking
        # and also be sorted
        return (
            sa.select(
                sa.orm.aliased(db.BlockDocument, all_block_documents_query),
                all_block_documents_query.c.reference_name,
                all_block_documents_query.c.reference_parent_block_document_id,
            )
            .select_from(all_block_documents_query)
            .order_by(all_block_documents_query.c.name)
        )

    async def _handle_filtered_block_document_ids(
        self, session, filtered_block_documents_query
    ):
        """Apply database-specific processing to a filtered block document query"""
        return filtered_block_documents_query


class AsyncPostgresQueryComponents(BaseQueryComponents):
    # --- Postgres-specific SqlAlchemy bindings

    def insert(self, obj):
        return postgresql.insert(obj)

    def greatest(self, *values):
        return sa.func.greatest(*values)

    # --- Postgres-specific JSON handling

    @property
    def uses_json_strings(self):
        return False

    def cast_to_json(self, json_obj):
        return json_obj

    def build_json_object(self, *args):
        return sa.func.jsonb_build_object(*args)

    def json_arr_agg(self, json_array):
        return sa.func.jsonb_agg(json_array)

    # --- Postgres-optimized subqueries

    def make_timestamp_intervals(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        interval: datetime.timedelta,
    ):
        # validate inputs
        start_time = pendulum.instance(start_time)
        end_time = pendulum.instance(end_time)
        assert isinstance(interval, datetime.timedelta)
        return (
            sa.select(
                sa.literal_column("dt").label("interval_start"),
                (sa.literal_column("dt") + interval).label("interval_end"),
            )
            .select_from(
                sa.func.generate_series(start_time, end_time, interval).alias("dt")
            )
            .where(sa.literal_column("dt") < end_time)
            # grab at most 500 intervals
            .limit(500)
        )

    def set_state_id_on_inserted_flow_runs_statement(
        self,
        fr_model,
        frs_model,
        inserted_flow_run_ids,
        insert_flow_run_states,
    ):
        """Given a list of flow run ids and associated states, set the state_id
        to the appropriate state for all flow runs"""
        # postgres supports `UPDATE ... FROM` syntax
        stmt = (
            sa.update(fr_model)
            .where(
                fr_model.id.in_(inserted_flow_run_ids),
                frs_model.flow_run_id == fr_model.id,
                frs_model.id.in_([r["id"] for r in insert_flow_run_states]),
            )
            .values(state_id=frs_model.id)
            # no need to synchronize as these flow runs are entirely new
            .execution_options(synchronize_session=False)
        )
        return stmt

    async def get_flow_run_notifications_from_queue(
        self, session: AsyncSession, db: "OrionDBInterface", limit: int
    ) -> List:

        queued_notifications = (
            sa.delete(db.FlowRunNotificationQueue)
            .returning(
                db.FlowRunNotificationQueue.id,
                db.FlowRunNotificationQueue.flow_run_notification_policy_id,
                db.FlowRunNotificationQueue.flow_run_state_id,
            )
            .where(
                db.FlowRunNotificationQueue.id.in_(
                    sa.select(db.FlowRunNotificationQueue.id)
                    .select_from(db.FlowRunNotificationQueue)
                    .order_by(db.FlowRunNotificationQueue.updated)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .cte("queued_notifications")
        )

        notification_details_stmt = (
            sa.select(
                queued_notifications.c.id.label("queue_id"),
                db.FlowRunNotificationPolicy.id.label(
                    "flow_run_notification_policy_id"
                ),
                db.FlowRunNotificationPolicy.message_template.label(
                    "flow_run_notification_policy_message_template"
                ),
                db.FlowRunNotificationPolicy.block_document_id,
                db.Flow.id.label("flow_id"),
                db.Flow.name.label("flow_name"),
                db.FlowRun.id.label("flow_run_id"),
                db.FlowRun.name.label("flow_run_name"),
                db.FlowRun.parameters.label("flow_run_parameters"),
                db.FlowRunState.type.label("flow_run_state_type"),
                db.FlowRunState.name.label("flow_run_state_name"),
                db.FlowRunState.timestamp.label("flow_run_state_timestamp"),
                db.FlowRunState.message.label("flow_run_state_message"),
            )
            .select_from(queued_notifications)
            .join(
                db.FlowRunNotificationPolicy,
                queued_notifications.c.flow_run_notification_policy_id
                == db.FlowRunNotificationPolicy.id,
            )
            .join(
                db.FlowRunState,
                queued_notifications.c.flow_run_state_id == db.FlowRunState.id,
            )
            .join(
                db.FlowRun,
                db.FlowRunState.flow_run_id == db.FlowRun.id,
            )
            .join(
                db.Flow,
                db.FlowRun.flow_id == db.Flow.id,
            )
        )

        result = await session.execute(notification_details_stmt)
        return result.fetchall()

    async def _handle_filtered_block_document_ids(
        self, session, filtered_block_documents_query
    ):
        """
        Transform the filtered block document query into a CTE and select its `id`
        """
        filtered_cte = filtered_block_documents_query.cte("filtered_block_documents")
        return sa.select(filtered_cte.c.id)


class AioSqliteQueryComponents(BaseQueryComponents):
    # --- Sqlite-specific SqlAlchemy bindings

    def insert(self, obj):
        return sqlite.insert(obj)

    def greatest(self, *values):
        return sa.func.max(*values)

    # --- Sqlite-specific JSON handling

    @property
    def uses_json_strings(self):
        return True

    def cast_to_json(self, json_obj):
        return sa.func.json(json_obj)

    def build_json_object(self, *args):
        return sa.func.json_object(*args)

    def json_arr_agg(self, json_array):
        return sa.func.json_group_array(json_array)

    # --- Sqlite-optimized subqueries

    def make_timestamp_intervals(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        interval: datetime.timedelta,
    ):
        from prefect.orion.utilities.database import Timestamp

        # validate inputs
        start_time = pendulum.instance(start_time)
        end_time = pendulum.instance(end_time)
        assert isinstance(interval, datetime.timedelta)

        return (
            sa.text(
                r"""
                -- recursive CTE to mimic the behavior of `generate_series`,
                -- which is only available as a compiled extension
                WITH RECURSIVE intervals(interval_start, interval_end, counter) AS (
                    VALUES(
                        strftime('%Y-%m-%d %H:%M:%f000', :start_time),
                        strftime('%Y-%m-%d %H:%M:%f000', :start_time, :interval),
                        1
                        )

                    UNION ALL

                    SELECT interval_end, strftime('%Y-%m-%d %H:%M:%f000', interval_end, :interval), counter + 1
                    FROM intervals
                    -- subtract interval because recursive where clauses are effectively evaluated on a t-1 lag
                    WHERE
                        interval_start < strftime('%Y-%m-%d %H:%M:%f000', :end_time, :negative_interval)
                        -- don't compute more than 500 intervals
                        AND counter < 500
                )
                SELECT * FROM intervals
                """
            )
            .bindparams(
                start_time=str(start_time),
                end_time=str(end_time),
                interval=f"+{interval.total_seconds()} seconds",
                negative_interval=f"-{interval.total_seconds()} seconds",
            )
            .columns(interval_start=Timestamp(), interval_end=Timestamp())
        )

    def set_state_id_on_inserted_flow_runs_statement(
        self,
        fr_model,
        frs_model,
        inserted_flow_run_ids,
        insert_flow_run_states,
    ):
        """Given a list of flow run ids and associated states, set the state_id
        to the appropriate state for all flow runs"""
        # sqlite requires a correlated subquery to update from another table
        subquery = (
            sa.select(frs_model.id)
            .where(
                frs_model.flow_run_id == fr_model.id,
                frs_model.id.in_([r["id"] for r in insert_flow_run_states]),
            )
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            sa.update(fr_model)
            .where(
                fr_model.id.in_(inserted_flow_run_ids),
            )
            .values(state_id=subquery)
            # no need to synchronize as these flow runs are entirely new
            .execution_options(synchronize_session=False)
        )
        return stmt

    async def get_flow_run_notifications_from_queue(
        self, session: AsyncSession, db: "OrionDBInterface", limit: int
    ) -> List:
        """
        Sqlalchemy has no support for DELETE RETURNING in sqlite (as of May 2022)
        so instead we issue two queries; one to get queued notifications and a second to delete
        them. This *could* introduce race conditions if multiple queue workers are
        running.
        """

        notification_details_stmt = (
            sa.select(
                db.FlowRunNotificationQueue.id.label("queue_id"),
                db.FlowRunNotificationPolicy.id.label(
                    "flow_run_notification_policy_id"
                ),
                db.FlowRunNotificationPolicy.message_template.label(
                    "flow_run_notification_policy_message_template"
                ),
                db.FlowRunNotificationPolicy.block_document_id,
                db.Flow.id.label("flow_id"),
                db.Flow.name.label("flow_name"),
                db.FlowRun.id.label("flow_run_id"),
                db.FlowRun.name.label("flow_run_name"),
                db.FlowRun.parameters.label("flow_run_parameters"),
                db.FlowRunState.type.label("flow_run_state_type"),
                db.FlowRunState.name.label("flow_run_state_name"),
                db.FlowRunState.timestamp.label("flow_run_state_timestamp"),
                db.FlowRunState.message.label("flow_run_state_message"),
            )
            .select_from(db.FlowRunNotificationQueue)
            .join(
                db.FlowRunNotificationPolicy,
                db.FlowRunNotificationQueue.flow_run_notification_policy_id
                == db.FlowRunNotificationPolicy.id,
            )
            .join(
                db.FlowRunState,
                db.FlowRunNotificationQueue.flow_run_state_id == db.FlowRunState.id,
            )
            .join(
                db.FlowRun,
                db.FlowRunState.flow_run_id == db.FlowRun.id,
            )
            .join(
                db.Flow,
                db.FlowRun.flow_id == db.Flow.id,
            )
            .order_by(db.FlowRunNotificationQueue.updated)
            .limit(limit)
        )

        result = await session.execute(notification_details_stmt)
        notifications = result.fetchall()

        # delete the notifications
        delete_stmt = (
            sa.delete(db.FlowRunNotificationQueue)
            .where(
                db.FlowRunNotificationQueue.id.in_([n.queue_id for n in notifications])
            )
            .execution_options(synchronize_session="fetch")
        )

        await session.execute(delete_stmt)

        return notifications

    async def _handle_filtered_block_document_ids(
        self, session, filtered_block_documents_query
    ):
        """
        On SQLite, including the filtered block document parameters confuses the
        compiler and it passes positional parameters in the wrong order (it is
        unclear why; SQLalchemy manual compilation works great. Switching to
        `named` paramstyle also works but fails elsewhere in the codebase). To
        resolve this, we materialize the filtered id query into a literal set of
        IDs rather than leaving it as a SQL select.
        """
        result = await session.execute(filtered_block_documents_query)
        return result.scalars().all()
