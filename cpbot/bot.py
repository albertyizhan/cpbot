import json
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .api import APIError
from .identity import member_present, targets
from .store import display_name


def date(stamp):
    return datetime.fromtimestamp(stamp, ZoneInfo('Asia/Hong_Kong')).strftime('%Y-%m-%d %H:%M') if stamp else '未知'


class Bot:
    def __init__(self, store, api, username):
        self.store, self.api, self.username = store, api, username
        self.sessions = {}
        self.triggers = {}

    def send(self, chat, text, rows=None, **extra):
        return self.api.call('sendMessage', chat_id=chat, text=text,
                             reply_markup={'inline_keyboard': rows or []}, **extra)

    def panel(self, uid, text, rows, pending=None):
        nonce = secrets.token_hex(5)
        self.sessions[uid] = {'nonce': nonce, 'until': time.monotonic() + 600, 'pending': pending}
        keyboard = [[{'text': label, 'callback_data': nonce + '|' + action} for label, action in row] for row in rows]
        return self.send(uid, text, keyboard)

    def home(self, uid):
        if not self.store.allowed(uid):
            return self.panel(uid, '请输入使用密码，验证后长期有效。', [[('取消', 'cancel')]], ('unlock',))
        rows = [[('🔎 查找人员', 'search'), ('📋 浏览车评', 'list:0')], [('🕘 修改记录', 'history:0:0')]]
        if uid == self.store.owner:
            rows += [[('🔑 设置密码', 'password'), ('👥 授权管理', 'users:0')]]
        return self.panel(uid, '车评后台\n转发人员消息，或通过 ID / @用户名查找。', rows)

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

    def show_person(self, uid, pid, intent=None):
        p = self.store.person(pid)
        if intent == 'delete':
            return self.panel(uid, self.card(p) + '\n\n确定删除此人的全部标签？备注和身份资料保留。',
                              [[('确认删除', 'delete:%s:%s' % (pid, p['version']))], [('返回', 'person:%s' % pid)]])
        return self.panel(uid, self.card(p), [[('编辑标签', 'edit:%s' % pid), ('删除标签', 'askdelete:%s' % pid)],
                                              [('修改记录', 'history:%s:0' % pid)], [('返回列表', 'list:0'), ('返回首页', 'home')]])

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

    def action(self, user, action):
        uid = user['id']
        parts = action.split(':')
        kind = parts[0]
        if kind in ('home', 'cancel'):
            return self.home(uid)
        if not self.store.allowed(uid):
            return self.home(uid)
        if kind in ('search', 'password'):
            if kind == 'password' and uid != self.store.owner:
                raise ValueError('仅总管理者可设置密码。')
            return self.panel(uid, '发送数字 ID、@用户名，或转发目标消息。' if kind == 'search' else
                              '发送新密码（8～128 个字符）。已有授权继续有效。',
                              [[('取消', 'home')]], (kind,))
        if kind == 'list':
            page = max(0, int(parts[1]))
            people = self.store.db.execute('SELECT * FROM people ORDER BY id DESC LIMIT 9 OFFSET ?', (page * 8,)).fetchall()
            rows = [[((p['name'] or ('@' + p['username'] if p['username'] else str(p['user_id'] or '待识别')))[:40],
                       'person:%s' % p['id'])] for p in people[:8]]
            nav = []
            if page:
                nav.append(('上一页', 'list:%s' % (page - 1)))
            if len(people) > 8:
                nav.append(('下一页', 'list:%s' % (page + 1)))
            if nav:
                rows.append(nav)
            return self.panel(uid, '全部车评 · 第 %s 页' % (page + 1) + ('' if people else '\n暂无资料。'),
                              rows + [[('查找 / 新增', 'search'), ('返回首页', 'home')]])
        if kind == 'person':
            return self.show_person(uid, int(parts[1]))
        if kind == 'edit':
            p = self.store.person(int(parts[1]))
            return self.panel(uid, self.card(p) + '\n\n发送两行：\n第一行：群备注\n第二行：群标签\n对应行填 - 可清空。',
                              [[('取消', 'person:%s' % p['id'])]], ('edit', p['id'], p['version']))
        if kind == 'askdelete':
            return self.show_person(uid, int(parts[1]), 'delete')
        if kind == 'delete':
            pid, version = map(int, parts[1:])
            p = self.store.person(pid)
            self.store.save(pid, version, user, p['note'], '', '删除标签')
            return self.show_person(uid, pid)
        if kind == 'history':
            return self.history(uid, int(parts[1]), max(0, int(parts[2])))
        if kind == 'entry':
            row = self.store.db.execute('SELECT * FROM history WHERE id=?', (int(parts[1]),)).fetchone()
            if not row:
                raise ValueError('记录不存在。')
            before, after = json.loads(row['before_value']), json.loads(row['after_value'])
            return self.panel(uid, '记录 #%s · %s\n操作人：%s（%s）\n操作：%s\n档案 #%s\n\n修改前\n备注：%s\n标签：%s\n\n修改后\n备注：%s\n标签：%s' % (
                row['id'], date(row['created']), row['actor_name'], row['actor'], row['action'], row['person'],
                before[0] or '无', before[1] or '无', after[0] or '无', after[1] or '无'),
                [[('返回记录', 'history:%s:0' % row['person']), ('返回首页', 'home')]])
        if kind in ('users', 'revoke', 'revokeyes'):
            if uid != self.store.owner:
                raise ValueError('仅总管理者可管理授权。')
            if kind == 'revoke':
                return self.panel(uid, '确定撤销用户 %s 的使用权限？' % parts[1],
                                  [[('确认撤销', 'revokeyes:' + parts[1])], [('取消', 'users:0')]])
            page = max(0, int(parts[1])) if kind == 'users' else 0
            if kind == 'revokeyes':
                self.store.revoke(uid, int(parts[1]))
                self.sessions.pop(int(parts[1]), None)
            grants = self.store.db.execute('SELECT * FROM grants ORDER BY created DESC LIMIT 9 OFFSET ?', (page * 8,)).fetchall()
            rows = [[('撤销 ' + g['name'][:25] + ' (%s)' % g['user_id'], 'revoke:%s' % g['user_id'])] for g in grants[:8]]
            nav = []
            if page:
                nav.append(('上一页', 'users:%s' % (page - 1)))
            if len(grants) > 8:
                nav.append(('下一页', 'users:%s' % (page + 1)))
            if nav:
                rows.append(nav)
            return self.panel(uid, '已授权用户\n撤销后需重新输入当前密码。', rows + [[('返回首页', 'home')]])
        raise ValueError('未知操作，请返回首页。')

    def callback(self, query):
        user, uid = query['from'], query['from']['id']
        msg = query.get('message', {})
        nonce, _, action = query.get('data', '').partition('|')
        session = self.sessions.get(uid)
        if msg.get('chat', {}).get('type') != 'private' or msg['chat']['id'] != uid or not session or nonce != session['nonce'] or session['until'] < time.monotonic():
            return self.api.call('answerCallbackQuery', callback_query_id=query['id'], text='菜单已过期，请发送 /start 重新打开。', show_alert=True)
        if not self.store.allowed(uid) and action not in ('home', 'cancel'):
            return self.api.call('answerCallbackQuery', callback_query_id=query['id'], text='请先输入密码获得授权。', show_alert=True)
        # Consume every button once before performing a mutation.
        self.sessions.pop(uid, None)
        try:
            self.action(user, action)
        except ValueError as error:
            self.api.call('answerCallbackQuery', callback_query_id=query['id'], text=str(error), show_alert=True)
            return self.home(uid)
        self.api.call('answerCallbackQuery', callback_query_id=query['id'])

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
        user, username, stamp = target
        if user:
            return self.store.observe(user, stamp, create=True)
        if username:
            return self.store.track('@' + username)
        raise ValueError('转发来源已隐藏，无法识别用户。请使用 @用户名或可见来源的转发。')

    def query(self, message):
        uid, chat = message['from']['id'], message['chat']
        if not self.store.allowed(uid) or message.get('sender_chat'):
            return
        found = [t for t in targets(message) if t[1] != self.username]
        for target in found:
            if not self.store.allowed(uid):
                return
            try:
                p = self.resolve(target)
            except ValueError as error:
                self.send(chat['id'], str(error))
                continue
            if p is None:
                continue
            if chat['type'] == 'private':
                self.show_person(uid, p['id'])
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
                        for label, intent in (('编辑标签', 'edit'), ('删除标签', 'delete'))]]
            self.send(chat['id'], self.card(p, chat['id']), buttons,
                      reply_parameters={'message_id': message['message_id'], 'allow_sending_without_reply': True})

    def message(self, message, edited=False):
        chat, user = message['chat'], message.get('from', {})
        if chat.get('type') in ('group', 'supergroup'):
            self.observe_message(message)
            if not edited and user.get('id') and not user.get('is_bot'):
                self.query(message)
            return
        if chat.get('type') != 'private' or not user.get('id') or edited:
            return
        uid, text = user['id'], message.get('text', '').strip()
        if text.split(' ', 1)[0].split('@')[0] in ('/start', '/cancel'):
            self.sessions.pop(uid, None)
            payload = text.partition(' ')[2]
            if self.store.allowed(uid) and payload.startswith(('edit_', 'delete_')):
                intent, _, pid = payload.partition('_')
                if pid.isdigit():
                    return self.action(user, ('edit:' if intent == 'edit' else 'askdelete:') + pid)
            return self.home(uid)
        session = self.sessions.get(uid)
        pending = session.get('pending') if session else None
        if pending and session['until'] < time.monotonic():
            self.sessions.pop(uid, None)
            self.send(uid, '输入已过期，请重新操作。')
            return self.home(uid)
        if not self.store.allowed(uid) and (not pending or pending[0] != 'unlock'):
            return self.home(uid)
        if not pending:
            self.store.observe(user, message.get('date', time.time()))
            return self.query(message) if targets(message) else self.home(uid)
        kind = pending[0]
        try:
            if kind in ('unlock', 'password'):
                # Password messages are never logged or stored in plaintext.
                try:
                    self.api.call('deleteMessage', chat_id=uid, message_id=message['message_id'])
                except APIError:
                    pass
                if kind == 'unlock':
                    self.store.unlock(user, text)
                else:
                    self.store.password_set(uid, text)
                return self.home(uid)
            if kind == 'search':
                found = targets(message)
                p = self.resolve(found[0]) if found else self.store.track(text)
                if p is None:
                    raise ValueError('请选择人员账号。')
                return self.show_person(uid, p['id'])
            if kind == 'edit':
                lines = text.splitlines()
                if len(lines) != 2 or any(not line.strip() for line in lines):
                    raise ValueError('请发送两行：备注、标签；清空对应行请填 -。')
                note, tags = ('' if line.strip() == '-' else line.strip() for line in lines)
                self.store.save(pending[1], pending[2], user, note, tags)
                return self.show_person(uid, pending[1])
        except ValueError as error:
            self.send(uid, str(error))
            if not self.store.allowed(uid) and kind != 'unlock':
                return self.home(uid)

    def handle(self, update):
        if 'callback_query' in update:
            return self.callback(update['callback_query'])
        if 'chat_member' in update:
            return self.member_update(update['chat_member'])
        if 'message' in update:
            return self.message(update['message'])
        if 'edited_message' in update:
            return self.message(update['edited_message'], edited=True)
