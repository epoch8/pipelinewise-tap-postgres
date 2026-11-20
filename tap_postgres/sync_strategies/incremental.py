import copy
import time
import psycopg2
import psycopg2.extras
import singer

from singer import utils
from functools import partial
from singer import metrics

import tap_postgres.db as post_db


LOGGER = singer.get_logger('tap_postgres')

UPDATE_BOOKMARK_PERIOD = 10000


# pylint: disable=invalid-name,missing-function-docstring
def fetch_max_replication_key(conn_config, replication_key, schema_name, table_name):
    with post_db.open_connection(conn_config, False) as conn:
        with conn.cursor() as cur:
            max_key_sql = f"""
                SELECT max({post_db.prepare_columns_sql(replication_key)})
                FROM {post_db.fully_qualified_table_name(schema_name, table_name)}"""

            LOGGER.info("determine max replication key value: %s", max_key_sql)
            cur.execute(max_key_sql)
            max_key = cur.fetchone()[0]

            LOGGER.info("max replication key value: %s", max_key)
            return max_key


# pylint: disable=too-many-locals
def sync_table(conn_info, stream, state, desired_columns, md_map):
    time_extracted = utils.now()

    stream_version = singer.get_bookmark(state, stream['tap_stream_id'], 'version')
    if stream_version is None:
        stream_version = int(time.time() * 1000)

    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'version',
                                  stream_version)
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    schema_name = md_map.get(()).get('schema-name')

    escaped_columns = map(partial(post_db.prepare_columns_for_select_sql, md_map=md_map), desired_columns)

    activate_version_message = singer.ActivateVersionMessage(
        stream=post_db.calculate_destination_stream_name(stream, md_map),
        version=stream_version)

    singer.write_message(activate_version_message)

    replication_key = md_map.get((), {}).get('replication-key')
    replication_key_value = singer.get_bookmark(state, stream['tap_stream_id'], 'replication_key_value')
    replication_key_sql_datatype = md_map.get(('properties', replication_key)).get('sql-datatype')

    hstore_available = post_db.hstore_available(conn_info)
    with metrics.record_counter(None) as counter:
        with post_db.open_connection(conn_info) as conn:

            # Set connection to autocommit
            conn.autocommit = True

            # Client side character encoding defaults to the value in postgresql.conf under client_encoding.
            # The server / db can also have its own configured encoding.
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

            if post_db.extract_mode(conn_info) == 'query':
                state = _sync_table_queries(conn, conn_info, stream, state, desired_columns, md_map,
                                            schema_name, replication_key, replication_key_value,
                                            replication_key_sql_datatype, stream_version,
                                            time_extracted, counter)
                return state

            # A previous query-mode run may have left a keyset bookmark. Cursor resume uses
            # replication_key_value only, so drop the keyset before it goes stale.
            if singer.get_bookmark(state, stream['tap_stream_id'], 'query_pk_value') is not None:
                state = singer.write_bookmark(state, stream['tap_stream_id'], 'query_pk_value', None)

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor, name='pipelinewise', withhold=True) as cur:
                cur.itersize = post_db.CURSOR_ITER_SIZE
                LOGGER.info("Beginning new incremental replication sync %s", stream_version)
                select_sql = _get_select_sql({"escaped_columns": escaped_columns,
                                              "replication_key": replication_key,
                                              "replication_key_sql_datatype": replication_key_sql_datatype,
                                              "replication_key_value": replication_key_value,
                                              "schema_name": schema_name,
                                              "table_name": stream['table_name'],
                                              "limit": conn_info['limit']
                                              })
                LOGGER.info('select statement: %s with itersize %s', select_sql, cur.itersize)
                cur.execute(select_sql)

                rows_saved = 0

                for rec in cur:
                    record_message = post_db.selected_row_to_singer_message(stream,
                                                                            rec,
                                                                            stream_version,
                                                                            desired_columns,
                                                                            time_extracted,
                                                                            md_map)

                    singer.write_message(record_message)
                    rows_saved += 1

                    #Picking a replication_key with NULL values will result in it ALWAYS been synced which is not great
                    #event worse would be allowing the NULL value to enter into the state
                    if record_message.record[replication_key] is not None:
                        state = singer.write_bookmark(state,
                                                      stream['tap_stream_id'],
                                                      'replication_key_value',
                                                      record_message.record[replication_key])

                    if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
                        singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

                    counter.increment()

    return state


def _keyset_columns(replication_key, md_map):
    """Replication key, then primary-key columns that are not already the replication key."""
    columns = [replication_key]
    for column in md_map.get((), {}).get('table-key-properties') or []:
        if column not in columns:
            columns.append(column)
    return columns


def _sql_datatype(md_map, column):
    datatype = md_map.get(('properties', column), {}).get('sql-datatype')
    if not datatype:
        raise Exception(f"extract_mode 'query' requires sql-datatype metadata for column {column}")
    return datatype


def _singer_values(row, columns, select_columns, md_map):
    values = []
    for column in columns:
        raw = row[select_columns.index(column)]
        values.append(post_db.selected_value_to_singer_value(raw, _sql_datatype(md_map, column)))
    return values


def _select_expressions(desired_columns, md_map, extra_columns):
    expressions = [post_db.prepare_columns_for_select_sql(column, md_map) for column in desired_columns]
    expressions.extend(post_db.prepare_columns_sql(column) for column in extra_columns)
    return expressions


# pylint: disable=too-many-arguments
def _query_page_sql(select_expressions, replication_key, keyset_columns, keyset_types,
                    schema_name, table_name, bookmark_value, keyset_values, limit):
    """One short incremental page. keyset_values resumes past an exact row; otherwise >= bookmark."""
    key_sql = [post_db.prepare_columns_sql(column) for column in keyset_columns]
    params = []
    if keyset_values:
        where_sql = f"WHERE ({', '.join(key_sql)}) > ({', '.join(f'%s::{datatype}' for datatype in keyset_types)})"
        params.extend(keyset_values)
    elif bookmark_value is not None:
        where_sql = f"WHERE {post_db.prepare_columns_sql(replication_key)} >= %s::{keyset_types[0]}"
        params.append(bookmark_value)
    else:
        where_sql = ''

    params.append(limit)
    select_sql = f"""
    SELECT {','.join(select_expressions)}
    FROM (
        SELECT *
        FROM {post_db.fully_qualified_table_name(schema_name, table_name)}
        {where_sql}
        ORDER BY {', '.join(f'{column} ASC' for column in key_sql)}
        LIMIT %s
    ) pg_speedup_trick;"""
    return select_sql, params


def _emit_row(stream, row, desired_columns, stream_version, time_extracted, md_map, replication_key, state):
    record_message = post_db.selected_row_to_singer_message(stream,
                                                            row[:len(desired_columns)],
                                                            stream_version,
                                                            desired_columns,
                                                            time_extracted,
                                                            md_map)
    singer.write_message(record_message)
    if record_message.record[replication_key] is not None:
        state = singer.write_bookmark(state,
                                      stream['tap_stream_id'],
                                      'replication_key_value',
                                      record_message.record[replication_key])
    return state, record_message.record[replication_key] is not None


# pylint: disable=too-many-locals,too-many-arguments,too-many-branches,too-many-statements
def _sync_table_queries(conn, conn_info, stream, state, desired_columns, md_map, schema_name,
                        replication_key, replication_key_value, replication_key_sql_datatype,
                        stream_version, time_extracted, counter):
    """Read an incremental stream as separate committed queries instead of one server cursor."""
    keyset_columns = _keyset_columns(replication_key, md_map)
    keyset_types = [_sql_datatype(md_map, column) for column in keyset_columns]
    keyset_types[0] = replication_key_sql_datatype
    use_keyset = len(keyset_columns) > 1
    extra_columns = [column for column in keyset_columns if column not in desired_columns]
    select_columns = list(desired_columns) + extra_columns
    select_expressions = _select_expressions(desired_columns, md_map, extra_columns)
    page_size = post_db.CURSOR_ITER_SIZE
    run_limit = conn_info.get('limit')
    keyset_values = singer.get_bookmark(state, stream['tap_stream_id'], 'query_pk_value') if use_keyset else None
    if keyset_values and len(keyset_values) != len(keyset_columns):
        LOGGER.warning('Ignoring query_pk_value because primary key columns changed')
        keyset_values = None

    LOGGER.info('Beginning incremental query sync %s with page size %s', stream_version, page_size)
    rows_saved = 0
    bookmark_value = replication_key_value

    while run_limit is None or rows_saved < run_limit:
        select_sql, params = _query_page_sql(select_expressions, replication_key, keyset_columns, keyset_types,
                                             schema_name, stream['table_name'], bookmark_value, keyset_values,
                                             page_size)
        rows = post_db.fetch_all(conn, select_sql, params)
        if not rows:
            break

        key_index = select_columns.index(replication_key)
        to_emit = rows
        held_boundary = None
        stop_after_page = False
        if not use_keyset and len(rows) == page_size:
            boundary = rows[-1][key_index]
            if boundary is None:
                to_emit = [row for row in rows if row[key_index] is not None]
                LOGGER.warning('Stopping incremental query sync at NULL replication keys for %s',
                               stream['tap_stream_id'])
                stop_after_page = True
            else:
                to_emit = [row for row in rows if row[key_index] != boundary]
                if not to_emit:
                    raise Exception(
                        f"extract_mode 'query' read {len(rows)} rows with replication key {boundary!r} "
                        f"and no primary key to resume from. Increase itersize above that key, or add a primary key."
                    )
                held_boundary = boundary

        page_keyset = keyset_values
        for row in to_emit:
            if run_limit is not None and rows_saved >= run_limit:
                break
            state, bookmarked = _emit_row(stream, row, desired_columns, stream_version, time_extracted, md_map,
                                          replication_key, state)
            if use_keyset and bookmarked:
                keyset_values = _singer_values(row, keyset_columns, select_columns, md_map)
                state = singer.write_bookmark(state, stream['tap_stream_id'], 'query_pk_value', keyset_values)
            rows_saved += 1
            if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))
            counter.increment()

        if use_keyset and len(rows) == page_size and keyset_values == page_keyset:
            raise Exception(
                f"extract_mode 'query' could not advance the bookmark for {stream['tap_stream_id']}. "
                f"The page of {len(rows)} rows has no usable replication key."
            )

        if held_boundary is not None and (run_limit is None or rows_saved < run_limit):
            boundary_row = rows[-1]
            bookmark_value = post_db.selected_value_to_singer_value(boundary_row[key_index], keyset_types[0])
            state = singer.write_bookmark(state, stream['tap_stream_id'], 'replication_key_value', bookmark_value)
            keyset_values = None
        elif use_keyset and keyset_values:
            bookmark_value = keyset_values[0]

        singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

        if stop_after_page or len(rows) < page_size or (run_limit is not None and rows_saved >= run_limit):
            break

    if not use_keyset:
        state = singer.write_bookmark(state, stream['tap_stream_id'], 'query_pk_value', None)
    return state


def _get_select_sql(params):
    escaped_columns = params['escaped_columns']
    replication_key = post_db.prepare_columns_sql(params['replication_key'])
    replication_key_sql_datatype = params['replication_key_sql_datatype']
    replication_key_value = params['replication_key_value']
    schema_name = params['schema_name']
    table_name = params['table_name']

    limit_statement = f'LIMIT {params["limit"]}' if params["limit"] else ''
    where_statement = f"WHERE {replication_key} >= '{replication_key_value}'::{replication_key_sql_datatype}" \
        if replication_key_value else ""

    select_sql = f"""
    SELECT {','.join(escaped_columns)}
    FROM (
        SELECT *
        FROM {post_db.fully_qualified_table_name(schema_name, table_name)} 
        {where_statement}
        ORDER BY {replication_key} ASC {limit_statement}
    ) pg_speedup_trick;"""

    return select_sql
