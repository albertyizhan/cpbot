"""Global identities, durable personal grants and transactional edit history."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager


class Store:
    def __init__(self, path, owner):
        self.owner = owner
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
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def allowed(self, uid):
        return uid == self.owner or bool(self.db.execute('SELECT 1 FROM grants WHERE user_id=?', (uid,)).fetchone())

    def password_set(self, uid, password):
        if uid != self.owner:
            raise ValueError('仅总管理者可设置密码。')
        if not 8 <= len(password) <= 128:
            raise ValueError('密码需要 8～128 个字符。')
        salt = os.urandom(16).hex()
        value = salt + ':' + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('password',?)", (value,))

    def unlock(self, user, password):
        uid, now = user['id'], time.time()
        row = self.db.execute('SELECT * FROM attempts WHERE user_id=?', (uid,)).fetchone()
        if row and row['until'] > now:
            raise ValueError('密码尝试过多，请 10 分钟后再试。')
        saved = self.db.execute("SELECT value FROM settings WHERE key='password'").fetchone()
        if not saved:
            raise ValueError('总管理者尚未设置密码。')
        salt, expected = saved[0].split(':')
        valid = len(password) <= 128 and hmac.compare_digest(expected, hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex())
        with self.transaction():
            if valid:
                self.db.execute('INSERT OR REPLACE INTO grants VALUES(?,?,?)', (uid, display_name(user), now))
                self.db.execute('DELETE FROM attempts WHERE user_id=?', (uid,))
            else:
                failures = (row['failures'] if row and row['until'] > now - 600 else 0) + 1
                self.db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)',
                                (uid, failures, now + 600 if failures >= 5 else now))
        if not valid:
            raise ValueError('密码不正确。')

    def revoke(self, actor, uid):
        if actor != self.owner or uid == self.owner:
            raise ValueError('无法执行此授权操作。')
        self.db.execute('DELETE FROM grants WHERE user_id=?', (uid,))

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

    def observe(self, user, stamp, create=False):
        uid = user.get('id')
        if not uid or user.get('is_bot'):
            return None
        username = (user.get('username') or '').lower() or None
        with self.transaction():
            by_id, by_name = self.lookup(uid), self.lookup(username=username) if username else None
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
                self.db.execute('UPDATE people SET user_id=?,username=?,name=?,observed=? WHERE id=?',
                                (uid, username, display_name(user), stamp, p['id']))
            return self.person(p['id'])

    def member(self, pid, chat, present, stamp, joined=None):
        self.db.execute('''INSERT INTO memberships VALUES(?,?,?,?,?,?)
            ON CONFLICT(person,chat_id) DO UPDATE SET title=excluded.title,
            joined=COALESCE(excluded.joined,memberships.joined),present=excluded.present,observed=excluded.observed
            WHERE excluded.observed>=memberships.observed''',
            (pid, chat['id'], chat.get('title', str(chat['id'])), joined, present, stamp))

    def save(self, pid, version, user, note, tags, action='编辑'):
        if len(note) > 500 or len(tags) > 300:
            raise ValueError('备注最多 500 字，标签最多 300 字。')
        with self.transaction():
            if not self.allowed(user['id']):
                raise ValueError('授权已失效，请重新输入密码。')
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
