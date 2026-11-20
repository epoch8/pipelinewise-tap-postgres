import copy
import time
import psycopg2
import psycopg2.extras
import singer

from functools import partial
from singer import utils
from singer import metrics

import tap_postgres.db as post_db

LOGGER = singer.get_logger('tap_postgres')

UPDATE_BOOKMARK_PERIOD = 1000
QUERY_PK_BOOKMARK = 'query_pk_value'


def _key_columns(md_map, is_view):
    table_md = md_map.get((), {})
    if is_view:
        return list(table_md.get('view-key-properties') or [])
    return list(table_md.get('table-key-properties') or [])


def _sql_datatype(md_map, column):
    datatype = md_map.get(('properties', column), {}).get('sql-datatype')
    if not datatype:
        raise Exception(f"extract_mode 'query' requires sql-datatype metadata for column {column}")
    return datatype


# pylint: disable=too-many-arguments
def _query_page_sql(select_expressions, schema_name, table_name, key_columns, key_types, key_values, limit):
    """One short full-table page. Without a key this is a single statement with no resume point."""
    params = []
    where_sql = ''
    order_sql = ''
    limit_sql = ''
    if key_columns:
        key_sql = [post_db.prepare_columns_sql(column) for column in key_columns]
        order_sql = f"ORDER BY {', '.join(f'{column} ASC' for column in key_sql)}"
        if key_values:
            placeholders = ', '.join(f'%s::{datatype}' for datatype in key_types)
            where_sql = f"WHERE ({', '.join(key_sql)}) > ({placeholders})"
            params.extend(key_values)
        limit_sql = 'LIMIT %s'
        params.append(limit)

    select_sql = f"""
        SELECT {','.join(select_expressions)}
        FROM (
            SELECT *
            FROM {post_db.fully_qualified_table_name(schema_name, table_name)}
            {where_sql}
            {order_sql}
            {limit_sql}
        ) pg_speedup_trick"""
    return select_sql, params


# pylint: disable=too-many-arguments,too-many-locals
def _sync_queries(conn, stream, state, desired_columns, select_expressions, md_map, schema_name,
                  version, time_extracted, counter, is_view=False):
    """Read a table or view as separate committed queries instead of one server cursor."""
    key_columns = _key_columns(md_map, is_view)
    key_types = [_sql_datatype(md_map, column) for column in key_columns]
    extra_columns = [column for column in key_columns if column not in desired_columns]
    select_columns = list(desired_columns) + extra_columns
    if extra_columns:
        select_expressions = list(select_expressions) + [
            post_db.prepare_columns_sql(column) for column in extra_columns]
    page_size = post_db.CURSOR_ITER_SIZE
    key_values = singer.get_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK)
    if not key_columns:
        LOGGER.warning("extract_mode 'query' cannot page %s without a key. Running one query.",
                       stream['tap_stream_id'])
        key_values = None
    elif key_values and len(key_values) != len(key_columns):
        raise Exception(
            f"query_pk_value for {stream['tap_stream_id']} does not match key columns {key_columns}"
        )

    LOGGER.info('Beginning full table query sync %s with page size %s', version, page_size if key_columns else None)
    rows_saved = 0
    while True:
        select_sql, params = _query_page_sql(select_expressions, schema_name, stream['table_name'],
                                             key_columns, key_types, key_values, page_size)
        rows = post_db.fetch_all(conn, select_sql, params)
        if not rows:
            break

        previous_key = key_values
        for row in rows:
            record_message = post_db.selected_row_to_singer_message(stream,
                                                                    row[:len(desired_columns)],
                                                                    version,
                                                                    desired_columns,
                                                                    time_extracted,
                                                                    md_map)
            singer.write_message(record_message)
            if key_columns:
                key_values = [
                    post_db.selected_value_to_singer_value(row[select_columns.index(column)], key_types[index])
                    for index, column in enumerate(key_columns)
                ]
                state = singer.write_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK, key_values)
            rows_saved += 1
            if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))
            counter.increment()

        singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))
        if not key_columns or len(rows) < page_size:
            break
        if key_values == previous_key:
            raise Exception(
                f"extract_mode 'query' could not advance the bookmark for {stream['tap_stream_id']}"
            )

    return state


# pylint: disable=invalid-name,missing-function-docstring,too-many-locals,duplicate-code
def sync_view(conn_info, stream, state, desired_columns, md_map):
    time_extracted = utils.now()

    # before writing the table version to state, check if we had one to begin with
    first_run = singer.get_bookmark(state, stream['tap_stream_id'], 'version') is None
    resuming_query = post_db.extract_mode(conn_info) == 'query' and singer.get_bookmark(
        state, stream['tap_stream_id'], QUERY_PK_BOOKMARK)
    if resuming_query and not first_run:
        nascent_stream_version = singer.get_bookmark(state, stream['tap_stream_id'], 'version')
    else:
        nascent_stream_version = int(time.time() * 1000)

    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'version',
                                  nascent_stream_version)
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    schema_name = md_map.get(()).get('schema-name')

    escaped_columns = map(post_db.prepare_columns_sql, desired_columns)

    activate_version_message = singer.ActivateVersionMessage(
        stream=post_db.calculate_destination_stream_name(stream, md_map),
        version=nascent_stream_version)

    if first_run:
        singer.write_message(activate_version_message)

    with metrics.record_counter(None) as counter:
        with post_db.open_connection(conn_info) as conn:
            if post_db.extract_mode(conn_info) == 'query':
                conn.autocommit = True
                escaped_columns = list(map(post_db.prepare_columns_sql, desired_columns))
                state = _sync_queries(conn, stream, state, desired_columns, escaped_columns, md_map, schema_name,
                                      nascent_stream_version, time_extracted, counter, is_view=True)
            else:
                _sync_view_cursor(conn, stream, state, escaped_columns, md_map, schema_name,
                                  nascent_stream_version, time_extracted, counter, desired_columns)

    # A finished view has no resume point. Drop a query-mode bookmark if one was written.
    if singer.get_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK) is not None:
        state = singer.write_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK, None)

    # always send the activate version whether first run or subsequent
    singer.write_message(activate_version_message)

    return state


def _sync_view_cursor(conn, stream, state, escaped_columns, md_map, schema_name,
                      nascent_stream_version, time_extracted, counter, desired_columns):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor, name='stitch_cursor') as cur:
        cur.itersize = post_db.CURSOR_ITER_SIZE
        select_sql = f"SELECT {','.join(escaped_columns)} FROM " \
                     f"{post_db.fully_qualified_table_name(schema_name, stream['table_name'])}"

        LOGGER.info("select %s with itersize %s", select_sql, cur.itersize)
        cur.execute(select_sql)

        rows_saved = 0
        for rec in cur:
            record_message = post_db.selected_row_to_singer_message(stream,
                                                                    rec,
                                                                    nascent_stream_version,
                                                                    desired_columns,
                                                                    time_extracted,
                                                                    md_map)
            singer.write_message(record_message)
            rows_saved += 1
            if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

            counter.increment()


# pylint: disable=too-many-statements,duplicate-code
def sync_table(conn_info, stream, state, desired_columns, md_map):
    time_extracted = utils.now()

    # before writing the table version to state, check if we had one to begin with
    first_run = singer.get_bookmark(state, stream['tap_stream_id'], 'version') is None

    # pick a new table version IFF we do not have an xmin in our state
    # the presence of an xmin indicates that we were interrupted last time through
    query_mode = post_db.extract_mode(conn_info) == 'query'
    query_pk_value = singer.get_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK)
    if query_mode and not query_pk_value and singer.get_bookmark(state, stream['tap_stream_id'], 'xmin') is not None:
        LOGGER.warning("Ignoring xmin bookmark for %s because extract_mode is query. "
                       "Full table replication will start from the beginning.", stream['tap_stream_id'])
        state = singer.write_bookmark(state, stream['tap_stream_id'], 'xmin', None)

    if singer.get_bookmark(state, stream['tap_stream_id'], 'xmin') is None and not (query_mode and query_pk_value):
        nascent_stream_version = int(time.time() * 1000)
    else:
        nascent_stream_version = singer.get_bookmark(state, stream['tap_stream_id'], 'version')

    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'version',
                                  nascent_stream_version)
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    schema_name = md_map.get(()).get('schema-name')

    escaped_columns = map(partial(post_db.prepare_columns_for_select_sql, md_map=md_map), desired_columns)

    activate_version_message = singer.ActivateVersionMessage(
        stream=post_db.calculate_destination_stream_name(stream, md_map),
        version=nascent_stream_version)

    if first_run:
        singer.write_message(activate_version_message)

    hstore_available = post_db.hstore_available(conn_info)
    with metrics.record_counter(None) as counter:
        with post_db.open_connection(conn_info) as conn:

            # Client side character encoding defaults to the value in postgresql.conf under client_encoding.
            # The server / db can also have its own configred encoding.
            with conn.cursor() as cur:
                cur.execute("show server_encoding")
                LOGGER.info("Current Server Encoding: %s", cur.fetchone()[0])
                cur.execute("show client_encoding")
                LOGGER.info("Current Client Encoding: %s", cur.fetchone()[0])

            if hstore_available:
                LOGGER.info("hstore is available")
                psycopg2.extras.register_hstore(conn)
            else:
                LOGGER.info("hstore is UNavailable")

            if query_mode:
                conn.autocommit = True
                select_expressions = list(
                    map(partial(post_db.prepare_columns_for_select_sql, md_map=md_map), desired_columns))
                state = _sync_queries(conn, stream, state, desired_columns, select_expressions, md_map, schema_name,
                                      nascent_stream_version, time_extracted, counter, is_view=False)
            else:
                if singer.get_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK) is not None:
                    state = singer.write_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK, None)
                state = _sync_table_cursor(conn, stream, state, escaped_columns, md_map, schema_name,
                                           nascent_stream_version, time_extracted, counter, desired_columns)

    # once we have completed the full table replication, discard the xmin bookmark.
    # the xmin bookmark only comes into play when a full table replication is interrupted
    state = singer.write_bookmark(state, stream['tap_stream_id'], 'xmin', None)
    if singer.get_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK) is not None:
        state = singer.write_bookmark(state, stream['tap_stream_id'], QUERY_PK_BOOKMARK, None)

    # always send the activate version whether first run or subsequent
    singer.write_message(activate_version_message)

    return state


def _sync_table_cursor(conn, stream, state, escaped_columns, md_map, schema_name,
                       nascent_stream_version, time_extracted, counter, desired_columns):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor, name='stitch_cursor') as cur:
        cur.itersize = post_db.CURSOR_ITER_SIZE

        fq_table_name = post_db.fully_qualified_table_name(schema_name, stream['table_name'])
        xmin = singer.get_bookmark(state, stream['tap_stream_id'], 'xmin')
        if xmin:
            LOGGER.info("Resuming Full Table replication %s from xmin %s", nascent_stream_version, xmin)
            select_sql = f"""
                SELECT {','.join(escaped_columns)}, xmin::text::bigint
                FROM {fq_table_name} where age(xmin::xid) <= age('{xmin}'::xid)
                ORDER BY xmin::text ASC"""
        else:
            LOGGER.info("Beginning new Full Table replication %s", nascent_stream_version)
            select_sql = f"""SELECT {','.join(escaped_columns)}, xmin::text::bigint
                              FROM {fq_table_name}
                             ORDER BY xmin::text ASC"""

        LOGGER.info("select %s with itersize %s", select_sql, cur.itersize)
        cur.execute(select_sql)

        rows_saved = 0
        for rec in cur:
            xmin = rec['xmin']
            rec = rec[:-1]
            record_message = post_db.selected_row_to_singer_message(stream,
                                                                    rec,
                                                                    nascent_stream_version,
                                                                    desired_columns,
                                                                    time_extracted,
                                                                    md_map)
            singer.write_message(record_message)
            state = singer.write_bookmark(state, stream['tap_stream_id'], 'xmin', xmin)
            rows_saved += 1
            if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

            counter.increment()
    return state
