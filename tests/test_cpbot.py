import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from cpbot.__main__ import admin_ids
from cpbot.api import APIError
from cpbot.bot import Bot
from cpbot.identity import targets
from cpbot.publishing import render_review
from cpbot.reviews import TZ, SCORES
from cpbot.store import Store


class FakeAPI:
    def __init__(self):
        self.calls=[];self.failure=None;self.mid=100
    def call(self,method,**params):
        self.calls.append((method,params))
        if self.failure and method in ('sendMessage','editMessageText'):
            failure=self.failure;self.failure=None;raise failure
        if method=='getChat':
            cid=params['chat_id']
            return {'id':cid,'type':'channel' if cid in (-10011,-10033) else 'supergroup','title':str(cid),'linked_chat_id':-10022 if cid==-10011 else -10011}
        if method=='getChatMember':return {'status':'administrator','can_post_messages':True,'can_edit_messages':True,'user':{'id':params['user_id']}}
        self.mid+=1;return {'message_id':self.mid}
    def document(self,uid,name,content):self.calls.append(('document',{'uid':uid,'content':content}))


def message(uid=2,text='',cid=None,**extra):
    return {'from':{'id':uid,'first_name':'客户','username':'customer'},'text':text,'message_id':20,'date':int(time.time()),
            'chat':{'id':cid or uid,'type':'supergroup' if cid else 'private','title':'群'},**extra}


class Tests(unittest.TestCase):
    def setUp(self):
        # Explicit paths, no recursive cleanup.
        self.path=Path(tempfile.gettempdir())/('cpbot-test-%s.sqlite3'%time.time_ns())
        self.s=Store(str(self.path),1);self.api=FakeAPI();self.bot=Bot(self.s,self.api,'review_bot',99)
        self.tid=self.s.save_teacher(1,'老师甲',10)
        self.bid=self.s.bind(1,'thread',{'id':-10011,'title':'专帖'},-10022)
        self.s.db.execute("INSERT OR REPLACE INTO settings VALUES('last_check',?)",(datetime.now(TZ).strftime('%Y-%m-%d'),))
    def tearDown(self):
        self.s.db.close()
        for suffix in ('','-wal','-shm'):
            path=Path(str(self.path)+suffix)
            if path.exists():path.unlink()
    def draft(self,uid=2,code='abc',token='token',stamp='2026-10-01 10:00'):
        d={'时间':stamp,'老师':self.tid,'留名':code,'人照':'符合','过程':'完整文字评价','匿名':True,**{f:8 for f in SCORES}}
        self.s.save_draft(uid,d,token);return d
    def submit(self,uid=2,code='abc',token='token',stamp='2026-10-01 10:00'):
        self.draft(uid,code,token,stamp)
        return self.s.submit({'id':uid,'username':'customer'},token)
    def tick(self,n=1):
        for _ in range(n):
            self.bot.publisher.next_global=0;self.bot.publisher.next_chat={}
            self.s.db.execute("UPDATE jobs SET due=0 WHERE state='pending'")
            self.bot.publisher.tick()
    def forward(self):
        post=self.s.db.execute('SELECT * FROM posts').fetchone()
        self.bot.handle({'message':message(cid=-10022,is_automatic_forward=True,
            forward_origin={'type':'channel','chat':{'id':-10011},'message_id':post['message']})})
    def click(self,label,uid=2,data=None):
        if data is None:
            panels=[p for m,p in self.api.calls if m in ('sendMessage','editMessageText') and 'reply_markup' in p]
            data=next(b['callback_data'] for row in panels[-1]['reply_markup']['inline_keyboard'] for b in row if b['text']==label)
        self.bot.handle({'callback_query':{'id':'cb','from':{'id':uid},'message':{'chat':{'id':uid,'type':'private'},'message_id':100},'data':data}})
        return data
    def test_normal_form_codes_publish_links_scores(self):
        self.s.add_codes(10,self.tid,['abc'])
        self.bot.message(message(text='/start review_1'))
        self.assertEqual(self.s.draft(2)[0]['老师'],self.tid)
        d=self.draft();self.bot.action({'id':2},'field:综合');self.click('10')
        self.assertEqual(self.s.draft(2)[0]['综合'],10)
        self.bot.action({'id':2},'submit')
        r=self.s.db.execute('SELECT * FROM reviews').fetchone();self.assertEqual(r['status'],'accepted')
        self.tick();self.forward();self.tick(3)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM publications').fetchone()[0],1)
        self.assertIn('10.0 / 10',self.bot.publisher.summary(self.tid)[0])
        self.bot.action({'id':2},'mine:0')
        self.assertIn('https://t.me/c/22/',str(self.api.calls[-1]))
        self.assertNotIn('customer',render_review(self.s,r))
        data=self.click('返回主菜单');self.click('',data=data)
        self.assertTrue(self.api.calls[-1][1]['show_alert'])
    def test_concurrent_code_consumption_and_reuse(self):
        self.s.add_codes(10,self.tid,['abc'])
        with self.assertRaises(ValueError):self.s.add_codes(1,self.tid,['abc'])
        self.draft();self.draft(3,token='other')
        other=Store(str(self.path),1)
        try:
            self.assertEqual(self.s.submit({'id':2},'token')[1],'accepted')
            self.assertEqual(other.submit({'id':3},'other')[1],'pending')
            self.assertEqual(self.s.submit({'id':2},'token')[0],1)
        finally:other.db.close()
        self.s.add_codes(1,self.tid,['abc'])
        self.s.moderate(1,2,True)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM codes WHERE state='open'").fetchone()[0],1)
        with self.assertRaises(ValueError):self.s.moderate(1,2,True)
    def test_permissions_global_tags_and_group_opt_in(self):
        self.assertFalse(self.s.allowed(2))
        # Old password grants do not confer superadmin rights.
        self.s.db.execute("INSERT INTO grants VALUES(2,'old',0)");self.assertFalse(self.s.allowed(2))
        p=self.s.track('50')
        with self.assertRaises(ValueError):self.s.save(p['id'],0,{'id':2},'','bad')
        self.s.set_admin(1,2);self.s.save(p['id'],0,{'id':2},'备注','鸽子')
        self.bot.message(message(text='@target_a',cid=-10044));self.assertEqual(len(self.api.calls),0)
        self.s.bind(1,'lookup',{'id':-10044,'title':'内群'})
        self.bot.message(message(text='查询',cid=-10044,reply_to_message={'from':{'id':50},'date':1}))
        self.assertIn('鸽子',str(self.api.calls[-1]));self.assertIn('新增',str(self.api.calls[-1]))
        self.s.set_admin(1,2,False)
        with self.assertRaises(ValueError):self.bot.action({'id':2},'edit:%s'%p['id'])
        with self.assertRaises(ValueError):self.s.set_admin(1,1,False)
    def test_binding_conflicts_required_and_admin_checks(self):
        for cid in (-10011,-10022):
            with self.assertRaises(ValueError):self.s.bind(1,'lookup',{'id':cid,'title':'冲突'})
        self.assertEqual(self.bot.publisher.validate('thread',-10011)[1],-10022)
        self.bot.action({'id':1},'unbindyes:%s'%self.bid)
        self.assertFalse(self.s.ready());self.bot.home(2)
        self.assertIn('尚未配置',str(self.api.calls[-1]))
        self.bot.home(1);self.assertIn('管理后台',str(self.api.calls[-1]))
    def test_moderation_expiry_manual_and_export(self):
        rid,_=self.submit();self.s.moderate(1,rid,True)
        r=self.s.db.execute('SELECT * FROM reviews').fetchone()
        self.assertIn('人工审核通过',render_review(self.s,r))
        rid2,_=self.submit(3,token='three');self.s.db.execute('UPDATE reviews SET expires=? WHERE id=?',(time.time()-1,rid2))
        with self.assertRaises(ValueError):self.s.moderate(1,rid2,True)
        self.tick()
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM reviews').fetchone()[0],1)
        backup=self.s.export();restored=sqlite3.connect(':memory:');restored.executescript(backup.decode())
        self.assertEqual(restored.execute('SELECT count(*) FROM reviews').fetchone()[0],1);restored.close()
        self.s.set_admin(1,3);self.bot.action({'id':3},'export');self.assertTrue(any(m=='document' for m,_ in self.api.calls))
    def test_nightly_missing_rebuild_and_pacing(self):
        self.s.add_codes(1,self.tid,['abc']);self.submit();self.tick();self.forward();self.tick(3)
        self.s.db.execute("DELETE FROM settings WHERE key='last_check'")
        stamp=datetime(2026,10,30,2,30,tzinfo=TZ).timestamp()
        self.bot.publisher.nightly(stamp);count=self.s.db.execute("SELECT count(*) FROM jobs WHERE kind='check'").fetchone()[0]
        self.bot.publisher.nightly(stamp);self.assertEqual(count,self.s.db.execute("SELECT count(*) FROM jobs WHERE kind='check'").fetchone()[0])
        self.s.db.execute("INSERT OR REPLACE INTO settings VALUES('last_check',?)",(datetime.now(TZ).strftime('%Y-%m-%d'),))
        self.tick()  # scheduled health check runs before message repair
        self.api.failure=APIError(400,description='Bad Request: message to edit not found')
        self.tick()
        self.assertEqual(self.s.db.execute('SELECT generation FROM posts').fetchone()[0],2)
        self.tick();self.forward();self.tick(3)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM publications WHERE generation=2').fetchone()[0],1)
        calls=len(self.api.calls);self.bot.publisher.next_global=time.time()+100;self.bot.publisher.tick();self.assertEqual(calls,len(self.api.calls))
    def test_rate_limit_uncertain_and_failed_send_keeps_code_used(self):
        self.s.add_codes(1,self.tid,['abc']);self.submit()
        self.api.failure=APIError(429,30);self.tick()
        job=self.s.db.execute("SELECT * FROM jobs WHERE kind='post'").fetchone();self.assertEqual(job['state'],'pending');self.assertGreater(job['due'],time.time())
        self.api.failure=APIError(0);self.tick();self.assertEqual(self.s.db.execute("SELECT state FROM jobs WHERE kind='post'").fetchone()[0],'uncertain')
        self.assertEqual(self.s.db.execute('SELECT state FROM codes').fetchone()[0],'used')
    def test_withdraw_and_edit_recompute_no_resurrection(self):
        self.s.add_codes(1,self.tid,['abc']);rid,_=self.submit();self.tick();self.forward();self.tick(3)
        self.bot.change_review(1,rid,{'综合':10});self.tick(3)
        self.assertIn('10.0',self.bot.publisher.summary(self.tid)[0])
        self.bot.change_review(1,rid,None);self.tick(3)
        self.assertIn('暂无',self.bot.publisher.summary(self.tid)[0]);self.assertIn('撤下',str(self.api.calls))
        self.assertEqual(self.s.db.execute('SELECT status FROM reviews').fetchone()[0],'withdrawn')
    def test_identity_recycling_and_unknown_username(self):
        p=self.s.track('@target_a');self.s.save(p['id'],0,{'id':1},'备注','标签')
        self.s.observe({'id':50,'username':'target_a'},100,True)
        self.s.observe({'id':51,'username':'target_a'},200,True)
        self.assertEqual(self.s.lookup(user_id=50)['tags'],'标签');self.assertEqual(self.s.lookup(user_id=51)['tags'],'')
        self.assertEqual(len(targets(message(reply_to_message={'from':{'id':60},'date':1}))),1)
    def test_env_admin_additions_and_revocation_semantics(self):
        self.assertEqual(admin_ids({'SUPERADMIN_ID':'1','SUPERADMIN_IDS':'2, 3 2，4'}),[1,2,3,4])
        for value in ('', '0', '-1', 'abc', '1,,bad', str(2**63)):
            with self.assertRaises(ValueError):admin_ids({'SUPERADMIN_IDS':value})
        self.s.set_admin(1,5)
        self.s.db.close();self.s=Store(str(self.path),1,[1,2,3])
        self.assertTrue(all(self.s.admin(uid) for uid in (1,2,3,5)))
        self.s.set_admin(1,2,False);self.assertFalse(self.s.admin(2))
        self.s.db.close();self.s=Store(str(self.path),1,[1,2,3])
        self.assertTrue(self.s.admin(2))
        self.s.set_admin(1,2,False)
        self.s.db.close();self.s=Store(str(self.path),1,[1])
        self.assertFalse(self.s.admin(2));self.assertTrue(self.s.admin(3));self.assertTrue(self.s.admin(5))

    def test_pause_and_bootstrap_not_readded(self):
        self.s.set_admin(1,3);self.s.set_admin(3,1,False)
        self.s.db.close();self.s=Store(str(self.path),1)
        self.assertFalse(self.s.admin(1));self.assertTrue(self.s.admin(3))
        self.bot=Bot(self.s,self.api,'review_bot',99)
        self.bot.action({'id':3},'pausejobs');self.tick()
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM jobs WHERE state='done'").fetchone()[0],0)
        self.bot.action({'id':3},'resumejobs');self.tick()
        self.assertGreater(self.s.db.execute("SELECT count(*) FROM jobs WHERE state='done'").fetchone()[0],0)

    def test_legacy_schema_migration_preserves_data(self):
        p=self.s.track('80');self.s.save(p['id'],0,{'id':1},'旧备注','旧标签');self.s.db.close()
        self.s=Store(str(self.path),1)
        self.assertEqual(self.s.lookup(user_id=80)['tags'],'旧标签');self.assertTrue(self.s.admin(1))


if __name__=='__main__':unittest.main()
