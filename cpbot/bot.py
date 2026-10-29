import html
import json
import secrets
import sqlite3
import time
from datetime import datetime

from .api import APIError
from .identity import targets, member_present
from .store import display_name
from .reviews import FIELDS, SCORES, MODES, TZ, now_text, link
from .publishing import Publisher, render_review


def date(stamp):
    return now_text(stamp) if stamp else '未知'


class Bot:
    def __init__(self, store, api, username, bot_id=0):
        self.store, self.api, self.username = store, api, username
        self.sessions, self.triggers = {}, {}
        self.publisher = Publisher(store, api, username, bot_id)
        self.edit_target = None

    def send(self, chat, text, rows=None, **extra):
        return self.api.call('sendMessage', chat_id=chat, text=text,
                             reply_markup={'inline_keyboard': rows or []}, **extra)

    def panel(self, uid, text, rows, pending=None, urls=None, **extra):
        nonce = secrets.token_hex(5)
        keyboard = [[{'text': label, 'callback_data': nonce + '|' + action} for label, action in row] for row in rows]
        keyboard += urls or []
        session = {'nonce': nonce, 'until': time.monotonic() + 600, 'pending': pending}
        self.sessions[uid] = session
        if self.edit_target:
            mid = self.edit_target
            self.edit_target = None
            try:
                result = self.api.call('editMessageText', chat_id=uid, message_id=mid, text=text,
                                       reply_markup={'inline_keyboard': keyboard}, **extra)
                session['message'] = mid
                return result
            except APIError as error:
                if 'message is not modified' in error.description:
                    session['message'] = mid
                    return
                raise
        result = self.send(uid, text, keyboard, **extra)
        session['message'] = result['message_id']
        return result

    def home(self, uid):
        rows = []
        if self.store.ready():
            rows = [[('🌷 写车评', 'write'), ('🔎 找老师', 'findteacher')],
                    [('👤 查学生', 'search'), ('📋 我的车评', 'mine:0')]]
            if self.store.db.execute('SELECT 1 FROM teachers WHERE user_id=?', (uid,)).fetchone():
                rows += [[('添加留名', 'mycodes')]]
        if self.store.admin(uid):
            rows += [[('⚙️ 管理后台', 'admin')]]
        return self.panel(uid, '请选择功能。' if self.store.ready() else '机器人尚未配置完成，请联系管理员绑定老师专帖频道。', rows)

    def prompt(self, uid, text, pending, back='home'):
        return self.panel(uid, text, [[('取消 / 返回', back)]], pending)

    def show_person(self, uid, pid, intent=None, notice=''):
        p = self.store.person(pid)
        if intent == 'delete':
            self.store.require_admin(uid)
            return self.panel(uid, self.card(p) + '\n\n确定清空标签？保留身份、备注和修改记录。',
                [[('确认删除', 'delete:%s:%s' % (pid, p['version']))], [('取消', 'person:%s' % pid)]])
        return self.panel(uid, (notice + '\n\n' if notice else '') + self.card(p),
                          [[('新增', 'edit:%s' % pid), ('删除标签', 'askdelete:%s' % pid)],
                           [('修改记录', 'history:%s:0' % pid)], [('返回主菜单', 'home')]])

    def draft_panel(self, uid):
        d, token = self.store.draft(uid)
        if not token:
            token = secrets.token_hex(12)
            self.store.save_draft(uid, d, token)
        lines = ['车评草稿 · 点击字段填写，提交前可修改']
        rows = []
        for field in FIELDS:
            value = d.get(field)
            if field == '老师' and value:
                value = self.store.teacher(int(value))['name']
            lines.append('【%s】：%s' % (field, str(value) if value is not None else '未填写'))
        for i in range(0, len(FIELDS), 2):
            rows.append([(f + (' ✓' if d.get(f) is not None else ''), 'field:' + f) for f in FIELDS[i:i+2]])
        rows += [[('✅ 匿名' if d.get('匿名', True) else '实名', 'anon'), ('预览', 'preview')],
                 [('提交', 'submit'), ('返回主菜单', 'home')]]
        return self.panel(uid, '\n'.join(lines), rows)

    def teachers(self, uid, intent='choose', search='', page=0):
        teachers = self.store.db.execute('SELECT * FROM teachers WHERE instr(lower(name),lower(?))>0 ORDER BY id LIMIT 9 OFFSET ?',
                                        (search, page*8)).fetchall()
        rows = [[('%s · #%s' % (t['name'], t['id']), '%s:%s' % (intent, t['id']))] for t in teachers[:8]]
        # Search state is retained in a short-lived session, not callback text.
        if len(teachers)>8:
            rows.append([('下一页', 'teacherspage:%s:%s' % (intent, page+1))])
        if page:
            rows.append([('上一页', 'teacherspage:%s:%s' % (intent, page-1))])
        return self.panel(uid, '选择老师' + ('' if teachers else '\n没有匹配的老师。'), rows + [[('返回主菜单','home')]], ('teacherlist',intent,search))

    def admin_panel(self, uid):
        self.store.require_admin(uid)
        return self.panel(uid, '管理后台', [
            [('老师列表', 'teachersadmin'), ('添加老师', 'teachernew')],
            [('留名管理', 'codesadmin'), ('待审核', 'queue:0')],
            [('频道 / 群绑定', 'bindings'), ('superadmin 名单', 'admins')],
            [('发布任务', 'jobs:0'), ('车评处理', 'reviewmanage')],
            [('导出数据备份', 'export'), ('返回主菜单', 'home')]])

    def action(self, user, action):
        uid = user['id']
        parts = action.split(':'); kind = parts[0]
        admin_kinds = {'admin','teachersadmin','teachernew','teacheredit','codesadmin','codefor','admins','adminadd','adminremove',
                       'bindings','bind','binding','recheck','unbind','unbindyes','queue','moderate','approve','reject',
                       'setroot','pausejobs','resumejobs','removeadmin','export','jobs','job','retry','jobdone','reviewmanage','reviewadmin','withdraw','withdrawyes','reviewedit'}
        if kind in admin_kinds:
            self.store.require_admin(uid)
        if kind not in admin_kinds and kind not in ('home','cancel') and not self.store.ready():
            raise ValueError('尚无可用的老师专帖绑定，业务暂不可用。')
        if kind in ('home','cancel'):
            return self.home(uid)
        if kind == 'admin':
            return self.admin_panel(uid)
        if kind == 'write':
            return self.draft_panel(uid)
        if kind == 'field':
            field = parts[1]
            if field not in FIELDS:
                raise ValueError('字段不存在。')
            if field == '老师':
                return self.teachers(uid)
            if field in SCORES:
                return self.panel(uid, '选择%s分数（满分 10 分）' % field,
                    [[(str(n), 'score:%s:%s' % (field,n)) for n in range(start,start+5)] for start in (1,6)] + [[('返回草稿','write')]])
            return self.prompt(uid, '请输入%s。%s' % (field, '格式 YYYY-MM-DD HH:MM；不核对预约日期。' if field=='时间' else ''), ('field',field), 'write')
        if kind in ('choose','score','anon'):
            d, token = self.store.draft(uid)
            if not token:
                token=secrets.token_hex(12)
            if kind=='choose':
                self.store.teacher(int(parts[1])); d['老师']=int(parts[1])
            elif kind=='score':
                if parts[1] not in SCORES or not 1<=int(parts[2])<=10:
                    raise ValueError('无效分数。')
                d[parts[1]]=int(parts[2])
            else:
                d['匿名']=not d.get('匿名',True)
            self.store.save_draft(uid,d,token)
            return self.draft_panel(uid)
        if kind=='preview':
            d, token=self.store.draft(uid)
            if not d.get('老师'):
                raise ValueError('请先选择老师。')
            row={'data':json.dumps(d),'teacher':d['老师'],'manual':0,'anonymous':d.get('匿名',True),'identity':json.dumps(user),'author':uid,'id':'预览'}
            return self.panel(uid,render_review(self.store,row),[[('返回修改','write'),('确认提交','submit')]],parse_mode='HTML')
        if kind=='submit':
            _,token=self.store.draft(uid)
            rid,status=self.store.submit(user,token)
            return self.panel(uid,'车评 #%s：%s' % (rid,'核验通过，正在排队发布。' if status=='accepted' else '留名未通过，已进入审核。审核区保留 72 小时。'),[[('返回主菜单','home')]])
        if kind=='findteacher':
            return self.prompt(uid,'输入老师名称，或发送“全部”浏览。',('findteacher',))
        if kind=='teacherspage':
            old=getattr(self,'previous_pending',None)
            return self.teachers(uid,parts[1],old[2] if old and old[0]=='teacherlist' else '',int(parts[2]))
        if kind=='teacherlink':
            t=self.store.teacher(int(parts[1])); urls=[]
            for p in self.store.db.execute('SELECT p.*,b.chat_id,b.title FROM posts p JOIN bindings b ON b.id=p.binding WHERE p.teacher=? AND p.message IS NOT NULL AND b.active=1',(t['id'],)):
                urls.append([{'text':p['title'],'url':link(p['chat_id'],p['message'])}])
            return self.panel(uid,t['name']+(' · 选择评价主帖' if urls else ' · 主帖正在准备中。'),[[('给这位老师写评价','choose:%s'%t['id']),('返回主菜单','home')]],urls=urls)
        if kind=='mine':
            page=max(0,int(parts[1])); rows=[]; text='我的车评 · 第 %s 页'%(page+1); urls=[]
            reviews=self.store.db.execute("SELECT * FROM reviews WHERE author=? AND status IN ('accepted','withdrawn') ORDER BY id DESC LIMIT 9 OFFSET ?",(uid,page*8)).fetchall()
            for r in reviews[:8]:
                pub=self.store.db.execute('''SELECT p.* FROM publications p JOIN bindings b ON b.id=p.binding
                    LEFT JOIN posts t ON t.binding=p.binding AND t.teacher=? WHERE p.review=? AND b.active=1
                    AND (b.mode='feed' OR p.generation=t.generation) ORDER BY CASE b.mode WHEN 'thread' THEN 0 ELSE 1 END,b.id LIMIT 1''',(r['teacher'],r['id'])).fetchone()
                label='#%s %s · %s'%(r['id'],self.store.teacher(r['teacher'])['name'],json.loads(r['data'])['时间'])
                if r['status']=='withdrawn': text+='\n'+label+' · 已撤下'
                elif pub: urls.append([{'text':label,'url':link(pub['chat_id'],pub['message'])}])
                else: text+='\n'+label+' · 待发布 / 请联系管理员核对任务'
            if not reviews: text+='\n暂无已通过车评。'
            if page: rows.append([('上一页','mine:%s'%(page-1))])
            if len(reviews)>8: rows.append([('下一页','mine:%s'%(page+1))])
            return self.panel(uid,text,rows+[[('返回主菜单','home')]],urls=urls)
        if kind=='search': return self.prompt(uid,'发送客户 ID、@用户名，或转发目标消息。',('search',))
        if kind=='person': return self.show_person(uid,int(parts[1]))
        if kind in ('edit','askdelete','delete'):
            self.store.require_admin(uid)
            p=self.store.person(int(parts[1]))
            if kind=='askdelete': return self.show_person(uid,p['id'],'delete')
            if kind=='edit': return self.prompt(uid,'发送两行：新增备注、追加标签。某行不新增请填 -。',('addtags',p['id'],p['version']),'person:%s'%p['id'])
            self.store.save(p['id'],int(parts[2]),user,p['note'],'','删除标签')
            return self.show_person(uid,p['id'])
        if kind=='history': return self.history(uid,int(parts[1]),max(0,int(parts[2])))
        if kind=='entry':
            row=self.store.db.execute('SELECT * FROM history WHERE id=?',(int(parts[1]),)).fetchone()
            if not row: raise ValueError('记录不存在。')
            return self.panel(uid,'操作人：%s（%s）\n时间：%s\n修改前：%s\n修改后：%s'%(row['actor_name'],row['actor'],date(row['created']),row['before_value'],row['after_value']),[[('返回主菜单','home')]])
        if kind in ('teachernew','teacheredit'):
            return self.prompt(uid,'发送两行：老师名称、老师 Telegram 数字 ID（不绑定填 -）。',('teacher',int(parts[1]) if kind=='teacheredit' else None),'admin')
        if kind=='teachersadmin': return self.teachers(uid,'teacheredit')
        if kind=='codesadmin': return self.teachers(uid,'codefor')
        if kind=='mycodes':
            t=self.store.db.execute('SELECT * FROM teachers WHERE user_id=?',(uid,)).fetchone()
            if not t: raise ValueError('未绑定老师身份。')
            return self.action(user,'codes:%s'%t['id'])
        if kind in ('codes','codefor'):
            tid=int(parts[1]); t=self.store.teacher(tid)
            if not self.store.admin(uid) and t['user_id']!=uid: raise ValueError('只能操作自己的留名。')
            count=self.store.db.execute("SELECT count(*) FROM codes WHERE teacher=? AND state='open'",(tid,)).fetchone()[0]
            return self.prompt(uid,'%s · 未使用留名 %s 个\n发送留名，每行一个；未使用的同名留名不能重复。'%(t['name'],count),('codes',tid),'admin' if self.store.admin(uid) else 'home')
        if kind=='admins':
            rows=[[('移除 %s'%r[0],'adminremove:%s'%r[0])] for r in self.store.db.execute('SELECT user_id FROM admins ORDER BY user_id')]
            return self.panel(uid,'superadmin 名单',rows+[[('添加','adminadd'),('返回','admin')]])
        if kind=='adminadd': return self.prompt(uid,'发送要添加的 Telegram 数字 ID。',('adminadd',),'admins')
        if kind=='adminremove':
            return self.panel(uid,'确认移除 %s？'%parts[1],[[('确认移除','removeadmin:%s'%parts[1])],[('取消','admins')]])
        if kind=='removeadmin':
            self.store.set_admin(uid,int(parts[1]),False);return self.home(uid)
        if kind=='bindings':
            rows=[[('%s · %s%s'%(MODES[b['mode']],b['title'],'（暂停）' if not b['active'] else ''),'binding:%s'%b['id'])] for b in self.store.db.execute('SELECT * FROM bindings')]
            rows += [[('添加'+name,'bind:'+mode)] for mode,name in MODES.items()]
            return self.panel(uid,'模式绑定 · 频道与群不可重复占用',rows+[[('返回','admin')]])
        if kind=='bind': return self.prompt(uid,'发送目标频道/群的数字 ID 或 @用户名。先把机器人设为管理员；专帖频道必须关联讨论组。',('bind',parts[1]),'bindings')
        if kind=='binding':
            b=self.store.db.execute('SELECT * FROM bindings WHERE id=?',(int(parts[1]),)).fetchone()
            if not b: raise ValueError('绑定不存在。')
            return self.panel(uid,'%s · %s\n频道/群：%s\n讨论组：%s\n%s'%(MODES[b['mode']],b['title'],b['chat_id'],b['discussion'] or '无',b['error'] or '已启用'),[[('重新检查权限 / 恢复','recheck:'+parts[1]),('解除绑定','unbind:'+parts[1])], [('登记讨论串根消息','setroot:'+parts[1])],[('返回','bindings')]])
        if kind=='setroot':
            return self.prompt(uid,'自动转发事件未收到时，可手动补充。发送两行：老师编号、讨论组中该主帖自动转发消息的 message_id。',('setroot',int(parts[1])),'bindings')
        if kind=='recheck':
            b=self.store.db.execute('SELECT * FROM bindings WHERE id=?',(int(parts[1]),)).fetchone()
            if not b: raise ValueError('绑定不存在。')
            _,discussion=self.publisher.validate(b['mode'],b['chat_id'])
            if discussion!=b['discussion']: raise ValueError('讨论组已变更，请解除旧绑定后重新绑定。')
            self.store.db.execute("UPDATE bindings SET active=1,error='' WHERE id=?",(b['id'],))
            self.store.db.execute("UPDATE jobs SET state='pending' WHERE binding=? AND state='blocked'",(b['id'],))
            return self.action(user,'binding:'+parts[1])
        if kind=='unbind': return self.panel(uid,'解除绑定会停止后续发布，已发出的消息保留。最后一个专帖绑定解除后业务停止。',[[('确认解除','unbindyes:'+parts[1])],[('取消','bindings')]])
        if kind=='unbindyes':
            with self.store.transaction():
                bid=int(parts[1]); self.store.db.execute("UPDATE jobs SET state='superseded' WHERE binding=?",(bid,))
                self.store.db.execute('DELETE FROM publications WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM jobs WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM posts WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM bindings WHERE id=?',(bid,));self.store.log(uid,'解除绑定',bid)
            return self.action(user,'bindings')
        if kind=='queue':
            page=max(0,int(parts[1])); records=self.store.db.execute("SELECT * FROM reviews WHERE status='pending' AND expires>? ORDER BY id LIMIT 9 OFFSET ?",(time.time(),page*8)).fetchall()
            rows=[[('#%s · %s'%(r['id'],self.store.teacher(r['teacher'])['name']),'moderate:%s'%r['id'])] for r in records[:8]]
            if len(records)>8:rows.append([('下一页','queue:%s'%(page+1))])
            if page:rows.append([('上一页','queue:%s'%(page-1))])
            return self.panel(uid,'待审核 · 保留 72 小时',rows+[[('返回','admin')]])
        if kind=='moderate':
            r=self.store.db.execute("SELECT * FROM reviews WHERE id=? AND status='pending' AND expires>?",(int(parts[1]),time.time())).fetchone()
            if not r:raise ValueError('记录已过期或处理。')
            return self.panel(uid,render_review(self.store,r)+'\n\n后台留名：'+html.escape(json.loads(r['data'])['留名'])+'\n作者 ID：'+str(r['author']),[[('通过','approve:'+parts[1]),('不通过','reject:'+parts[1])],[('返回','queue:0')]],parse_mode='HTML')
        if kind in ('approve','reject'):
            self.store.moderate(uid,int(parts[1]),kind=='approve');return self.action(user,'queue:0')
        if kind=='export':
            self.api.document(uid,'cpbot-%s.sql'%datetime.now(TZ).strftime('%Y%m%d-%H%M%S'),self.store.export())
            return self.admin_panel(uid)
        if kind=='reviewmanage':return self.prompt(uid,'输入车评编号。',('reviewmanage',),'admin')
        if kind=='reviewadmin':
            r=self.store.db.execute('SELECT * FROM reviews WHERE id=?',(int(parts[1]),)).fetchone()
            if not r:raise ValueError('车评不存在。')
            return self.panel(uid,render_review(self.store,r)+'\n状态：'+r['status'],[[('修改字段','reviewedit:'+parts[1]),('撤下','withdraw:'+parts[1])],[('返回','admin')]],parse_mode='HTML')
        if kind=='reviewedit':return self.prompt(uid,'发送 JSON 对象，例如 {"综合":8,"过程":"更正后的内容"}。仅可修改时间、人照、评分、过程；不会重新消耗留名。',('reviewedit',int(parts[1])),'reviewadmin:'+parts[1])
        if kind=='withdraw':return self.panel(uid,'确认撤下？将停止计分，各已发布消息改为撤下提示。',[[('确认撤下','withdrawyes:'+parts[1])],[('取消','reviewadmin:'+parts[1])]])
        if kind=='withdrawyes':
            self.change_review(uid,int(parts[1]),None)
            return self.action(user,'reviewadmin:'+parts[1])
        if kind in ('pausejobs','resumejobs'):
            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('publishing_paused',?)",('1' if kind=='pausejobs' else '0',))
            self.store.log(uid,'暂停发布' if kind=='pausejobs' else '继续发布','')
            return self.action(user,'jobs:0')
        if kind=='jobs':
            page=max(0,int(parts[1])); rows=self.store.db.execute("SELECT * FROM jobs WHERE state IN ('uncertain','blocked') ORDER BY id LIMIT 9 OFFSET ?",(page*8,)).fetchall()
            counts=', '.join('%s=%s'%(r[0],r[1]) for r in self.store.db.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
            buttons=[[('#%s %s · %s'%(r['id'],r['state'],r['error'][:20]),'job:%s'%r['id'])] for r in rows[:8]]
            if len(rows)>8:buttons.append([('下一页','jobs:%s'%(page+1))])
            if page:buttons.append([('上一页','jobs:%s'%(page-1))])
            return self.panel(uid,'发布任务\n'+counts,buttons+[[('暂停任务','pausejobs'),('继续任务','resumejobs')],[('返回','admin')]])
        if kind=='job':
            j=self.store.db.execute('SELECT * FROM jobs WHERE id=?',(int(parts[1]),)).fetchone()
            if not j:raise ValueError('任务不存在。')
            return self.panel(uid,'任务 #%s · %s\n%s\n结果不明时请先核对目标：若已发送，登记远端消息 ID；若确认未发送，才重试。'%(j['id'],j['kind'],j['error']),[[('确认未发送，重试','retry:'+parts[1]),('已发送，登记消息 ID','jobdone:'+parts[1])],[('返回','jobs:0')]])
        if kind=='retry':
            self.store.db.execute("UPDATE jobs SET state='pending',due=0 WHERE id=? AND state IN ('blocked','uncertain')",(int(parts[1]),));self.store.log(uid,'人工重试任务',parts[1]);return self.action(user,'jobs:0')
        if kind=='jobdone':return self.prompt(uid,'发送目标中已发出消息的数字 message_id。',('jobdone',int(parts[1])),'jobs:0')
        raise ValueError('未知操作，请返回主菜单。')

    def change_review(self, uid, rid, changes):
        self.store.require_admin(uid)
        with self.store.transaction():
            r=self.store.db.execute("SELECT * FROM reviews WHERE id=? AND status='accepted'",(rid,)).fetchone()
            if not r:raise ValueError('仅可处理正式有效车评。')
            if changes is None:self.store.db.execute("UPDATE reviews SET status='withdrawn',version=version+1 WHERE id=?",(rid,))
            else:
                if not isinstance(changes,dict) or any(k not in (*SCORES,'时间','人照','过程') for k in changes):raise ValueError('包含不可修改的字段。')
                d=json.loads(r['data']);d.update(changes)
                if any(type(d[k]) is not int or not 1<=d[k]<=10 for k in SCORES):raise ValueError('分数需要 1～10 的整数。')
                datetime.strptime(d['时间'],'%Y-%m-%d %H:%M')
                if not isinstance(d['过程'],str) or not 1<=len(d['过程'])<=1600 or not isinstance(d['人照'],str) or not 1<=len(d['人照'])<=200:raise ValueError('过程或人照长度不正确。')
                self.store.db.execute('UPDATE reviews SET data=?,version=version+1 WHERE id=?',(json.dumps(d,ensure_ascii=False),rid))
            self.store.log(uid,'修改车评' if changes else '撤下车评',rid)
            for b in self.store.db.execute("SELECT * FROM bindings WHERE mode IN ('thread','feed')"):
                if b['mode']=='feed' and not self.store.db.execute('SELECT 1 FROM publications WHERE binding=? AND review=?',(b['id'],rid)).fetchone():
                    continue
                key=int(time.time()*1000000)
                self.store.enqueue('check' if b['mode']=='thread' else 'feed',b['id'],r['teacher'],rid,key)
                if b['mode']=='thread':self.store.enqueue('summary',b['id'],r['teacher'],generation=key)

    def callback(self, q):
        uid=q['from']['id'];msg=q.get('message',{});nonce,_,action=q.get('data','').partition('|');s=self.sessions.get(uid)
        if msg.get('chat',{}).get('type')!='private' or msg['chat']['id']!=uid or not s or nonce!=s['nonce'] or s['until']<time.monotonic():
            return self.api.call('answerCallbackQuery',callback_query_id=q['id'],text='菜单已过期，请发送 /start。',show_alert=True)
        self.sessions.pop(uid,None);self.previous_pending=s.get('pending');self.edit_target=msg.get('message_id') or s.get('message')
        try:
            self.action(q['from'],action)
        except (ValueError,sqlite3.IntegrityError) as e:
            self.api.call('answerCallbackQuery',callback_query_id=q['id'],text=str(e)[:180] if isinstance(e,ValueError) else '数据冲突，请重新检查。',show_alert=True)
            self.home(uid);return
        finally:self.edit_target=None
        self.api.call('answerCallbackQuery',callback_query_id=q['id'])

    def message(self, msg, edited=False):
        if self.publisher.automatic_forward(msg):return
        chat=msg['chat'];user=msg.get('from',{});uid=user.get('id');text=msg.get('text','').strip()
        if chat['type'] in ('group','supergroup'):
            b=self.store.db.execute("SELECT 1 FROM bindings WHERE mode='lookup' AND chat_id=? AND active=1",(chat['id'],)).fetchone()
            if b and self.store.ready():
                self.observe_message(msg)
                if uid and not edited and not user.get('is_bot'):self.query(msg)
            return
        if chat['type']!='private' or not uid or edited:return
        try:
            if text.split(' ',1)[0].split('@')[0] in ('/start','/cancel'):
                self.sessions.pop(uid,None);payload=text.partition(' ')[2]
                if payload.startswith('review_') and payload[7:].isdigit():return self.action(user,'choose:'+payload[7:])
                if payload.startswith(('edit_','delete_')) and payload.partition('_')[2].isdigit():
                    return self.action(user,('edit:' if payload.startswith('edit_') else 'askdelete:')+payload.partition('_')[2])
                return self.home(uid)
            if not self.store.ready() and not self.store.admin(uid):return self.home(uid)
            session=self.sessions.get(uid);pending=session.get('pending') if session else None
            if session and session['until']<time.monotonic():
                self.sessions.pop(uid,None);return self.home(uid)
            if not pending:
                if targets(msg) or text.isdigit():
                    if text.isdigit():return self.show_person(uid,self.store.track(text)['id'])
                    return self.query(msg)
                return self.home(uid)
            kind=pending[0]
            if kind=='field':
                if not text:raise ValueError('请发送文字，不接收图片。')
                limits={'过程':1600,'人照':200,'留名':80,'时间':16}
                if len(text)>limits[pending[1]]:raise ValueError('内容超过长度限制。')
                if pending[1]=='时间':datetime.strptime(text,'%Y-%m-%d %H:%M')
                d,token=self.store.draft(uid);d[pending[1]]=text;self.store.save_draft(uid,d,token or secrets.token_hex(12));return self.draft_panel(uid)
            if kind=='findteacher':return self.teachers(uid,'teacherlink','' if text=='全部' else text)
            if kind=='search':
                found=targets(msg);p=self.resolve(found[0]) if found else self.store.track(text)
                return self.show_person(uid,p['id'],notice=self.match_notice(found[0]) if found else '')
            if kind=='codes':
                self.store.add_codes(uid,pending[1],text.splitlines());return self.action(user,'codes:%s'%pending[1])
            self.store.require_admin(uid)
            if kind=='addtags':
                lines=text.splitlines()
                if len(lines)!=2:raise ValueError('需要两行：备注、标签；不新增填 -。')
                p=self.store.person(pending[1]);note,tags_=[x.strip() for x in lines]
                note='\n'.join(filter(None,(p['note'],'' if note=='-' else note)));tags_='、'.join(filter(None,(p['tags'],'' if tags_=='-' else tags_)))
                self.store.save(p['id'],pending[2],user,note,tags_,'新增标签');return self.show_person(uid,p['id'])
            if kind=='teacher':
                lines=text.splitlines()
                if len(lines)!=2:raise ValueError('需要两行：名称、数字 ID 或 -。')
                self.store.save_teacher(uid,lines[0],None if lines[1]=='-' else int(lines[1]),pending[1]);return self.admin_panel(uid)
            if kind=='adminadd':self.store.set_admin(uid,int(text));return self.action(user,'admins')
            if kind=='bind':
                cid=int(text) if text.lstrip('-').isdigit() else text
                c,d=self.publisher.validate(pending[1],cid);self.store.bind(uid,pending[1],c,d);return self.action(user,'bindings')
            if kind=='setroot':
                tid,root=map(int,text.splitlines())
                b=self.store.db.execute("SELECT * FROM bindings WHERE id=? AND mode='thread'",(pending[1],)).fetchone()
                if not b or root<=0:raise ValueError('无效绑定或消息 ID。')
                changed=self.store.db.execute('UPDATE posts SET root=? WHERE binding=? AND teacher=? AND message IS NOT NULL',(root,b['id'],tid)).rowcount
                if not changed:raise ValueError('老师主帖尚未创建。')
                self.store.log(uid,'登记讨论根消息',text);return self.action(user,'bindings')
            if kind=='reviewmanage':return self.action(user,'reviewadmin:'+str(int(text)))
            if kind=='reviewedit':self.change_review(uid,pending[1],json.loads(text));return self.action(user,'reviewadmin:%s'%pending[1])
            if kind=='jobdone':
                mid=int(text)
                if mid<=0:raise ValueError('消息 ID 必须大于 0。')
                with self.store.transaction():
                    j=self.store.db.execute("SELECT j.*,b.mode,b.chat_id,b.discussion FROM jobs j JOIN bindings b ON b.id=j.binding WHERE j.id=? AND j.state='uncertain'",(pending[1],)).fetchone()
                    if not j:raise ValueError('任务不在待核对状态。')
                    if j['kind']=='post':self.store.db.execute('UPDATE posts SET message=? WHERE binding=? AND teacher=?',(mid,j['binding'],j['teacher']))
                    elif j['review']:
                        p=self.store.db.execute('SELECT generation FROM posts WHERE binding=? AND teacher=?',(j['binding'],j['teacher'])).fetchone()
                        self.store.db.execute('INSERT OR REPLACE INTO publications VALUES(?,?,?,?,?)',(j['binding'],j['review'],p[0] if p else 1,mid,j['discussion'] if j['mode']=='thread' else j['chat_id']))
                    self.store.db.execute("UPDATE jobs SET state='done' WHERE id=?",(j['id'],));self.store.log(uid,'登记发送结果',j['id'])
                return self.action(user,'jobs:0')
        except (ValueError,TypeError,sqlite3.IntegrityError) as error:
            self.send(uid,str(error)[:200] if not isinstance(error,sqlite3.IntegrityError) else '数据冲突，可能身份已绑定或内容重复。')

    def handle(self, update):
        if 'callback_query' in update:return self.callback(update['callback_query'])
        if 'message' in update:return self.message(update['message'])
        if 'edited_message' in update:return self.message(update['edited_message'],True)
        if 'chat_member' in update:
            e=update['chat_member']
            if self.store.db.execute("SELECT 1 FROM bindings WHERE chat_id=? AND mode='lookup' AND active=1",(e['chat']['id'],)).fetchone():self.member_update(e)
        if 'my_chat_member' in update:
            e=update['my_chat_member'];cid=e['chat']['id']
            if e['chat'].get('type') == 'channel' and e['new_chat_member']['status'] in ('administrator','creator'):
                try:
                    self.publisher.validate('thread',cid)
                    notice='频道检查通过，可在后台选择绑定模式。'
                except (ValueError,APIError):
                    notice='频道检查未通过，请核对机器人发布/编辑权限、关联讨论组及其管理员权限。'
                for admin in self.store.db.execute('SELECT user_id FROM admins'):
                    try:self.send(admin[0],notice+'\n频道 ID：'+str(cid))
                    except APIError:pass
            if e['new_chat_member']['status'] not in ('administrator','creator'):
                self.store.db.execute("UPDATE bindings SET active=0,error='机器人管理员权限已失效' WHERE chat_id=? OR discussion=?",(cid,cid))

    def card(self, p, chat_id=None):
        text = '\n'.join(('当前名称：' + (p['name'] or '未知'),
                          'Telegram ID：' + (str(p['user_id']) if p['user_id'] else '待识别'),
                          '用户名：' + ('@' + p['username'] if p['username'] else '未知 / 未设置'),
                          '群备注：' + (p['note'] or '无'), '群标签：' + (p['tags'] or '无')))
        memberships = self.store.db.execute('SELECT * FROM memberships WHERE person=?' +
                                            (' AND chat_id=?' if chat_id else '') + ' ORDER BY observed DESC LIMIT 8',
                                            (p['id'], chat_id) if chat_id else (p['id'],)).fetchall()
        if not memberships:
            text += '\n是否在群：未知\n入群时间：未知'
        for m in memberships:
            text += '\n\n%s\n是否在群：%s\n入群时间：%s\n状态更新：%s' % (
                m['title'][:100], {1: '是', 0: '否'}.get(m['present'], '未知'), date(m['joined']), date(m['observed']))
        return text
    def history(self, uid, pid, page):
        where, args = (' WHERE person=?', [pid]) if pid else ('', [])
        rows = self.store.db.execute('SELECT * FROM history' + where + ' ORDER BY id DESC LIMIT 4 OFFSET ?',
                                     (*args, page * 3)).fetchall()
        text = '修改记录 · 第 %s 页' % (page + 1)
        # A single entry's full before/after can exceed a page. Show summaries here,
        # and a dedicated full entry on demand without truncating stored data.
        buttons = []
        for row in rows[:3]:
            text += '\n\n%s · %s\n%s · 档案 #%s' % (date(row['created']), row['actor_name'], row['action'], row['person'])
            buttons.append([('查看记录 #%s' % row['id'], 'entry:%s' % row['id'])])
        if not rows:
            text += '\n暂无记录。'
        nav = []
        if page:
            nav.append(('上一页', 'history:%s:%s' % (pid, page - 1)))
        if len(rows) > 3:
            nav.append(('下一页', 'history:%s:%s' % (pid, page + 1)))
        if nav:
            buttons.append(nav)
        buttons.append([('返回资料', 'person:%s' % pid)] if pid else [('返回首页', 'home')])
        return self.panel(uid, text, buttons)
    def observe_message(self, message):
        chat, stamp = message['chat'], message.get('date', time.time())
        if not message.get('sender_chat'):
            p = self.store.observe(message.get('from', {}), stamp, create=True)
            if p:
                self.store.member(p['id'], chat, True, stamp)
        for user in message.get('new_chat_members', []):
            p = self.store.observe(user, stamp, create=True)
            if p:
                self.store.member(p['id'], chat, True, stamp, stamp)
        user = message.get('left_chat_member')
        if user:
            p = self.store.observe(user, stamp, create=True)
            if p:
                self.store.member(p['id'], chat, False, stamp)
    def member_update(self, event):
        chat = event['chat']
        if chat.get('type') not in ('group', 'supergroup'):
            return
        member = event.get('new_chat_member', {})
        present = member_present(member)
        if present is None:
            return
        stamp = event['date']
        p = self.store.observe(member.get('user', {}), stamp, create=True)
        if p:
            joined = stamp if present and member_present(event.get('old_chat_member', {})) is False else None
            self.store.member(p['id'], chat, present, stamp, joined)
    def resolve(self, target):
        user, username, stamp, hidden_name = target
        if user:
            return self.store.observe(user, stamp, create=True)
        if username:
            return self.store.track('@' + username)
        matches = self.store.by_display_name(hidden_name)
        guidance = ('请让原消息发送者在 Telegram「设置 → 隐私与安全 → 转发消息」中，'
                    '允许转发消息链接到其账号，再重新转发；也可在群里回复该人的消息查询，无需用户名。')
        if len(matches) > 1:
            raise ValueError('发现多个名称为「%s」的档案，无法确定是哪一人。%s' % (hidden_name, guidance))
        if matches:
            return matches[0]
        raise ValueError('转发来源已隐藏，未找到同名档案。' + guidance)
    def match_notice(self, target):
        return ('按转发名称「%s」匹配：已有档案中仅一人同名，身份尚未确认，请核对后操作。' % target.hidden_name
                if target.hidden_name else '')
    def query(self, message):
        uid, chat = message['from']['id'], message['chat']
        if message.get('sender_chat'):
            return
        found = [t for t in targets(message) if t[1] != self.username]
        for target in found:
            try:
                p = self.resolve(target)
            except ValueError as error:
                self.send(chat['id'], str(error))
                continue
            if p is None:
                continue
            if chat['type'] == 'private':
                self.show_person(uid, p['id'], notice=self.match_notice(target))
                continue
            now = time.monotonic()
            key = (chat['id'], p['id'])
            if self.triggers.get(key, 0) > now:
                continue
            if len(self.triggers) >= 2048:
                self.triggers = {k: v for k, v in self.triggers.items() if v > now}
                if len(self.triggers) >= 2048:
                    self.triggers.pop(next(iter(self.triggers)))
            self.triggers[key] = now + 15
            if p['user_id']:
                try:
                    member = self.api.call('getChatMember', chat_id=chat['id'], user_id=p['user_id'])
                    state = member_present(member)
                    if state is not None:
                        self.store.member(p['id'], chat, state, time.time())
                    p = self.store.observe(member.get('user', {}), time.time()) or p
                except APIError as error:
                    if error.code == 429:
                        raise
                    # Existing state is explicitly dated in the card.
                    pass
            buttons = [[{'text': label, 'url': 'https://t.me/%s?start=%s_%s' % (self.username, intent, p['id'])}
                        for label, intent in (('新增', 'edit'), ('删除标签', 'delete'))]]
            notice = self.match_notice(target)
            self.send(chat['id'], (notice + '\n\n' if notice else '') + self.card(p, chat['id']), buttons,
                      reply_parameters={'message_id': message['message_id'], 'allow_sending_without_reply': True})
