import json
import secrets
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
    def photo(self,content,**params):
        self.calls.append(('photo',params))
        if self.failure:
            failure=self.failure;self.failure=None;raise failure
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
        self.proof_files=[]
        self.path=Path(tempfile.gettempdir())/('cpbot-test-%s.sqlite3'%time.time_ns())
        self.s=Store(str(self.path),1);self.api=FakeAPI();self.bot=Bot(self.s,self.api,'review_bot',99)
        self.tid=self.s.save_teacher(1,'老师甲',10)
        self.bid=self.s.bind(1,'thread',{'id':-10011,'title':'专帖'},-10022)
        self.s.db.execute("INSERT OR REPLACE INTO settings VALUES('last_check',?)",(datetime.now(TZ).strftime('%Y-%m-%d'),))
    def tearDown(self):
        self.s.db.close()
        for path in self.proof_files:
            if path.exists():path.unlink()
        for path in self.recovery_paths:
            if path.exists():path.unlink()
            if path.suffix=='.json' and path.with_suffix('.zip').exists():path.with_suffix('.zip').unlink()
        for suffix in ('','-wal','-shm'):
            path=Path(str(self.path)+suffix)
            if path.exists():path.unlink()
    def draft(self,uid=2,code='abc',token='token',stamp='2026-10-01 10:00'):
        proof=secrets.token_hex(16)+'.jpg'
        path=self.s.proof_path(proof);path.parent.mkdir(exist_ok=True);path.write_bytes(b'photo')
        self.proof_files.append(path)
        d={'时间':stamp,'老师':self.tid,'出击证明':proof,'过程':'完整文字评价','匿名':True,**{f:8 for f in SCORES}}
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
    def test_direct_submit_idempotence_and_proof_required(self):
        self.draft();self.draft(3,token='other')
        self.assertEqual(self.s.submit({'id':2},'token')[1],'accepted')
        self.assertEqual(self.s.submit({'id':3},'other')[1],'accepted')
        self.assertEqual(self.s.submit({'id':2},'token')[0],1)
        with self.assertRaises(ValueError):self.s.add_codes(1,self.tid,['abc'])
        with self.assertRaises(ValueError):self.s.moderate(1,1,True)
        d=self.draft(4,token='four');d.pop('出击证明');self.s.save_draft(4,d,'four')
        with self.assertRaises(ValueError):self.s.submit({'id':4},'four')

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
    def test_proof_upload_preview_publication_and_backup(self):
        self.draft()
        self.bot.action({'id':2},'field:出击证明')
        self.bot.message(message(text='文字不能做证明'))
        self.assertIn('照片',self.api.calls[-1][1]['text'])
        self.api.upload=b'new photo'
        self.bot.message(message(photo=[{'file_id':'p','width':100,'height':100}]))
        d=self.s.draft(2)[0];path=self.s.proof_path(d['出击证明']);self.proof_files.append(path)
        self.assertEqual(path.read_bytes(),b'new photo')
        self.bot.action({'id':2},'preview')
        self.assertTrue(any(m=='photo' for m,_ in self.api.calls))
        rid,status=self.s.submit({'id':2,'username':'customer'},'token')
        self.assertEqual(status,'accepted')
        self.tick();self.forward();self.tick(8)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM proof_publications').fetchone()[0],1)
        proof_call=[p for m,p in self.api.calls if m=='photo' and p['chat_id']==-10022][-1]
        pub=self.s.db.execute('SELECT * FROM publications').fetchone()
        self.assertEqual(proof_call['reply_parameters']['message_id'],pub['message'])
        row=self.s.db.execute('SELECT * FROM reviews').fetchone()
        text=render_review(self.s,row)
        self.assertNotIn('作者',text);self.assertNotIn('留名',text)
        real=dict(row);real['anonymous']=0
        self.assertIn('tg://user?id=2',render_review(self.s,real))
        self.assertIn('@customer',render_review(self.s,real))
        data=validate_backup(self.s.export(99),99)
        self.assertIn(d['出击证明'],data['media'])
        data['media']={}
        with self.assertRaises(ValueError):validate_backup(json.dumps(data).encode(),99)
        self.bot.change_review(1,rid,None);self.tick(8)
        self.assertTrue(any(m=='photo' and p.get('_method')=='editMessageMedia' for m,p in self.api.calls))

    def test_split_backup_upload_mismatch_zip_safety_and_restore(self):
        import io,zipfile
        self.submit()
        self.bot.action({'id':1},'export')
        docs=[p for m,p in self.api.calls if m=='document']
        self.assertEqual(len(docs),2)
        text,images=docs[0]['content'],docs[1]['content']
        self.assertNotIn('media',json.loads(text))
        validate_backup(text,99,images)
        other_text,other_images=self.s.export(99,split=True)
        with self.assertRaisesRegex(ValueError,'同一批'):validate_backup(text,99,other_images)
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('manifest.json',json.dumps({'bundle':json.loads(text)['image_bundle']}))
            archive.writestr('../evil.jpg',b'bad')
        with self.assertRaises(ValueError):validate_backup(text,99,buffer.getvalue())
        self.bot.action({'id':1},'import')
        self.api.upload=images
        self.bot.message(message(uid=1,document={'file_id':'a','file_name':'images.zip'}))
        self.assertIn('继续上传',self.api.calls[-1][1]['text'])
        self.api.upload=text
        self.bot.message(message(uid=1,document={'file_id':'b','file_name':'data.json'}))
        self.assertIn('尚未导入',self.api.calls[-1][1]['text'])
        self.click('确认覆盖并恢复',uid=1)
        self.assertTrue(self.bot.paused())
        recovery=self.s.db.execute("SELECT detail FROM audit WHERE action='导入备份' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.recovery_paths.append(Path(recovery))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM reviews').fetchone()[0],1)

    def test_target_silent_observation_separate_from_lookup(self):
        self.s.db.execute("INSERT INTO settings VALUES('membership_target',?)",(json.dumps({'id':-10055,'title':'目标群'}),))
        before=len(self.api.calls)
        self.bot.message(message(uid=50,cid=-10055,text='@unknown_name') | {'from':{'id':50,'username':'old_name'}})
        self.assertEqual(len(self.api.calls),before)
        self.assertEqual(self.s.lookup(username='old_name')['user_id'],50)
        self.assertIsNone(self.s.lookup(username='unknown_name'))
        self.bot.message(message(uid=50,cid=-10055,text='编辑消息') | {'from':{'id':50,'username':'new_name'}},edited=True)
        self.assertIsNone(self.s.lookup(username='old_name'))
        self.assertEqual(self.s.lookup(username='new_name')['user_id'],50)
        self.bot.message(message(uid=51,cid=-10066,text='非目标群'))
        self.assertIsNone(self.s.lookup(user_id=51))
        self.s.db.execute('UPDATE bindings SET active=0')
        self.bot.message(message(uid=52,cid=-10055,text='专帖关闭仍记录') | {'from':{'id':52,'username':'quiet_member'}})
        self.assertEqual(self.s.lookup(username='quiet_member')['user_id'],52)
        self.bot.message(message(uid=52,cid=-10055,text='去掉用户名') | {'from':{'id':52}})
        self.assertIsNone(self.s.lookup(username='quiet_member'))
        self.bot.message(message(uid=50,cid=-10055,new_chat_members=[{'id':53,'username':'new_joiner'}]))
        person=self.s.lookup(user_id=53)
        self.assertEqual(person['username'],'new_joiner')
        self.bot.handle({'chat_member':{'chat':{'id':-10055,'type':'supergroup','title':'目标群'},'date':int(time.time())+1,
            'old_chat_member':{'status':'member','user':{'id':53}},
            'new_chat_member':{'status':'left','user':{'id':53,'username':'new_joiner'}}}})
        self.assertEqual(self.s.db.execute('SELECT present FROM memberships WHERE person=? AND chat_id=-10055',(person['id'],)).fetchone()[0],0)
        self.assertEqual(len(self.api.calls),before)

    def test_designated_membership_target_not_current_group(self):
        self.bot.action({'id':1},'membertargetset')
        self.bot.message(message(uid=1,text='-10055'))
        self.assertIn('检查通过',self.api.calls[-1][1]['text'])
        original=self.api.call
        def status(method,**params):
            if method=='getChatMember' and params['user_id']==50:
                self.assertEqual(params['chat_id'],-10055)
                return {'status':'restricted','is_member':True}
            return original(method,**params)
        self.api.call=status
        p=self.s.track('50');self.bot.show_person(2,p['id'])
        self.assertIn('是否在群：是',self.api.calls[-1][1]['text'])
        self.assertIn('入群时间：未知',self.api.calls[-1][1]['text'])
        self.s.bind(1,'lookup',{'id':-10044,'title':'查询群'})
        self.bot.message(message(text='查',cid=-10044,reply_to_message={'from':{'id':50},'date':1}))
        self.assertIn('判定群：-10055',self.api.calls[-1][1]['text'])
        def failed(method,**params):
            if method=='getChatMember' and params['user_id']==50:raise APIError(429,10)
            return original(method,**params)
        self.api.call=failed;self.bot.show_person(2,p['id'])
        self.assertIn('暂时无法确认',self.api.calls[-1][1]['text'])
        self.assertNotIn('是否在群：否',self.api.calls[-1][1]['text'])
        self.api.call=lambda method,**params: {'status':'left'} if method=='getChatMember' and params['user_id']==50 else original(method,**params)
        self.bot.show_person(2,p['id']);self.assertIn('是否在群：否',self.api.calls[-1][1]['text'])
        self.bot.show_person(2,self.s.track('@unknown_name')['id'])
        self.assertIn('尚未识别',self.api.calls[-1][1]['text'])
        with self.assertRaises(ValueError):self.bot.action({'id':2},'membertargetclear')
        text,images=self.s.export(99,split=True)
        self.assertEqual(validate_backup(text,99,images)['membership_target']['id'],-10055)
        self.api.call=original

    def test_proof_timeout_never_blindly_resends(self):
        self.submit();self.tick();self.forward()
        self.tick()  # publish review text
        self.s.db.execute("UPDATE jobs SET state='done' WHERE kind='summary'")
        self.api.failure=APIError(0)
        self.tick()  # proof upload result unknown
        job=self.s.db.execute("SELECT * FROM jobs WHERE kind='proof'").fetchone()
        self.assertEqual(job['state'],'uncertain')
        count=sum(m=='photo' for m,p in self.api.calls)
        self.tick(3)
        self.assertEqual(sum(m=='photo' for m,p in self.api.calls),count)
        self.bot.action({'id':1},'jobdone:%s'%job['id'])
        self.bot.message(message(uid=1,text='800'))
        self.assertEqual(self.s.db.execute('SELECT message FROM proof_publications').fetchone()[0],800)
        self.assertNotEqual(self.s.db.execute('SELECT message FROM publications').fetchone()[0],800)

    def test_proof_feed_retry_and_files_restore(self):
        self.s.bind(1,'feed',{'id':-10033,'title':'直发'})
        rid,_=self.submit()
        self.tick();self.forward();self.tick(12)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM proof_publications').fetchone()[0],2)
        self.assertTrue(any(m=='photo' and p['chat_id']==-10033 for m,p in self.api.calls))
        content=self.s.export(99)
        name=json.loads(self.s.db.execute('SELECT data FROM reviews').fetchone()[0])['出击证明']
        path=self.s.proof_path(name);path.unlink()
        recovery=self.s.recovery_backup
        recovery_path=self.path.with_suffix('.media-recovery.json');self.recovery_paths.append(recovery_path)
        recovery_path.write_bytes(content)
        self.s.recovery_backup=lambda bot:recovery_path
        try:self.s.import_backup(1,content,99)
        finally:self.s.recovery_backup=recovery
        self.assertEqual(path.read_bytes(),b'photo')
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM proof_publications').fetchone()[0],2)
        old=json.loads(content)
        old.pop('media');old['tables'].pop('proof_publications')
        for row in old['tables']['reviews']:
            d=json.loads(row['data']);d.pop('出击证明');d['留名']='旧留名';row['data']=json.dumps(d)
        old['tables']['jobs']=[j for j in old['tables']['jobs'] if j['kind']!='proof']
        validate_backup(json.dumps(old).encode(),99)

    def test_nightly_missing_rebuild_and_pacing(self):
        self.submit();self.tick();self.forward();self.tick(3)
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
        self.submit()
        self.api.failure=APIError(429,30);self.tick()
        job=self.s.db.execute("SELECT * FROM jobs WHERE kind='post'").fetchone();self.assertEqual(job['state'],'pending');self.assertGreater(job['due'],time.time())
        self.api.failure=APIError(0);self.tick();self.assertEqual(self.s.db.execute("SELECT state FROM jobs WHERE kind='post'").fetchone()[0],'uncertain')
        self.assertEqual(self.s.db.execute('SELECT status FROM reviews').fetchone()[0],'accepted')
    def test_withdraw_and_edit_recompute_no_resurrection(self):
        rid,_=self.submit();self.tick();self.forward();self.tick(3)
        with self.assertRaises(ValueError):self.bot.change_review(1,rid,{'综合':10})
        self.bot.change_review(1,rid,{f:10 for f in SCORES});self.tick(3)
        self.assertIn('10.0',self.bot.publisher.summary(self.tid)[0])
        self.bot.change_review(1,rid,None);self.tick(3)
        self.assertIn('暂无',self.bot.publisher.summary(self.tid)[0]);self.assertIn('撤下',str(self.api.calls))
        self.assertEqual(self.s.db.execute('SELECT status FROM reviews').fetchone()[0],'withdrawn')
    def test_score_form_derived_preview_and_invalid_inputs(self):

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
        rid,_=self.submit()
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
        with self.assertRaises(ValueError):self.bot.action({'id':1},'codefor:1')
        self.bot.action({'id':1},'adminadd')
        self.bot.message(message(uid=1,text='3'))
        self.assertIn('已加入名单',self.api.calls[-1][1]['text'])
        rid,_=self.submit(code='not-found')
        self.assertEqual(self.s.db.execute('SELECT status FROM reviews WHERE id=?',(rid,)).fetchone()[0],'accepted')
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
        self.submit();self.tick();self.forward();self.tick(3)
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
        self.recovery_paths.append(Path(recovery).with_suffix('.zip'))
        old=validate_backup(Path(recovery).read_bytes(),99,Path(recovery).with_suffix('.zip').read_bytes())
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

    def test_teacher_search_name_username_and_draft_return(self):
        self.bot.message(message(uid=10,text='/start') | {'from':{'id':10,'username':'Teacher_A'}})
        self.bot.action({'id':2},'field:老师')
        self.click('搜索老师')
        self.bot.message(message(text='@teacher_a'))
        self.assertIn('老师甲',self.api.calls[-1][1]['text']+str(self.api.calls[-1][1]['reply_markup']))
        self.click('老师甲 · #1')
        self.assertEqual(self.s.draft(2)[0]['老师'],self.tid)
        self.assertIn('车评草稿',self.api.calls[-2][1]['text'])
        self.bot.action({'id':2},'field:老师')
        self.bot.message(message(text='teacher_A'))
        self.assertIn('老师甲',str(self.api.calls[-1]))
        self.bot.message(message(text='不存在'))
        self.assertIn('没有匹配',self.api.calls[-1][1]['text'])
        self.click('查看全部老师')
        self.assertIn('老师甲',str(self.api.calls[-2]))
        self.bot.message(message(text='老师'))
        self.assertIn('老师甲',str(self.api.calls[-1]))
        with self.assertRaises(ValueError):self.bot.action({'id':2},'teachersearch:codefor')
        self.bot.message(message(uid=10,text='/start') | {'from':{'id':10,'username':'New_Name'}})
        self.bot.message(message(text='@teacher_a'))
        self.assertIn('没有匹配',self.api.calls[-1][1]['text'])
        self.bot.message(message(text='@new_name'))
        self.assertIn('老师甲',str(self.api.calls[-1]))

    def test_teacher_name_search_pagination_keeps_filter(self):
        self.s.import_teachers(1,[('筛选老师%s'%n,None) for n in range(10)])
        self.bot.action({'id':2},'field:老师')
        self.bot.message(message(text='筛选'))
        self.click('下一页')
        panel=self.api.calls[-2][1]
        self.assertIn('搜索：筛选',panel['text'])
        self.assertIn('筛选老师9',str(panel['reply_markup']))
        self.assertNotIn('老师甲',str(panel['reply_markup']))
        self.click('返回草稿')
        self.assertIn('车评草稿',self.api.calls[-2][1]['text'])

    def test_teacher_batch_preview_confirm_and_teacher_codes(self):
        self.bot.action({'id':1},'teacherbatch')
        self.bot.message(message(uid=1,text='老师乙\n老师丙 | 30\n老师丁 40'))
        panel=self.api.calls[-1][1]
        self.assertIn('尚未保存',panel['text'])
        self.assertIn('绑定账号 2 位',panel['text'])
        self.assertIn('账号 ID：30',panel['text'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM teachers').fetchone()[0],1)
        confirm=self.click('确认导入全部 3 位',uid=1)
        self.assertIn('已导入 3 位',self.api.calls[-2][1]['text'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM teachers').fetchone()[0],4)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM posts').fetchone()[0],4)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM jobs WHERE kind='post'").fetchone()[0],4)
        self.bot.home(30)
        self.assertNotIn('添加留名',str(self.api.calls[-1]))
        tid=self.s.db.execute('SELECT id FROM teachers WHERE user_id=30').fetchone()[0]
        self.assertEqual(self.s.teacher(tid)['user_id'],30)
        self.click('',uid=1,data=confirm)
        self.assertTrue(self.api.calls[-1][1]['show_alert'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM teachers').fetchone()[0],4)

    def test_teacher_batch_validation_cancel_expiry_and_concurrent_conflict(self):
        for text in ('', '老师甲', '重复\n重复', '乙 | 10', '乙 | 20\n丙 | 20',
                     '乙 | abc', '乙 | -1', '乙 | 0', '乙 | '+str(2**63),
                     '乙 | 20 | 30', '长'*81, '\n'.join('老师%s'%n for n in range(101))):
            with self.assertRaises(ValueError):self.s.parse_teachers(1,text)
        with self.assertRaises(ValueError):self.s.parse_teachers(2,'乙')
        with self.assertRaises(ValueError):self.bot.action({'id':2},'teacherbatch')
        self.bot.action({'id':1},'teacherbatch')
        self.bot.message(message(uid=1,text='老师乙 | abc'))
        self.assertIn('数字 ID',self.api.calls[-1][1]['text'])
        self.bot.message(message(uid=1,text='老师乙\n老师丙 | 30'))
        self.s.save_teacher(1,'其他老师',30)
        self.click('确认导入全部 2 位',uid=1)
        self.assertTrue(self.api.calls[-1][1]['show_alert'])
        self.assertIsNone(self.s.db.execute("SELECT 1 FROM teachers WHERE name='老师乙'").fetchone())
        self.click('取消导入',uid=1)
        self.assertFalse(self.bot.teacher_imports)
        self.bot.action({'id':1},'teacherbatch')
        self.bot.message(message(uid=1,text='老师乙'))
        self.bot.teacher_imports[1]['until']=0
        self.click('确认导入全部 1 位',uid=1)
        self.assertIn('过期',self.api.calls[-1][1]['text'])
        self.assertIsNone(self.s.db.execute("SELECT 1 FROM teachers WHERE name='老师乙'").fetchone())
        entries=[('老师乙',None),('老师戊',50)]
        self.s.db.execute("CREATE TRIGGER fail_batch BEFORE INSERT ON teachers WHEN NEW.name='老师戊' BEGIN SELECT RAISE(ABORT,'test'); END")
        before=self.s.db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):self.s.import_teachers(1,entries)
        self.assertIsNone(self.s.db.execute("SELECT 1 FROM teachers WHERE name='老师乙'").fetchone())
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM jobs').fetchone()[0],before)

    def test_teacher_batch_large_preview_pagination_and_revocation(self):
        self.bot.action({'id':1},'teacherbatch')
        self.bot.message(message(uid=1,text='\n'.join('批量老师%s'%n for n in range(100))))
        self.assertIn('第 1/7 页',self.api.calls[-1][1]['text'])
        for _ in range(6):self.click('下一页',uid=1)
        self.assertIn('100. 批量老师99',self.api.calls[-2][1]['text'])
        self.click('上一页',uid=1)
        self.s.set_admin(1,3);self.s.set_admin(3,1,False)
        self.click('确认导入全部 100 位',uid=1)
        self.assertTrue(self.api.calls[-1][1]['show_alert'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM teachers').fetchone()[0],1)

    def test_teacher_name_and_optional_identity_are_separate(self):
        self.bot.action({'id':1},'teachernew')
        self.bot.message(message(uid=1,text='只填名称'))
        tid=self.s.db.execute("SELECT id FROM teachers WHERE name='只填名称'").fetchone()[0]
        self.assertIsNone(self.s.teacher(tid)['user_id'])
        self.assertIn('未绑定',self.api.calls[-1][1]['text'])
        self.assertIsNotNone(self.s.db.execute('SELECT 1 FROM posts WHERE teacher=?',(tid,)).fetchone())
        self.bot.action({'id':1},'teacherbind:%s'%tid)
        self.bot.message(message(uid=1,text='20'))
        self.assertEqual(self.s.teacher(tid)['user_id'],20)
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
        self.assertIsNone(self.s.teacher(tid)['user_id'])
        with self.assertRaises(ValueError):self.bot.action({'id':2},'teacherbind:%s'%tid)

    def test_date_only_input_legacy_backup_and_invalid_dates(self):

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
