import html
import json
import re
import secrets
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime
from heapq import merge
from itertools import islice, repeat

from .api import APIError
from .comment_buttons import (COMMENT_COLORS, MAX_COMMENT_COLUMNS, MAX_COMMENT_ROWS,
                              REVIEW_ACTION, REVIEW_TEMPLATE, parse_comment_color)
from .identity import targets, member_present
from .store import display_name
from .reviews import FIELDS, INPUT_FIELDS, SCORES, valid_score, overall_score, missing_fields, valid_proof, MODES, TZ, now_text, review_date
from .publishing import Publisher, render_review
from .backup import validate_backup, MAX_BACKUP_BYTES, MAX_IMAGE_ZIP_BYTES

MEMBER_ACTIONS = {'write': '写车评', 'findteacher': '找老师', 'search': '查学生', 'lookup': '查学生'}
COMMENT_EDITOR_ACTIONS = {'commentbuttons', 'cbsize', 'cbcell', 'cbedit', 'cbtemplate',
                          'cbcontent', 'cbcolor', 'cbclear'}
COMMENT_LINK_HINT = '链接可用 {cluster_id}、{teacher_id}、{review_id}，分别带上集群、老师和车评编号。'

def date(stamp):
    return now_text(stamp) if stamp else '未知'


class Bot:
    def __init__(self, store, api, username, bot_id=0, router=None):
        self.store, self.api, self.username = store, api, username
        self.router = router
        self.sessions, self.triggers = {}, {}
        self.publisher = Publisher(store, api, username, bot_id, router.pacing if router else None)
        self.edit_target = None
        self.notices = {}
        self.imports = {}
        self.teacher_imports = {}

    def send(self, chat, text, rows=None, **extra):
        return self.api.call('sendMessage', chat_id=chat, text=text,
                             reply_markup={'inline_keyboard': rows or []}, **extra)

    def panel(self, uid, text, rows, pending=None, urls=None, styles=None, **extra):
        notice = self.notices.pop(uid, None)
        if notice:
            text = (html.escape(notice) if extra.get('parse_mode') == 'HTML' else notice) + "\n\n" + text
        nonce = secrets.token_hex(5)
        styles = styles or {}
        keyboard = [[{'text': label, 'callback_data': nonce + '|' + action,
                      **({'style': styles[action]} if styles.get(action) else {})}
                     for label, action in row] for row in rows]
        keyboard += urls or []
        session = {'nonce': nonce, 'until': time.monotonic() + 600, 'pending': pending,
                   'actions': {action for row in rows for _, action in row}}
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
            return '时间格式应为 YYYY-MM-DD，例如 2026-10-29。请重新输入。'
        if kind == 'reviewedit' and isinstance(error, json.JSONDecodeError):
            return 'JSON 格式不正确，请按示例重新输入，字段名和文字使用英文双引号。'
        if kind in ('teacher','adminadd','setroot','reviewmanage','jobdone') and (
                isinstance(error, TypeError) or 'invalid literal' in str(error) or 'unpack' in str(error)):
            return '输入格式不正确，请按上方提示填写数字 ID 和所需行数，再发送一次。'
        return str(error)[:180] or '输入无效，请按上方提示重新填写。'

    def home(self, uid):
        rows = []
        stores = [bot.store for bot in self.router.bots.values()] if self.router else [self.store]
        ready = any(store.ready() for store in stores)
        if ready:
            row = [('🔎 找老师', 'findteacher')]
            if any(store.can_publish() for store in stores):
                row.insert(0, ('🌷 写车评', 'write'))
            rows = [row, [('👤 查学生', 'search'), ('📋 我的车评', 'mine:0')]]
            if self.router:
                rows = [[(label, 'member:' + action if action in MEMBER_ACTIONS else action)
                         for label, action in row] for row in rows]
        elif self.router:
            rows = [[('📋 我的车评', 'mine:0')]]
        if self.store.admin(uid):
            rows += [[('⚙️ 管理后台', 'admin')]]
        text = '🏠 车评主菜单' if ready else '⚠️ 尚未配置可用的集群绑定，请联系管理员。'
        if self.router and len(stores) > 1:
            if any(bot.store.can_publish() and bot.paused() for bot in self.router.bots.values()):
                text += '\n⏸ 有集群暂停发布，写车评时可查看所选集群的状态。'
        elif self.paused():
            text += '\n⏸ 发布队列已暂停，已提交的车评会等待管理员恢复发布。'
        return self.panel(uid, text, rows)

    def scoped(self, text):
        if self.router:
            return '集群：%s\n\n%s' % (self.store.clusters.names[self.store.cluster_id], text)
        return text

    def prompt(self, uid, text, pending, back='home'):
        return self.panel(uid, text + '\n\n✉️ 请直接发送内容，或点击下方取消。', [[('取消 / 返回', back)]], pending)

    def show_person(self, uid, pid, intent=None, notice=''):
        p = self.store.person(pid)
        if intent == 'delete':
            self.store.require_editor(uid)
            return self.panel(uid, self.scoped(self.card(p) + '\n\n确定清空标签？保留身份和修改记录。'),
                [[('确认删除', 'delete:%s:%s' % (pid, p['version']))], [('取消', 'person:%s' % pid)]])
        return self.panel(uid, self.scoped((notice + '\n\n' if notice else '') + self.card(p) + self.target_membership(p)),
                          [[('新增标签', 'edit:%s' % pid), ('删除标签', 'askdelete:%s' % pid)],
                           [('修改记录', 'history:%s:0' % pid)], [('返回主菜单', 'home')]])

    def draft_panel(self, uid):
        d, token = self.store.draft(uid)
        if not token:
            token = secrets.token_hex(12)
            self.store.save_draft(uid, d, token)
        filled = len(INPUT_FIELDS) - len(missing_fields(d))
        lines = ['📝 车评草稿 · 已填写 %s/%s 项' % (filled, len(INPUT_FIELDS)),
                 '点击字段逐项填写，提交前可修改。草稿已保存。',
                 '🔒 匿名发布：不显示作者信息。证明图将公开，请先遮挡个人信息。' if d.get('匿名', True) else '👤 实名发布：公开 Telegram ID、身份链接及已有用户名。']
        if self.paused():
            lines.insert(1, '⏸ 本集群已暂停发布。你仍可提交，恢复后会自动发布。')
        rows = []
        for field in FIELDS:
            value = d.get(field)
            if field == '出击证明':
                value = '已上传 ✅（点击可替换）' if valid_proof(value) else '未上传'
            if field == '综合':
                value = ('%.1f（自动计算）' % overall_score(d)) if all(valid_score(d.get(f)) for f in SCORES) else '六项评分填齐后自动计算'
            elif field in SCORES and value is not None and not valid_score(value):
                value = '需重新评分'
            if field == '老师' and value:
                value = self.store.teacher(int(value))['name']
            if field == '时间' and value:
                value = review_date(value, legacy=True)
            lines.append('【%s】：%s' % (field, str(value) if value is not None else '未填写'))
        for i in range(0, len(INPUT_FIELDS), 2):
            rows.append([(f + (' ✅' if f not in missing_fields(d) else ''), 'field:' + f) for f in INPUT_FIELDS[i:i+2]])
        rows += [[('✅ 匿名' if d.get('匿名', True) else '✅ 实名', 'anon'), ('预览', 'preview')],
                 [('提交', 'submit'), ('返回主菜单', 'home')]]
        return self.panel(uid, self.scoped('\n'.join(lines)), rows)

    def teachers(self, uid, intent='choose', search='', page=0):
        if intent not in ('choose','teacherlink','teacherdetail','codefor'):
            raise ValueError('无效的老师列表。')
        if intent in ('teacherdetail','codefor'):
            self.store.require_admin(uid)
        search = search.strip()
        teachers = self.store.db.execute('''SELECT t.*,p.username FROM teachers t
            LEFT JOIN people p ON p.user_id=t.user_id
            WHERE t.deleted_at IS NULL AND ((? NOT LIKE '@%' AND instr(lower(t.name),lower(?))>0)
               OR (t.user_id IS NOT NULL AND p.username=? COLLATE NOCASE)
               OR (t.user_id IS NULL AND t.username=? COLLATE NOCASE))
            ORDER BY t.id LIMIT 9 OFFSET ?''',
            (search,search,search.lstrip('@'),search.lstrip('@'),max(0,page)*8)).fetchall()
        rows = [[('%s · #%s' % (t['name'], t['id']), '%s:%s' % (intent, t['id']))] for t in teachers[:8]]
        rows.append([('搜索老师','teachersearch:'+intent)])
        if search:
            rows.append([('查看全部老师','teacherall:'+intent)])
        # Search state is retained in a short-lived session, not callback text.
        if len(teachers)>8:
            rows.append([('下一页', 'teacherspage:%s:%s' % (intent, page+1))])
        if page:
            rows.append([('上一页', 'teacherspage:%s:%s' % (intent, page-1))])
        return self.panel(uid, self.scoped('👩‍🏫 选择老师 · 第 %s 页' % (page+1) + ('\n搜索：'+search if search else '') + ('\n点击下方老师继续，也可直接发送姓名或 @用户名搜索。' if teachers else '\n📭 没有匹配的老师，请换个姓名或用户名。用户名需老师绑定本人账号并私聊机器人后才能识别。')), rows + [[('返回草稿','write')] if intent=='choose' else [('返回主菜单','home')]], ('teacherlist',intent,search))

    def teacher_batch_preview(self, uid, page=0):
        self.store.require_admin(uid)
        batch = self.teacher_imports.get(uid)
        if not batch or batch['until'] < time.monotonic():
            raise ValueError('预览已过期，请重新进入批量导入并发送名单。')
        entries = batch['entries']
        pages = (len(entries) + 14) // 15
        if not 0 <= page < pages:
            raise ValueError('预览页码无效。')
        bound = sum(entry[1] is not None for entry in entries)
        lines = ['📋 老师导入预览 · 尚未保存',
                 '共 %s 位 · 绑定账号 %s 位 · 未绑定 %s 位' % (len(entries), bound, len(entries)-bound),
                 '第 %s/%s 页 · 确认将导入整批名单，预览 10 分钟内有效。' % (page+1, pages)]
        for index, entry in enumerate(entries[page*15:(page+1)*15], page*15+1):
            name,account=entry[:2];username=entry[2] if len(entry)>2 else None
            lines.append('%s. %s\n   %s' % (index, name, '账号 ID：%s%s' % (account,' · @'+username if username else '') if account else ('等待识别 @'+username if username else '未绑定本人账号')))
        rows = []
        navigation = []
        if page:
            navigation.append(('上一页', 'teacherbatchpage:%s' % (page-1)))
        if page+1 < pages:
            navigation.append(('下一页', 'teacherbatchpage:%s' % (page+1)))
        if navigation:
            rows.append(navigation)
        rows += [[('确认导入全部 %s 位' % len(entries), 'teacherbatchconfirm')],
                 [('重新填写', 'teacherbatch'), ('取消导入', 'teacherbatchcancel')]]
        return self.panel(uid, '\n'.join(lines), rows)

    def admin_panel(self, uid):
        self.store.require_admin(uid)
        counts = self.store.db.execute("SELECT (SELECT count(*) FROM teachers WHERE deleted_at IS NULL), (SELECT count(*) FROM bindings WHERE mode IN ('thread','feed') AND active=1), (SELECT count(*) FROM reviews WHERE status='accepted')", ).fetchone()
        heading = '⚙️ 管理后台'
        cluster_rows = []
        if self.router:
            heading += ' · 集群 ' + self.store.clusters.names[self.store.cluster_id]
            cluster_rows = [[('选择集群', 'clusters:admin:0'), ('修改名称', 'clusterrename')]]
            cluster_rows += [[('成员资格来源', 'msources:0')]]
            cluster_rows += [[('集群授权 · ' + ('已激活' if self.store.service_authorized() else '未激活'), 'activation')]]
            if self.store.service_authorized() and not self.router.legacy_access(self) and not self.store.member_source_ids():
                heading += '\n请设置成员资格来源，开放普通成员访问。'
        return self.panel(uid, heading + '\n👩‍🏫 老师 %s 位 · 📣 可用发布频道 %s 个\n📝 有效车评 %s 条 · %s' % (*counts, '⏸ 发布已暂停' if self.paused() else '▶️ 发布队列运行中'), cluster_rows + [
            [('👩‍🏫 老师列表', 'teachersadmin'), ('➕ 添加老师', 'teachernew')],
            [('📥 批量导入老师', 'teacherbatch')],
            [('🗑 老师回收站', 'teachertrash'), ('⏳ 设置保留天数', 'trashdays')],

            [('🎯 在群判定目标', 'membertarget')],
            [('📣 频道 / 群绑定', 'bindings'), ('👑 superadmin 名单', 'admins')],
            [('评论按钮', 'commentbuttons')],
            [('📤 发布任务', 'jobs:0'), ('🛠 车评处理', 'reviewmanage')],
            [('💾 导出数据备份', 'export'), ('📥 导入数据备份', 'import')],
            [('返回主菜单', 'home')]])

    def comment_buttons_panel(self, uid):
        layout = self.store.comment_buttons()
        rows = [[(f"{'✓ ' if size == layout[axis] else ''}{size} {unit}", f'cbsize:{axis}:{size}')
                 for size in range(1, limit + 1)]
                for axis, unit, limit in (('rows', '行', MAX_COMMENT_ROWS), ('columns', '列', MAX_COMMENT_COLUMNS))]
        styles = {}
        visible_positions = set()
        for row in range(1, layout['rows'] + 1):
            cells = []
            for column in range(1, layout['columns'] + 1):
                position = f'{row}:{column}'
                visible_positions.add(position)
                action = f'cbcell:{position}'
                button = layout['buttons'].get(position)
                cells.append((button['text'] if button else f'＋ {row}-{column}', action))
                if button:
                    styles[action] = button['style']
            rows.append(cells)
        text = '评论按钮 · 编辑布局\n先选最大行数和列数，再点击对应位置。公开评论会跳过空位和空行。'
        hidden = len(layout['buttons'].keys() - visible_positions)
        if hidden:
            text += f'\n布局外保留了 {hidden} 个按钮。增大行列后可重新显示。'
        return self.panel(uid, self.scoped(text), rows + [[('返回管理后台', 'admin')]], styles=styles)

    def comment_button_panel(self, uid, row, column):
        layout = self.store.comment_buttons()
        if row > layout['rows'] or column > layout['columns']:
            return self.comment_buttons_panel(uid)
        position = f'{row}:{column}'
        button = layout['buttons'].get(position)
        header = f'评论按钮 · 第 {row} 行第 {column} 列'
        if not button:
            return self.panel(uid, self.scoped(header + '\n此位置为空。'),
                              [[('套用「评价当前老师」', f'cbtemplate:{position}')],
                               [('自行填写', f'cbedit:{position}:new')],
                               [('返回布局', 'commentbuttons')]])
        content = '评价当前老师（动态入口）' if button['content'] == REVIEW_ACTION else button['content']
        detail = (f"显示名称　{button['text']}\n内部内容　{content}\n颜色　{COMMENT_COLORS[button['style']]}")
        rows = [[('更改显示名称', f'cbedit:{position}:text'), ('更改内容', f'cbedit:{position}:content')],
                [('更改颜色', f'cbedit:{position}:style'), ('清空此位置', f'cbclear:{position}')]]
        if button != REVIEW_TEMPLATE:
            rows.append([('重置为「评价当前老师」模板', f'cbtemplate:{position}')])
        return self.panel(uid, self.scoped(header + '\n' + detail),
                          rows + [[('返回布局', 'commentbuttons')]],
                          link_preview_options={'is_disabled': True})

    def edit_comment_button(self, uid, row, column, field):
        position = f'{row}:{column}'
        back = f'cbcell:{position}'
        pending = ('commentbuttoninput', row, column, field)
        if field == 'new':
            text = ('按顺序发送以下内容，每项一行。\n'
                    '第 1 行：显示名称\n第 2 行：内部内容（HTTP 或 HTTPS 链接）\n'
                    '第 3 行（可省略）：蓝色、绿色、红色或无颜色\n\n' + COMMENT_LINK_HINT)
        elif field == 'text':
            text = '发送新的显示名称，限一行，1～40 字。'
        elif field == 'content':
            text = self.scoped('发送新的 HTTP 或 HTTPS 链接。\n' + COMMENT_LINK_HINT)
            return self.panel(uid, text, [[('使用「评价当前老师」入口', f'cbcontent:{position}')],
                                          [('取消 / 返回', back)]], pending)
        else:
            button = self.store.comment_buttons()['buttons'].get(position)
            if not button:
                return self.comment_button_panel(uid, row, column)
            styles = {f'cbcolor:{position}:{style or "none"}': style for style in COMMENT_COLORS}
            options = [(('✓ ' if style == button['style'] else '') + COMMENT_COLORS[style], action)
                       for action, style in styles.items()]
            return self.panel(uid, self.scoped('选择按钮颜色。'),
                              [options, [('取消 / 返回', back)]], styles=styles)
        return self.prompt(uid, self.scoped(text), pending, back)

    def cluster_panel(self, uid, destination, page):
        choices = list(self.store.clusters.names.items())
        rows = [[(('✓ ' if cluster_id == self.store.cluster_id else '') +
                  ('%s%s' % (name if name == str(cluster_id) else '%s · #%s' % (name, cluster_id),
                             '' if cluster_id in self.store.clusters.authorized else '（未激活）')),
                  'clusterselect:%s:%s' % (destination, cluster_id))]
                for cluster_id, name in choices[page * 8:(page + 1) * 8]]
        nav = []
        if page:
            nav.append(('上一页', 'clusters:%s:%s' % (destination, page - 1)))
        if len(choices) > (page + 1) * 8:
            nav.append(('下一页', 'clusters:%s:%s' % (destination, page + 1)))
        if nav:
            rows.append(nav)
        if destination != 'home':
            rows.append([('＋ 新建集群', 'clusternew')])
        return self.panel(uid, '选择集群' if choices else '暂无可用集群。', rows + [[('返回', destination)]])

    def activation_panel(self, uid, issued=None):
        clusters = self.store.clusters
        cid = self.store.cluster_id
        if self.store.service_authorized():
            text = '集群授权 · 已激活'
            rows = [[('撤销授权', 'activationrevoke')]]
        else:
            text = '集群授权 · 未激活'
            pending = clusters.pending_activation(cid)
            if issued:
                code, expires = issued
                text += '\n\n%s\n有效至 %s。此码只显示一次，仅用于当前集群。' % (code, date(expires))
            elif pending:
                text += '\n授权码有效至 %s。重新生成会使旧码失效。' % date(pending)
            rows = [[('重新生成授权码' if pending else '生成授权码', 'activationissue'),
                     ('输入授权码', 'activationinput')]]
            if pending:
                rows.append([('作废未使用的授权码', 'activationrevoke')])
        return self.panel(uid, self.scoped(text), rows + [[('返回管理后台', 'admin')]])

    def member_sources_panel(self, uid, page):
        sources = list(self.store.member_sources().items())
        selected = self.store.member_source_ids() or set()
        rows = [[('%s %s' % ('☑️' if cid in selected else '⬜', title[:60]),
                  'msource:%s:%s' % (cid, page))]
                for cid, title in sources[page * 8:(page + 1) * 8]]
        navigation = []
        if page:
            navigation.append(('上一页', 'msources:%s' % (page - 1)))
        if len(sources) > (page + 1) * 8:
            navigation.append(('下一页', 'msources:%s' % (page + 1)))
        if navigation:
            rows.append(navigation)
        text = '成员资格来源\n成员加入任意一个已勾选的群或频道，即可使用这个集群。'
        if not sources:
            text += '\n请先绑定频道或群。'
        elif not selected.intersection(cid for cid, _ in sources):
            text += ('\n当前沿用单集群的公开访问。勾选后开始验证成员身份。'
                     if self.router.legacy_access(self) else '\n请选择至少一个来源，才能向普通成员开放。')
        return self.panel(uid, self.scoped(text), rows + [[('返回管理后台', 'admin')]])

    def review_items(self, uid, limit, offset=0):
        return self.store.db.execute("""SELECT * FROM reviews WHERE author=?
            AND status IN ('accepted','withdrawn','pending','rejected')
            ORDER BY created DESC,id DESC LIMIT ? OFFSET ?""", (uid, limit, offset))

    def my_reviews_panel(self, uid, page, items):
        rows, urls = [], []
        text = '📋 我的车评 · 第 %s 页' % (page + 1)
        notes = []
        for bot, review in items[:8]:
            publication = bot.store.publication_link(review)
            label = '#%s %s · %s' % (review['id'], bot.store.teacher(review['teacher'])['name'],
                                     review_date(json.loads(review['data'])['时间'], legacy=True))
            if self.router and len(self.router.bots) > 1:
                label = self.store.clusters.names[bot.store.cluster_id] + ' · ' + label
            status = {'pending': '🕓 待审核', 'rejected': '❌ 未通过审核',
                      'withdrawn': '已撤下'}.get(review['status'])
            if not status and publication:
                urls.append([{'text': label, 'url': publication}])
            else:
                notes.append(label + ' · ' + (status or '暂无可用链接'))
        if notes:
            text += '\n' + '\n'.join(notes)
        if not items:
            text += '\n📭 暂无车评。可返回主菜单选择「写车评」。'
        if page:
            rows.append([('上一页', 'mine:%s' % (page - 1))])
        if len(items) > 8:
            rows.append([('下一页', 'mine:%s' % (page + 1))])
        return self.panel(uid, text, rows + [[('返回主菜单', 'home')]], urls=urls)

    def action(self, user, action):
        uid = user['id']
        parts = action.split(':'); kind = parts[0]
        if self.router and kind in ('clusters', 'clusterselect', 'clusternew', 'clusterrename'):
            self.store.require_admin(uid)
            if kind == 'clusternew':
                return self.router.switch(self, uid, self.router.create(uid), 'admin')
            if kind == 'clusterrename':
                destination = 'bindings' if parts[1:] == ['bindings'] else 'admin'
                return self.prompt(uid, '当前名称：%s\n发送新名称（1～40 字）。' %
                                   self.store.clusters.names[self.store.cluster_id], ('clusterrename', destination),
                                   destination)
            destination = parts[1]
            if destination not in ('admin', 'bindings'):
                raise ValueError('请从主菜单重新选择集群。')
            if kind == 'clusters':
                return self.cluster_panel(uid, destination, int(parts[2]))
            return self.router.switch(self, uid, int(parts[2]), destination)
        if self.router and kind in ('member', 'memberpage', 'memberpick'):
            if kind == 'memberpick':
                previous = self.previous_pending
                message = previous[2] if previous and previous[0] == 'memberpick' else None
                return self.router.enter(self, user, parts[1], int(parts[2]), message)
            previous = self.previous_pending if kind == 'memberpage' else None
            message = previous[2] if previous and previous[0] == 'memberpick' else None
            return self.router.member_menu(self, user, parts[1], int(parts[2]) if kind == 'memberpage' else 0, message)
        if self.router and kind == 'mine':
            return self.router.mine(self, uid, max(0, int(parts[1])))
        admin_kinds = {'membertarget','membertargetset','membertargetclear','admin','teachersadmin','teachernew','teacheredit','teacherdetail','teacherbind','teacherunbind','teacherunbindyes','teacherrename','teacherdelete','teacherdeleteyes','teachertrash','teacherpurge','teacherpurgeyes','trashdays','trashdaysset','codesadmin','codefor','admins',
                       'teacherbatch','teacherbatchpage','teacherbatchconfirm','teacherbatchcancel',
                       'msources','msource',
                       'activation','activationissue','activationinput','activationrevoke','activationrevokeyes',
                       'bindings','bind','binding','recheck','unbind','unbindyes','queue','moderate','approve','reject',
                       'import','importconfirm','importcancel','setroot','pausejobs','resumejobs','export','jobs','job','retry','jobdone','reviewmanage','reviewadmin','withdraw','withdrawyes','reviewedit'}
        if kind in ('mycodes','codes','codefor','codesadmin','queue','moderate','approve','reject'):
            raise ValueError('留名及审核机制已取消，请上传出击证明后直接提交车评。')
        admin_kinds |= COMMENT_EDITOR_ACTIONS
        if kind in admin_kinds:
            self.store.require_admin(uid)
        elif self.router and kind not in ('home', 'cancel'):
            self.router.require_member(self, uid)
        if kind not in admin_kinds and kind not in ('home','cancel') and not self.store.ready():
            raise ValueError('当前集群没有可用绑定。')
        if kind in ('write','choose','field','score','anon','preview','submit') and not self.store.can_publish():
            raise ValueError('写评需要一个可用的车评直发或老师专帖频道。')
        if kind in ('home','cancel'):
            self.imports.pop(uid, None)
            self.teacher_imports.pop(uid, None)
            return self.home(uid)
        if kind == 'admin':
            return self.admin_panel(uid)
        if kind == 'commentbuttons':
            return self.comment_buttons_panel(uid)
        if kind == 'cbsize':
            if self.store.resize_comment_buttons(uid, parts[1], int(parts[2])):
                self.success(uid, '布局已保存。')
            return self.comment_buttons_panel(uid)
        if kind in COMMENT_EDITOR_ACTIONS:
            row, column = map(int, parts[1:3])
            if kind == 'cbedit':
                return self.edit_comment_button(uid, row, column, parts[3])
            if kind != 'cbcell':
                changes = {'cbtemplate': REVIEW_TEMPLATE, 'cbcontent': {'content': REVIEW_ACTION}, 'cbclear': None}
                change = {'style': None if parts[3] == 'none' else parts[3]} if kind == 'cbcolor' else changes[kind]
                if self.store.set_comment_button(uid, row, column, change):
                    self.success(uid, '已保存，历史评论将依次更新。')
            return self.comment_button_panel(uid, row, column)
        if self.router and kind.startswith('activation'):
            clusters = self.store.clusters
            cid = self.store.cluster_id
            if kind == 'activation':
                return self.activation_panel(uid)
            if kind == 'activationissue':
                return self.activation_panel(uid, clusters.issue_activation(uid, cid))
            if kind == 'activationinput':
                return self.prompt(uid, self.scoped('发送本集群的授权码。'), ('activationinput',), 'activation')
            if kind == 'activationrevoke':
                text = ('撤销后，本集群的查档、写评和发布会停止。已保存的资料和任务保留。'
                        if self.store.service_authorized() else '作废后，该授权码将无法激活集群。')
                return self.panel(uid, self.scoped(text),
                                  [[('确认', 'activationrevokeyes'), ('取消', 'activation')]])
            if kind == 'activationrevokeyes':
                clusters.revoke_activation(uid, cid)
                return self.activation_panel(uid)
        if kind in ('msources', 'msource'):
            page = int(parts[-1])
            if kind == 'msource':
                cid = int(parts[1])
                if cid not in (self.store.member_source_ids() or set()):
                    self.publisher._rights(cid, False, '成员资格来源')
                self.store.toggle_member_source(uid, cid)
                self.router.invalidate_chat(cid)
            return self.member_sources_panel(uid, page)
        if kind=='membertarget':
            target=self.store.db.execute("SELECT value FROM settings WHERE key='membership_target'").fetchone()
            value=json.loads(target[0]) if target else None
            return self.panel(uid,'🎯 在群判定目标\n'+('%s（%s）'%(value['title'],value['id']) if value else '尚未设置')+'\n查学生时实时查询此群，不使用当前查档群作为判定目标。机器人必须是目标群管理员。查询失败显示无法确认。',[[('设置 / 更换目标群','membertargetset')],[('取消指定目标','membertargetclear'),('返回','admin')]])
        if kind=='membertargetset':
            return self.prompt(uid,'发送判定目标群的数字 ID 或 @群用户名。请先将机器人设为该群管理员。',('membertargetset',),'membertarget')
        if kind=='trashdays':
            return self.prompt(uid,'当前保留 %s 天。发送新的保留天数（1～3650）。'%self.store.trash_days(),('trashdaysset',),'admin')
        if kind=='teachertrash':
            rows=[]
            for t in self.store.db.execute('SELECT * FROM teachers WHERE deleted_at IS NOT NULL ORDER BY deleted_at'):
                count=self.store.db.execute('SELECT count(*) FROM reviews WHERE teacher=?',(t['id'],)).fetchone()[0]
                rows.append([('%s · #%s · %s 条评价'%(t['name'],t['id'],count),'teacherpurge:%s'%t['id'])])
            return self.panel(uid,'🗑 老师回收站 · 保留 %s 天'%self.store.trash_days()+ ('\n点击可立即彻底删除。' if rows else '\n回收站为空。'),rows+[[('返回后台','admin')]])
        if kind=='teacherpurge':
            t=self.store.teacher(int(parts[1]))
            if t.get('deleted_at') is None:raise ValueError('老师不在回收站。')
            return self.panel(uid,'⚠️ 彻底删除「%s」及全部评价、发布记录和关联证明图？仍被其他评价或草稿使用的图片会保留。此操作不可恢复。'%t['name'],[[('彻底删除','teacherpurgeyes:%s'%t['id'])],[('取消','teachertrash')]])
        if kind=='teacherpurgeyes':
            self.store.purge_teacher(uid,int(parts[1]));self.success(uid,'老师及全部评价已彻底删除。');return self.action(user,'teachertrash')
        if kind=='trashdaysset':
            return self.prompt(uid,'发送新的保留天数（1～3650）。',('trashdaysset',),'admin')
        if kind=='membertargetclear':
            self.store.set_membership_target(uid, None)
            self.success(uid,'已取消指定判定群。')
            return self.action(user,'membertarget')
        if kind == 'write':
            return self.draft_panel(uid)
        if kind == 'field':
            field = parts[1]
            if field not in INPUT_FIELDS:
                raise ValueError('该字段不可填写；综合分由六项评分自动计算。')
            if field == '出击证明':
                return self.prompt(uid,'📷 请发送一张出击证明照片（以照片方式发送，不是文件）。\n图片会随车评公开发布，匿名时也会公开，请先遮挡姓名、头像和聊天账号。可重新上传替换，提交后由管理员处理。',('proof',),'write')
            if field == '老师':
                return self.teachers(uid)
            if field in SCORES:
                return self.panel(uid, '🔢 选择%s分数（满分 10 分）' % field,
                    [[(str(n), 'score:%s:%s' % (field,n)) for n in range(start,start+5)] for start in (1,6)] + [[('返回草稿','write')]])
            return self.prompt(uid, '✍️ 请输入%s。%s' % (field, '格式 YYYY-MM-DD；不核对预约日期。' if field=='时间' else ''), ('field',field), 'write')
        if kind in ('choose','score','anon'):
            d, token = self.store.draft(uid)
            if not token:
                token=secrets.token_hex(12)
            if kind=='choose':
                self.store.active_teacher(int(parts[1])); d['老师']=int(parts[1])
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
            missing = missing_fields(d)
            if missing:
                raise ValueError('请先填写：' + '、'.join(missing))
            d['综合'] = overall_score(d)
            self.api.photo(self.store.proof_path(d['出击证明']).read_bytes(),chat_id=uid,caption='📷 出击证明预览 · 此图将公开发布')
            row={'data':json.dumps(d),'teacher':d['老师'],'manual':0,'anonymous':d.get('匿名',True),'identity':json.dumps(user),'author':uid,'id':'预览'}
            return self.panel(uid,render_review(self.store,row),[[('返回修改','write'),('确认提交','submit')]],parse_mode='HTML')
        if kind=='submit':
            draft,token=self.store.draft(uid)
            missing = missing_fields(draft)
            if missing:
                raise ValueError('还未填写：'+'、'.join(missing)+'。请点击对应字段补齐。')
            rid,status=self.store.submit(user,token)
            return self.panel(uid,'📝 车评 #%s\n✅ 已提交，无需审核。%s'%(rid,'⏸ 发布已暂停，恢复后自动发布。' if self.paused() else '正在限速发布文字和证明图，可在「我的车评」查看链接。'),[[('返回主菜单','home')]])
        if kind=='findteacher':
            return self.prompt(uid,self.scoped('输入老师姓名关键词或 @用户名，或发送“全部”浏览。'),('findteacher',))
        if kind in ('teachersearch','teacherall'):
            intent=parts[1]
            if intent not in ('choose','teacherlink','teacherdetail','codefor'):
                raise ValueError('无效的老师列表。')
            if intent in ('teacherdetail','codefor'):
                self.store.require_admin(uid)
            if kind=='teacherall':return self.teachers(uid,intent)
            return self.prompt(uid,'🔎 搜索老师\n发送姓名关键词或 Telegram 用户名（可带 @）。\n用户名需老师绑定本人账号并私聊机器人后才能识别。',('teachersearch',intent),'teacherall:'+intent)
        if kind=='teacherspage':
            old=getattr(self,'previous_pending',None)
            return self.teachers(uid,parts[1],old[2] if old and old[0]=='teacherlist' else '',int(parts[2]))
        if kind=='teacherlink':
            teacher = self.store.active_teacher(int(parts[1]))
            tid = teacher['id']
            page = max(0, int(parts[2])) if len(parts) > 2 else 0
            reviews = self.store.db.execute("""SELECT id,teacher,data FROM reviews WHERE teacher=? AND status='accepted'
                ORDER BY json_extract(data,'$.时间') DESC,id DESC LIMIT 9 OFFSET ?""", (tid,page*8)).fetchall()
            lines = [html.escape(self.scoped('%s · 车评 · 第 %s 页' % (teacher['name'], page+1)))]
            for review in reviews[:8]:
                label = '#%s · %s' % (review['id'], review_date(json.loads(review['data'])['时间'], legacy=True))
                url = self.store.publication_link(review)
                lines.append('<a href="%s">%s</a>' % (url,label) if url else label + ' · 暂无可用链接')
            if not reviews:
                lines.append('本页暂无车评。')
            nav = []
            if page:
                nav.append(('上一页', 'teacherlink:%s:%s' % (tid,page-1)))
            if len(reviews) > 8:
                nav.append(('下一页', 'teacherlink:%s:%s' % (tid,page+1)))
            rows = [nav] if nav else []
            if self.store.can_publish():
                rows.append([('给这位老师写评价','choose:%s' % tid)])
            rows.append([('返回老师列表','teacherall:teacherlink'),('返回主菜单','home')])
            return self.panel(uid,'\n'.join(lines),rows,parse_mode='HTML',link_preview_options={'is_disabled':True})
        if kind=='mine':
            page = max(0, int(parts[1]))
            return self.my_reviews_panel(uid, page, [(self, r) for r in self.review_items(uid, 9, page * 8)])
        if kind=='search': return self.prompt(uid,self.scoped('发送客户 ID、@用户名，或转发目标消息。'),('search',))
        if kind=='person': return self.show_person(uid,int(parts[1]))
        if kind in ('edit','askdelete','delete'):
            self.store.require_editor(uid)
            p=self.store.person(int(parts[1]))
            if kind=='askdelete': return self.show_person(uid,p['id'],'delete')
            if kind=='edit': return self.prompt(uid,'发送要追加的标签。',('addtags',p['id'],p['version']),'person:%s'%p['id'])
            self.store.save(p['id'],int(parts[2]),user,'','删除标签')
            self.success(uid, '标签已清空，身份资料保留。')
            return self.show_person(uid,p['id'])
        if kind=='history': return self.history(uid,int(parts[1]),max(0,int(parts[2])))
        if kind=='entry':
            row=self.store.db.execute('SELECT * FROM history WHERE id=?',(int(parts[1]),)).fetchone()
            if not row: raise ValueError('记录不存在。')
            return self.panel(uid,'操作人：%s（%s）\n时间：%s\n修改前：%s\n修改后：%s'%(row['actor_name'],row['actor'],date(row['created']),json.loads(row['before_value'])[1] or '无',json.loads(row['after_value'])[1] or '无'),[[('返回主菜单','home')]])
        if kind=='teachernew':
            return self.prompt(uid,'👩‍🏫 添加老师\n发送老师名称即可（1～80 字）。本人账号可在详情中另行绑定。',('teachernew',),'admin')
        if kind=='teacherbatch':
            self.teacher_imports.pop(uid, None)
            return self.prompt(uid,'📥 批量导入老师\n每行一位，每批最多 100 位，信息用空格分隔，可混用：\n老师甲\n老师乙 @teacher_b\n老师丙 @teacher_c 987654321\n老师丁 123456789\n\n用户名和数字 ID 均可省略；仅填用户名将等待账号出现后自动绑定。已绑定老师可编辑学生标签。用户名请带 @，最后一项纯数字按 Telegram ID 识别。\n先生成列表预览，确认后才保存；重名或账号冲突不会覆盖已有资料。',('teacherbatch',),'teacherbatchcancel')
        if kind=='teacherbatchcancel':
            self.teacher_imports.pop(uid, None)
            self.success(uid,'已取消，本批老师未导入。')
            return self.admin_panel(uid)
        if kind=='teacherbatchpage':
            return self.teacher_batch_preview(uid,int(parts[1]))
        if kind=='teacherbatchconfirm':
            batch=self.teacher_imports.get(uid)
            if not batch or batch['until'] < time.monotonic():
                raise ValueError('预览已过期，请重新进入批量导入并发送名单。')
            ids=self.store.import_teachers(uid,batch['entries'])
            self.teacher_imports.pop(uid,None)
            self.success(uid,'已导入 %s 位老师。已绑定账号可用于用户名搜索。'%len(ids))
            return self.admin_panel(uid)
        if kind in ('teacherdetail','teacheredit'):
            t=self.store.teacher(int(parts[1]))
            identity='✅ 已绑定本人账号：%s' % t['user_id'] if t['user_id'] else ('⏳ 等待识别 @'+t['username'] if t.get('username') else 'ℹ️ 未绑定本人账号，可按名称搜索。')
            rows=[[('修改名称','teacherrename:%s'%t['id'])],[('绑定本人账号（可选）' if not t['user_id'] else '更换本人账号','teacherbind:%s'%t['id'])]]
            if t['user_id']:rows.append([('解除本人绑定','teacherunbind:%s'%t['id'])])
            if t.get('deleted_at') is None:rows.append([('🗑 删除老师','teacherdelete:%s'%t['id'])])
            rows += [[('返回老师列表','teachersadmin')]]
            return self.panel(uid,'👩‍🏫 %s · #%s\n%s\n未绑定不影响主帖、客户写评和查评。'%(t['name'],t['id'],identity),rows)
        if kind=='teacherdelete':
            t=self.store.teacher(int(parts[1]))
            return self.panel(uid,'⚠️ 删除「%s」？\n老师和全部评价将进入回收站 %s 天。已发布的评价正文和证明图会依次清除；到期后自动彻底删除。'%(t['name'],self.store.trash_days()),[[('确认删除','teacherdeleteyes:%s'%t['id'])],[('取消','teacherdetail:%s'%t['id'])]])
        if kind=='teacherdeleteyes':
            tid=int(parts[1]);count=self.store.delete_teacher(uid,tid)
            self.success(uid,'老师已移入回收站，%s 条评价的公开内容正在清除。'%count)
            return self.action(user,'teachertrash')
        if kind=='teacherrename':
            t=self.store.teacher(int(parts[1]))
            return self.prompt(uid,'当前名称：%s\n发送新名称（1～80 字），本人账号保持不变。'%t['name'],('teacherrename',t['id']),'teacherdetail:%s'%t['id'])
        if kind=='teacherbind':
            t=self.store.teacher(int(parts[1]))
            return self.prompt(uid,'为「%s」绑定本人账号：发送老师 Telegram 数字 ID。\n绑定后可通过该账号的用户名搜索老师；老师需先私聊机器人以记录用户名。'%t['name'],('teacherbind',t['id']),'teacherdetail:%s'%t['id'])
        if kind=='teacherunbind':
            t=self.store.teacher(int(parts[1]))
            return self.panel(uid,'⚠️ 确认解除「%s」的本人账号绑定？\n解除后不能再通过原账号用户名搜索该老师，已有评价保留。'%t['name'],[[('确认解除','teacherunbindyes:%s'%t['id'])],[('取消','teacherdetail:%s'%t['id'])]])
        if kind=='teacherunbindyes':
            tid=int(parts[1])
            self.store.set_teacher_account(uid,tid,None)
            self.success(uid,'本人账号已解绑，历史评价保留。')
            return self.action(user,'teacherdetail:%s'%tid)
        if kind=='teachersadmin': return self.teachers(uid,'teacherdetail')
        if kind=='admins':
            ids = '\n'.join(str(admin) for admin in self.store.admin_ids())
            return self.panel(uid,'👑 superadmin 名单\n修改 .env 后重启生效。\n\n' + ids,
                              [[('返回','admin')]])
        if kind=='bindings':
            page = max(0, int(parts[1])) if len(parts) > 1 else 0
            bindings = self.store.db.execute('SELECT * FROM bindings ORDER BY id LIMIT 13 OFFSET ?', (page * 12,)).fetchall()
            rows=[[('%s · %s%s'%(MODES[b['mode']],b['title'],'（暂停）' if not b['active'] else ''),'binding:%s'%b['id'])] for b in bindings[:12]]
            nav = []
            if page:
                nav.append(('上一页', 'bindings:%s' % (page - 1)))
            if len(bindings) > 12:
                nav.append(('下一页', 'bindings:%s' % (page + 1)))
            if nav:
                rows.append(nav)
            rows += [[('添加'+name,'bind:'+mode)] for mode,name in MODES.items()]
            heading = '📣 模式绑定'
            if self.router:
                heading += ' · 集群 ' + self.store.clusters.names[self.store.cluster_id]
                rows.insert(0, [('选择集群', 'clusters:bindings:0'), ('修改名称', 'clusterrename:bindings')])
            return self.panel(uid,heading + '\n✅ 已启用 %s 个 · ⏸ 已暂停 %s 个\n频道和群不可重复占用。选择绑定可查看状态、同步进度。' % (self.store.db.execute('SELECT count(*) FROM bindings WHERE active=1').fetchone()[0], self.store.db.execute('SELECT count(*) FROM bindings WHERE active=0').fetchone()[0]),rows+[[('返回','admin')]])
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
                detail += '\n⏸ %s发布已暂停，请到发布任务中继续。' % ('本集群' if self.router else '全局')
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
        if kind=='unbind': return self.panel(uid,'解除后此目标停止服务，已发消息保留。其他绑定可继续使用。',[[('确认解除','unbindyes:'+parts[1])],[('取消','bindings')]])
        if kind=='unbindyes':
            with self.store.transaction():
                bid=int(parts[1]); self.store.db.execute("UPDATE jobs SET state='superseded' WHERE binding=?",(bid,))
                self.store.db.execute('DELETE FROM publications WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM proof_publications WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM jobs WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM posts WHERE binding=?',(bid,))
                self.store.db.execute('DELETE FROM bindings WHERE id=?',(bid,));self.store.log(uid,'解除绑定',bid)
                self.store.prune_member_sources()
            if self.store.clusters:
                self.store.clusters.refresh_routes(self.store)
            self.success(uid, '绑定已解除，后续不再向此目标发布；远端消息保留。')
            return self.action(user,'bindings')
        if kind=='export':
            text,images=self.store.export(self.publisher.bot_id,split=True)
            name='cpbot-%s-%s' % (self.store.cluster_id, datetime.now(TZ).strftime('%Y%m%d-%H%M%S'))
            self.api.document(uid,name+'.json',text)
            self.api.document(uid,name+'-images.zip',images)
            self.success(uid, 'JSON 数据和 ZIP 图片集已发送，请将这两个配套文件一起保存。')
            return self.admin_panel(uid)
        if kind=='import':
            self.imports.pop(uid, None)
            scope = '当前集群'
            if self.router:
                scope = '集群「%s」' % self.store.clusters.names[self.store.cluster_id]
            return self.prompt(uid,'📥 导入数据备份\n请上传同一批 JSON（最大 15 MB）和 ZIP（最大 19 MB），顺序不限。\n确认后覆盖%s的业务数据。\n上传后可先预览。' % scope,('importfile',),'admin')
        if kind=='importcancel':
            self.imports.pop(uid, None)
            self.success(uid,'已取消导入，当前数据未改变。')
            return self.admin_panel(uid)
        if kind=='importconfirm':
            saved = self.imports.get(uid)
            if not saved or saved['until'] < time.monotonic():
                raise ValueError('上传备份已过期，请重新选择导入并上传文件。')
            recovery = self.store.import_backup(uid,saved['content'],self.publisher.bot_id,saved.get('images'))
            # Every old draft/menu/callback refers to the replaced dataset.
            self.teacher_imports.clear()
            self.sessions.clear()
            self.imports.clear()
            self.triggers.clear()
            self.success(uid,'数据已恢复，导入前数据已保存。所有绑定和发布队列已暂停：请逐个检查频道权限、核对消息位置，再继续发布。')
            self.admin_panel(uid)
            try:
                self.api.document(uid,recovery.name,recovery.read_bytes())
                image_path=recovery.with_suffix('.zip')
                if image_path.exists():self.api.document(uid,image_path.name,image_path.read_bytes())
            except APIError:
                self.send(uid,'⚠️ 恢复已完成，但原数据备份发送失败；服务器保留位置：'+str(recovery))
            return
        if kind=='reviewmanage':return self.prompt(uid,'输入车评编号。',('reviewmanage',),'admin')
        if kind=='reviewadmin':
            r=self.store.db.execute('SELECT * FROM reviews WHERE id=?',(int(parts[1]),)).fetchone()
            if not r:raise ValueError('车评不存在。')
            return self.panel(uid,render_review(self.store,r)+'\n状态：'+{'accepted':'✅ 已通过','pending':'🕓 待审核','rejected':'❌ 已拒绝','withdrawn':'🚫 已撤下'}.get(r['status'],r['status']),[[('修改字段','reviewedit:'+parts[1]),('撤下','withdraw:'+parts[1])],[('返回','admin')]],parse_mode='HTML')
        if kind=='reviewedit':return self.prompt(uid,'发送 JSON 对象，例如 {"人照":8,"过程":"更正后的内容"}。仅可修改时间、六项评分、过程；综合分自动计算。',('reviewedit',int(parts[1])),'reviewadmin:'+parts[1])
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
            if j['kind'] == 'commentbuttons':
                return self.panel(uid, '📤 任务 #%s · 更新评论按钮\n%s' % (j['id'], j['error']),
                                  [[('重试更新', 'retry:' + parts[1])], [('返回', 'jobs:0')]])
            kinds={'post':'创建老师主帖','comment':'发布评价评论','feed':'频道直发','summary':'更新总评价','checkpost':'巡检主帖','check':'巡检评价','health':'检查权限','proof':'发布 / 巡检证明图'}
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
                if not isinstance(changes,dict) or any(k not in (*SCORES,'时间','过程') for k in changes):raise ValueError('包含不可修改的字段。')
                d=json.loads(r['data']);d.update(changes)
                if any(not valid_score(v) for k,v in changes.items() if k in SCORES):raise ValueError('分数需要 1～10 的整数。')
                # Legacy text 人照 has no numeric meaning: preserve its old overall until rescored.
                if isinstance(d.get('人照'),str):
                    if any(k in SCORES for k in changes):raise ValueError('这是一条旧版车评，请同时补充人照分数，再更正评分。')
                else:
                    d['综合'] = overall_score(d)
                d['时间'] = review_date(d['时间'], legacy='时间' not in changes)
                if not isinstance(d['过程'],str) or not 1<=len(d['过程'])<=1600:raise ValueError('过程长度不正确。')
                self.store.db.execute('UPDATE reviews SET data=?,version=version+1 WHERE id=?',(json.dumps(d,ensure_ascii=False),rid))
            self.store.log(uid,'修改车评' if changes else '撤下车评',rid)
            for b in self.store.db.execute("SELECT * FROM bindings WHERE mode IN ('thread','feed')"):
                if b['mode']=='feed' and not self.store.db.execute('SELECT 1 FROM publications WHERE binding=? AND review=?',(b['id'],rid)).fetchone():
                    continue
                key=int(time.time()*1000000)
                self.store.enqueue('check' if b['mode']=='thread' else 'feed',b['id'],r['teacher'],rid,key)
                if b['mode']=='thread':self.store.enqueue('summary',b['id'],r['teacher'],generation=key)
                if json.loads(r['data']).get('出击证明'):
                    self.store.enqueue('proof',b['id'],r['teacher'],rid,key)

    def callback(self, q):
        uid=q['from']['id'];msg=q.get('message',{});nonce,_,action=q.get('data','').partition('|');s=self.sessions.get(uid)
        if (msg.get('chat',{}).get('type')!='private' or msg['chat']['id']!=uid or not s
                or nonce!=s['nonce'] or s['until']<time.monotonic() or action not in s['actions']):
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
            route=self.store.db.execute('''SELECT
                EXISTS(SELECT 1 FROM settings WHERE key='membership_target' AND json_extract(value,'$.id')=?),
                EXISTS(SELECT 1 FROM bindings WHERE mode='lookup' AND chat_id=? AND active=1)''',(chat['id'],chat['id'])).fetchone()
            is_target,lookup_enabled=route
            if is_target or lookup_enabled:
                self.observe_message(msg)
            if lookup_enabled and uid and not edited and not user.get('is_bot'):
                self.query(msg)
            return
        if chat['type']!='private' or not uid or edited:return
        if not self.router:
            self.store.observe_private_teacher(user, msg.get('date', time.time()))
        pending = None
        try:
            if text.split(' ',1)[0].split('@')[0] in ('/start','/cancel'):
                self.imports.pop(uid, None)
                self.teacher_imports.pop(uid, None)
                self.sessions.pop(uid,None);payload=text.partition(' ')[2]
                if payload.startswith('review_') and payload[7:].isdigit():return self.action(user,'choose:'+payload[7:])
                if payload.startswith(('edit_','delete_')) and payload.partition('_')[2].isdigit():
                    return self.action(user,('edit:' if payload.startswith('edit_') else 'askdelete:')+payload.partition('_')[2])
                return self.home(uid)
            session=self.sessions.get(uid);pending=session.get('pending') if session else None
            if session and session['until']<time.monotonic():
                self.sessions.pop(uid,None)
                self.notices[uid] = '⌛ 上次输入已过期，请重新选择操作。草稿仍保留。'
                return self.home(uid)
            if not pending:
                if not self.router and not self.store.ready() and not self.store.admin(uid):
                    return self.home(uid)
                if targets(msg) or text.isdigit():
                    if self.router:
                        return self.router.member_menu(self, user, 'lookup', message=msg)
                    return self.private_lookup(msg)
                return self.home(uid)
            kind=pending[0]
            if self.router and kind == 'memberpick':
                return self.send(uid, '请点击上方按钮选择集群。')
            if self.router and not self.store.admin(uid):
                self.router.require_member(self, uid)
            if not self.store.ready() and not self.store.admin(uid):
                return self.home(uid)
            if kind=='proof':
                photos=msg.get('photo')
                if not photos:raise ValueError('请以照片方式上传一张图片，不接受文字、文件或视频。')
                photo=max(photos,key=lambda p:p.get('width',0)*p.get('height',0))
                content=self.api.download_document(photo,10*1024*1024)
                if not content:raise ValueError('图片下载为空，请重新上传。')
                name=secrets.token_hex(16)+'.jpg'
                path=self.store.proof_path(name)
                path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                import os
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as file:
                    file.write(content);file.flush();os.fsync(file.fileno())
                d,token=self.store.draft(uid);d['出击证明']=name;d.pop('留名',None)
                self.store.save_draft(uid,d,token or secrets.token_hex(12))
                self.success(uid,'出击证明已保存。图片将公开发布，请确认已遮挡个人信息。')
                return self.draft_panel(uid)
            if kind=='field':
                if not text:raise ValueError('请发送文字，不接收图片。')
                if pending[1] not in ('过程','时间'):raise ValueError('评分请返回草稿，点击对应按钮选择；综合分自动计算。')
                limits={'过程':1600,'时间':10}
                if len(text)>limits[pending[1]]:raise ValueError('内容超过长度限制。')
                if pending[1]=='时间':text=review_date(text)
                d,token=self.store.draft(uid);d[pending[1]]=text;self.store.save_draft(uid,d,token or secrets.token_hex(12))
                self.success(uid, '%s已保存，可继续填写其他字段。' % pending[1])
                return self.draft_panel(uid)
            if kind=='findteacher':return self.teachers(uid,'teacherlink','' if text=='全部' else text)
            if kind in ('teachersearch','teacherlist'):
                if not text:raise ValueError('请发送老师姓名或用户名。')
                return self.teachers(uid,pending[1],text)
            if kind=='search':
                found=targets(msg);p=self.resolve(found[0]) if found else self.store.track(text)
                return self.show_person(uid,p['id'],notice=self.match_notice(found[0]) if found else '')
            if kind=='addtags':
                if not text or '\n' in text:raise ValueError('请发送一行要追加的标签。')
                p=self.store.person(pending[1]);tags_='、'.join(filter(None,(p['tags'],text)))
                self.store.save(p['id'],pending[2],user,tags_,'新增标签')
                self.success(uid, '新增内容已保存，下方显示最新资料。')
                return self.show_person(uid,p['id'])
            self.store.require_admin(uid)
            if kind == 'commentbuttoninput':
                row, column, field = pending[1:]
                if field == 'new':
                    lines = text.splitlines()
                    if len(lines) not in (2, 3):
                        raise ValueError('请用两到三行填写，依次发送显示名称、链接和可省略的颜色。')
                    label, content, color = (lines + [''])[:3]
                    changes = {'text': label.strip(), 'content': content.strip(), 'style': parse_comment_color(color)}
                else:
                    changes = {field: text}
                if changes.get('content') == REVIEW_ACTION:
                    raise ValueError('请填写 HTTP 或 HTTPS 链接，或点击按钮选用模板入口。')
                changed = self.store.set_comment_button(uid, row, column, changes)
                self.success(uid, '已保存，历史评论将依次更新。' if changed else '按钮设置未变更。')
                return self.comment_button_panel(uid, row, column)
            if self.router and kind == 'activationinput':
                self.store.clusters.redeem_activation(uid, self.store.cluster_id, text)
                return self.activation_panel(uid)
            if kind=='clusterrename':
                self.store.clusters.rename(uid, self.store.cluster_id, text)
                self.success(uid, '集群名称已更新。')
                return self.action(user, pending[1])
            if kind=='membertargetset':
                cid=int(text) if text.lstrip('-').isdigit() else text
                chat,_=self.publisher.validate('lookup',cid)
                value={'id':chat['id'],'title':chat.get('title',str(chat['id']))}
                self.store.set_membership_target(uid, value)
                self.success(uid,'判定目标已保存，机器人管理员权限检查通过。')
                return self.action(user,'membertarget')
            if kind=='trashdaysset':
                if not text.isascii() or not text.isdigit():raise ValueError('请发送 1～3650 的整数天数。')
                self.store.set_trash_days(uid,int(text))
                self.success(uid,'回收站保留天数已设为 %s 天。'%int(text))
                return self.action(user,'admin')
            if kind=='teacherbatch':
                entries=self.store.parse_teachers(uid,text)
                self.teacher_imports[uid]={'entries':entries,'until':time.monotonic()+600}
                return self.teacher_batch_preview(uid)
            if kind=='importfile':
                document=msg.get('document')
                if not document:
                    raise ValueError('请以文件形式上传 JSON 备份，不是粘贴文字或截图。')
                suffix=document.get('file_name','').lower()
                if not suffix.endswith(('.json','.zip')):
                    raise ValueError('请上传 JSON 数据或 ZIP 图片集；旧 SQL 不能直接上传。')
                self.imports={k:v for k,v in self.imports.items() if v['until']>time.monotonic()}
                if uid not in self.imports and len(self.imports)>=2:
                    raise ValueError('已有管理员准备导入，请稍后重试。')
                saved=self.imports.setdefault(uid,{'until':time.monotonic()+600})
                content=self.api.download_document(document,MAX_IMAGE_ZIP_BYTES if suffix.endswith('.zip') else MAX_BACKUP_BYTES)
                saved['images' if suffix.endswith('.zip') else 'content']=content
                saved['until']=time.monotonic()+600
                if 'content' not in saved:
                    return self.prompt(uid,'📥 ZIP 图片集已收到，尚未导入。请继续上传配套 JSON 数据文件。',('importfile',),'importcancel')
                try:
                    data=validate_backup(saved['content'],self.publisher.bot_id,saved.get('images'))
                except ValueError as error:
                    if '需要配套的 ZIP' in str(error):
                        return self.prompt(uid,'📥 JSON 数据已收到，尚未导入。请继续上传配套 ZIP 图片集。',('importfile',),'importcancel')
                    raise
                tables=data['tables']
                self.store.check_backup_scope(data)
                scope = self.store.clusters.names[self.store.cluster_id] if self.router else '1'
                summary='📥 备份校验通过，尚未导入\n目标集群：%s\n导出时间：%s\n老师：%s · 客户：%s · 车评：%s\n证明图：%s · 绑定：%s\n\n⚠️ 确认后覆盖此集群的数据，包括预览期间的新数据。\n恢复前自动保存原数据。恢复后，此集群的绑定和队列暂停，请核对后恢复发布。' % (scope,date(data['created']),len(tables['teachers']),len(tables['people']),len(tables['reviews']),len(data.get('media',{})),len(tables['bindings']))
                return self.panel(uid,summary,[[('确认覆盖并恢复','importconfirm')],[('取消，不改数据','importcancel')]],('importfile',))
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
                self.success(uid,'本人账号已绑定，请老师私聊机器人以记录用户名。')
                return self.action(user,'teacherdetail:%s'%pending[1])
            if kind=='bind':
                cid=int(text) if text.lstrip('-').isdigit() else text
                c,d=self.publisher.validate(pending[1],cid)
                bid=self.store.bind(uid,pending[1],c,d)
                effect={'thread':'将限速创建老师主帖，并从旧到新补发历史评价。','feed':'之后的新车评会发布到此频道，不补发历史。','lookup':'此群已允许转发、回复和 @用户名查档。'}[pending[1]]
                if self.paused() and pending[1] != 'lookup':
                    effect += '⏸ 发布队列当前暂停，恢复后开始处理。'
                if not self.store.service_authorized():
                    effect = '激活集群后启用。'
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
                        table='proof_publications' if j['kind']=='proof' else 'publications'
                        self.store.db.execute('INSERT OR REPLACE INTO '+table+' VALUES(?,?,?,?,?)',(j['binding'],j['review'],p[0] if p else 1,mid,j['discussion'] if j['mode']=='thread' else j['chat_id']))
                    self.store.db.execute("UPDATE jobs SET state='done' WHERE id=?",(j['id'],));self.store.log(uid,'登记发送结果',j['id'])
                self.success(uid, '远端消息位置已登记，任务已完成，不会重复发送。')
                return self.action(user,'jobs:0')
        except APIError as error:
            self.notices.pop(uid, None)
            self.send(uid,self.api_hint(error))
        except (ValueError,TypeError,sqlite3.IntegrityError,OSError) as error:
            self.notices.pop(uid, None)
            self.send(uid,'⚠️ '+('图片或数据保存失败，请检查磁盘空间后重试。' if isinstance(error,OSError) else self.input_error(pending,error)))

    def handle(self, update):
        if 'callback_query' in update:return self.callback(update['callback_query'])
        if 'message' in update:return self.message(update['message'])
        if 'edited_message' in update:return self.message(update['edited_message'],True)
        if 'chat_member' in update:
            e=update['chat_member']
            target=self.store.db.execute("SELECT value FROM settings WHERE key='membership_target'").fetchone()
            if (target and json.loads(target[0])['id']==e['chat']['id']) or self.store.db.execute("SELECT 1 FROM bindings WHERE chat_id=? AND mode='lookup' AND active=1",(e['chat']['id'],)).fetchone():self.member_update(e)
        if 'my_chat_member' in update:
            e=update['my_chat_member'];cid=e['chat']['id']
            if e['chat'].get('type') == 'channel' and e['new_chat_member']['status'] in ('administrator','creator'):
                try:
                    self.publisher.validate('feed',cid)
                    notice='✅ 频道发布权限检查通过。选择老师专帖时会另行检查讨论组。\nℹ️ 当前尚未自动绑定，请在管理后台选择绑定模式。'
                except ValueError as error:
                    notice='⚠️ 频道发布权限检查未通过。\n'+str(error)
                except APIError as error:
                    notice=self.api_hint(error)
                for admin in self.store.admin_ids():
                    try:self.send(admin,notice+'\n频道 ID：'+str(cid))
                    except APIError:pass
            if e['new_chat_member']['status'] not in ('administrator','creator'):
                self.store.db.execute("UPDATE bindings SET active=0,error='机器人管理员权限已失效' WHERE chat_id=? OR discussion=?",(cid,cid))

    def private_lookup(self, msg):
        uid = msg['from']['id']
        text = msg.get('text', '').strip()
        if text.isdigit():
            return self.show_person(uid, self.store.track(text)['id'])
        return self.query(msg)

    def card(self, p, chat_id=None):
        text = '\n'.join(('档案编号：' + str(p['id']),
                          '👤 当前名称：' + (p['name'] or '未知'),
                          'Telegram ID：' + (str(p['user_id']) if p['user_id'] else '待识别'),
                          '用户名：' + ('@' + p['username'] if p['username'] else '未知 / 未设置'),
                          '🏷 群标签：' + (p['tags'] or '无')))
        if self.store.db.execute("SELECT 1 FROM settings WHERE key='membership_target'").fetchone():
            return text
        memberships = self.store.db.execute('SELECT * FROM memberships WHERE person=?' +
                                            (' AND chat_id=?' if chat_id else '') + ' ORDER BY observed DESC LIMIT 8',
                                            (p['id'], chat_id) if chat_id else (p['id'],)).fetchall()
        if not memberships:
            text += '\n是否在群：未知\n入群时间：未知'
        for m in memberships:
            text += '\n\n%s\n是否在群：%s\n入群时间：%s\n状态更新：%s' % (
                m['title'][:100], {1: '是', 0: '否'}.get(m['present'], '未知'), date(m['joined']), date(m['observed']))
        return text
    def target_membership(self, person, group=False):
        saved=self.store.db.execute("SELECT value FROM settings WHERE key='membership_target'").fetchone()
        if not saved:return ''
        target=json.loads(saved[0])
        prefix='\n\n' if group else '\n\n🎯 判定群：%s（%s）\n'%(target['title'],target['id'])
        if not person['user_id']:
            return prefix+'是否在群：无法确认（尚未识别用户数字 ID）'
        try:
            self.publisher._rights(target['id'],False,'目标群' if group else '判定群')
            member=self.api.call('getChatMember',chat_id=target['id'],user_id=person['user_id'])
            present=member_present(member)
            if present is None:return prefix+'是否在群：暂时无法确认（未知成员状态）'
            stamp=time.time()
            self.store.member(person['id'],target,present,stamp)
            record=self.store.db.execute('SELECT joined FROM memberships WHERE person=? AND chat_id=?',(person['id'],target['id'])).fetchone()
            return prefix+'是否在群：%s\n入群时间：%s\n查询时间：%s'%('是' if present else '否',date(record[0]) if record else '未知',date(stamp))
        except (APIError,ValueError) as error:
            detail=self.api_hint(error) if isinstance(error,APIError) else str(error)
            return prefix+'是否在群：暂时无法确认\n'+detail

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
            self.store.observe_member(message.get('from', {}), chat, True, stamp)
        for user in message.get('new_chat_members', []):
            self.store.observe_member(user, chat, True, stamp, stamp)
        user = message.get('left_chat_member')
        if user:
            self.store.observe_member(user, chat, False, stamp)
    def member_update(self, event):
        chat = event['chat']
        if chat.get('type') not in ('group', 'supergroup'):
            return
        member = event.get('new_chat_member', {})
        present = member_present(member)
        if present is None:
            return
        stamp = event['date']
        joined = stamp if present and member_present(event.get('old_chat_member', {})) is False else None
        self.store.observe_member(member.get('user', {}), chat, present, stamp, joined)
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
            if p['user_id'] and not self.store.db.execute("SELECT 1 FROM settings WHERE key='membership_target'").fetchone():
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
            buttons = [[{'text': label, 'url': 'https://t.me/%s?start=%s' %
                        (self.username, self.store.start_payload(intent, p['id']))}
                        for label, intent in (('新增标签', 'edit'), ('删除标签', 'delete'))]]
            notice = self.match_notice(target)
            self.send(chat['id'], (notice + '\n\n' if notice else '') + self.card(p, chat['id']) + self.target_membership(p, group=True) + '\n\n以上为查询时的资料，更新后请重新查询。', buttons,
                      reply_parameters={'message_id': message['message_id'], 'allow_sending_without_reply': True})


class ClusterBot:
    """One polling identity, with a private menu context and a group route."""

    def __init__(self, stores, api, username, bot_id):
        self.stores, self.api, self.username, self.bot_id = stores, api, username, bot_id
        self.pacing = [0, {}]
        self.bots = {cid: self.make_bot(store) for cid, store in stores.stores.items()}
        self.selected = {}
        self.member_cache = OrderedDict()
        self.membership_retry_at = 0
        self.next_publisher = 0

    def make_bot(self, store):
        return Bot(store, self.api, self.username, self.bot_id, self)

    def create(self, uid):
        store = self.stores.create(uid)
        self.bots[store.cluster_id] = self.make_bot(store)
        return store.cluster_id

    def legacy_access(self, bot):
        return len(self.bots) == 1 and bot.store.member_source_ids() is None

    def invalidate_chat(self, chat_id, uid=None):
        if uid is not None:
            self.member_cache.pop((chat_id, uid), None)
            return
        for key in list(self.member_cache):
            if key[0] == chat_id:
                del self.member_cache[key]

    def chat_member(self, chat_id, uid):
        key = chat_id, uid
        now = time.monotonic()
        previous = self.member_cache.get(key)
        if previous and now < previous[0]:
            self.member_cache.move_to_end(key)
            return previous[1]
        if now < self.membership_retry_at:
            return None
        try:
            member = self.api.call('getChatMember', chat_id=chat_id, user_id=uid)
            result = (member.get('status') in ('administrator', 'creator') if uid == self.bot_id
                      else member_present(member))
        except APIError as error:
            if error.code == 429:
                self.membership_retry_at = time.monotonic() + max(1, error.retry_after)
            result = None
        self.member_cache[key] = time.monotonic() + (30 if result is not None else 3), result
        self.member_cache.move_to_end(key)
        if len(self.member_cache) > 8192:
            self.member_cache.popitem(last=False)
        return result

    def member_access(self, bot, uid, is_admin):
        if not bot.store.service_authorized():
            return False
        if not bot.store.ready():
            # Temporarily unavailable bindings cannot prove membership.
            return None if bot.store.member_source_ids() else False
        if is_admin or self.legacy_access(bot):
            return True
        selected = bot.store.member_source_ids() or set()
        sources = selected.intersection(bot.store.member_sources())
        unknown = bool(selected - sources)
        for chat_id in sorted(sources):
            member = self.chat_member(chat_id, uid) if self.chat_member(chat_id, self.bot_id) else None
            if member is True:
                return True
            unknown |= member is None
        return None if unknown else False

    def require_member(self, bot, uid):
        if not bot.store.service_authorized():
            raise ValueError('该集群尚未激活，请联系管理员。')
        if self.stores.primary.admin(uid):
            return
        result = self.member_access(bot, uid, False)
        if result is None:
            raise ValueError('暂时无法确认成员身份，请稍后重试。')
        if result is False:
            raise ValueError('请先加入该集群指定的群或频道。')

    def member_menu(self, source, user, operation, page=0, message=None):
        uid = user['id']
        if operation not in MEMBER_ACTIONS:
            raise ValueError('请从主菜单选择要使用的功能。')
        is_admin = self.stores.primary.admin(uid)
        choices = []
        unknown = False
        for cid, bot in self.bots.items():
            if operation == 'write' and not bot.store.can_publish():
                continue
            result = self.member_access(bot, uid, is_admin)
            if result and bot.store.ready():
                choices.append(cid)
            unknown |= result is None
        if len(choices) == 1:
            if unknown:
                self.bots[choices[0]].notices[uid] = '部分集群暂时无法核验。'
            return self.enter(source, user, operation, choices[0], message)
        if not choices:
            text = ('暂时无法确认成员身份，请稍后重试。' if unknown else
                    '暂无可用集群。请先加入对应群或频道，或联系管理员。')
            return source.panel(uid, text, [[('重试', 'memberpage:%s:0' % operation)], [('返回主菜单', 'home')]],
                                ('memberpick', operation, message))
        page = min(max(0, page), (len(choices) - 1) // 8)
        rows = []
        for cid in choices[page * 8:(page + 1) * 8]:
            label = self.stores.names[cid]
            if operation == 'write' and self.bots[cid].paused():
                label += '（暂停发布）'
            rows.append([(label, 'memberpick:%s:%s' % (operation, cid))])
        nav = []
        if page:
            nav.append(('上一页', 'memberpage:%s:%s' % (operation, page - 1)))
        if len(choices) > (page + 1) * 8:
            nav.append(('下一页', 'memberpage:%s:%s' % (operation, page + 1)))
        if nav:
            rows.append(nav)
        text = '选择集群 · ' + MEMBER_ACTIONS[operation]
        if unknown:
            text += '\n部分集群暂时无法核验。'
            rows.append([('重新核验', 'memberpage:%s:0' % operation)])
        return source.panel(uid, text, rows + [[('返回主菜单', 'home')]], ('memberpick', operation, message))

    def enter(self, source, user, operation, cluster_id, message=None):
        uid = user['id']
        target = self.bots[cluster_id]
        self.require_member(target, uid)
        if not target.store.ready():
            raise ValueError('此集群暂不可用。')
        target.edit_target = source.edit_target
        try:
            if operation == 'lookup':
                result = target.private_lookup(message)
            else:
                result = target.action(user, operation)
        finally:
            target.edit_target = None
        self.select(source, target, uid)
        return result

    def mine(self, source, uid, page):
        start = page * 8
        streams = [zip(repeat(bot), bot.review_items(uid, start + 9)) for bot in self.bots.values()]
        items = merge(*streams, key=lambda item: (item[1]['created'], item[1]['id'], item[0].store.cluster_id),
                      reverse=True)
        return source.my_reviews_panel(uid, page, list(islice(items, start, start + 9)))

    def select(self, source, target, uid):
        if target is source:
            return
        source.sessions.pop(uid, None)
        source.teacher_imports.pop(uid, None)
        source.imports.pop(uid, None)
        self.selected[uid] = target.store.cluster_id

    def switch(self, source, uid, cluster_id, destination):
        target = self.bots.get(cluster_id)
        if not target or (not self.stores.primary.admin(uid) and not target.store.ready()):
            raise ValueError('此集群暂不可用。')
        target.edit_target = source.edit_target
        try:
            result = target.action({'id': uid}, destination)
        finally:
            target.edit_target = None
        self.select(source, target, uid)
        return result

    def handle(self, update):
        if 'callback_query' in update:
            uid = update['callback_query']['from']['id']
            return self.bots[self.selected.get(uid, 1)].handle(update)
        event = next((update[key] for key in ('message', 'edited_message', 'chat_member', 'my_chat_member')
                      if key in update), None)
        if not event:
            return
        chat = event['chat']
        if chat['type'] != 'private':
            if 'chat_member' in update:
                self.invalidate_chat(chat['id'], event['new_chat_member']['user']['id'])
            elif 'my_chat_member' in update:
                self.invalidate_chat(chat['id'])
            cluster_id = self.stores.routes.get(chat['id'])
            if 'my_chat_member' in update:
                return self.bots[cluster_id or 1].handle(update)
            if cluster_id is not None and self.bots[cluster_id].store.service_authorized():
                return self.bots[cluster_id].handle(update)
            return
        uid = chat['id']
        source = self.bots[self.selected.get(uid, 1)]
        if 'message' in update:
            for store in self.stores.stores.values():
                if store.service_authorized():
                    store.observe_private_teacher(event.get('from', {}), event.get('date', time.time()))
            match = re.fullmatch(r'(/start(?:@\w+)?)\s+(?:c([1-9]\d{0,18})_)?((?:review|edit|delete)_[0-9]{1,19})',
                                 event.get('text', '').strip(), flags=re.ASCII)
            if match:
                cluster_id = int(match[2] or 1)
                target = self.bots.get(cluster_id)
                if not target:
                    return source.send(uid, '该链接对应的集群不存在。')
                try:
                    self.require_member(target, uid)
                except ValueError as error:
                    return source.send(uid, str(error))
                self.select(source, target, uid)
                try:
                    return target.message({**event, 'text': match[1] + ' ' + match[3]})
                except ValueError as error:
                    return target.send(uid, str(error))
        return source.handle(update)

    def tick(self):
        publishers = [bot.publisher for bot in self.bots.values() if bot.store.service_authorized()]
        if not publishers:
            return
        start = self.next_publisher
        for i in range(len(publishers)):
            index = (start + i) % len(publishers)
            before = self.pacing[0]
            publishers[index].tick()
            if self.pacing[0] != before:
                self.next_publisher = (index + 1) % len(publishers)

    def poll_timeout(self):
        return min((bot.publisher.poll_timeout() for bot in self.bots.values()
                    if bot.store.service_authorized()), default=20)
