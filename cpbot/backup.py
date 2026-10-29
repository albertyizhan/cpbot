"""Versioned data-only backups. Uploaded content is never executed as SQL."""
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .reviews import FIELDS, SCORES, MODES

MAX_BACKUP_BYTES = 15 * 1024 * 1024
TABLES = ('admins', 'people', 'teachers', 'codes', 'drafts', 'reviews', 'bindings',
          'memberships', 'history', 'posts', 'publications', 'jobs', 'audit')


def encode_backup(db, source_bot=0):
    tables = {table: [dict(row) for row in db.execute('SELECT * FROM ' + table)] for table in TABLES}
    return json.dumps({'format': 'cpbot-data', 'version': 1, 'created': time.time(),
                       'source_bot': source_bot, 'tables': tables}, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')


def validate_backup(content, bot_id=0):
    """Validate into an isolated database with application-owned schema/constraints."""
    if len(content) > MAX_BACKUP_BYTES:
        raise ValueError('备份不能超过 15 MB。')
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('备份中存在重复字段。')
            obj[key] = value
        return obj
    try:
        data = json.loads(content.decode('utf-8-sig'), object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('无效数值。')))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError('不是有效的 cpbot JSON 备份。旧 SQL 文件不能直接上传，请使用新版重新导出。') from None
    if not isinstance(data, dict) or data.get('format') != 'cpbot-data' or type(data.get('version')) is not int or data['version'] != 1:
        raise ValueError('备份格式或版本不受支持，请上传 cpbot 导出的 JSON 文件。')
    source = data.get('source_bot')
    if type(source) is not int or source < 0 or source >= 2**63:
        raise ValueError('备份的机器人身份信息无效。')
    if source and bot_id and source != bot_id:
        raise ValueError('这是其他机器人的备份，消息映射不能直接恢复到当前机器人。')
    if type(data.get('created')) not in (int, float) or not math.isfinite(data['created']) or data['created'] <= 0:
        raise ValueError('备份导出时间无效。')
    tables = data.get('tables')
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ValueError('备份数据表不完整或包含未知数据。')
    # Local import avoids a module cycle. Only our schema is ever installed.
    from .store import Store
    stage = Store(':memory:', 1)
    try:
        stage.db.execute('PRAGMA foreign_keys=OFF')
        with stage.transaction():
            for table in TABLES:
                stage.db.execute('DELETE FROM ' + table)
                info = stage.db.execute('PRAGMA table_info(' + table + ')').fetchall()
                columns = [row['name'] for row in info]
                rows = tables[table]
                if not isinstance(rows, list):
                    raise ValueError('数据表格式错误：' + table)
                for row in rows:
                    if not isinstance(row, dict) or set(row) != set(columns):
                        raise ValueError('数据字段不完整：' + table)
                    for column in info:
                        value = row[column['name']]
                        kind = column['type']
                        if value is None:
                            if column['notnull'] or column['pk']:
                                raise ValueError('必填数据为空：' + table)
                        elif (kind == 'INTEGER' and (type(value) is not int or not -(2**63) <= value < 2**63)) or (
                              kind == 'REAL' and (type(value) not in (int, float) or not math.isfinite(value))) or (
                              kind == 'TEXT' and not isinstance(value, str)):
                            raise ValueError('数据类型错误：' + table)
                    stage.db.execute('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                                     [row[c] for c in columns])
        if stage.db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('备份中存在失去关联的记录。')
        check_relations(stage.db)
    except (sqlite3.Error, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError('备份内容冲突或损坏，未修改当前数据。') from None
    finally:
        stage.db.close()
    return data


def check_relations(db):
    def require(condition, message):
        if not condition:
            raise ValueError('备份校验失败：' + message)
    teacher_ids = {r['id'] for r in db.execute('SELECT id FROM teachers')}
    reviews = {r['id']: dict(r) for r in db.execute('SELECT * FROM reviews')}
    bindings = {r['id']: dict(r) for r in db.execute('SELECT * FROM bindings')}
    require(bool(db.execute('SELECT 1 FROM admins').fetchone()), '管理员名单为空。')
    for r in db.execute('SELECT * FROM admins'):
        require(r['user_id'] > 0, '管理员 ID 无效。')
    for t in db.execute('SELECT * FROM teachers'):
        require(t['id'] > 0 and 1 <= len(t['name']) <= 80 and (t['user_id'] is None or t['user_id'] > 0), '老师资料无效。')
    for r in reviews.values():
        require(r['status'] in ('accepted','pending','rejected','withdrawn') and r['author'] > 0 and r['anonymous'] in (0,1) and r['manual'] in (0,1), '车评状态无效。')
        require((r['status'] in ('pending','rejected')) == (r['expires'] is not None), '审核到期时间不一致。')
        try:
            d, identity = json.loads(r['data']), json.loads(r['identity'])
            require(isinstance(d, dict) and all(str(d.get(f, '')).strip() for f in FIELDS), '车评字段缺失。')
            require(type(d['老师']) is int and d['老师'] == r['teacher'], '老师关联不一致。')
            require(all(type(d[k]) is int and 1 <= d[k] <= 10 for k in SCORES), '评分无效。')
            from datetime import datetime
            datetime.strptime(d['时间'], '%Y-%m-%d %H:%M')
            require(isinstance(d['过程'], str) and len(d['过程']) <= 1600 and isinstance(d['人照'], str) and len(d['人照']) <= 200 and isinstance(d['留名'], str) and len(d['留名']) <= 80, '车评文字无效。')
            require(isinstance(identity, dict) and identity.get('id') == r['author'] and ('username' not in identity or isinstance(identity['username'], str)), '作者身份无效。')
        except (ValueError, TypeError, KeyError):
            raise ValueError('备份中的车评内容无效。') from None
    for r in db.execute('SELECT * FROM drafts'):
        try:
            d = json.loads(r['data'])
            require(isinstance(d,dict) and set(d) <= set(FIELDS) | {'匿名'} and r['user_id'] > 0 and bool(r['token']), '草稿无效。')
            if '老师' in d: require(type(d['老师']) is int and d['老师'] in teacher_ids, '草稿老师不存在。')
            for key, value in d.items():
                if key in SCORES: require(type(value) is int and 1 <= value <= 10, '草稿评分无效。')
                elif key == '匿名': require(type(value) is bool, '草稿匿名状态无效。')
                elif key != '老师': require(isinstance(value,str) and len(value) <= {'过程':1600,'人照':200,'留名':80,'时间':16}[key], '草稿文字无效。')
        except (ValueError, TypeError):
            raise ValueError('备份中的草稿无效。') from None
    for c in db.execute('SELECT * FROM codes'):
        require(c['state'] in ('open','used') and 1 <= len(c['value']) <= 80, '留名状态无效。')
        require((c['state']=='open' and c['used_review'] is None) or (c['state']=='used' and c['used_review'] in reviews and reviews[c['used_review']]['teacher']==c['teacher']), '留名使用关联无效。')
    occupied = set()
    for b in bindings.values():
        require(b['mode'] in MODES and b['chat_id'] < 0 and b['active'] in (0,1), '绑定无效。')
        require((b['mode']=='thread' and b['discussion'] is not None and b['discussion'] < 0) or (b['mode']!='thread' and b['discussion'] is None), '讨论组配置无效。')
        for cid in (b['chat_id'], b['discussion']):
            if cid is not None:
                require(cid not in occupied, '目标重复占用。')
                occupied.add(cid)
    posts = {(p['binding'],p['teacher']): dict(p) for p in db.execute('SELECT * FROM posts')}
    for p in posts.values():
        require(p['teacher'] in teacher_ids and p['generation'] > 0 and bindings[p['binding']]['mode']=='thread', '主帖关联无效。')
        require((p['message'] is None or p['message'] > 0) and (p['root'] is None or p['message'] is not None and p['root'] > 0), '主帖消息位置无效。')
    for p in db.execute('SELECT * FROM publications'):
        b = bindings.get(p['binding']); r = reviews.get(p['review'])
        require(b is not None and r is not None and b['mode'] in ('thread','feed') and p['message'] > 0 and p['generation'] > 0, '发布关联无效。')
        require(p['chat_id'] == (b['discussion'] if b['mode']=='thread' else b['chat_id']), '发布目标错误。')
    for j in db.execute('SELECT * FROM jobs'):
        require(j['binding'] in bindings and j['state'] in ('pending','sending','done','blocked','uncertain','superseded') and j['generation'] > 0, '任务状态无效。')
        require(j['kind'] in ('post','comment','feed','summary','checkpost','check','health'), '任务类型无效。')
        require(j['kind']=='health' or j['teacher'] in teacher_ids, '任务老师不存在。')
        require(not j['review'] or j['review'] in reviews and reviews[j['review']]['teacher']==j['teacher'], '任务评价不存在。')


class BackupStore:
    def export(self, source_bot=0):
        target = sqlite3.connect(':memory:')
        target.row_factory = sqlite3.Row
        try:
            self.db.backup(target)
            return encode_backup(target, source_bot)
        finally:
            target.close()

    def recovery_backup(self, source_bot=0):
        location = self.db.execute('PRAGMA database_list').fetchone()['file']
        directory = Path(location).parent / 'recovery' if location else Path('/tmp/cpbot-recovery')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / ('before-import-%s.json' % time.time_ns())
        content = self.export(source_bot)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd,'wb') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        return path

    def import_backup(self, actor, content, source_bot=0):
        self.require_admin(actor)
        data = validate_backup(content, source_bot)
        recovery = self.recovery_backup(source_bot)
        # Polling offset, deployment configuration and existing schema stay local.
        with self.transaction():
            self.require_admin(actor)
            self.db.execute('PRAGMA defer_foreign_keys=ON')
            for table in reversed(TABLES):
                self.db.execute('DELETE FROM '+table)
            for table in TABLES:
                columns = [r['name'] for r in self.db.execute('PRAGMA table_info('+table+')')]
                self.db.executemany('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                                    ([row[c] for c in columns] for row in data['tables'][table]))
            for uid in {actor, *self.configured_admins}:
                self.db.execute('INSERT OR IGNORE INTO admins VALUES(?)',(uid,))
            self.db.execute("UPDATE bindings SET active=0,error='备份已恢复，请重新检查权限及消息位置后启用'")
            self.db.execute("UPDATE jobs SET state='uncertain',error='恢复前的发送结果不明，请核对' WHERE state='sending'")
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('publishing_paused','1')")
            self.db.execute("DELETE FROM settings WHERE key='last_check'")
            self.log(actor,'导入备份',str(recovery))
        return recovery
