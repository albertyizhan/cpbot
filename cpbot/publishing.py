"""Single-owner paced durable outbox, discussion mapping and nightly reconciliation."""
import html
import json
import time
from datetime import datetime

from .api import APIError
from .reviews import FIELDS, TZ, review_date


def render_review(store, row):
    data = json.loads(row['data'])
    result = []
    for key in FIELDS:
        if key == '出击证明':
            continue
        value = data.get(key, '')
        if key == '时间' and value:
            value = review_date(value, legacy=True)
        elif key == '综合':
            value = '%.1f' % value
        elif key == '老师':
            value = store.teacher(row['teacher'])['name']
        elif key == '留名':
            value = '人工审核通过' if row['manual'] else ('已核验' if row['anonymous'] else value)
        result.append('【%s】：%s' % (key, html.escape(str(value))))
    if not row['anonymous']:
        identity = json.loads(row['identity'])
        uid = row['author']
        result.append('【作者】：<a href="tg://user?id=%s">%s</a>%s' %
                      (uid, uid, ' @' + html.escape(identity['username']) if identity.get('username') else ''))
    result.append('【评价编号】：%s' % row['id'])
    return '\n'.join(result)


class Publisher:
    def __init__(self, store, api, username, bot_id):
        self.s, self.api, self.username, self.bot_id = store, api, username, bot_id
        self.next_global = 0
        self.next_chat = {}
        self.next_cleanup = 0
        migration = 'summary_body_link_v1'
        if not self.s.db.execute('SELECT 1 FROM settings WHERE key=?', (migration,)).fetchone():
            with self.s.transaction():
                for post in self.s.db.execute('SELECT * FROM posts WHERE message IS NOT NULL'):
                    self.s.enqueue('summary',post['binding'],post['teacher'],generation=100000000000000001)
                self.s.db.execute('INSERT INTO settings VALUES(?,?)',(migration,'1'))

    def _chat(self, cid, label):
        try:
            return self.api.call('getChat', chat_id=cid)
        except APIError as error:
            if error.code in (400, 403):
                raise ValueError('%s（%s）无法访问，请确认目标 ID 正确，并已将当前机器人 @%s 加入。' % (label, cid, self.username)) from None
            raise

    def validate(self, mode, cid):
        label = '查档群' if mode == 'lookup' else '频道'
        chat = self._chat(cid, label)
        expected = ('group', 'supergroup') if mode == 'lookup' else ('channel',)
        if chat['type'] not in expected:
            raise ValueError('目标类型不符合此模式：%s（%s）不是%s。' % (chat.get('title', ''), chat['id'], label))
        target = '%s「%s」' % (label, chat.get('title', chat['id']))
        self._rights(chat['id'], mode != 'lookup', target)
        discussion = chat.get('linked_chat_id') if mode == 'thread' else None
        if mode == 'thread':
            if not str(chat['id']).startswith('-100'):
                raise ValueError('需要有效频道 ID。')
            if not discussion:
                raise ValueError('%s尚未关联讨论组。请在频道设置 → 讨论，关联群组后重试。' % target)
            group = self._chat(discussion, '关联讨论组（频道权限已通过）')
            self._rights(discussion, False, '关联讨论组「%s」（频道权限已通过）' % group.get('title', discussion))
            if group.get('linked_chat_id') != chat['id']:
                raise ValueError('频道与讨论组关联关系不一致，请检查 Telegram 的频道讨论设置。')
        return chat, discussion

    def _rights(self, cid, channel, label):
        try:
            m = self.api.call('getChatMember', chat_id=cid, user_id=self.bot_id)
        except APIError as error:
            if error.code in (400, 403):
                raise ValueError('%s（%s）无法检查 @%s 的身份，请确认机器人已加入该目标。' % (label, cid, self.username)) from None
            raise
        status = m.get('status')
        if status not in ('administrator', 'creator'):
            state = {'member':'普通成员', 'left':'未加入或已退出', 'kicked':'已被移除', 'restricted':'受限制成员'}.get(status, '未知身份')
            raise ValueError('%s（%s）：@%s 当前为%s，需要将此机器人设为管理员。频道和讨论组的管理员身份相互独立。' % (label, cid, self.username, state))
        if channel and status != 'creator':
            missing = [name for key, name in (('can_post_messages','发布消息'), ('can_edit_messages','编辑消息')) if not m.get(key)]
            if missing:
                raise ValueError('%s（%s）：@%s 已是管理员，但缺少「%s」权限，请在频道管理员设置中开启。' % (label, cid, self.username, '、'.join(missing)))

    def summary(self, tid):
        t = self.s.teacher(tid)
        stats = self.s.db.execute("SELECT count(*),avg(CAST(json_extract(data,'$.综合') AS REAL)) FROM reviews WHERE teacher=? AND status='accepted'", (tid,)).fetchone()
        text = '【老师】：%s\n【综合评分】：%s\n【有效评价】：%s\n\n历史评价请查看本帖评论区。' % (
            html.escape(t['name']), '%.1f / 10' % stats[1] if stats[0] else '暂无', stats[0])
        text += '\n\n✍️ <a href="https://t.me/%s?start=review_%s">给这位老师写评价</a>' % (self.username, tid)
        return text, {'inline_keyboard': []}

    def automatic_forward(self, msg):
        if not msg.get('is_automatic_forward'):
            return False
        origin = msg.get('forward_origin', {})
        cid = origin.get('chat', {}).get('id') or msg.get('forward_from_chat', {}).get('id')
        mid = origin.get('message_id') or msg.get('forward_from_message_id')
        self.s.db.execute('''UPDATE posts SET root=? WHERE message=? AND binding IN
            (SELECT id FROM bindings WHERE chat_id=? AND discussion=?)''',
            (msg['message_id'], mid, cid, msg['chat']['id']))
        return True

    def nightly(self, stamp=None):
        now = stamp or time.time()
        local = datetime.fromtimestamp(now, TZ)
        day = local.strftime('%Y-%m-%d')
        saved = self.s.db.execute("SELECT value FROM settings WHERE key='last_check'").fetchone()
        if (local.hour, local.minute) < (2, 30) or (saved and saved[0] == day):
            return
        with self.s.transaction():
            for b in self.s.db.execute("SELECT * FROM bindings WHERE mode='thread' AND active=1"):
                self.s.enqueue('health', b['id'], 0, generation=int(local.strftime('%Y%m%d')))
                for t in self.s.db.execute('SELECT id FROM teachers'):
                    p = self.s.ensure_post(b['id'], t['id'])
                    publications = self.s.db.execute("""SELECT pub.message FROM publications pub JOIN reviews r ON r.id=pub.review
                        WHERE pub.binding=? AND pub.generation=? AND r.teacher=? AND r.status='accepted'
                        ORDER BY json_extract(r.data,'$.时间'),r.id""", (b['id'], p['generation'], t['id'])).fetchall()
                    mids = [row[0] for row in publications]
                    if mids != sorted(mids):
                        self.rebuild(b['id'], t['id'])
                    # A new daily generation of edit jobs, not a new publication generation.
                    self.s.enqueue('checkpost', b['id'], t['id'], generation=int(local.strftime('%Y%m%d')))
                    for r in self.s.db.execute("SELECT id,data FROM reviews WHERE teacher=? AND status='accepted' ORDER BY json_extract(data,'$.时间'),id", (t['id'],)):
                        self.s.enqueue('check', b['id'], t['id'], r['id'], int(local.strftime('%Y%m%d')))
                        if json.loads(r['data']).get('出击证明'):
                            self.s.enqueue('proof',b['id'],t['id'],r['id'],int(local.strftime('%Y%m%d')))
            self.s.db.execute("INSERT OR REPLACE INTO settings VALUES('last_check',?)", (day,))

    def rebuild(self, bid, tid):
        with self.s.transaction():
            self.s.db.execute('UPDATE posts SET message=NULL,root=NULL,generation=generation+1 WHERE binding=? AND teacher=?', (bid, tid))
            self.s.db.execute("UPDATE jobs SET state='superseded' WHERE binding=? AND teacher=? AND state IN ('pending','blocked')", (bid, tid))
            self.s.ensure_post(bid, tid)

    def _edit(self, cid, mid, text, **extra):
        try:
            return self.api.call('editMessageText', chat_id=cid, message_id=mid, text=text, **extra)
        except APIError as error:
            if error.description == 'Bad Request: message is not modified' or 'message is not modified' in error.description:
                return
            raise

    def tick(self):
        now = time.time()
        if time.monotonic() >= self.next_cleanup:
            self.s.db.execute("DELETE FROM reviews WHERE status IN ('pending','rejected') AND expires<=?", (now,))
            self.next_cleanup = time.monotonic() + 60
        self.nightly(now)
        paused = self.s.db.execute("SELECT value FROM settings WHERE key='publishing_paused'").fetchone()
        if paused and paused[0] == '1':
            return
        if now < self.next_global:
            return
        job = self.s.db.execute('''SELECT j.*,b.mode,b.chat_id,b.discussion FROM jobs j JOIN bindings b ON b.id=j.binding
            LEFT JOIN reviews r ON r.id=j.review
            WHERE j.state='pending' AND j.due<=? AND b.active=1
            ORDER BY CASE j.kind WHEN 'health' THEN 0 WHEN 'post' THEN 1 WHEN 'checkpost' THEN 2 ELSE 3 END,
            CASE WHEN j.review=0 THEN '' ELSE COALESCE(json_extract(r.data,'$.时间'),'') END,j.review,j.id LIMIT 1''', (now,)).fetchone()
        if not job:
            return
        j = dict(job)
        target = j['discussion'] if j['mode']=='thread' and j['kind'] in ('comment', 'check', 'proof') else j['chat_id']
        if self.next_chat.get(target, 0) > now:
            self.s.db.execute('UPDATE jobs SET due=? WHERE id=?', (self.next_chat[target], j['id']))
            return
        p = self.s.db.execute('SELECT * FROM posts WHERE binding=? AND teacher=?', (j['binding'], j['teacher'])).fetchone()
        if j['mode']=='thread' and j['kind'] in ('comment', 'check', 'proof') and (not p or not p['root']):
            self.s.db.execute("UPDATE jobs SET due=?,error='等待频道自动转发到讨论组' WHERE id=?", (now + 10, j['id']))
            return
        # A comment waits for earlier reviews for this teacher/target to finish.
        if j['kind'] == 'comment':
            earlier = self.s.db.execute('''SELECT 1 FROM jobs x JOIN reviews r ON r.id=x.review JOIN reviews current ON current.id=?
                WHERE x.binding=? AND x.teacher=? AND x.generation=? AND x.kind IN ('comment','proof')
                AND x.state NOT IN ('done','superseded') AND (json_extract(r.data,'$.时间')<json_extract(current.data,'$.时间')
                OR (json_extract(r.data,'$.时间')=json_extract(current.data,'$.时间') AND r.id<current.id)) LIMIT 1''',
                (j['review'], j['binding'], j['teacher'], j['generation'])).fetchone()
            if earlier:
                self.s.db.execute('UPDATE jobs SET due=? WHERE id=?', (now + 10, j['id']))
                return
        self.next_global = now + 1.1
        self.next_chat[target] = now + (3.2 if j['discussion'] == target else 1.1)
        self.s.db.execute("UPDATE jobs SET state='sending' WHERE id=?", (j['id'],))
        try:
            kind = j['kind']
            if kind == 'proof':
                r=self.s.db.execute('SELECT * FROM reviews WHERE id=?',(j['review'],)).fetchone()
                generation=p['generation'] if j['mode']=='thread' else 1
                pub=self.s.db.execute('SELECT * FROM publications WHERE binding=? AND review=? AND generation=?',(j['binding'],j['review'],generation)).fetchone()
                if not pub:
                    self.s.db.execute("UPDATE jobs SET state='pending',due=? WHERE id=?",(now+3,j['id']))
                    return
                proof=self.s.db.execute('SELECT * FROM proof_publications WHERE binding=? AND review=? AND generation=?',(j['binding'],j['review'],generation)).fetchone()
                if r['status']=='accepted':
                    content=self.s.proof_path(json.loads(r['data'])['出击证明']).read_bytes()
                    caption='📷 出击证明 · 车评 #%s'%r['id']
                    if proof:
                        self.api.call('editMessageCaption',chat_id=target,message_id=proof['message'],caption=caption)
                    else:
                        sent=self.api.photo(content,chat_id=target,caption=caption,reply_parameters={'message_id':pub['message'],'allow_sending_without_reply':False})
                        self.s.db.execute('INSERT INTO proof_publications VALUES(?,?,?,?,?)',(j['binding'],j['review'],generation,sent['message_id'],target))
                elif proof:
                    # Replace the public proof with a neutral image when withdrawing.
                    import struct, zlib
                    def chunk(kind,content):
                        return struct.pack('!I',len(content))+kind+content+struct.pack('!I',zlib.crc32(kind+content))
                    blank=(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',320,180,8,2,0,0,0))
                           +chunk(b'IDAT',zlib.compress((b'\x00'+b'\xe8\xe8\xe8'*320)*180))+chunk(b'IEND',b''))
                    self.api.photo(blank,_method='editMessageMedia',chat_id=target,message_id=proof['message'],caption='此评价已由管理员撤下。')
            elif kind == 'health':
                _, discussion = self.validate(j['mode'], j['chat_id'])
                if discussion != j['discussion']:
                    raise ValueError('频道讨论组关联已变化，请重新绑定。')
            elif kind in ('post', 'checkpost', 'summary'):
                text, markup = self.summary(j['teacher'])
                if p and p['message']:
                    self._edit(j['chat_id'], p['message'], text, reply_markup=markup, parse_mode='HTML', link_preview_options={'is_disabled': True})
                else:
                    result = self.api.call('sendMessage', chat_id=j['chat_id'], text=text, reply_markup=markup, parse_mode='HTML', link_preview_options={'is_disabled': True})
                    self.s.db.execute('UPDATE posts SET message=? WHERE binding=? AND teacher=?', (result['message_id'], j['binding'], j['teacher']))
            else:
                r = self.s.db.execute('SELECT * FROM reviews WHERE id=?', (j['review'],)).fetchone()
                if not r:
                    raise ValueError('评价已不存在。')
                generation = p['generation'] if j['mode'] == 'thread' else 1
                pub = self.s.db.execute('SELECT * FROM publications WHERE binding=? AND review=? AND generation=?',
                                        (j['binding'], j['review'], generation)).fetchone()
                text = render_review(self.s, r) if r['status'] == 'accepted' else '此评价已由管理员撤下。'
                if pub:
                    self._edit(pub['chat_id'], pub['message'], text, parse_mode='HTML')
                elif r['status'] == 'accepted':
                    params = {'reply_parameters': {'message_id': p['root'], 'allow_sending_without_reply': False}} if j['mode'] == 'thread' else {}
                    cid = j['discussion'] if j['mode'] == 'thread' else j['chat_id']
                    result = self.api.call('sendMessage', chat_id=cid, text=text, parse_mode='HTML', **params)
                    self.s.db.execute('INSERT INTO publications VALUES(?,?,?,?,?)', (j['binding'], j['review'], generation, result['message_id'], cid))
            self.s.db.execute("UPDATE jobs SET state='done',error='' WHERE id=?", (j['id'],))
            if j['review'] and j['mode'] == 'thread' and j['kind'] == 'comment':
                # Refresh after successful publishing. One revision key per accepted review.
                self.s.enqueue('summary', j['binding'], j['teacher'], generation=j['review'] + 1000000)
        except (ValueError,OSError) as error:
            if j['kind'] == 'health':
                self.s.db.execute('UPDATE bindings SET active=0,error=? WHERE id=?', (str(error), j['binding']))
            self.s.db.execute("UPDATE jobs SET state='blocked',error=? WHERE id=?", (str(error), j['id']))
        except APIError as error:
            description = error.description.lower()
            if 'message is not modified' in description:
                self.s.db.execute("UPDATE jobs SET state='done',error='' WHERE id=?",(j['id'],))
            elif error.code == 429:
                until = now + max(1, error.retry_after)
                self.next_global = until
                self.s.db.execute("UPDATE jobs SET state='pending',due=?,error='Telegram 限流' WHERE id=?", (until, j['id']))
            elif error.code == 0 or error.code >= 500:
                self.s.db.execute("UPDATE jobs SET state='uncertain',error='请求结果不明，请核对后处理' WHERE id=?", (j['id'],))
            elif any(text in description for text in ('message to edit not found', 'message_id_invalid', 'message to be replied not found', 'message to be replied to not found')):
                # Rebuild the affected teacher replica to preserve chronological order.
                if j['mode'] == 'thread':
                    self.rebuild(j['binding'], j['teacher'])
                    self.s.db.execute("UPDATE jobs SET state='superseded' WHERE id=?", (j['id'],))
                else:
                    if j['kind']=='proof':
                        self.s.db.execute('DELETE FROM proof_publications WHERE binding=? AND review=? AND generation=1',(j['binding'],j['review']))
                    self.s.db.execute("UPDATE jobs SET state='blocked',error='原消息已丢失' WHERE id=?", (j['id'],))
            else:
                self.s.db.execute("UPDATE jobs SET state='blocked',error=? WHERE id=?", (error.description[:200], j['id']))
                if error.code == 403:
                    self.s.db.execute("UPDATE bindings SET active=0,error='访问或管理员权限失效，请重新检查' WHERE id=?", (j['binding'],))
