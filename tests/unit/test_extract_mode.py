from unittest import TestCase
from unittest.mock import patch

import psycopg2

import tap_postgres
import tap_postgres.db as post_db
from tap_postgres.sync_strategies import full_table, incremental


class RecordingCursor:
    """Unnamed cursor whose fetchall walks a prepared list of pages."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params))

    def fetchone(self):
        return ['UTF8']

    def fetchall(self):
        if not self.conn.pages:
            return []
        return self.conn.pages.pop(0)


class RecordingConnection:
    """Connection that records cursor names and statements."""

    def __init__(self, pages):
        self.pages = pages
        self.statements = []
        self.cursor_names = []
        self.autocommit = False

    def cursor(self, name=None, **kwargs):
        self.cursor_names.append(name)
        return RecordingCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestExtractMode(TestCase):
    """extract_mode=query reads pages with short statements instead of a server cursor."""

    def test_default_and_invalid_extract_mode(self):
        self.assertEqual(tap_postgres._extract_mode({}), 'cursor')
        self.assertEqual(tap_postgres._extract_mode({'extract_mode': 'query'}), 'query')
        with self.assertRaises(ValueError):
            tap_postgres._extract_mode({'extract_mode': 'stream'})

    def test_query_pk_resumes_interrupted_logical_initial_sync(self):
        stream = {
            'tap_stream_id': 'public-t',
            'table_name': 't',
            'metadata': [{'breadcrumb': (), 'metadata': {'replication-method': 'LOG_BASED'}}],
            'schema': {'properties': {'id': {'type': 'integer'}}},
        }
        state = {'bookmarks': {'public-t': {
            'lsn': '0/16',
            'query_pk_value': [1],
            'last_replication_method': 'LOG_BASED',
        }}}

        lookup, _, _ = tap_postgres.sync_method_for_streams([stream], state, None)

        self.assertEqual(lookup['public-t'], 'logical_initial_interrupted')

    def test_fetch_all_retries_recovery_conflict(self):
        class ConflictConnection:
            """Fails the first statement and stays aborted until rollback, as Postgres does."""

            def __init__(self):
                self.aborted = False
                self.autocommit = True
                self.rollbacks = 0
                self.cursor_impl = self.ConflictCursor(self)

            class ConflictCursor:
                def __init__(self, conn):
                    self.conn = conn
                    self.calls = 0

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def execute(self, sql, params=None):
                    if self.conn.aborted:
                        raise psycopg2.errors.InFailedSqlTransaction(
                            'current transaction is aborted, commands ignored until end of transaction block')
                    self.calls += 1
                    if self.calls == 1:
                        self.conn.aborted = True
                        raise psycopg2.errors.SerializationFailure(
                            'canceling statement due to conflict with recovery')

                def fetchall(self):
                    return [(1,)]

            def cursor(self, **kwargs):
                return self.cursor_impl

            def rollback(self):
                self.rollbacks += 1
                self.aborted = False

        conn = ConflictConnection()
        with patch('tap_postgres.db.time.sleep') as sleep:
            rows = post_db.fetch_all(conn, 'SELECT 1')

        self.assertEqual(rows, [(1,)])
        self.assertEqual(conn.cursor_impl.calls, 2)
        self.assertEqual(conn.rollbacks, 1)
        self.assertTrue(conn.autocommit)
        self.assertFalse(conn.aborted)
        sleep.assert_called_once()

    @patch('tap_postgres.sync_strategies.incremental.post_db.hstore_available', return_value=False)
    @patch('tap_postgres.sync_strategies.incremental.post_db.open_connection')
    def test_incremental_query_pages_without_a_named_cursor(self, open_connection, _hstore):
        conn = RecordingConnection(pages=[
            [(1,), (2,)],
            [(2,), (3,)],
            [(3,)],
        ])
        open_connection.return_value = conn
        original_page_size = post_db.CURSOR_ITER_SIZE
        post_db.CURSOR_ITER_SIZE = 2
        stream = {'tap_stream_id': 5, 'stream': 'bar', 'table_name': 'pg_tbl'}
        md_map = {
            (): {'schema-name': 'public', 'replication-key': 'foo_key'},
            ('properties', 'foo_key'): {'sql-datatype': 'integer'},
        }
        state = {'bookmarks': {5: {'version': 1}}}
        conn_config = {'extract_mode': 'query', 'limit': None}

        try:
            actual_state = incremental.sync_table(conn_config, stream, state, ['foo_key'], md_map)
        finally:
            post_db.CURSOR_ITER_SIZE = original_page_size

        self.assertNotIn('pipelinewise', conn.cursor_names)
        selects = [statement for statement in conn.statements if 'pg_speedup_trick' in statement[0]]
        self.assertEqual(len(selects), 3)
        self.assertNotIn('WHERE', selects[0][0])
        self.assertIn('>= %s::integer', selects[1][0])
        self.assertEqual(selects[1][1], [2, 2])
        self.assertEqual(selects[2][1], [3, 2])
        self.assertEqual(actual_state['bookmarks'][5]['replication_key_value'], 3)
        self.assertIsNone(actual_state['bookmarks'][5]['query_pk_value'])

    @patch('tap_postgres.sync_strategies.incremental.post_db.hstore_available', return_value=False)
    @patch('tap_postgres.sync_strategies.incremental.post_db.open_connection')
    def test_incremental_query_resumes_with_primary_key(self, open_connection, _hstore):
        conn = RecordingConnection(pages=[[(1, 11), (2, 12)], [(3, 13)]])
        open_connection.return_value = conn
        original_page_size = post_db.CURSOR_ITER_SIZE
        post_db.CURSOR_ITER_SIZE = 2
        stream = {'tap_stream_id': 'public-t', 'stream': 't', 'table_name': 't'}
        md_map = {
            (): {'schema-name': 'public', 'replication-key': 'updated_at', 'table-key-properties': ['id']},
            ('properties', 'updated_at'): {'sql-datatype': 'integer'},
            ('properties', 'id'): {'sql-datatype': 'integer'},
        }
        state = {'bookmarks': {'public-t': {'version': 1, 'replication_key_value': 1, 'query_pk_value': [1, 10]}}}
        conn_config = {'extract_mode': 'query', 'limit': None}

        try:
            actual_state = incremental.sync_table(
                conn_config, stream, state, ['updated_at', 'id'], md_map)
        finally:
            post_db.CURSOR_ITER_SIZE = original_page_size

        selects = [statement for statement in conn.statements if 'pg_speedup_trick' in statement[0]]
        self.assertIn('> (%s::integer, %s::integer)', selects[0][0])
        self.assertEqual(selects[0][1], [1, 10, 2])
        self.assertEqual(actual_state['bookmarks']['public-t']['query_pk_value'], [3, 13])
        self.assertEqual(actual_state['bookmarks']['public-t']['replication_key_value'], 3)

    @patch('tap_postgres.sync_strategies.full_table.post_db.hstore_available', return_value=False)
    @patch('tap_postgres.sync_strategies.full_table.post_db.open_connection')
    def test_full_table_query_pages_by_primary_key(self, open_connection, _hstore):
        conn = RecordingConnection(pages=[[(1, 'a'), (2, 'b')], [(3, 'c')]])
        open_connection.return_value = conn
        original_page_size = post_db.CURSOR_ITER_SIZE
        post_db.CURSOR_ITER_SIZE = 2
        stream = {'tap_stream_id': 'public-t', 'stream': 't', 'table_name': 't'}
        md_map = {
            (): {'schema-name': 'public', 'table-key-properties': ['id']},
            ('properties', 'id'): {'sql-datatype': 'integer'},
            ('properties', 'name'): {'sql-datatype': 'text'},
        }
        state = {'bookmarks': {'public-t': {'version': 4, 'query_pk_value': [1]}}}
        conn_config = {'extract_mode': 'query'}

        try:
            with patch('time.time', return_value=1):
                actual_state = full_table.sync_table(conn_config, stream, state, ['id', 'name'], md_map)
        finally:
            post_db.CURSOR_ITER_SIZE = original_page_size

        self.assertNotIn('stitch_cursor', conn.cursor_names)
        selects = [statement for statement in conn.statements if 'pg_speedup_trick' in statement[0]]
        self.assertEqual(len(selects), 2)
        self.assertIn('> (%s::integer)', selects[0][0])
        self.assertEqual(selects[0][1], [1, 2])
        self.assertNotIn('xmin', selects[0][0])
        self.assertEqual(actual_state['bookmarks']['public-t']['version'], 4)
        self.assertIsNone(actual_state['bookmarks']['public-t']['query_pk_value'])
