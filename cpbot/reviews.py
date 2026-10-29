"""Transactional review domain. Telegram publications are replicas, never source data."""
import json
import re
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
FIELDS = ('时间', '老师', '出击证明', '人照', '颜值', '身材', '服务', '态度', '环境', '综合', '过程')
INPUT_FIELDS = tuple(f for f in FIELDS if f != '综合')
LEGACY_FIELDS = tuple('留名' if f == '出击证明' else f for f in FIELDS)

def valid_proof(value):
    import re
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}\.jpg', value) is not None
SCORES = ('人照', '颜值', '身材', '服务', '态度', '环境')

def valid_score(value):
    return type(value) is int and 1 <= value <= 10

def overall_score(data):
    if not all(valid_score(data.get(f)) for f in SCORES):
        raise ValueError('人照、颜值、身材、服务、态度、环境均需通过按钮选择 1～10 分。')
    return sum(data[f] for f in SCORES) / len(SCORES)

def missing_fields(data):
    return [f for f in INPUT_FIELDS if (not valid_score(data.get(f)) if f in SCORES else (not valid_proof(data.get(f)) if f == '出击证明' else not str(data.get(f, '')).strip()))]

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
        CREATE TABLE IF NOT EXISTS proof_publications(binding INTEGER NOT NULL,review INTEGER NOT NULL REFERENCES reviews(id),
            generation INTEGER NOT NULL,message INTEGER NOT NULL,chat_id INTEGER NOT NULL,
            PRIMARY KEY(binding,review,generation));
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,binding INTEGER NOT NULL,
            teacher INTEGER NOT NULL,review INTEGER NOT NULL DEFAULT 0,generation INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'pending',due REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
            UNIQUE(kind,binding,teacher,review,generation));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor INTEGER NOT NULL,action TEXT NOT NULL,
            detail TEXT NOT NULL,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS jobs_pending_due ON jobs(due,binding) WHERE state='pending';
        CREATE INDEX IF NOT EXISTS jobs_open_review ON jobs(binding,teacher,generation,review)
            WHERE state NOT IN ('done','superseded');
        CREATE INDEX IF NOT EXISTS reviews_teacher_time ON reviews(teacher,status,json_extract(data,'$.时间'),id);
        CREATE INDEX IF NOT EXISTS reviews_author_created ON reviews(author,created DESC);
        CREATE INDEX IF NOT EXISTS reviews_expiry ON reviews(expires) WHERE status IN ('pending','rejected');
        CREATE INDEX IF NOT EXISTS publications_review ON publications(review,binding,generation);
        ''')
        if 'username' not in {r['name'] for r in self.db.execute('PRAGMA table_info(teachers)')}:
            self.db.execute('ALTER TABLE teachers ADD COLUMN username TEXT COLLATE NOCASE')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS teachers_username ON teachers(username COLLATE NOCASE)')
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
                self.db.execute('UPDATE teachers SET user_id=?,username=NULL WHERE id=?',(uid,tid))
                self.log(actor,'绑定老师账号' if uid else '解绑老师账号',tid)
        except sqlite3.IntegrityError:
            raise ValueError('此账号已绑定其他老师，请先解除原绑定。') from None

    def parse_teachers(self, actor, text):
        self.require_admin(actor)
        entries=[]
        for number,line in enumerate(text.splitlines(),1):
            line=line.strip()
            if not line:continue
            if any(sep in line for sep in ('|','｜','+','＋')):
                parts=[p.strip() for p in re.split(r'[|｜+＋]',line)]
                if len(parts)>3:raise ValueError('第 %s 行字段过多。'%number)
                name=parts[0];extras=parts[1:]
            else:
                parts=line.split();extras=[]
                while len(parts)>1 and (parts[-1].startswith('@') or parts[-1].isascii() and parts[-1].isdigit()):
                    extras.insert(0,parts.pop())
                name=' '.join(parts)
            username=None;uid=None
            for value in extras:
                if not value:continue
                if value.isascii() and value.isdigit():
                    if uid is not None:raise ValueError('第 %s 行有多个 ID。'%number)
                    uid=int(value)
                else:
                    value=value.lstrip('@').lower()
                    if username is not None or not re.fullmatch(r'[a-z][a-z0-9_]{3,31}',value):
                        raise ValueError('第 %s 行用户名无效。'%number)
                    username=value
            entries.append((name,uid,username))
        self.validate_teachers(entries)
        return entries

    def validate_teachers(self, entries):
        if not 1<=len(entries)<=100:raise ValueError('每批请填写 1～100 位老师，每行一位。')
        names,accounts,usernames=set(),set(),set()
        for index,entry in enumerate(entries,1):
            name,uid=entry[:2];username=entry[2] if len(entry)>2 else None
            if not 1<=len(name)<=80 or '\n' in name or (uid is not None and (type(uid) is not int or not 0<uid<2**63)):
                raise ValueError('第 %s 位老师名称或数字 ID 无效。'%index)
            if username is not None and (not isinstance(username,str) or not re.fullmatch(r'[a-z][a-z0-9_]{3,31}',username)):
                raise ValueError('第 %s 位用户名无效。'%index)
            if name in names or self.db.execute('SELECT 1 FROM teachers WHERE name=?',(name,)).fetchone():
                raise ValueError('第 %s 位名称重复，不会覆盖已有老师。'%index)
            if uid is not None and (uid in accounts or self.db.execute('SELECT 1 FROM teachers WHERE user_id=?',(uid,)).fetchone()):
                raise ValueError('第 %s 位账号 ID 已绑定或重复。'%index)
            if username and (username in usernames or self.db.execute('SELECT 1 FROM teachers WHERE username=? COLLATE NOCASE',(username,)).fetchone()):
                raise ValueError('第 %s 位用户名已登记或重复。'%index)
            names.add(name);accounts.add(uid);usernames.add(username)

    def import_teachers(self, actor, entries):
        with self.transaction():
            self.require_admin(actor);self.validate_teachers(entries)
            ids=[]
            for entry in entries:
                name,uid=entry[:2];username=entry[2] if len(entry)>2 else None
                tid=self.db.execute('INSERT INTO teachers(name,user_id,username) VALUES(?,?,?)',(name,uid,username)).lastrowid
                ids.append(tid);self.log(actor,'批量添加老师',tid)
                for b in self.db.execute("SELECT id FROM bindings WHERE mode='thread'"):
                    self.ensure_post(b['id'],tid)
            return ids

    def identify_teacher(self, user):
        uid=user.get('id');username=(user.get('username') or '').lower()
        if not uid or user.get('is_bot') or not username:return
        # Most observed members are not pending teachers. Do not run an UPDATE
        # for every ordinary group message; the username index makes this cheap.
        pending=self.db.execute('SELECT id FROM teachers WHERE username=? COLLATE NOCASE AND user_id IS NULL',
                                (username,)).fetchone()
        if not pending:return
        changed=self.db.execute("UPDATE teachers SET user_id=? WHERE user_id IS NULL AND username=? COLLATE NOCASE AND NOT EXISTS(SELECT 1 FROM teachers WHERE user_id=?)",(uid,username,uid)).rowcount
        if changed:self.log(uid,'用户名自动绑定老师账号',username)

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
        raise ValueError('留名机制已取消，请上传出击证明后直接提交车评。')

    def proof_path(self, name):
        from pathlib import Path
        if not valid_proof(name):
            raise ValueError('出击证明无效，请重新上传图片。')
        location = self.db.execute('PRAGMA database_list').fetchone()['file']
        if not location:
            raise ValueError('出击证明需要持久化数据库目录。')
        return Path(location).parent / 'proofs' / name

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
            if missing_fields(data):
                raise ValueError('请补齐所有字段。')
            path=self.proof_path(data['出击证明'])
            if not path.is_file() or path.stat().st_size==0:
                raise ValueError('出击证明文件缺失，请重新上传图片。')
            data.pop('留名',None)
            data['综合'] = overall_score(data)
            self.teacher(int(data['老师']))
            try:
                data['时间'] = review_date(data['时间'], legacy=True)
            except (ValueError, TypeError):
                raise ValueError('日期格式应为 YYYY-MM-DD，不进行预约日期核验。') from None
            if not isinstance(data['过程'], str) or len(data['过程']) > 1600:
                raise ValueError('过程最多 1600 字。')
            last = self.db.execute('SELECT created FROM reviews WHERE author=? ORDER BY id DESC LIMIT 1', (user['id'],)).fetchone()
            if last and time.time() - last[0] < 30:
                raise ValueError('提交过于频繁，请稍后再试。')
            status = 'accepted'
            now = time.time()
            rid = self.db.execute('''INSERT INTO reviews(author,identity,teacher,data,anonymous,status,created,expires,submission)
                VALUES(?,?,?,?,?,?,?,?,?)''', (user['id'], json.dumps(user, ensure_ascii=False), data['老师'],
                json.dumps(data, ensure_ascii=False), int(data.get('匿名', True)), status, now,
                None, token)).lastrowid
            self.enqueue_review(rid)
            self.db.execute('DELETE FROM drafts WHERE user_id=?', (user['id'],))
            return rid, status

    def moderate(self, actor, rid, approve):
        raise ValueError('车评审核已取消，新车评上传出击证明后直接发布。')

    def enqueue(self, kind, bid, tid, rid=0, generation=1):
        self.db.execute('INSERT OR IGNORE INTO jobs(kind,binding,teacher,review,generation) VALUES(?,?,?,?,?)',
                        (kind, bid, tid, rid, generation))

    def ensure_post(self, bid, tid, backfill=True):
        self.db.execute('INSERT OR IGNORE INTO posts(binding,teacher) VALUES(?,?)', (bid, tid))
        p = self.db.execute('SELECT * FROM posts WHERE binding=? AND teacher=?', (bid, tid)).fetchone()
        if not p['message']:
            self.enqueue('post', bid, tid, generation=p['generation'])
        if not backfill:
            return p
        for r in self.db.execute("SELECT id,data FROM reviews WHERE teacher=? AND status='accepted' ORDER BY json_extract(data,'$.时间'),id", (tid,)):
            self.enqueue('comment', bid, tid, r['id'], p['generation'])
            if json.loads(r['data']).get('出击证明'):
                self.enqueue('proof', bid, tid, r['id'], p['generation'])
        return p

    def enqueue_review(self, rid):
        r = self.db.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone()
        for b in self.db.execute("SELECT * FROM bindings WHERE mode IN ('thread','feed')"):
            if b['mode'] == 'thread':
                # Binding/rebuild/nightly own historical reconciliation. New submissions
                # only enqueue themselves; otherwise N reviews cause O(N²) inserts.
                p = self.ensure_post(b['id'], r['teacher'], backfill=False)
                self.enqueue('comment', b['id'], r['teacher'], rid, p['generation'])
                if json.loads(r['data']).get('出击证明'):
                    self.enqueue('proof', b['id'], r['teacher'], rid, p['generation'])
            else:
                self.enqueue('feed', b['id'], r['teacher'], rid)
                if json.loads(r['data']).get('出击证明'):
                    self.enqueue('proof', b['id'], r['teacher'], rid)

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
