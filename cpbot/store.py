"""Global identities, durable personal grants and transactional edit history."""
import json
import re
import sqlite3
import time
from contextlib import contextmanager, nullcontext


from .reviews import ReviewStore
from .backup import BackupStore


class Store(ReviewStore, BackupStore):
    def __init__(self, path, owner, configured_admins=()):
        self.owner = owner
        self.configured_admins = tuple(configured_admins)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS grants(user_id INTEGER PRIMARY KEY,name TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(user_id INTEGER PRIMARY KEY,failures INTEGER NOT NULL,until REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS people(
                id INTEGER PRIMARY KEY,user_id INTEGER UNIQUE,username TEXT UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL DEFAULT '', observed REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',tags TEXT NOT NULL DEFAULT '',version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS memberships(
                person INTEGER NOT NULL REFERENCES people(id),chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,joined REAL,present INTEGER,observed REAL NOT NULL,
                PRIMARY KEY(person,chat_id));
            CREATE TABLE IF NOT EXISTS history(
                id INTEGER PRIMARY KEY,person INTEGER NOT NULL REFERENCES people(id),
                actor INTEGER NOT NULL,actor_name TEXT NOT NULL,action TEXT NOT NULL,
                before_value TEXT NOT NULL,after_value TEXT NOT NULL,created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS people_name ON people(name);
            CREATE INDEX IF NOT EXISTS history_person_id ON history(person,id);
        ''')

        self.setup_reviews()
        with self.transaction():
            for uid in configured_admins:
                if not isinstance(uid, int) or not 0 < uid < 2**63:
                    raise ValueError("无效的 superadmin ID。")
                self.db.execute("INSERT OR IGNORE INTO admins VALUES(?)", (uid,))

    @contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        self.db.execute('SAVEPOINT nested_write' if nested else 'BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('RELEASE nested_write' if nested else 'COMMIT')
        except BaseException:
            if nested:
                self.db.execute('ROLLBACK TO nested_write')
                self.db.execute('RELEASE nested_write')
            else:
                self.db.execute('ROLLBACK')
            raise

    def allowed(self, uid):
        return self.admin(uid) or bool(self.db.execute('SELECT 1 FROM teachers WHERE user_id=?',(uid,)).fetchone())

    def require_editor(self, uid):
        if not self.allowed(uid):raise ValueError('仅已绑定老师和 superadmin 可以修改学生标签。')

    def person(self, pid):
        row = self.db.execute('SELECT * FROM people WHERE id=?', (pid,)).fetchone()
        if not row:
            raise ValueError('资料不存在，请重新查找。')
        return dict(row)

    def lookup(self, user_id=None, username=None):
        row = self.db.execute('SELECT * FROM people WHERE user_id=?' if user_id else
                              'SELECT * FROM people WHERE username=? COLLATE NOCASE',
                              (user_id or username,)).fetchone()
        return dict(row) if row else None

    def track(self, value):
        value = value.strip()
        if value.isascii() and value.isdigit() and 0 < int(value) < 2**63:
            uid, username = int(value), None
        elif re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}', value):
            uid, username = None, value[1:].lower()
        else:
            raise ValueError('请输入有效的数字 ID 或 @用户名。')
        with self.transaction():
            p = self.lookup(uid, username)
            if p:
                return p
            cursor = self.db.execute('INSERT INTO people(user_id,username) VALUES(?,?)', (uid, username))
            return self.person(cursor.lastrowid)

    def by_display_name(self, name):
        # Exact current-name match only. Two results suffice to detect ambiguity.
        if not name:
            return []
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM people WHERE name=? ORDER BY id LIMIT 2', (name,))]

    def observe(self, user, stamp, create=False):
        uid = user.get('id')
        if not uid or user.get('is_bot'):
            return None
        username = (user.get('username') or '').lower() or None
        with (nullcontext() if self.db.in_transaction else self.transaction()):
            matches = self.db.execute('SELECT * FROM people WHERE user_id=? OR username=? COLLATE NOCASE',
                                      (uid, username)).fetchall()
            by_id = next((dict(p) for p in matches if p['user_id'] == uid), None)
            by_name = next((dict(p) for p in matches if username and p['username'] and p['username'].lower() == username), None)
            if not by_id and not by_name and not create:
                return None
            if by_name and by_name['user_id'] not in (None, uid):
                if stamp < by_name['observed']:
                    return by_id
                self.db.execute('UPDATE people SET username=NULL WHERE id=?', (by_name['id'],))
                by_name = None
            if by_id and by_name and by_id['id'] != by_name['id']:
                # Unresolved annotations stay separate; never transfer them automatically.
                self.db.execute('UPDATE people SET username=NULL WHERE id=?', (by_name['id'],))
            p = by_id or by_name
            if not p:
                cursor = self.db.execute('INSERT INTO people(user_id) VALUES(?)', (uid,))
                p = self.person(cursor.lastrowid)
            if stamp >= p['observed']:
                name = display_name(user)
                if (p['user_id'],p['username'],p['name'],p['observed']) != (uid,username,name,stamp):
                    self.db.execute('UPDATE people SET user_id=?,username=?,name=?,observed=? WHERE id=?',
                                    (uid, username, name, stamp, p['id']))
                    p.update(user_id=uid,username=username,name=name,observed=stamp)
            if stamp >= p['observed']:
                self.identify_teacher(user)
            return p

    def member(self, pid, chat, present, stamp, joined=None):
        self.db.execute('''INSERT INTO memberships VALUES(?,?,?,?,?,?)
            ON CONFLICT(person,chat_id) DO UPDATE SET title=excluded.title,
            joined=COALESCE(excluded.joined,memberships.joined),present=excluded.present,observed=excluded.observed
            WHERE excluded.observed>=memberships.observed AND
            (excluded.title IS NOT memberships.title OR excluded.present IS NOT memberships.present
             OR excluded.observed IS NOT memberships.observed
             OR (excluded.joined IS NOT NULL AND excluded.joined IS NOT memberships.joined))''',
            (pid, chat['id'], chat.get('title', str(chat['id'])), joined, present, stamp))

    def observe_member(self, user, chat, present, stamp, joined=None):
        # Identity and membership become durable together, with one disk commit.
        with self.transaction():
            person = self.observe(user, stamp, create=True)
            if person:
                self.member(person['id'], chat, present, stamp, joined)
            return person

    def save(self, pid, version, user, note, tags, action='编辑'):
        if len(note) > 500 or len(tags) > 300:
            raise ValueError('备注最多 500 字，标签最多 300 字。')
        with self.transaction():
            if not self.allowed(user['id']):
                raise ValueError('仅已绑定老师和 superadmin 可以修改标签。')
            p = self.person(pid)
            changed = self.db.execute('UPDATE people SET note=?,tags=?,version=version+1 WHERE id=? AND version=?',
                                      (note, tags, pid, version)).rowcount
            if not changed:
                raise ValueError('资料已被别人修改，请重新打开后编辑。')
            self.db.execute('INSERT INTO history(person,actor,actor_name,action,before_value,after_value,created) VALUES(?,?,?,?,?,?,?)',
                            (pid, user['id'], display_name(user), action,
                             json.dumps([p['note'], p['tags']], ensure_ascii=False),
                             json.dumps([note, tags], ensure_ascii=False), time.time()))


def display_name(user):
    return ' '.join(filter(None, (user.get('first_name'), user.get('last_name'))))[:160] or str(user['id'])
