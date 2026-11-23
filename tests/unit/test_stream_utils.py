import unittest
from unittest.mock import patch, MagicMock

from tap_postgres import stream_utils


class TestRefreshStreamsSchema(unittest.TestCase):
    @patch('tap_postgres.stream_utils.discover_db')
    @patch('tap_postgres.stream_utils.open_connection')
    def test_column_selection_is_preserved(self, open_conn, discover_db):
        open_conn.return_value.__enter__.return_value = MagicMock()
        discover_db.return_value = [{
            'tap_stream_id': 'public-t',
            'schema': {'properties': {'a': {}, 'b': {}, 'c': {}}},
            'metadata': [
                {'breadcrumb': (), 'metadata': {'table-key-properties': ['a']}},
                {'breadcrumb': ('properties', 'a'), 'metadata': {'inclusion': 'available'}},
                {'breadcrumb': ('properties', 'b'), 'metadata': {'inclusion': 'available'}},
                {'breadcrumb': ('properties', 'c'), 'metadata': {'inclusion': 'available'}},
            ]}]
        streams = [{
            'tap_stream_id': 'public-t',
            'table_name': 't',
            'schema': {},
            'metadata': [
                {'breadcrumb': (), 'metadata': {'selected': True}},
                {'breadcrumb': ('properties', 'a'), 'metadata': {'selected': True}},
                {'breadcrumb': ('properties', 'b'), 'metadata': {'selected': False}},
            ]}]

        stream_utils.refresh_streams_schema({}, streams)

        md = {m['breadcrumb']: m['metadata'] for m in streams[0]['metadata']}
        self.assertTrue(md[()]['selected'])
        self.assertEqual(md[()]['table-key-properties'], ['a'])
        self.assertTrue(md[('properties', 'a')]['selected'])
        self.assertFalse(md[('properties', 'b')]['selected'])
        self.assertNotIn('selected', md[('properties', 'c')])

    @patch('tap_postgres.stream_utils.discover_db')
    @patch('tap_postgres.stream_utils.open_connection')
    def test_view_key_properties_are_preserved_when_discovery_finds_none(self, open_conn, discover_db):
        open_conn.return_value.__enter__.return_value = MagicMock()
        discover_db.return_value = [{
            'tap_stream_id': 'public-direct_view__post_hashtag_lower',
            'schema': {'properties': {'id': {}, 'hashtag': {}, 'updated_at': {}}},
            'metadata': [
                {'breadcrumb': (), 'metadata': {'table-key-properties': [], 'is-view': True}},
            ]}]
        streams = [{
            'tap_stream_id': 'public-direct_view__post_hashtag_lower',
            'table_name': 'direct_view__post_hashtag_lower',
            'schema': {},
            'metadata': [
                {'breadcrumb': (), 'metadata': {
                    'table-key-properties': ['hashtag', 'id'],
                    'replication-method': 'INCREMENTAL',
                    'replication-key': 'updated_at',
                }},
            ]}]

        stream_utils.refresh_streams_schema({}, streams)

        table_md = streams[0]['metadata'][0]['metadata']
        self.assertEqual(table_md['table-key-properties'], ['hashtag', 'id'])
        self.assertEqual(table_md['replication-key'], 'updated_at')
        self.assertTrue(table_md['is-view'])
