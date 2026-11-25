"""Cluster data, deployment authority and transactional edit history."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager, nullcontext


from .reviews import ReviewStore
from .backup import BackupStore


class Store(ReviewStore, BackupStore):
    def __init__(self, path, owner, configured_admins=()):
        self.owner = owner
        self.configured_admins = tuple(dict.fromkeys(configured_admins or (owner,)))
        if any(type(uid) is not int or not 0 < uid < 2**63 for uid in self.configured_admins):
            raise ValueError('无效的 superadmin ID。')
        self.cluster_id = 1
        self.clusters = None
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
            if not self.db.execute("SELECT 1 FROM settings WHERE key='notes_removed'").fetchone():
                self.clear_notes()
                self.db.execute("INSERT INTO settings VALUES('notes_removed','1')")

    def admin_ids(self):
        authority = self.clusters.primary if self.clusters else self
        return sorted(authority.configured_admins)

    def admin(self, uid):
        authority = self.clusters.primary if self.clusters else self
        return uid in authority.configured_admins

    def service_authorized(self):
        return not self.clusters or self.cluster_id in self.clusters.authorized

    def membership_target(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='membership_target'").fetchone()
        return json.loads(row[0]) if row else None

    def member_sources(self):
        sources = {}
        for row in self.db.execute('SELECT chat_id,title,discussion FROM bindings WHERE active=1 ORDER BY id'):
            sources[row['chat_id']] = row['title']
            if row['discussion']:
                sources[row['discussion']] = '%s · 讨论组' % row['title']
        target = self.membership_target()
        if target:
            sources.setdefault(target['id'], target['title'])
        return sources

    def member_source_ids(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='cluster_member_sources'").fetchone()
        return set(json.loads(row[0])) if row else None

    def toggle_member_source(self, actor, chat_id):
        self.require_admin(actor)
        if chat_id not in self.member_sources():
            raise ValueError('该群或频道暂不可用，请检查绑定状态。')
        with self.transaction():
            selected = self.member_source_ids() or set()
            selected.symmetric_difference_update({chat_id})
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('cluster_member_sources',?)",
                            (json.dumps(sorted(selected)),))
            self.log(actor, '设置集群成员资格来源', '%s %s' % (chat_id, '启用' if chat_id in selected else '停用'))

    def prune_member_sources(self):
        selected = self.member_source_ids()
        if selected is None:
            return
        owned = {cid for row in self.db.execute('SELECT chat_id,discussion FROM bindings') for cid in row if cid}
        target = self.membership_target()
        if target:
            owned.add(target['id'])
        if selected - owned:
            self.db.execute("UPDATE settings SET value=? WHERE key='cluster_member_sources'",
                            (json.dumps(sorted(selected & owned)),))

    def start_payload(self, action, record_id):
        payload = '%s_%s' % (action, record_id)
        return 'c%s_%s' % (self.cluster_id, payload) if self.clusters else payload

    def check_chat_ownership(self, chat_ids):
        if self.clusters:
            for chat_id in chat_ids:
                owner = self.clusters.routes.get(chat_id)
                if owner is not None and owner != self.cluster_id:
                    raise ValueError('群或频道 %s 已归属集群「%s」。' % (chat_id, self.clusters.names[owner]))

    def set_membership_target(self, actor, target):
        self.require_admin(actor)
        if target:
            self.check_chat_ownership([target['id']])
        with self.transaction():
            if target:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('membership_target',?)",
                                (json.dumps(target, ensure_ascii=False),))
                self.log(actor, '设置在群判定目标', target['id'])
            else:
                self.db.execute("DELETE FROM settings WHERE key='membership_target'")
                self.log(actor, '取消在群判定目标', '')
            self.prune_member_sources()
        if self.clusters:
            self.clusters.refresh_routes(self)

    def clear_notes(self):
        self.db.execute("UPDATE people SET note='' WHERE note<>''")
        for row in self.db.execute('SELECT id,before_value,after_value FROM history').fetchall():
            values = []
            for raw in (row['before_value'], row['after_value']):
                value = json.loads(raw)
                values.append(json.dumps(['', value[1]], ensure_ascii=False))
            if values != [row['before_value'], row['after_value']]:
                self.db.execute('UPDATE history SET before_value=?,after_value=? WHERE id=?', (*values, row['id']))

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
        return self.admin(uid) or bool(self.db.execute('SELECT 1 FROM teachers WHERE user_id=? AND deleted_at IS NULL',(uid,)).fetchone())

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

    def save(self, pid, version, user, tags, action='编辑'):
        if len(tags) > 300:
            raise ValueError('标签最多 300 字。')
        with self.transaction():
            if not self.allowed(user['id']):
                raise ValueError('仅已绑定老师和 superadmin 可以修改标签。')
            p = self.person(pid)
            changed = self.db.execute('UPDATE people SET tags=?,version=version+1 WHERE id=? AND version=?',
                                      (tags, pid, version)).rowcount
            if not changed:
                raise ValueError('资料已被别人修改，请重新打开后编辑。')
            self.db.execute('INSERT INTO history(person,actor,actor_name,action,before_value,after_value,created) VALUES(?,?,?,?,?,?,?)',
                            (pid, user['id'], display_name(user), action,
                             json.dumps(['', p['tags']], ensure_ascii=False),
                             json.dumps(['', tags], ensure_ascii=False), time.time()))


def display_name(user):
    return ' '.join(filter(None, (user.get('first_name'), user.get('last_name'))))[:160] or str(user['id'])


def cluster_directory(database):
    return database.with_name(database.name + '.clusters')


class ClusterStores:
    """The original database holds cluster 1, the registry and global administrators."""

    def __init__(self, path, primary):
        self.directory = cluster_directory(path)
        self.primary = primary
        self.stores = {1: primary}
        self.routes = {}
        with primary.transaction():
            primary.db.execute('''CREATE TABLE IF NOT EXISTS clusters(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)''')
            primary.db.execute("INSERT OR IGNORE INTO clusters(id,name) VALUES(1,'1')")
            if not primary.db.execute("SELECT 1 FROM sqlite_master WHERE name='cluster_authorizations'").fetchone():
                primary.db.execute('''CREATE TABLE cluster_authorizations(
                    cluster_id INTEGER PRIMARY KEY REFERENCES clusters(id))''')
                primary.db.execute('INSERT INTO cluster_authorizations SELECT id FROM clusters')
            primary.db.execute('''CREATE TABLE IF NOT EXISTS cluster_activation_codes(
                id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL REFERENCES clusters(id),
                code_hash TEXT UNIQUE NOT NULL, expires REAL NOT NULL,
                redeemed REAL, revoked INTEGER NOT NULL DEFAULT 0)''')
            primary.db.execute('''CREATE INDEX IF NOT EXISTS cluster_activation_pending
                ON cluster_activation_codes(cluster_id) WHERE redeemed IS NULL AND revoked=0''')
        self.names = dict(primary.db.execute('SELECT id,name FROM clusters ORDER BY id').fetchall())
        self.authorized = {row[0] for row in primary.db.execute('SELECT cluster_id FROM cluster_authorizations')}
        primary.clusters = self
        for cluster_id in self.names:
            if cluster_id != 1:
                self.stores[cluster_id] = self.open_store(cluster_id, existing=True)
        for store in self.stores.values():
            self.refresh_routes(store)

    def open_store(self, cluster_id, existing=False):
        directory = self.directory / str(cluster_id)
        path = directory / 'database.sqlite3'
        if existing:
            if not path.is_file():
                raise ValueError('缺少集群 %s 的数据库：%s' % (cluster_id, path))
        else:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        store = Store(str(path), self.primary.owner)
        store.cluster_id, store.clusters = cluster_id, self
        return store

    def refresh_routes(self, store):
        cluster_id = store.cluster_id
        ids = {value for row in store.db.execute('SELECT chat_id,discussion FROM bindings')
               for value in row if value is not None}
        target = store.membership_target()
        if target:
            ids.add(target['id'])
        store.check_chat_ownership(ids)
        for chat_id, owner in list(self.routes.items()):
            if owner == cluster_id and chat_id not in ids:
                del self.routes[chat_id]
        self.routes.update(dict.fromkeys(ids, cluster_id))

    def create(self, actor):
        self.primary.require_admin(actor)
        store = None
        try:
            with self.primary.transaction():
                cluster_id = self.primary.db.execute("INSERT INTO clusters(name) VALUES('')").lastrowid
                name = str(cluster_id)
                self.primary.db.execute('UPDATE clusters SET name=? WHERE id=?', (name, cluster_id))
                store = self.open_store(cluster_id)
                self.primary.log(actor, '新建集群', cluster_id)
        except BaseException:
            if store:
                store.db.close()
            raise
        self.stores[cluster_id], self.names[cluster_id] = store, name
        return store

    def rename(self, actor, cluster_id, name):
        self.primary.require_admin(actor)
        if not 1 <= len(name) <= 40 or not name.isprintable():
            raise ValueError('请发送 1～40 个可显示字符作为集群名称。')
        with self.primary.transaction():
            self.primary.db.execute('UPDATE clusters SET name=? WHERE id=?', (name, cluster_id))
            self.primary.log(actor, '集群改名', '%s · %s' % (cluster_id, name))
        self.names[cluster_id] = name

    def pending_activation(self, cluster_id):
        row = self.primary.db.execute('''SELECT expires FROM cluster_activation_codes
            WHERE cluster_id=? AND redeemed IS NULL AND revoked=0 AND expires>?''',
                                      (cluster_id, time.time())).fetchone()
        return row['expires'] if row else None

    def issue_activation(self, actor, cluster_id):
        self.primary.require_admin(actor)
        code = secrets.token_urlsafe(24)
        digest = hashlib.sha256(code.encode('ascii')).hexdigest()
        expires = time.time() + 86400
        with self.primary.transaction():
            if self.primary.db.execute('SELECT 1 FROM cluster_authorizations WHERE cluster_id=?', (cluster_id,)).fetchone():
                raise ValueError('集群已激活。')
            self.primary.db.execute('''UPDATE cluster_activation_codes SET revoked=1
                WHERE cluster_id=? AND redeemed IS NULL AND revoked=0''', (cluster_id,))
            self.primary.db.execute('''INSERT INTO cluster_activation_codes(cluster_id,code_hash,expires)
                VALUES(?,?,?)''', (cluster_id, digest, expires))
            self.primary.log(actor, '生成集群授权码', cluster_id)
        return code, expires

    def redeem_activation(self, actor, cluster_id, code):
        self.primary.require_admin(actor)
        if not re.fullmatch(r'[A-Za-z0-9_-]{32}', code):
            raise ValueError('请输入完整的集群授权码。')
        digest = hashlib.sha256(code.encode('ascii')).hexdigest()
        now = time.time()
        with self.primary.transaction():
            if self.primary.db.execute('SELECT 1 FROM cluster_authorizations WHERE cluster_id=?', (cluster_id,)).fetchone():
                raise ValueError('集群已激活。')
            redeemed = self.primary.db.execute('''UPDATE cluster_activation_codes SET redeemed=?
                WHERE cluster_id=? AND code_hash=? AND redeemed IS NULL AND revoked=0 AND expires>?''',
                                               (now, cluster_id, digest, now))
            if not redeemed.rowcount:
                raise ValueError('此授权码不适用于当前集群，或已过期、使用、作废。')
            self.primary.db.execute('INSERT INTO cluster_authorizations VALUES(?)', (cluster_id,))
            self.primary.log(actor, '激活集群', cluster_id)
        self.authorized.add(cluster_id)

    def revoke_activation(self, actor, cluster_id):
        self.primary.require_admin(actor)
        with self.primary.transaction():
            self.primary.db.execute('DELETE FROM cluster_authorizations WHERE cluster_id=?', (cluster_id,))
            self.primary.db.execute('UPDATE cluster_activation_codes SET revoked=1 WHERE cluster_id=? AND revoked=0',
                                    (cluster_id,))
            self.primary.log(actor, '撤销集群授权', cluster_id)
        self.authorized.discard(cluster_id)

    def close(self):
        for store in self.stores.values():
            if store is not self.primary:
                store.db.close()
