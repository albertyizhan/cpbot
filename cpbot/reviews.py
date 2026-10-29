"""Transactional review domain. Telegram publications are replicas, never source data."""
import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
FIELDS = ('时间', '老师', '留名', '人照', '颜值', '身材', '服务', '态度', '环境', '综合', '过程')
INPUT_FIELDS = tuple(f for f in FIELDS if f != '综合')
SCORES = ('人照', '颜值', '身材', '服务', '态度', '环境')

def valid_score(value):
    return type(value) is int and 1 <= value <= 10

def overall_score(data):
    if not all(valid_score(data.get(f)) for f in SCORES):
        raise ValueError('人照、颜值、身材、服务、态度、环境均需通过按钮选择 1～10 分。')
    return sum(data[f] for f in SCORES) / len(SCORES)

def missing_fields(data):
    return [f for f in INPUT_FIELDS if (not valid_score(data.get(f)) if f in SCORES else not str(data.get(f, '')).strip())]

MODES = {'thread': '老师专帖', 'feed': '车评直发', 'lookup': '内部查档'}


def now_text(stamp=None):
    return datetime.fromtimestamp(stamp or time.time(), TZ).strftime('%Y-%m-%d %H:%M')


def review_date(value, legacy=False):
    """New input is a date; existing minute-precision data remains readable."""
    if not isinstance(value, str):
        raise ValueError('日期格式应为 YYYY-MM-DD。')
    formats = ('%Y-%m-%d', '%Y-%m-%d %H:%M') if legacy else ('%Y-%m-%d',)
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.strftime(fmt) == value:
                return parsed.strftime('%Y-%m-%d')
        except ValueError:
            pass
    raise ValueError('日期格式应为 YYYY-MM-DD，例如 2026-10-29。')


def link(chat, message):
    return 'https://t.me/c/%s/%s' % (str(chat).removeprefix('-100'), message)


class ReviewStore:
    def setup_reviews(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS teachers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,user_id INTEGER UNIQUE);
        CREATE TABLE IF NOT EXISTS codes(id INTEGER PRIMARY KEY,teacher INTEGER NOT NULL REFERENCES teachers(id),
            value TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'open',created REAL NOT NULL,used_review INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS open_code ON codes(teacher,value) WHERE state='open';
        CREATE TABLE IF NOT EXISTS drafts(user_id INTEGER PRIMARY KEY,data TEXT NOT NULL,token TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,author INTEGER NOT NULL,identity TEXT NOT NULL,
            teacher INTEGER NOT NULL REFERENCES teachers(id),data TEXT NOT NULL,anonymous INTEGER NOT NULL,
            status TEXT NOT NULL,manual INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,expires REAL,
            submission TEXT UNIQUE NOT NULL,version INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS bindings(id INTEGER PRIMARY KEY,mode TEXT NOT NULL,chat_id INTEGER UNIQUE NOT NULL,
            discussion INTEGER UNIQUE,title TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS posts(binding INTEGER NOT NULL REFERENCES bindings(id),teacher INTEGER NOT NULL,
            message INTEGER,root INTEGER,generation INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(binding,teacher));
        CREATE TABLE IF NOT EXISTS publications(binding INTEGER NOT NULL,review INTEGER NOT NULL REFERENCES reviews(id),
            generation INTEGER NOT NULL,message INTEGER NOT NULL,chat_id INTEGER NOT NULL,
            PRIMARY KEY(binding,review,generation));
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,binding INTEGER NOT NULL,
            teacher INTEGER NOT NULL,review INTEGER NOT NULL DEFAULT 0,generation INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'pending',due REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
            UNIQUE(kind,binding,teacher,review,generation));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor INTEGER NOT NULL,action TEXT NOT NULL,
            detail TEXT NOT NULL,created REAL NOT NULL);
        ''')
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM settings WHERE key='admins_initialized'").fetchone():
                self.db.execute('INSERT OR IGNORE INTO admins VALUES(?)', (self.owner,))
                self.db.execute("INSERT INTO settings VALUES('admins_initialized','1')")
        # A crashed send may have succeeded remotely: never blindly resend it.
        self.db.execute("UPDATE jobs SET state='uncertain',error='进程在请求期间中断，请核对远端消息' WHERE state='sending'")

    def admin(self, uid):
        return bool(self.db.execute('SELECT 1 FROM admins WHERE user_id=?', (uid,)).fetchone())

    def require_admin(self, uid):
        if not self.admin(uid):
            raise ValueError('仅 superadmin 可以操作。')

    def log(self, uid, action, detail):
        self.db.execute('INSERT INTO audit(actor,action,detail,created) VALUES(?,?,?,?)',
                        (uid, action, str(detail), time.time()))

    def set_admin(self, actor, uid, add=True):
        self.require_admin(actor)
        if not 0 < uid < 2**63:
            raise ValueError('无效的 Telegram ID。')
        with self.transaction():
            if not add and self.db.execute('SELECT count(*) FROM admins').fetchone()[0] == 1 and self.admin(uid):
                raise ValueError('不能移除最后一位 superadmin。')
            self.db.execute('INSERT OR IGNORE INTO admins VALUES(?)' if add else 'DELETE FROM admins WHERE user_id=?', (uid,))
            self.log(actor, '添加管理员' if add else '移除管理员', uid)

    def teacher(self, tid):
        row = self.db.execute('SELECT * FROM teachers WHERE id=?', (tid,)).fetchone()
        if not row:
            raise ValueError('老师不存在。')
        return dict(row)

    def save_teacher(self, actor, name, uid=None, tid=None):
        self.require_admin(actor)
        if not name.strip() or len(name) > 80 or (uid is not None and not 0 < uid < 2**63):
            raise ValueError('名称需要 1～80 字，身份 ID 必须是正整数。')
        with self.transaction():
            if tid:
                self.teacher(tid)
                self.db.execute('UPDATE teachers SET name=?,user_id=? WHERE id=?', (name.strip(), uid, tid))
            else:
                tid = self.db.execute('INSERT INTO teachers(name,user_id) VALUES(?,?)', (name.strip(), uid)).lastrowid
            self.log(actor, '保存老师', tid)
            for b in self.db.execute("SELECT id FROM bindings WHERE mode='thread'"):
                self.ensure_post(b['id'], tid)
                self.enqueue('summary', b['id'], tid, generation=time.time_ns() // 1000)
            return tid

    def set_teacher_account(self, actor, tid, uid):
        self.require_admin(actor)
        if uid is not None and (type(uid) is not int or not 0 < uid < 2**63):
            raise ValueError('请输入有效的 Telegram 数字 ID。')
        try:
            with self.transaction():
                self.teacher(tid)
                self.db.execute('UPDATE teachers SET user_id=? WHERE id=?',(uid,tid))
                self.log(actor,'绑定老师账号' if uid else '解绑老师账号',tid)
        except sqlite3.IntegrityError:
            raise ValueError('此账号已绑定其他老师，请先解除原绑定。') from None

    def parse_teachers(self, actor, text):
        """Parse name-only or name | ID lines; preview never writes data."""
        self.require_admin(actor)
        entries = []
        for line_no, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            uid = None
            if '|' in line or '｜' in line:
                parts = line.replace('｜', '|').split('|')
                if len(parts) != 2:
                    raise ValueError('第 %s 行格式错误，请使用 老师名称 | 数字ID。' % line_no)
                name, raw = (v.strip() for v in parts)
                if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 2**63:
                    raise ValueError('第 %s 行需要有效的 Telegram 数字 ID。' % line_no)
                uid = int(raw)
            else:
                parts = line.rsplit(None, 1)
                if len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
                    name, raw = parts
                    uid = int(raw)
                else:
                    name = line
            entries.append((name.strip(), uid))
        self.validate_teachers(entries)
        return entries

    def validate_teachers(self, entries):
        if not 1 <= len(entries) <= 100:
            raise ValueError('每批请填写 1～100 位老师，每行一位。')
        names, accounts = set(), set()
        for index, (name, uid) in enumerate(entries, 1):
            if not 1 <= len(name) <= 80 or '\n' in name or (uid is not None and (type(uid) is not int or not 0 < uid < 2**63)):
                raise ValueError('第 %s 位老师名称或数字 ID 无效。' % index)
            if name in names or self.db.execute('SELECT 1 FROM teachers WHERE name=?', (name,)).fetchone():
                raise ValueError('第 %s 位「%s」名称重复，请修改后重新预览；不会覆盖已有老师。' % (index, name))
            if uid is not None and (uid in accounts or self.db.execute('SELECT 1 FROM teachers WHERE user_id=?', (uid,)).fetchone()):
                raise ValueError('第 %s 位的账号 %s 已绑定或在本批中重复。' % (index, uid))
            names.add(name)
            if uid is not None:
                accounts.add(uid)

    def import_teachers(self, actor, entries):
        with self.transaction():
            self.require_admin(actor)
            self.validate_teachers(entries)
            ids = []
            for name, uid in entries:
                tid = self.db.execute('INSERT INTO teachers(name,user_id) VALUES(?,?)', (name, uid)).lastrowid
                ids.append(tid)
                self.log(actor, '批量添加老师', tid)
                for b in self.db.execute("SELECT id FROM bindings WHERE mode='thread'"):
                    self.ensure_post(b['id'], tid)
            return ids

    def rename_teacher(self, actor, tid, name):
        self.require_admin(actor)
        if not name.strip() or len(name)>80 or '\n' in name:
            raise ValueError('老师名称需要一行，1～80 字。')
        with self.transaction():
            self.teacher(tid)
            self.db.execute('UPDATE teachers SET name=? WHERE id=?',(name.strip(),tid))
            self.log(actor,'修改老师名称',tid)
            for b in self.db.execute("SELECT id FROM bindings WHERE mode='thread'"):
                self.ensure_post(b['id'],tid)
                self.enqueue('summary',b['id'],tid,generation=time.time_ns()//1000)

    def add_codes(self, actor, tid, values):
        teacher = self.teacher(tid)
        if not self.admin(actor) and teacher['user_id'] != actor:
            raise ValueError('只能为自己添加留名。')
        values = [v.strip() for v in values if v.strip()]
        if not values or any(len(v) > 80 for v in values) or len(set(values)) != len(values):
            raise ValueError('留名不能为空、不能在本次输入中重复，每个最多 80 字。')
        try:
            with self.transaction():
                for value in values:
                    self.db.execute('INSERT INTO codes(teacher,value,created) VALUES(?,?,?)', (tid, value, time.time()))
                self.log(actor, '添加留名', '%s：%s 个' % (tid, len(values)))
        except sqlite3.IntegrityError:
            raise ValueError('存在尚未使用的同名留名，本次未添加。') from None

    def draft(self, uid):
        row = self.db.execute('SELECT * FROM drafts WHERE user_id=?', (uid,)).fetchone()
        return (json.loads(row['data']), row['token']) if row else ({'匿名': True}, None)

    def save_draft(self, uid, data, token):
        self.db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?)', (uid, json.dumps(data, ensure_ascii=False), token))

    def ready(self):
        return bool(self.db.execute("SELECT 1 FROM bindings WHERE mode='thread' AND active=1").fetchone())

    def submit(self, user, token):
        if not self.ready():
            raise ValueError('尚无可用的老师专帖频道。')
        with self.transaction():
            existing = self.db.execute('SELECT id,status FROM reviews WHERE submission=?', (token,)).fetchone()
            if existing:
                return existing['id'], existing['status']
            data, current = self.draft(user['id'])
            if token != current:
                raise ValueError('草稿已变更，请重新打开。')
            if any(not str(data.get(f, '')).strip() for f in INPUT_FIELDS):
                raise ValueError('请补齐所有字段。')
            data['综合'] = overall_score(data)
            self.teacher(int(data['老师']))
            try:
                data['时间'] = review_date(data['时间'], legacy=True)
            except (ValueError, TypeError):
                raise ValueError('日期格式应为 YYYY-MM-DD，不进行预约日期核验。') from None
            if not isinstance(data['过程'], str) or not isinstance(data['留名'], str) or len(data['过程']) > 1600 or len(data['留名']) > 80:
                raise ValueError('过程最多 1600 字，留名最多 80 字。')
            last = self.db.execute('SELECT created FROM reviews WHERE author=? ORDER BY id DESC LIMIT 1', (user['id'],)).fetchone()
            if last and time.time() - last[0] < 30:
                raise ValueError('提交过于频繁，请稍后再试。')
            code = self.db.execute("SELECT id FROM codes WHERE teacher=? AND value=? AND state='open'",
                                   (data['老师'], data['留名'].strip())).fetchone()
            status = 'accepted' if code else 'pending'
            if not code and self.db.execute("SELECT 1 FROM reviews WHERE author=? AND teacher=? AND status='pending'",
                                            (user['id'], data['老师'])).fetchone():
                raise ValueError('该老师已有一条待审核申请，请等待处理。')
            now = time.time()
            rid = self.db.execute('''INSERT INTO reviews(author,identity,teacher,data,anonymous,status,created,expires,submission)
                VALUES(?,?,?,?,?,?,?,?,?)''', (user['id'], json.dumps(user, ensure_ascii=False), data['老师'],
                json.dumps(data, ensure_ascii=False), int(data.get('匿名', True)), status, now,
                now + 72*3600 if not code else None, token)).lastrowid
            if code:
                self.db.execute("UPDATE codes SET state='used',used_review=? WHERE id=? AND state='open'", (rid, code['id']))
                self.enqueue_review(rid)
            self.db.execute('DELETE FROM drafts WHERE user_id=?', (user['id'],))
            return rid, status

    def moderate(self, actor, rid, approve):
        self.require_admin(actor)
        with self.transaction():
            row = self.db.execute("SELECT * FROM reviews WHERE id=? AND status='pending' AND expires>?", (rid, time.time())).fetchone()
            if not row:
                raise ValueError('审核记录已处理或超过 72 小时。')
            self.db.execute('UPDATE reviews SET status=?,manual=1,expires=? WHERE id=?',
                            ('accepted' if approve else 'rejected', None if approve else row['expires'], rid))
            if approve:
                self.enqueue_review(rid)
            self.log(actor, '审核通过' if approve else '审核拒绝', rid)

    def enqueue(self, kind, bid, tid, rid=0, generation=1):
        self.db.execute('INSERT OR IGNORE INTO jobs(kind,binding,teacher,review,generation) VALUES(?,?,?,?,?)',
                        (kind, bid, tid, rid, generation))

    def ensure_post(self, bid, tid):
        self.db.execute('INSERT OR IGNORE INTO posts(binding,teacher) VALUES(?,?)', (bid, tid))
        p = self.db.execute('SELECT * FROM posts WHERE binding=? AND teacher=?', (bid, tid)).fetchone()
        if not p['message']:
            self.enqueue('post', bid, tid, generation=p['generation'])
        for r in self.db.execute("SELECT id FROM reviews WHERE teacher=? AND status='accepted' ORDER BY json_extract(data,'$.时间'),id", (tid,)):
            self.enqueue('comment', bid, tid, r['id'], p['generation'])
        return p

    def enqueue_review(self, rid):
        r = self.db.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone()
        for b in self.db.execute("SELECT * FROM bindings WHERE mode IN ('thread','feed')"):
            if b['mode'] == 'thread':
                self.ensure_post(b['id'], r['teacher'])
            else:
                self.enqueue('feed', b['id'], r['teacher'], rid)

    def bind(self, actor, mode, chat, discussion=None):
        self.require_admin(actor)
        if mode not in MODES:
            raise ValueError('未知模式。')
        ids = [chat['id']] + ([discussion] if discussion else [])
        with self.transaction():
            for cid in ids:
                if self.db.execute('SELECT 1 FROM bindings WHERE chat_id=? OR discussion=?', (cid, cid)).fetchone():
                    raise ValueError('频道或群组已经被其他绑定占用。')
            bid = self.db.execute('INSERT INTO bindings(mode,chat_id,discussion,title) VALUES(?,?,?,?)',
                                  (mode, chat['id'], discussion, chat.get('title', str(chat['id'])))).lastrowid
            if mode == 'thread':
                for t in self.db.execute('SELECT id FROM teachers'):
                    self.ensure_post(bid, t['id'])
            self.log(actor, '绑定 ' + mode, chat['id'])
            return bid
