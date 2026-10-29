import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from cpbot.__main__ import admin_ids
from cpbot.backup import validate_backup, MAX_BACKUP_BYTES
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
    def download_document(self,document,max_bytes):return self.upload
    def document(self,uid,name,content):self.calls.append(('document',{'uid':uid,'content':content}))


def message(uid=2,text='',cid=None,**extra):
    return {'from':{'id':uid,'first_name':'客户','username':'customer'},'text':text,'message_id':20,'date':int(time.time()),
            'chat':{'id':cid or uid,'type':'supergroup' if cid else 'private','title':'群'},**extra}


class Tests(unittest.TestCase):
    def setUp(self):
        # Explicit paths, no recursive cleanup.
        self.recovery_paths=[]
        self.path=Path(tempfile.gettempdir())/('cpbot-test-%s.sqlite3'%time.time_ns())
        self.s=Store(str(self.path),1);self.api=FakeAPI();self.bot=Bot(self.s,self.api,'review_bot',99)
        self.tid=self.s.save_teacher(1,'老师甲',10)
        self.bid=self.s.bind(1,'thread',{'id':-10011,'title':'专帖'},-10022)
        self.s.db.execute("INSERT OR REPLACE INTO settings VALUES('last_check',?)",(datetime.now(TZ).strftime('%Y-%m-%d'),))
    def tearDown(self):
        self.s.db.close()
        for path in self.recovery_paths:
            if path.exists():path.unlink()
        for suffix in ('','-wal','-shm'):
            path=Path(str(self.path)+suffix)
            if path.exists():path.unlink()
    def draft(self,uid=2,code='abc',token='token',stamp='2026-10-01 10:00'):
        d={'时间':stamp,'老师':self.tid,'留名':code,'过程':'完整文字评价','匿名':True,**{f:8 for f in SCORES}}
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
        d=self.draft();self.bot.action({'id':2},'field:人照');self.click('10')
        self.assertEqual(self.s.draft(2)[0]['人照'],10)
        self.bot.action({'id':2},'submit')
        r=self.s.db.execute('SELECT * FROM reviews').fetchone();self.assertEqual(r['status'],'accepted')
        self.tick();self.forward();self.tick(3)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM publications').fetchone()[0],1)
        self.assertIn('8.3 / 10',self.bot.publisher.summary(self.tid)[0])
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
        self.assertIn('鸽子',str(self.api.calls[-1]))
        self.assertEqual([b['text'] for b in self.api.calls[-1][1]['reply_markup']['inline_keyboard'][0]], ['新增标签','删除标签'])
        self.bot.show_person(2,p['id'])
        self.assertEqual([b['text'] for b in self.api.calls[-1][1]['reply_markup']['inline_keyboard'][0]], ['新增标签','删除标签'])
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
        backup=self.s.export();restored=Store(':memory:',1)
        try:
            recovery=restored.import_backup(1,backup)
            self.assertEqual(restored.db.execute('SELECT count(*) FROM reviews').fetchone()[0],1)
            recovery.unlink()
        finally:restored.db.close()
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
        with self.assertRaises(ValueError):self.bot.change_review(1,rid,{'综合':10})
        self.bot.change_review(1,rid,{f:10 for f in SCORES});self.tick(3)
        self.assertIn('10.0',self.bot.publisher.summary(self.tid)[0])
        self.bot.change_review(1,rid,None);self.tick(3)
        self.assertIn('暂无',self.bot.publisher.summary(self.tid)[0]);self.assertIn('撤下',str(self.api.calls))
        self.assertEqual(self.s.db.execute('SELECT status FROM reviews').fetchone()[0],'withdrawn')
    def test_score_form_derived_preview_and_invalid_inputs(self):
        self.s.add_codes(1,self.tid,['abc'])
        d=self.draft();d['综合']=1
        self.s.save_draft(2,d,'token')
        self.bot.draft_panel(2)
        panel=self.api.calls[-1][1]
        self.assertNotIn('field:综合',str(panel['reply_markup']))
        self.assertIn('8.0（自动计算）',panel['text'])
        with self.assertRaises(ValueError):self.bot.action({'id':2},'field:综合')
        self.bot.action({'id':2},'field:人照')
        buttons=self.api.calls[-1][1]['reply_markup']['inline_keyboard']
        self.assertEqual([b['text'] for row in buttons[:2] for b in row],[str(n) for n in range(1,11)])
        for bad in (0,11,True,'符合'):
            d['人照']=bad;self.s.save_draft(2,d,'token')
            with self.assertRaises(ValueError):self.s.submit({'id':2},'token')
        self.bot.draft_panel(2)
        self.assertIn('需重新评分',self.api.calls[-1][1]['text'])
        d['人照']=10;self.s.save_draft(2,d,'token')
        self.bot.action({'id':2},'preview')
        self.assertIn('【综合】：8.3',self.api.calls[-1][1]['text'])
        rid,_=self.s.submit({'id':2},'token')
        saved=json.loads(self.s.db.execute('SELECT data FROM reviews WHERE id=?',(rid,)).fetchone()[0])
        self.assertAlmostEqual(saved['综合'],50/6)
        validate_backup(self.s.export(99),99)
        broken=json.loads(self.s.export(99));saved['综合']=1
        broken['tables']['reviews'][0]['data']=json.dumps(saved)
        with self.assertRaises(ValueError):validate_backup(json.dumps(broken).encode(),99)

    def test_legacy_text_score_backup_and_admin_correction(self):
        self.s.add_codes(1,self.tid,['abc']);rid,_=self.submit()
        old=json.loads(self.s.db.execute('SELECT data FROM reviews').fetchone()[0])
        old.update({'人照':'符合','综合':9})
        self.s.db.execute('UPDATE reviews SET data=? WHERE id=?',(json.dumps(old),rid))
        self.s.save_draft(3,old,'legacy')
        backup=validate_backup(self.s.export(99),99)
        self.assertEqual(json.loads(backup['tables']['reviews'][0]['data'])['人照'],'符合')
        self.bot.change_review(1,rid,{'过程':'修正过程'})
        self.assertIn('9.0 / 10',self.bot.publisher.summary(self.tid)[0])
        with self.assertRaises(ValueError):self.bot.change_review(1,rid,{'颜值':10})
        self.bot.change_review(1,rid,{'人照':10})
        self.assertIn('8.3 / 10',self.bot.publisher.summary(self.tid)[0])
        validate_backup(self.s.export(99),99)

    def test_identity_recycling_and_unknown_username(self):
        p=self.s.track('@target_a');self.s.save(p['id'],0,{'id':1},'备注','标签')
        self.s.observe({'id':50,'username':'target_a'},100,True)
        self.s.observe({'id':51,'username':'target_a'},200,True)
        self.assertEqual(self.s.lookup(user_id=50)['tags'],'标签');self.assertEqual(self.s.lookup(user_id=51)['tags'],'')
        self.assertEqual(len(targets(message(reply_to_message={'from':{'id':60},'date':1}))),1)
    def test_binding_reports_exact_permission_target(self):
        original=self.api.call
        self.assertEqual(self.bot.publisher.validate('thread',-10011)[1],-10022)
        def group_member(method,**params):
            if method=='getChatMember' and params['chat_id']==-10022:
                self.assertEqual(params['user_id'],99)
                return {'status':'member'}
            return original(method,**params)
        self.api.call=group_member
        with self.assertRaisesRegex(ValueError,'关联讨论组.*频道权限已通过.*普通成员'):
            self.bot.publisher.validate('thread',-10011)
        def missing_channel_right(method,**params):
            if method=='getChatMember' and params['chat_id']==-10011:
                return {'status':'administrator','can_post_messages':True,'can_edit_messages':False}
            return original(method,**params)
        self.api.call=missing_channel_right
        with self.assertRaisesRegex(ValueError,'已是管理员.*编辑消息'):
            self.bot.publisher.validate('thread',-10011)
        def no_group_access(method,**params):
            if method=='getChat' and params['chat_id']==-10022:raise APIError(400)
            return original(method,**params)
        self.api.call=no_group_access
        with self.assertRaisesRegex(ValueError,'关联讨论组.*频道权限已通过.*无法访问'):
            self.bot.publisher.validate('thread',-10011)
        def limited(method,**params):
            if method=='getChatMember':raise APIError(429,30)
            return original(method,**params)
        self.api.call=limited
        with self.assertRaises(APIError) as error:self.bot.publisher.validate('thread',-10011)
        self.assertEqual(error.exception.code,429)
        self.api.call=original

    def test_binding_feedback_all_modes_and_failure_retry(self):
        # Real entry points, not just store mutations.
        for mode,cid in (('thread',-10011),('feed',-10033),('lookup',-10044)):
            if mode=='thread':self.bot.action({'id':1},'unbindyes:%s'%self.bid)
            self.bot.action({'id':1},'bind:'+mode)
            self.bot.message(message(uid=1,text=str(cid)))
            panel=self.api.calls[-1][1]
            self.assertIn('✅',panel['text']);self.assertIn('绑定成功',panel['text'])
            self.assertIn(str(cid),panel['text']);self.assertIn('已启用',panel['text'])
            self.assertIn('待处理',panel['text'])
        self.bot.action({'id':1},'bind:thread')
        self.bot.message(message(uid=1,text='-10011'))
        self.assertIn('⚠️',self.api.calls[-1][1]['text'])
        self.assertEqual(self.bot.sessions[1]['pending'],('bind','thread'))
        self.bot.action({'id':1},'bind:feed')
        original=self.api.call
        def fail(method,**params):
            if method=='getChat':raise APIError(403)
            return original(method,**params)
        self.api.call=fail
        self.bot.message(message(uid=1,text='-10055'))
        self.assertIn('无法访问',self.api.calls[-1][1]['text'])
        self.assertEqual(self.bot.sessions[1]['pending'],('bind','feed'))
        self.api.call=original

    def test_validation_retains_draft_and_callback_menu(self):
        self.bot.action({'id':2},'write')
        nonce=self.bot.sessions[2]['nonce']
        self.click('提交')
        self.assertEqual(self.bot.sessions[2]['nonce'],nonce)
        self.assertIn('还未填写',self.api.calls[-1][1]['text'])
        self.assertTrue(self.api.calls[-1][1]['show_alert'])
        self.click('时间')
        self.bot.message(message(text='yesterday'))
        self.assertIn('YYYY-MM-DD',self.api.calls[-1][1]['text'])
        self.assertNotIn('strptime',self.api.calls[-1][1]['text'])
        self.bot.message(message(text='2026-10-29'))
        self.assertIn('时间已保存',self.api.calls[-1][1]['text'])
        self.assertIn('1/10',self.api.calls[-1][1]['text'])
        self.bot.action({'id':2},'anon')
        self.assertIn('✅ 实名',str(self.api.calls[-1]))

    def test_mutation_feedback_and_operational_states(self):
        self.bot.action({'id':1},'teachernew')
        self.bot.message(message(uid=1,text='新增老师'))
        self.assertIn('已保存',self.api.calls[-1][1]['text'])
        self.bot.action({'id':1},'codefor:1')
        self.bot.message(message(uid=1,text='first\nsecond'))
        self.assertIn('添加 2 个留名',self.api.calls[-1][1]['text'])
        self.bot.action({'id':1},'adminadd')
        self.bot.message(message(uid=1,text='3'))
        self.assertIn('已加入名单',self.api.calls[-1][1]['text'])
        rid,_=self.submit(code='not-found')
        self.bot.action({'id':1},'moderate:%s'%rid)
        self.click('通过',uid=1)
        self.assertIn('已通过',str(self.api.calls[-2]));self.assertIn('已通过',self.api.calls[-1][1]['text'])
        self.bot.action({'id':1},'pausejobs')
        self.assertIn('⏸ 已暂停',self.api.calls[-1][1]['text'])
        self.bot.home(2);self.assertIn('发布队列已暂停',self.api.calls[-1][1]['text'])
        self.bot.action({'id':1},'bindings')
        self.click('老师专帖 · 专帖',uid=1)
        self.assertIn('待处理',self.api.calls[-2][1]['text'])
        self.assertIn('全局发布已暂停',self.api.calls[-2][1]['text'])
        self.bot.action({'id':1},'jobs:0')
        self.assertNotIn('pending=',self.api.calls[-1][1]['text'])

    def test_json_import_end_to_end_permissions_preview_and_recovery(self):
        self.s.add_codes(1,self.tid,['abc']);self.submit();self.tick();self.forward();self.tick(3)
        p=self.s.track('50');self.s.save(p['id'],0,{'id':1},'旧备注','旧标签')
        backup=self.s.export(99)
        self.s.save(p['id'],1,{'id':1},'新备注','新标签')
        self.s.set_admin(1,3)
        self.s.db.execute("INSERT OR REPLACE INTO settings VALUES('offset','123')")
        self.api.upload=backup
        self.bot.action({'id':3},'import')
        self.bot.message(message(uid=3,document={'file_id':'file','file_name':'backup.json'}))
        self.assertIn('尚未导入',self.api.calls[-1][1]['text'])
        self.assertEqual(self.s.person(p['id'])['tags'],'新标签')
        self.click('确认覆盖并恢复',uid=3)
        self.assertEqual(self.s.person(p['id'])['tags'],'旧标签')
        self.assertTrue(self.s.admin(3));self.assertFalse(self.s.ready());self.assertTrue(self.bot.paused())
        self.assertEqual(self.s.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()[0],'123')
        recovery=self.s.db.execute("SELECT detail FROM audit WHERE action='导入备份' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.recovery_paths.append(Path(recovery))
        old=validate_backup(Path(recovery).read_bytes(),99)
        self.assertEqual(old['tables']['people'][0]['tags'],'新标签')
        self.assertTrue(any(m=='document' for m,_ in self.api.calls))
        with self.assertRaises(ValueError):self.bot.action({'id':2},'import')

    def test_import_failure_rolls_back_and_preserves_recovery(self):
        content=self.s.export(99)
        self.s.db.execute("CREATE TRIGGER fail_import BEFORE INSERT ON teachers BEGIN SELECT RAISE(ABORT,'test failure'); END")
        # Fix a known recovery filename for deterministic evidence and cleanup.
        recovery=self.path.with_suffix('.recovery.json')
        self.recovery_paths.append(recovery)
        original=self.s.recovery_backup
        def backup(bot):
            recovery.write_bytes(self.s.export(bot))
            return recovery
        self.s.recovery_backup=backup
        try:
            with self.assertRaises(sqlite3.IntegrityError):self.s.import_backup(1,content,99)
            self.assertEqual(self.s.teacher(self.tid)['name'],'老师甲')
            self.assertTrue(self.s.ready());self.assertTrue(self.s.admin(1))
            self.assertEqual(validate_backup(recovery.read_bytes(),99)['version'],1)
        finally:self.s.recovery_backup=original

    def test_json_backup_rejects_invalid_and_cancel_expire(self):
        content=self.s.export(99)
        data=json.loads(content)
        for bad in (b'DROP TABLE teachers;',b'{',content[:10],b'x'*(MAX_BACKUP_BYTES+1)):
            with self.assertRaises(ValueError):validate_backup(bad,99)
        for mutate in (
            lambda d:d.update(version=999),
            lambda d:d.update(source_bot=100),
            lambda d:d['tables'].pop('codes'),
            lambda d:d['tables']['teachers'][0].update(id='wrong'),
            lambda d:d['tables']['posts'][0].update(teacher=999),
            lambda d:d['tables']['bindings'][0].update(discussion=-10011),
        ):
            d=json.loads(content);mutate(d)
            with self.assertRaises(ValueError):validate_backup(json.dumps(d).encode(),99)
        self.api.upload=content
        self.bot.action({'id':1},'import')
        self.bot.message(message(uid=1,document={'file_id':'f','file_name':'old.sql'}))
        self.assertIn('旧 SQL',self.api.calls[-1][1]['text'])
        self.bot.message(message(uid=1,document={'file_id':'f','file_name':'new.json'}))
        self.click('取消，不改数据',uid=1);self.assertTrue(self.s.ready());self.assertFalse(self.bot.imports)
        self.bot.action({'id':1},'import')
        self.bot.message(message(uid=1,document={'file_id':'f','file_name':'new.json'}))
        self.bot.imports[1]['until']=0
        self.click('确认覆盖并恢复',uid=1)
        self.assertTrue(self.s.ready());self.assertIn('过期',self.api.calls[-1][1]['text'])

    def test_teacher_name_and_optional_identity_are_separate(self):
        self.bot.action({'id':1},'teachernew')
        self.bot.message(message(uid=1,text='只填名称'))
        tid=self.s.db.execute("SELECT id FROM teachers WHERE name='只填名称'").fetchone()[0]
        self.assertIsNone(self.s.teacher(tid)['user_id'])
        self.assertIn('未绑定',self.api.calls[-1][1]['text'])
        self.assertIsNotNone(self.s.db.execute('SELECT 1 FROM posts WHERE teacher=?',(tid,)).fetchone())
        self.bot.action({'id':1},'teacherbind:%s'%tid)
        self.bot.message(message(uid=1,text='20'))
        self.s.add_codes(20,tid,['留名'])
        self.bot.action({'id':1},'teacherrename:%s'%tid)
        self.bot.message(message(uid=1,text='新名称'))
        self.assertEqual(self.s.teacher(tid)['user_id'],20)
        self.assertEqual(self.s.teacher(tid)['name'],'新名称')
        self.bot.action({'id':1},'teacherbind:%s'%tid)
        self.bot.message(message(uid=1,text='10'))
        self.assertEqual(self.s.teacher(tid)['user_id'],20)
        self.assertIn('已绑定其他老师',self.api.calls[-1][1]['text'])
        self.bot.action({'id':1},'teacherunbindyes:%s'%tid)
        with self.assertRaises(ValueError):self.s.add_codes(20,tid,['其他'])
        self.s.add_codes(1,tid,['其他'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM codes WHERE teacher=?',(tid,)).fetchone()[0],2)
        with self.assertRaises(ValueError):self.bot.action({'id':2},'teacherbind:%s'%tid)

    def test_date_only_input_legacy_backup_and_invalid_dates(self):
        self.s.add_codes(1,self.tid,['abc'])
        self.draft(stamp='2026-10-29')
        self.bot.action({'id':2},'field:时间')
        for bad in ('2026-02-30','2026-10-29 14:30','2026-1-2'):
            self.bot.message(message(text=bad))
            self.assertIn('⚠️',self.api.calls[-1][1]['text'])
        self.bot.message(message(text='2026-10-29'))
        rid,status=self.s.submit({'id':2},'token')
        self.assertEqual(status,'accepted')
        self.assertEqual(json.loads(self.s.db.execute('SELECT data FROM reviews WHERE id=?',(rid,)).fetchone()[0])['时间'],'2026-10-29')
        self.bot.change_review(1,rid,{'时间':'2026-10-28'})
        d=json.loads(self.s.db.execute('SELECT data FROM reviews WHERE id=?',(rid,)).fetchone()[0])
        d['时间']='2026-10-28 14:30'
        self.s.db.execute('UPDATE reviews SET data=? WHERE id=?',(json.dumps(d),rid))
        validate_backup(self.s.export(99),99)
        r=self.s.db.execute('SELECT * FROM reviews WHERE id=?',(rid,)).fetchone()
        self.assertIn('【时间】：2026-10-28',render_review(self.s,r))
        self.assertNotIn('14:30',render_review(self.s,r))

    def test_master_body_link_and_existing_keyboard_migration(self):
        self.tick()
        post=self.s.db.execute('SELECT * FROM posts').fetchone()
        published=[p for method,p in self.api.calls if method=='sendMessage' and p['chat_id']==-10011][-1]
        self.assertEqual(published['reply_markup']['inline_keyboard'],[])
        self.assertIn('href="https://t.me/review_bot?start=review_1"',published['text'])
        self.assertEqual(published['parse_mode'],'HTML')
        self.assertTrue(published['link_preview_options']['is_disabled'])
        self.s.db.execute("DELETE FROM settings WHERE key='summary_body_link_v1'")
        self.bot=Bot(self.s,self.api,'review_bot',99)
        self.tick()
        edited=[p for method,p in self.api.calls if method=='editMessageText'][-1]
        self.assertEqual(edited['message_id'],post['message'])
        self.assertEqual(edited['reply_markup'],{'inline_keyboard':[]})
        self.assertIn('给这位老师写评价</a>',edited['text'])
        count=self.s.db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        self.bot=Bot(self.s,self.api,'review_bot',99)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM jobs').fetchone()[0],count)

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
