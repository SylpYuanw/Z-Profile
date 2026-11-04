# -*- coding: utf-8 -*-
"""网页登录与项目隔离的临时库验收，不使用真实账号或访问外站。"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "05_Web服务"))
from server.app import create_app
from server.core.accounts import AccountError
from server.core.browser_login import BrowserLogins, login_cookies
from server.core.browser_runtime import cached_edge_driver


COOKIES = [{"name": "unb", "value": "isolated-platform-id", "domain": ".taobao.com"},
           {"name": "cookie2", "value": "isolated-not-a-real-secret", "domain": ".taobao.com"}]


class FakeBrowser:
    def __init__(self, cookie_list=None):
        self.cookies = COOKIES if cookie_list is None else cookie_list
        self.current_url = "https://www.taobao.com/"
        self.closed = False
        self.opened = ""

    def get(self, url):
        self.opened = url

    def get_cookies(self):
        return self.cookies

    def quit(self):
        self.closed = True


class LoginProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bili, taobao = self.root / "bili.db", self.root / "goods.db"
        with closing(sqlite3.connect(bili)) as c:
            c.execute('CREATE TABLE PYTHON (标题 TEXT,播放量 INTEGER,点赞 INTEGER,投币 INTEGER,收藏 INTEGER,URL TEXT)')
            c.execute('INSERT INTO PYTHON VALUES (?,?,?,?,?,?)', ('B站历史专用',1000,100,20,30,'https://www.bilibili.com/video/test'))
            c.commit()
        with closing(sqlite3.connect(taobao)) as c:
            c.execute('CREATE TABLE 商品(title TEXT,price REAL,pay_count INTEGER,url TEXT)')
            c.execute('INSERT INTO 商品 VALUES (?,?,?,?)', ('淘宝历史专用',25,7,'https://item.taobao.com/test'))
            c.commit()
        self.browser = FakeBrowser()
        self.app = create_app(web_db=self.root/'web.db', account_db=self.root/'accounts.db',
                              legacy_sources=[('bili_legacy','B站历史',bili),('taobao_legacy','淘宝历史',taobao)],
                              login_browser_factory=lambda: self.browser)
        self.ext = self.app.extensions['zforge']
        self.addCleanup(self.ext['store'].close)
        self.addCleanup(self.ext['pool'].close)
        self.addCleanup(lambda: self.ext['runner'].shutdown(wait=True))
        self.addCleanup(self.ext['logins'].shutdown)
        self.client = self.app.test_client()
        self.pid = self.ext['pool'].resolve_pool('taobao')

    def start(self, **changes):
        return self.client.post('/api/account-logins', json={'pool_id':self.pid,'phone':'13800000000',**changes})

    def finish(self, job):
        self.ext['logins']._jobs[job['id']]['_thread'].join(timeout=5)
        return self.client.get('/api/account-logins/'+job['id']).json

    def test_phone_default_name_saved_only_after_cookie_capture(self):
        response = self.start()
        self.assertEqual(response.status_code,202,response.text)
        job = self.finish(response.json)
        self.assertEqual(job['state'],'saved',job)
        account = job['account']
        self.assertEqual((account['phone'],account['label']),('13800000000','13800000000'))
        self.assertEqual(account['login_status'],'unknown')
        self.assertEqual(account['pool_ids'],[self.pid])
        self.assertTrue(self.browser.closed)
        self.assertEqual(self.browser.opened,'https://login.taobao.com/member/login.jhtml')
        self.assertNotIn('isolated-not-a-real-secret',json.dumps(job))
        self.assertEqual(self.ext['pool'].get(account['id']).used_today,0)

    def test_alias_and_profile_keep_identity_cookie_and_history(self):
        job = self.finish(self.start(label='工作用手机').json)
        aid = job['account']['id']
        old = self.ext['pool'].get_credential(aid)
        response = self.client.patch(f'/api/accounts/{aid}',json={'label':'新名称','phone':'13800000000'})
        self.assertEqual(response.status_code,200)
        self.assertEqual((response.json['label'],response.json['phone']),('新名称','13800000000'))
        self.assertEqual(self.ext['pool'].get_credential(aid),old)
        default = self.client.patch(f'/api/accounts/{aid}',json={'label':''})
        self.assertEqual(default.json['label'],'13800000000')

    def test_phone_required_and_masked_phone_rejected_without_browser(self):
        for phone in ('','138****0000',None):
            self.assertEqual(self.start(phone=phone).status_code,400)
        self.assertEqual(self.browser.opened,'')

    def test_cancel_closes_browser_and_saves_nothing(self):
        self.browser.cookies=[]
        response=self.start()
        self.client.post('/api/account-logins/'+response.json['id']+'/cancel')
        job=self.finish(response.json)
        self.assertEqual(job['state'],'canceled')
        self.assertEqual(self.ext['pool'].list(),[])
        self.assertTrue(self.browser.closed)

    def test_duplicate_identity_requires_explicit_same_identity_update(self):
        self.ext['pool'].import_cookies(self.pid,'旧名称',COOKIES)
        before=self.ext['pool'].get_credential(1)
        job=self.finish(self.start().json)
        self.assertEqual(job['state'],'failed')
        self.assertEqual(len(self.ext['pool'].list()),1)
        self.assertEqual(self.ext['pool'].get_credential(1),before)
        job=self.finish(self.start(update_account_id=1).json)
        self.assertEqual(job['state'],'saved',job)
        self.assertEqual(job['account']['label'],'旧名称')

    def test_browser_error_and_timeout_do_not_write_credentials(self):
        broken=BrowserLogins(self.ext['pool'],browser_factory=lambda: (_ for _ in ()).throw(RuntimeError('secret must not escape')))
        job=broken.start(self.pid,'13800000000')
        broken._jobs[job['id']]['_thread'].join(2)
        self.assertEqual(broken.get(job['id'])['state'],'failed')
        self.assertNotIn('secret must not escape',json.dumps(broken.get(job['id'])))
        timed=BrowserLogins(self.ext['pool'],browser_factory=lambda: self.browser,timeout=0)
        job=timed.start(self.pid,'13800000000')
        timed._jobs[job['id']]['_thread'].join(2)
        self.assertEqual(timed.get(job['id'])['state'],'expired')
        self.assertEqual(self.ext['pool'].list(),[])

    def test_cookie_site_and_required_keys(self):
        self.assertIsNone(login_cookies('taobao','https://evil.example/',COOKIES))
        self.assertIsNone(login_cookies('taobao','https://taobao.com.evil.example/',COOKIES))
        self.assertIsNone(login_cookies('taobao','https://www.taobao.com/',COOKIES[:1]))
        self.assertIsNotNone(login_cookies('taobao','https://login.taobao.com/member/login.jhtml',COOKIES))

    def test_current_and_history_are_separate_and_cannot_cross_project(self):
        self.ext['store'].insert_item(source='bilibili',item={'title':'当前B站专用','views':50,'url':'bili-current'})
        self.ext['store'].insert_item(source='taobao',item={'title':'当前淘宝专用','price':20,'pay_count':4,'url':'taobao-current'})
        catalog=self.client.get('/api/data/projects').json['projects']
        projects={p['id']:p for p in catalog}
        self.assertEqual({p['id'] for p in catalog},{'bilibili','taobao'})
        history=next(d for d in projects['bilibili']['datasets'] if d['origin']=='history')
        current=self.client.get('/api/data/projects/bilibili/datasets/current').json
        self.assertEqual([r['title'] for r in current['items']],['当前B站专用'])
        self.assertNotIn('淘宝',json.dumps(current,ensure_ascii=False))
        old=self.client.get('/api/data/projects/bilibili/datasets/'+history['id']).json
        self.assertEqual([r['title'] for r in old['items']],['B站历史专用'])
        self.assertEqual(json.loads(old['items'][0]['extra_json'])['favorite'],30)
        self.assertEqual(self.client.get('/api/data/projects/taobao/datasets/'+history['id']).status_code,404)
        self.ext['store'].insert_item(source='bilibili',item={'title':'第二条当前B站','views':20,'url':'bili-current-2'})
        limited=self.client.get('/api/data/projects/bilibili/datasets/current?limit=1').json
        self.assertEqual((len(limited['rows']),limited['total'],limited['limit'],limited['truncated']),(1,2,1,True))

    def test_empty_current_does_not_fall_back_to_history(self):
        current=self.client.get('/api/data/projects/taobao/datasets/current').json
        self.assertEqual(current['items'],[])
        self.assertEqual(current['total'],0)
        project=self.client.get('/api/data/projects').json['projects'][1]
        history=next(d for d in project['datasets'] if d['origin']=='history')
        old=self.client.get('/api/data/projects/taobao/datasets/'+history['id']).json
        self.assertEqual(old['items'][0]['title'],'淘宝历史专用')

    def test_driver_cache_requires_matching_browser_build(self):
        cache=self.root/'driver-cache'
        browser=self.root/'msedge.exe'
        browser.write_bytes(b'fixture')
        versions={browser:(155,0,4283,62)}
        for version in ((154,0,4283,99),(155,0,4283,45),(155,0,4283,60),(155,0,9999,99)):
            path=cache/'msedgedriver/win64'/'.'.join(map(str,version))/'msedgedriver.exe'
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
            versions[path]=version
        with patch.dict(os.environ,{'SE_CACHE_PATH':str(cache)}), patch('server.core.browser_runtime.file_version',side_effect=lambda p: versions.get(p)):
            selected=cached_edge_driver(browser)
        self.assertEqual(versions[selected],(155,0,4283,60))

    def test_incompatible_cached_driver_is_not_reused(self):
        cache=self.root/'driver-cache'
        browser=self.root/'msedge.exe'
        old=cache/'msedgedriver/win64/154.0.1.0/msedgedriver.exe'
        old.parent.mkdir(parents=True);old.write_bytes(b'fixture')
        versions={browser:(155,0,4283,62),old:(154,0,1,0)}
        with patch.dict(os.environ,{'SE_CACHE_PATH':str(cache)}), patch('server.core.browser_runtime.file_version',side_effect=lambda p: versions.get(p)):
            self.assertIsNone(cached_edge_driver(browser))


if __name__=='__main__':
    unittest.main()
