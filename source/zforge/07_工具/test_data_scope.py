# -*- coding: utf-8 -*-
"""用隔离库验证工作台不注册、查询或导出外部 LLM 数据。"""
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "05_Web服务"))
from server.app import LEGACY_SOURCES, create_app
from server.store.projects import ProjectData


class DataScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.history = self.root / "bili.db"
        self.external = self.root / "external.db"
        with closing(sqlite3.connect(self.history)) as c:
            c.execute('CREATE TABLE PYTHON (标题 TEXT, 播放量 INTEGER)')
            c.execute('INSERT INTO PYTHON VALUES (?, ?)', ('隔离历史视频', 100))
            c.commit()
        with closing(sqlite3.connect(self.external)) as c:
            c.execute('CREATE TABLE request_records (key_mask TEXT, metrics_key TEXT)')
            c.execute('INSERT INTO request_records VALUES (?, ?)', ('isolated-private-marker', 'isolated-metric-id'))
            c.execute('CREATE TABLE circuit_states (circuit_state TEXT)')
            c.execute('INSERT INTO circuit_states VALUES (?)', ('closed',))
            c.commit()
        self.before = hashlib.sha256(self.external.read_bytes()).hexdigest()
        self.sources = [('bili_legacy', 'B站历史', self.history),
                        ('metrics', '无关数据库', self.external),
                        ('unrelated', '其他未注册数据库', self.external)]
        self.app = create_app(web_db=self.root / 'web.db', account_db=self.root / 'accounts.db',
                              legacy_sources=self.sources)
        self.ext = self.app.extensions['zforge']
        self.addCleanup(self.ext['store'].close)
        self.addCleanup(self.ext['pool'].close)
        self.addCleanup(lambda: self.ext['runner'].shutdown(wait=True))
        self.addCleanup(self.ext['logins'].shutdown)
        self.client = self.app.test_client()

    def test_default_sources_only_register_crawler_history(self):
        self.assertEqual({s[0] for s in LEGACY_SOURCES}, {'bili_legacy', 'taobao_legacy'})

    def test_catalog_and_system_info_exclude_external_data(self):
        sources = self.client.get('/api/data/sources').json['sources']
        self.assertEqual({s['id'] for s in sources}, {'zforge', 'bili_legacy'})
        info = self.client.get('/api/system/info').json
        self.assertEqual({s['id'] for s in info['sources']}, {'zforge', 'bili_legacy'})
        self.assertFalse({'litellm', 'tiktoken'} & {d['name'] for d in info['deps']})
        catalog = self.client.get('/api/data/projects').json['projects']
        self.assertEqual({p['id'] for p in catalog}, {'bilibili', 'taobao'})
        self.assertNotIn('isolated-private-marker', json.dumps(catalog))
        self.assertEqual(set(ProjectData(self.ext['store'], self.ext['registry'], self.sources).legacy), {'bili_legacy'})
        self.assertEqual(self.client.get('/api/health').json['data_scope'], 'crawler-only-v1')

    def test_direct_table_rows_and_sql_sources_are_rejected(self):
        for source in ('metrics', 'unrelated'):
            for table in ('request_records', 'circuit_states'):
                with self.subTest(source=source, table=table):
                    responses = [self.client.get(f'/api/data/{source}/tables'),
                                 self.client.get(f'/api/data/{source}/rows', query_string={'table': table}),
                                 self.client.post('/api/sql', json={'source_id': source, 'sql': f'SELECT * FROM {table}'})]
                    for response in responses:
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn('isolated-private-marker', response.text)

    def test_old_project_links_cannot_load_external_records(self):
        for table in ('request_records', 'circuit_states'):
            dataset = 'metrics-' + hashlib.sha256(table.encode()).hexdigest()[:12]
            response = self.client.get(f'/api/data/projects/llm/datasets/{dataset}')
            self.assertEqual(response.status_code, 404)

    def test_sql_cannot_attach_or_read_external_file(self):
        queries = [f"ATTACH DATABASE '{self.external.as_posix()}' AS outside",
                   f"SELECT readfile('{self.external.as_posix()}')",
                   'SELECT * FROM request_records']
        for sql in queries:
            with self.subTest(sql_type=sql.split()[0]):
                response = self.client.post('/api/sql', json={'sql': sql})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn('isolated-private-marker', response.text)

    def test_crawler_history_remains_readable_and_external_file_unchanged(self):
        self.assertEqual(self.client.get('/api/data/bili_legacy/tables').json['tables'][0]['rows'], 1)
        rows = self.client.get('/api/data/bili_legacy/rows?table=PYTHON').json['rows']
        self.assertEqual(rows, [['隔离历史视频', 100]])
        projects = self.client.get('/api/data/projects').json['projects']
        bili = next(p for p in projects if p['id'] == 'bilibili')
        dataset = next(d for d in bili['datasets'] if d['origin'] == 'history')
        detail = self.client.get(f"/api/data/projects/bilibili/datasets/{dataset['id']}").json
        self.assertEqual(detail['rows'], rows)
        response = self.client.post('/api/sql', json={'source_id': 'bili_legacy', 'sql': 'SELECT * FROM PYTHON'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['rows'], rows)
        self.assertEqual(hashlib.sha256(self.external.read_bytes()).hexdigest(), self.before)


class LauncherScopeTests(unittest.TestCase):
    def test_launcher_rejects_old_service_and_reuses_patched_service(self):
        spec = importlib.util.spec_from_file_location('scope_launcher', Path(__file__).resolve().parents[1] / '05_Web服务/run.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as temp:
            status = Path(temp) / 'service-status.json'
            url = 'http://127.0.0.1:8765'
            status.write_text(json.dumps({'url': url}), encoding='utf-8')
            with patch.object(launcher, 'STATUS_FILE', status), patch.object(launcher, 'healthy', return_value={'ok': True}):
                with self.assertRaisesRegex(SystemExit, '旧版服务仍在运行'):
                    launcher.existing_service()
                self.assertEqual(json.loads(status.read_text(encoding='utf-8'))['url'], url)
            with patch.object(launcher, 'STATUS_FILE', status), patch.object(launcher, 'healthy', return_value={'ok': True, 'data_scope': 'crawler-only-v1'}):
                self.assertEqual(launcher.existing_service(), url)


if __name__ == '__main__':
    unittest.main(verbosity=2)
