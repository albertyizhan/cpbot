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
from .backup import validate_backup, MAX_BACKUP_BYTES


def date(stamp):
    return now_text(stamp) if stamp else '未知'


class Bot:
    def __init__(self, store, api, username, bot_id=0):
        self.store, self.api, self.username = store, api, username
        self.sessions, self.triggers = {}, {}
        self.publisher = Publisher(store, api, username, bot_id)
        self.edit_target = None
        self.notices = {}
        self.imports = {}

    def send(self, chat, text, rows=None, **extra):
        return self.api.call('sendMessage', chat_id=chat, text=text,
                             reply_markup={'inline_keyboard': rows or []}, **extra)

    def panel(self, uid, text, rows, pending=None, urls=None, **extra):
        notice = self.notices.pop(uid, None)
        if notice:
            text = (html.escape(notice) if extra.get('parse_mode') == 'HTML' else notice) + "\n\n" + text
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

    def success(self, uid, text):
        self.notices[uid] = '✅ ' + text

    def paused(self):
        row = self.store.db.execute("SELECT value FROM settings WHERE key='publishing_paused'").fetchone()
        return bool(row and row[0] == '1')

    def api_hint(self, error):
        if error.code == 429:
            return '⏳ Telegram 请求限流，请 %s 秒后重试。' % max(1, error.retry_after)
        if error.code == 403:
            return '🔒 无法访问目标，请检查机器人管理员权限和群/频道是否仍可访问。'
        if error.code == 400:
            return '⚠️ Telegram 未接受请求，请检查目标 ID、关联讨论组及消息是否有效。'
        return '⚠️ Telegram 请求结果暂不明确。若刚提交了操作，请先查看当前记录，确认后再重试。'

    def input_error(self, pending, error):
        if isinstance(error, sqlite3.IntegrityError):
            return '数据冲突，可能身份已绑定或内容重复。请检查后重新输入。'
        kind = pending[0] if pending else ''
        if kind == 'field' and pending[1] == '时间':
            return '时间格式应为 YYYY-MM-DD HH:MM，例如 2026-10-29 14:30。请重新输入。'
        if kind == 'reviewedit' and isinstance(error, json.JSONDecodeError):
            return 'JSON 格式不正确，请按示例重新输入，字段名和文字使用英文双引号。'
        if kind in ('teacher','adminadd','setroot','reviewmanage','jobdone') and (
                isinstance(error, TypeError) or 'invalid literal' in str(error) or 'unpack' in str(error)):
            return '输入格式不正确，请按上方提示填写数字 ID 和所需行数，再发送一次。'
        return str(error)[:180] or '输入无效，请按上方提示重新填写。'

    def home(self, uid):
        rows = []
        if self.store.ready():
            rows = [[('🌷 写车评', 'write'), ('🔎 找老师', 'findteacher')],
                    [('👤 查学生', 'search'), ('📋 我的车评', 'mine:0')]]
            if self.store.db.execute('SELECT 1 FROM teachers WHERE user_id=?', (uid,)).fetchone():
                rows += [[('🔑 添加留名', 'mycodes')]]
        if self.store.admin(uid):
            rows += [[('⚙️ 管理后台', 'admin')]]
        text = '🏠 车评主菜单\n🟢 当前可查询、填写车评。' if self.store.ready() else '⚠️ 机器人尚未配置完成或专帖绑定已失效。\n普通业务暂不可用，请联系管理员检查老师专帖频道。'
        if self.paused():
            text += '\n⏸ 发布队列已暂停，已提交的车评会等待管理员恢复发布。'
        return self.panel(uid, text, rows)

    def prompt(self, uid, text, pending, back='home'):
        return self.panel(uid, text + '\n\n✉️ 请直接发送内容，或点击下方取消。', [[('取消 / 返回', back)]], pending)

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
        filled = sum(bool(str(d.get(f, '')).strip()) for f in FIELDS)
        lines = ['📝 车评草稿 · 已填写 %s/%s 项' % (filled, len(FIELDS)),
                 '点击字段逐项填写，提交前可修改。草稿已保存。',
                 '🔒 匿名发布：隐藏身份及留名原文。' if d.get('匿名', True) else '👤 实名发布：公开 Telegram ID、身份链接及已有用户名。']
        rows = []
        for field in FIELDS:
            value = d.get(field)
            if field == '老师' and value:
                value = self.store.teacher(int(value))['name']
            lines.append('【%s】：%s' % (field, str(value) if value is not None else '未填写'))
        for i in range(0, len(FIELDS), 2):
            rows.append([(f + (' ✅' if d.get(f) is not None else ''), 'field:' + f) for f in FIELDS[i:i+2]])
        rows += [[('✅ 匿名' if d.get('匿名', True) else '✅ 实名', 'anon'), ('预览', 'preview')],
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
        return self.panel(uid, '👩‍🏫 选择老师 · 第 %s 页' % (page+1) + ('\n点击下方老师继续。' if teachers else '\n📭 没有匹配的老师，可返回后换个关键词。'), rows + [[('返回主菜单','home')]], ('teacherlist',intent,search))

    def admin_panel(self, uid):
        self.store.require_admin(uid)
        counts = self.store.db.execute("SELECT (SELECT count(*) FROM teachers), (SELECT count(*) FROM bindings WHERE mode='thread' AND active=1), (SELECT count(*) FROM reviews WHERE status='pending' AND expires>?)", (time.time(),)).fetchone()
        return self.panel(uid, '⚙️ 管理后台\n👩‍🏫 老师 %s 位 · 📣 可用专帖频道 %s 个\n🕓 待审核 %s 条 · %s' % (*counts, '⏸ 发布已暂停' if self.paused() else '▶️ 发布队列运行中'), [
            [('👩‍🏫 老师列表', 'teachersadmin'), ('➕ 添加老师', 'teachernew')],
            [('🔑 留名管理', 'codesadmin'), ('🕓 待审核', 'queue:0')],
            [('📣 频道 / 群绑定', 'bindings'), ('👑 superadmin 名单', 'admins')],
            [('📤 发布任务', 'jobs:0'), ('🛠 车评处理', 'reviewmanage')],
            [('💾 导出数据备份', 'export'), ('📥 导入数据备份', 'import')],
            [('返回主菜单', 'home')]])

    def action(self, user, action):
        uid = user['id']
        parts = action.split(':'); kind = parts[0]
        admin_kinds = {'admin','teachersadmin','teachernew','teacheredit','teacherdetail','teacherbind','teacherunbind','teacherunbindyes','teacherrename','codesadmin','codefor','admins','adminadd','adminremove',
                       'bindings','bind','binding','recheck','unbind','unbindyes','queue','moderate','approve','reject',
                       'import','importconfirm','importcancel','setroot','pausejobs','resumejobs','removeadmin','export','jobs','job','retry','jobdone','reviewmanage','reviewadmin','withdraw','withdrawyes','reviewedit'}
        if kind in admin_kinds:
            self.store.require_admin(uid)
        if kind not in admin_kinds and kind not in ('home','cancel') and not self.store.ready():
            raise ValueError('尚无可用的老师专帖绑定，业务暂不可用。')
        if kind in ('home','cancel'):
            self.imports.pop(uid, None)
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
                return self.panel(uid, '🔢 选择%s分数（满分 10 分）' % field,
                    [[(str(n), 'score:%s:%s' % (field,n)) for n in range(start,start+5)] for start in (1,6)] + [[('返回草稿','write')]])
            return self.prompt(uid, '✍️ 请输入%s。%s' % (field, '格式 YYYY-MM-DD HH:MM；不核对预约日期。' if field=='时间' else ''), ('field',field), 'write')
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
            missing = [f for f in FIELDS if not str(d.get(f, '')).strip()]
            if missing:
                raise ValueError('请先填写：' + '、'.join(missing))
            row={'data':json.dumps(d),'teacher':d['老师'],'manual':0,'anonymous':d.get('匿名',True),'identity':json.dumps(user),'author':uid,'id':'预览'}
            return self.panel(uid,render_review(self.store,row),[[('返回修改','write'),('确认提交','submit')]],parse_mode='HTML')
        if kind=='submit':
            draft,token=self.store.draft(uid)
            missing = [f for f in FIELDS if not str(draft.get(f, '')).strip()]
            if missing:
                raise ValueError('还未填写：'+'、'.join(missing)+'。请点击对应字段补齐。')
            rid,status=self.store.submit(user,token)
            return self.panel(uid,'📝 车评 #%s\n%s' % (rid,('✅ 核验通过。⏸ 发布已暂停，恢复后自动发布。' if self.paused() else '✅ 核验通过，正在限速排队发布。发布后可在「我的车评」查看链接。') if status=='accepted' else '🕓 留名未通过，已进入人工审核；审核区保留 72 小时。请等待管理员处理，不必重复提交。'),[[('返回主菜单','home')]])
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
            page=max(0,int(parts[1])); rows=[]; text='📋 我的车评 · 第 %s 页'%(page+1); urls=[]
            reviews=self.store.db.execute("SELECT * FROM reviews WHERE author=? AND status IN ('accepted','withdrawn','pending','rejected') ORDER BY id DESC LIMIT 9 OFFSET ?",(uid,page*8)).fetchall()
            for r in reviews[:8]:
                pub=self.store.db.execute('''SELECT p.* FROM publications p JOIN bindings b ON b.id=p.binding
                    LEFT JOIN posts t ON t.binding=p.binding AND t.teacher=? WHERE p.review=? AND b.active=1
                    AND (b.mode='feed' OR p.generation=t.generation) ORDER BY CASE b.mode WHEN 'thread' THEN 0 ELSE 1 END,b.id LIMIT 1''',(r['teacher'],r['id'])).fetchone()
                label='#%s %s · %s'%(r['id'],self.store.teacher(r['teacher'])['name'],json.loads(r['data'])['时间'])
                if r['status'] in ('pending','rejected'):
                    text+='\n'+label+(' · 🕓 待审核' if r['status']=='pending' else ' · ❌ 未通过审核')
                elif r['status']=='withdrawn': text+='\n'+label+' · 已撤下'
                elif pub: urls.append([{'text':label,'url':link(pub['chat_id'],pub['message'])}])
                else: text+='\n'+label+' · 待发布 / 请联系管理员核对任务'
            if not reviews: text+='\n📭 暂无车评。可返回主菜单选择「写车评」。'
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
            self.success(uid, '标签已清空，备注和身份资料保留。')
            return self.show_person(uid,p['id'])
        if kind=='history': return self.history(uid,int(parts[1]),max(0,int(parts[2])))
        if kind=='entry':
            row=self.store.db.execute('SELECT * FROM history WHERE id=?',(int(parts[1]),)).fetchone()
            if not row: raise ValueError('记录不存在。')
            return self.panel(uid,'操作人：%s（%s）\n时间：%s\n修改前：%s\n修改后：%s'%(row['actor_name'],row['actor'],date(row['created']),row['before_value'],row['after_value']),[[('返回主菜单','home')]])
        if kind=='teachernew':
            return self.prompt(uid,'👩‍🏫 添加老师\n发送老师名称即可（1～80 字）。本人账号可在详情中另行绑定。',('teachernew',),'admin')
        if kind in ('teacherdetail','teacheredit'):
            t=self.store.teacher(int(parts[1]))
            identity='✅ 已绑定本人账号：%s' % t['user_id'] if t['user_id'] else 'ℹ️ 未绑定本人账号，由 superadmin 添加留名。'
            rows=[[('修改名称','teacherrename:%s'%t['id'])],[('绑定本人账号（可选）' if not t['user_id'] else '更换本人账号','teacherbind:%s'%t['id'])]]
            if t['user_id']:rows.append([('解除本人绑定','teacherunbind:%s'%t['id'])])
            rows += [[('添加留名','codefor:%s'%t['id'])],[('返回老师列表','teachersadmin')]]
            return self.panel(uid,'👩‍🏫 %s · #%s\n%s\n未绑定不影响主帖、客户写评和查评。'%(t['name'],t['id'],identity),rows)
        if kind=='teacherrename':
            t=self.store.teacher(int(parts[1]))
            return self.prompt(uid,'当前名称：%s\n发送新名称（1～80 字），本人账号保持不变。'%t['name'],('teacherrename',t['id']),'teacherdetail:%s'%t['id'])
        if kind=='teacherbind':
            t=self.store.teacher(int(parts[1]))
            return self.prompt(uid,'为「%s」绑定本人账号：发送老师 Telegram 数字 ID。\n绑定后该账号可为此老师添加留名；更换后旧账号立即失去此权限。'%t['name'],('teacherbind',t['id']),'teacherdetail:%s'%t['id'])
        if kind=='teacherunbind':
            t=self.store.teacher(int(parts[1]))
            return self.panel(uid,'⚠️ 确认解除「%s」的本人账号绑定？\n原账号将不能为该老师添加留名，已有留名和评价不变。'%t['name'],[[('确认解除','teacherunbindyes:%s'%t['id'])],[('取消','teacherdetail:%s'%t['id'])]])
        if kind=='teacherunbindyes':
            tid=int(parts[1])
            self.store.set_teacher_account(uid,tid,None)
            self.success(uid,'本人账号已解绑，历史留名和评价保留。')
            return self.action(user,'teacherdetail:%s'%tid)
        if kind=='teachersadmin': return self.teachers(uid,'teacherdetail')
        if kind=='codesadmin': return self.teachers(uid,'codefor')
        if kind=='mycodes':
            t=self.store.db.execute('SELECT * FROM teachers WHERE user_id=?',(uid,)).fetchone()
            if not t: raise ValueError('未绑定老师身份。')
            return self.action(user,'codes:%s'%t['id'])
        if kind in ('codes','codefor'):
            tid=int(parts[1]); t=self.store.teacher(tid)
            if not self.store.admin(uid) and t['user_id']!=uid: raise ValueError('只能操作自己的留名。')
            count=self.store.db.execute("SELECT count(*) FROM codes WHERE teacher=? AND state='open'",(tid,)).fetchone()[0]
            return self.prompt(uid,'🔑 %s · 未使用留名 %s 个\n发送留名，每行一个；未使用的同名留名不能重复。'%(t['name'],count),('codes',tid),'admin' if self.store.admin(uid) else 'home')
        if kind=='admins':
            rows=[[('移除 %s'%r[0],'adminremove:%s'%r[0])] for r in self.store.db.execute('SELECT user_id FROM admins ORDER BY user_id')]
            return self.panel(uid,'👑 superadmin 名单\n下列账号拥有同等管理权限。添加后立即生效。',rows+[[('添加','adminadd'),('返回','admin')]])
        if kind=='adminadd': return self.prompt(uid,'发送要添加的 Telegram 数字 ID。',('adminadd',),'admins')
        if kind=='adminremove':
            return self.panel(uid,'⚠️ 确认移除 %s？\n该账号将失去后台权限。若 .env 仍包含此 ID，重启后会重新加入。'%parts[1],[[('确认移除','removeadmin:%s'%parts[1])],[('取消','admins')]])
        if kind=='removeadmin':
            self.store.set_admin(uid,int(parts[1]),False)
            self.success(uid, '已移除管理员 %s。如该 ID 仍在 .env 中，重启后会重新添加。' % parts[1])
            return self.home(uid)
        if kind=='bindings':
            rows=[[('%s · %s%s'%(MODES[b['mode']],b['title'],'（暂停）' if not b['active'] else ''),'binding:%s'%b['id'])] for b in self.store.db.execute('SELECT * FROM bindings')]
            rows += [[('添加'+name,'bind:'+mode)] for mode,name in MODES.items()]
            return self.panel(uid,'📣 模式绑定\n✅ 已启用 %s 个 · ⏸ 已暂停 %s 个\n频道和群不可重复占用。选择绑定可查看状态、同步进度。' % (self.store.db.execute('SELECT count(*) FROM bindings WHERE active=1').fetchone()[0], self.store.db.execute('SELECT count(*) FROM bindings WHERE active=0').fetchone()[0]),rows+[[('返回','admin')]])
        if kind=='bind': return self.prompt(uid,'发送目标频道/群的数字 ID 或 @用户名。先把机器人设为管理员；专帖频道必须关联讨论组。',('bind',parts[1]),'bindings')
        if kind=='binding':
            b=self.store.db.execute('SELECT * FROM bindings WHERE id=?',(int(parts[1]),)).fetchone()
            if not b: raise ValueError('绑定不存在。')
            counts = dict(self.store.db.execute('SELECT state,count(*) FROM jobs WHERE binding=? GROUP BY state',(b['id'],)).fetchall())
            status = '🟢 已启用' if b['active'] else '⏸ 已暂停'
            progress = '📤 待处理 %s · 完成 %s · 需处理 %s' % (counts.get('pending',0), counts.get('done',0), counts.get('blocked',0)+counts.get('uncertain',0))
            detail = '\n'.join(('%s · %s' % (MODES[b['mode']], b['title']), status,
                '频道/群：%s' % b['chat_id'], '讨论组：%s' % (b['discussion'] or '不需要'), progress))
            if self.paused() and b['mode'] != 'lookup':
                detail += '\n⏸ 全局发布已暂停，请到发布任务中继续。'
            waiting = self.store.db.execute('SELECT count(*) FROM posts WHERE binding=? AND message IS NOT NULL AND root IS NULL',(b['id'],)).fetchone()[0]
            if waiting:
                detail += '\n⏳ %s 位老师正在等待讨论串关联。长时间不变请检查讨论组权限或登记根消息。' % waiting
            if b['error']:
                detail += '\n⚠️ ' + b['error']
            if b['mode']=='thread' and not self.store.db.execute('SELECT 1 FROM teachers').fetchone():
                detail += '\nℹ️ 暂无老师，添加老师后才会创建主帖。'
            rows = [[('重新检查权限 / 恢复','recheck:'+parts[1]),('解除绑定','unbind:'+parts[1])]]
            if b['mode']=='thread': rows.append([('登记讨论串根消息','setroot:'+parts[1])])
            return self.panel(uid,detail,rows+[[('返回','bindings')]])
        if kind=='setroot':
            return self.prompt(uid,'自动转发事件未收到时，可手动补充。发送两行：老师编号、讨论组中该主帖自动转发消息的 message_id。',('setroot',int(parts[1])),'bindings')
        if kind=='recheck':
            b=self.store.db.execute('SELECT * FROM bindings WHERE id=?',(int(parts[1]),)).fetchone()
            if not b: raise ValueError('绑定不存在。')
            _,discussion=self.publisher.validate(b['mode'],b['chat_id'])
            if discussion!=b['discussion']: raise ValueError('讨论组已变更，请解除旧绑定后重新绑定。')
            self.store.db.execute("UPDATE bindings SET active=1,error='' WHERE id=?",(b['id'],))
            self.store.db.execute("UPDATE jobs SET state='pending' WHERE binding=? AND state='blocked'",(b['id'],))
            self.success(uid, '权限及关联检查通过，绑定已恢复；受阻任务已重新排队。')
            return self.action(user,'binding:'+parts[1])
        if kind=='unbind': return self.panel(uid,'解除绑定会停止后续发布，已发出的消息保留。最后一个专帖绑定解除后业务停止。',[[('确认解除','unbindyes:'+parts[1])],[('取消','bindings')]])
        if kind=='unbindyes':
            with self.store.transaction():
                bid=int(parts[1]); self.store.db.execute("UPDATE jobs SET state='superseded' WHERE binding=?",(bid,))
                self.store.db.execute('DELETE FROM publications WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM jobs WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM posts WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM bindings WHERE id=?',(bid,));self.store.log(uid,'解除绑定',bid)
            self.success(uid, '绑定已解除，后续不再向此目标发布；远端消息保留。')
            return self.action(user,'bindings')
        if kind=='queue':
            page=max(0,int(parts[1])); records=self.store.db.execute("SELECT * FROM reviews WHERE status='pending' AND expires>? ORDER BY id LIMIT 9 OFFSET ?",(time.time(),page*8)).fetchall()
            rows=[[('#%s · %s'%(r['id'],self.store.teacher(r['teacher'])['name']),'moderate:%s'%r['id'])] for r in records[:8]]
            if len(records)>8:rows.append([('下一页','queue:%s'%(page+1))])
            if page:rows.append([('上一页','queue:%s'%(page-1))])
            return self.panel(uid,'🕓 待审核 · 第 %s 页\n记录进入审核区后保留 72 小时。%s' % (page+1, '\n📭 当前没有待审核车评。' if not records else ''),rows+[[('返回','admin')]])
        if kind=='moderate':
            r=self.store.db.execute("SELECT * FROM reviews WHERE id=? AND status='pending' AND expires>?",(int(parts[1]),time.time())).fetchone()
            if not r:raise ValueError('记录已过期或处理。')
            return self.panel(uid,render_review(self.store,r)+'\n\n后台留名：'+html.escape(json.loads(r['data'])['留名'])+'\n作者 ID：'+str(r['author']),[[('通过','approve:'+parts[1]),('不通过','reject:'+parts[1])],[('返回','queue:0')]],parse_mode='HTML')
        if kind in ('approve','reject'):
            self.store.moderate(uid,int(parts[1]),kind=='approve')
            self.success(uid, '车评 #%s %s' % (parts[1], '已通过，正在排队发布。' if kind=='approve' else '已拒绝，不会公开；记录到期清理。'))
            return self.action(user,'queue:0')
        if kind=='export':
            self.api.document(uid,'cpbot-%s.json'%datetime.now(TZ).strftime('%Y%m%d-%H%M%S'),self.store.export(self.publisher.bot_id))
            self.success(uid, '备份文件已发送到本私聊，请下载并妥善保存。')
            return self.admin_panel(uid)
        if kind=='import':
            self.imports.pop(uid, None)
            return self.prompt(uid,'📥 导入数据备份\n请上传本机器人导出的 JSON 文件（最大 15 MB）。\n将按备份整体恢复，不合并；覆盖当前老师、标签、车评、管理员名单和发布记录。\n上传后先校验和预览，确认后才写入；当前操作者和 .env 管理员保留。',('importfile',),'admin')
        if kind=='importcancel':
            self.imports.pop(uid, None)
            self.success(uid,'已取消导入，当前数据未改变。')
            return self.admin_panel(uid)
        if kind=='importconfirm':
            saved = self.imports.pop(uid, None)
            if not saved or saved['until'] < time.monotonic():
                raise ValueError('上传备份已过期，请重新选择导入并上传文件。')
            recovery = self.store.import_backup(uid,saved['content'],self.publisher.bot_id)
            # Every old draft/menu/callback refers to the replaced dataset.
            self.sessions.clear()
            self.imports.clear()
            self.triggers.clear()
            self.publisher.next_chat.clear()
            self.success(uid,'数据已恢复，导入前数据已保存。所有绑定和发布队列已暂停：请逐个检查频道权限、核对消息位置，再继续发布。')
            self.admin_panel(uid)
            try:
                self.api.document(uid,recovery.name,recovery.read_bytes())
            except APIError:
                self.send(uid,'⚠️ 恢复已完成，但原数据备份发送失败；服务器保留位置：'+str(recovery))
            return
        if kind=='reviewmanage':return self.prompt(uid,'输入车评编号。',('reviewmanage',),'admin')
        if kind=='reviewadmin':
            r=self.store.db.execute('SELECT * FROM reviews WHERE id=?',(int(parts[1]),)).fetchone()
            if not r:raise ValueError('车评不存在。')
            return self.panel(uid,render_review(self.store,r)+'\n状态：'+{'accepted':'✅ 已通过','pending':'🕓 待审核','rejected':'❌ 已拒绝','withdrawn':'🚫 已撤下'}.get(r['status'],r['status']),[[('修改字段','reviewedit:'+parts[1]),('撤下','withdraw:'+parts[1])],[('返回','admin')]],parse_mode='HTML')
        if kind=='reviewedit':return self.prompt(uid,'发送 JSON 对象，例如 {"综合":8,"过程":"更正后的内容"}。仅可修改时间、人照、评分、过程；不会重新消耗留名。',('reviewedit',int(parts[1])),'reviewadmin:'+parts[1])
        if kind=='withdraw':return self.panel(uid,'确认撤下？将停止计分，各已发布消息改为撤下提示。',[[('确认撤下','withdrawyes:'+parts[1])],[('取消','reviewadmin:'+parts[1])]])
        if kind=='withdrawyes':
            self.change_review(uid,int(parts[1]),None)
            self.success(uid, '车评已撤下、不再计分，公开消息正在排队更新。')
            return self.action(user,'reviewadmin:'+parts[1])
        if kind in ('pausejobs','resumejobs'):
            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('publishing_paused',?)",('1' if kind=='pausejobs' else '0',))
            self.store.log(uid,'暂停发布' if kind=='pausejobs' else '继续发布','')
            self.success(uid, '发布队列已暂停，已提交内容保留，审核区仍会到期清理。' if kind=='pausejobs' else '发布队列已继续，将按限速处理待办任务。')
            return self.action(user,'jobs:0')
        if kind=='jobs':
            page=max(0,int(parts[1])); rows=self.store.db.execute("SELECT * FROM jobs WHERE state IN ('uncertain','blocked') ORDER BY id LIMIT 9 OFFSET ?",(page*8,)).fetchall()
            states={'pending':'⏳ 待处理','sending':'📤 请求中','done':'✅ 完成','blocked':'⚠️ 受阻','uncertain':'❓ 待核对','superseded':'已替代'}
            counts='\n'.join('%s：%s'%(states.get(r[0],r[0]),r[1]) for r in self.store.db.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
            buttons=[[('#%s %s · %s'%(r['id'],states.get(r['state'],r['state']),r['error'][:20]),'job:%s'%r['id'])] for r in rows[:8]]
            if len(rows)>8:buttons.append([('下一页','jobs:%s'%(page+1))])
            if page:buttons.append([('上一页','jobs:%s'%(page-1))])
            return self.panel(uid,'📤 发布任务 · '+('⏸ 已暂停' if self.paused() else '▶️ 运行中')+'\n'+(counts or '📭 暂无发布任务。'),buttons+[[('暂停任务','pausejobs'),('继续任务','resumejobs')],[('返回','admin')]])
        if kind=='job':
            j=self.store.db.execute('SELECT * FROM jobs WHERE id=?',(int(parts[1]),)).fetchone()
            if not j:raise ValueError('任务不存在。')
            kinds={'post':'创建老师主帖','comment':'发布评价评论','feed':'频道直发','summary':'更新总评价','checkpost':'巡检主帖','check':'巡检评价','health':'检查权限'}
            return self.panel(uid,'📤 任务 #%s · %s\n%s\n结果不明时请先核对目标：若已发送，登记远端消息 ID；若确认未发送，才重试。'%(j['id'],kinds.get(j['kind'],j['kind']),j['error']),[[('确认未发送，重试','retry:'+parts[1]),('已发送，登记消息 ID','jobdone:'+parts[1])],[('返回','jobs:0')]])
        if kind=='retry':
            self.store.db.execute("UPDATE jobs SET state='pending',due=0 WHERE id=? AND state IN ('blocked','uncertain')",(int(parts[1]),));self.store.log(uid,'人工重试任务',parts[1])
            self.success(uid, '任务已重新排队，将按限速处理。')
            return self.action(user,'jobs:0')
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
            return self.api.call('answerCallbackQuery',callback_query_id=q['id'],text='⌛ 菜单已过期或已使用，请发送 /start 重新打开。草稿仍保留。',show_alert=True)
        self.sessions.pop(uid,None);self.previous_pending=s.get('pending');self.edit_target=msg.get('message_id') or s.get('message')
        try:
            self.action(q['from'],action)
        except (ValueError,sqlite3.Error,OSError,APIError) as e:
            self.notices.pop(uid, None)
            if uid not in self.sessions:
                self.sessions[uid] = s
            detail = self.api_hint(e) if isinstance(e, APIError) else '⚠️ ' + (str(e)[:170] if isinstance(e,ValueError) else '数据写入或备份保存失败，请检查磁盘空间和文件权限；恢复未成功时原数据保留。')
            self.api.call('answerCallbackQuery',callback_query_id=q['id'],text=detail,show_alert=True)
            return
        finally:self.edit_target=None
        feedback={'score':'✅ 分数已保存','choose':'✅ 已选择老师','anon':'✅ 发布身份已切换','pausejobs':'⏸ 队列已暂停','resumejobs':'▶️ 队列已继续','recheck':'✅ 检查通过','approve':'✅ 已通过，排队发布','reject':'✅ 已拒绝','retry':'✅ 已重新排队','delete':'✅ 标签已清空','withdrawyes':'✅ 已撤下','removeadmin':'✅ 名单已更新','unbindyes':'✅ 已解除绑定','export':'✅ 备份文件已发送'}
        self.api.call('answerCallbackQuery',callback_query_id=q['id'],text=feedback.get(action.split(':')[0],''))

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
        pending = None
        try:
            if text.split(' ',1)[0].split('@')[0] in ('/start','/cancel'):
                self.imports.pop(uid, None)
                self.sessions.pop(uid,None);payload=text.partition(' ')[2]
                if payload.startswith('review_') and payload[7:].isdigit():return self.action(user,'choose:'+payload[7:])
                if payload.startswith(('edit_','delete_')) and payload.partition('_')[2].isdigit():
                    return self.action(user,('edit:' if payload.startswith('edit_') else 'askdelete:')+payload.partition('_')[2])
                return self.home(uid)
            if not self.store.ready() and not self.store.admin(uid):return self.home(uid)
            session=self.sessions.get(uid);pending=session.get('pending') if session else None
            if session and session['until']<time.monotonic():
                self.sessions.pop(uid,None)
                self.notices[uid] = '⌛ 上次输入已过期，请重新选择操作。草稿仍保留。'
                return self.home(uid)
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
                d,token=self.store.draft(uid);d[pending[1]]=text;self.store.save_draft(uid,d,token or secrets.token_hex(12))
                self.success(uid, '%s已保存，可继续填写其他字段。' % pending[1])
                return self.draft_panel(uid)
            if kind=='findteacher':return self.teachers(uid,'teacherlink','' if text=='全部' else text)
            if kind=='search':
                found=targets(msg);p=self.resolve(found[0]) if found else self.store.track(text)
                return self.show_person(uid,p['id'],notice=self.match_notice(found[0]) if found else '')
            if kind=='codes':
                self.store.add_codes(uid,pending[1],text.splitlines())
                self.success(uid, '已为 %s 添加 %s 个留名，未使用前持续有效。' % (self.store.teacher(pending[1])['name'],len([v for v in text.splitlines() if v.strip()])))
                return self.action(user,'codes:%s'%pending[1])
            self.store.require_admin(uid)
            if kind=='importfile':
                document=msg.get('document')
                if not document:
                    raise ValueError('请以文件形式上传 JSON 备份，不是粘贴文字或截图。')
                if not document.get('file_name','').lower().endswith('.json'):
                    raise ValueError('请上传新版 JSON 备份。旧 SQL 只能在服务器离线恢复后重新导出，机器人不会执行上传 SQL。')
                content=self.api.download_document(document,MAX_BACKUP_BYTES)
                data=validate_backup(content,self.publisher.bot_id)
                self.imports={k:v for k,v in self.imports.items() if v['until']>time.monotonic()}
                if uid not in self.imports and len(self.imports)>=2:
                    raise ValueError('已有管理员准备导入，请稍后重试。')
                self.imports[uid]={'content':content,'until':time.monotonic()+600}
                tables=data['tables']
                summary='📥 备份校验通过，尚未导入\n导出时间：%s\n老师：%s · 客户：%s · 车评：%s\n留名：%s · 绑定：%s · superadmin：%s\n\n⚠️ 确认后整体覆盖当前数据，不合并。预览期间的新数据也将被覆盖。\n✅ 恢复前自动保存原数据；保留当前操作者及 .env 管理员。\n⏸ 恢复后绑定及发布全部暂停，须重新检查，防止旧备份重复发帖。' % (date(data['created']),len(tables['teachers']),len(tables['people']),len(tables['reviews']),len(tables['codes']),len(tables['bindings']),len(tables['admins']))
                return self.panel(uid,summary,[[('确认覆盖并恢复','importconfirm')],[('取消，不改数据','importcancel')]])
            if kind=='addtags':
                lines=text.splitlines()
                if len(lines)!=2:raise ValueError('需要两行：备注、标签；不新增填 -。')
                p=self.store.person(pending[1]);note,tags_=[x.strip() for x in lines]
                note='\n'.join(filter(None,(p['note'],'' if note=='-' else note)));tags_='、'.join(filter(None,(p['tags'],'' if tags_=='-' else tags_)))
                self.store.save(p['id'],pending[2],user,note,tags_,'新增标签')
                self.success(uid, '新增内容已保存，下方显示最新资料。')
                return self.show_person(uid,p['id'])
            if kind in ('teachernew','teacherrename'):
                if len(text.splitlines())!=1:
                    raise ValueError('只需发送一行老师名称；本人账号请在详情中单独绑定。')
                if kind=='teachernew':
                    tid=self.store.save_teacher(uid,text)
                else:
                    tid=pending[1]
                    self.store.rename_teacher(uid,tid,text)
                self.success(uid,'老师名称已保存。')
                return self.action(user,'teacherdetail:%s'%tid)
            if kind=='teacherbind':
                if not text.isascii() or not text.isdigit() or not 0<int(text)<2**63:
                    raise ValueError('请输入有效的 Telegram 数字 ID，不是 @用户名。')
                self.store.set_teacher_account(uid,pending[1],int(text))
                self.success(uid,'本人账号已绑定，可私聊添加留名。')
                return self.action(user,'teacherdetail:%s'%pending[1])
            if kind=='adminadd':
                self.store.set_admin(uid,int(text))
                self.success(uid, 'superadmin %s 已加入名单，发送 /start 即可进入后台。' % int(text))
                return self.action(user,'admins')
            if kind=='bind':
                cid=int(text) if text.lstrip('-').isdigit() else text
                c,d=self.publisher.validate(pending[1],cid)
                bid=self.store.bind(uid,pending[1],c,d)
                effect={'thread':'将限速创建老师主帖，并从旧到新补发历史评价。','feed':'之后的新车评会发布到此频道，不补发历史。','lookup':'此群已允许转发、回复和 @用户名查档。'}[pending[1]]
                if self.paused() and pending[1] != 'lookup':
                    effect += '⏸ 发布队列当前暂停，恢复后开始处理。'
                if pending[1] == 'lookup' and not self.store.ready():
                    effect = '群配置已保存；仍需先绑定有效的老师专帖频道，查档才会开放。'
                self.success(uid, '%s绑定成功，权限检查已通过。%s' % (MODES[pending[1]], effect))
                return self.action(user,'binding:%s' % bid)
            if kind=='setroot':
                tid,root=map(int,text.splitlines())
                b=self.store.db.execute("SELECT * FROM bindings WHERE id=? AND mode='thread'",(pending[1],)).fetchone()
                if not b or root<=0:raise ValueError('无效绑定或消息 ID。')
                changed=self.store.db.execute('UPDATE posts SET root=? WHERE binding=? AND teacher=? AND message IS NOT NULL',(root,b['id'],tid)).rowcount
                if not changed:raise ValueError('老师主帖尚未创建。')
                self.store.log(uid,'登记讨论根消息',text)
                self.success(uid, '讨论串位置已保存，待发评价将在队列运行时继续发布。')
                return self.action(user,'binding:%s' % b['id'])
            if kind=='reviewmanage':return self.action(user,'reviewadmin:'+str(int(text)))
            if kind=='reviewedit':
                self.change_review(uid,pending[1],json.loads(text))
                self.success(uid, '更正已保存，评分和公开消息正在排队同步。')
                return self.action(user,'reviewadmin:%s'%pending[1])
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
                self.success(uid, '远端消息位置已登记，任务已完成，不会重复发送。')
                return self.action(user,'jobs:0')
        except APIError as error:
            self.notices.pop(uid, None)
            self.send(uid,self.api_hint(error))
        except (ValueError,TypeError,sqlite3.IntegrityError) as error:
            self.notices.pop(uid, None)
            self.send(uid,'⚠️ '+self.input_error(pending,error))

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
                    notice='✅ 频道权限和讨论组检查通过。\nℹ️ 当前尚未自动绑定，请在管理后台选择绑定模式。'
                except ValueError as error:
                    notice='⚠️ 专帖频道检查未通过。\n'+str(error)
                except APIError as error:
                    notice=self.api_hint(error)
                for admin in self.store.db.execute('SELECT user_id FROM admins'):
                    try:self.send(admin[0],notice+'\n频道 ID：'+str(cid))
                    except APIError:pass
            if e['new_chat_member']['status'] not in ('administrator','creator'):
                self.store.db.execute("UPDATE bindings SET active=0,error='机器人管理员权限已失效' WHERE chat_id=? OR discussion=?",(cid,cid))

    def card(self, p, chat_id=None):
        text = '\n'.join(('👤 当前名称：' + (p['name'] or '未知'),
                          'Telegram ID：' + (str(p['user_id']) if p['user_id'] else '待识别'),
                          '用户名：' + ('@' + p['username'] if p['username'] else '未知 / 未设置'),
                          '📝 群备注：' + (p['note'] or '无'), '🏷 群标签：' + (p['tags'] or '无')))
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
