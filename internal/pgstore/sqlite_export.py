"""Stream cpbot's offline SQLite state to its Go importer using Python's sqlite3."""

from contextlib import ExitStack, closing
import json
from pathlib import Path
import re
import sqlite3
import stat
import sys


BASE = ('settings', 'people', 'memberships', 'history')
REVIEWS = ('teachers', 'drafts', 'reviews', 'codes', 'bindings', 'posts',
           'publications', 'proof_publications', 'jobs', 'audit')
REGISTRY = ('clusters', 'cluster_authorizations', 'cluster_activation_codes', 'cluster_management_groups')
RETIRED = {'admins', 'grants', 'attempts'}


def emit(**record):
    print(json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(',', ':')))


def open_db(stack, path):
    db = stack.enter_context(closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)))
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    if [tuple(r) for r in db.execute('PRAGMA quick_check')] != [('ok',)]:
        raise ValueError('SQLite 完整性检查失败：' + str(path))
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('SQLite 外键检查失败：' + str(path))
    return db


def tables_for(db, primary):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    required = set(BASE)
    if tables.intersection(REVIEWS):
        required.update(set(REVIEWS) - {'proof_publications'})
    allowed = set(BASE + REVIEWS) | RETIRED
    if primary:
        allowed.update(REGISTRY)
    if not required <= tables or not tables <= allowed or ('clusters' not in tables and tables.intersection(REGISTRY)):
        raise ValueError('SQLite 数据表不完整或版本不支持。')
    return tables


def batches(db, table, transform):
    cursor = db.execute(f'SELECT * FROM {table}')  # table is from the built-in table list
    while rows := cursor.fetchmany(128):
        records = [record for row in rows if (record := transform(dict(row))) is not None]
        if records:
            emit(table=table, rows=records)


def check_proof(directory, name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-f0-9]{32}\.jpg', name):
        raise ValueError('证明图文件名无效。')
    try:
        file = (directory / 'proofs' / name).lstat()
    except FileNotFoundError:
        raise ValueError(f'缺少证明图：{directory / "proofs" / name}') from None
    if not stat.S_ISREG(file.st_mode) or not 0 < file.st_size <= 10 * 1024 * 1024:
        raise ValueError(f'证明图文件类型或大小无效：{directory / "proofs" / name}')


def cluster_data(db, tables, cluster_id, folder, group):
    target = db.execute("SELECT value FROM settings WHERE key='membership_target'").fetchone()
    target = json.loads(target[0]) if target else None
    if target is not None and (not isinstance(target, dict) or set(target) != {'id', 'title'}
                               or type(target['id']) is not int or not isinstance(target['title'], str)):
        raise ValueError('在群判定目标格式无效。')
    binding_chats = [cid for r in db.execute('SELECT chat_id,discussion FROM bindings')
                     for cid in r if cid is not None] if 'bindings' in tables else []
    if len(binding_chats) != len(set(binding_chats)):
        raise ValueError('集群内的频道或讨论组被多个绑定重复占用。')
    chats = set(binding_chats)
    if target is not None:
        chats.add(target['id'])
    if group is not None:
        chats.add(group['chat_id'])
    if chats:
        emit(table='chat_ownership', rows=[{'cluster_id': cluster_id, 'chat_id': cid} for cid in sorted(chats)])
    if target is not None:
        emit(table='membership_targets', rows=[dict(cluster_id=cluster_id, chat_id=target['id'], title=target['title'])])
    if group is not None:
        emit(table='cluster_management_groups', rows=[group])

    proofs = set()

    def transform(table, row):
        if table == 'settings' and row['key'] in {'membership_target', 'password', 'admins_initialized', 'notes_removed'}:
            return None
        if table == 'people':
            del row['note']
        if table == 'teachers':
            row.setdefault('username', None)
            row.setdefault('deleted_at', None)
        if table == 'history':
            for key in ('before_value', 'after_value'):
                value = json.loads(row[key])
                if value[0] != '':
                    row[key] = json.dumps(['', value[1]], ensure_ascii=False)
        if table in ('reviews', 'drafts'):
            row['data'] = json.loads(row['data'])
            name = row['data'].get('出击证明')
            if name is not None:
                if not isinstance(name, str):
                    raise ValueError('证明图文件名无效。')
                if name not in proofs:
                    check_proof(folder, name)
                    proofs.add(name)
        if table == 'reviews':
            row['identity'] = json.loads(row['identity'])
        if table == 'jobs':
            row['attempt_generation'] = None
            if row['state'] == 'sending':
                row.update(state='uncertain', error='迁移前的发送结果不明，请核对远端消息')
        return dict(row, cluster_id=cluster_id)

    for table in BASE + REVIEWS:
        if table in tables:
            batches(db, table, lambda row: transform(table, row))
    return len(proofs)


def export(source):
    with ExitStack() as stack:
        primary = open_db(stack, source)
        tables = tables_for(primary, True)
        clusters = dict(primary.execute('SELECT id,name FROM clusters')) if 'clusters' in tables else {}
        clusters.setdefault(1, '1')
        if any(type(cid) is not int or cid <= 0 for cid in clusters):
            raise ValueError('集群编号无效。')
        emit(table='clusters', rows=[{'id': cid, 'name': name} for cid, name in clusters.items()])
        if 'cluster_authorizations' in tables:
            batches(primary, 'cluster_authorizations', lambda row: row)
        else:
            emit(table='cluster_authorizations', rows=[{'cluster_id': cid} for cid in clusters])
        if 'cluster_activation_codes' in tables:
            batches(primary, 'cluster_activation_codes', lambda row: row)
        groups = ({r['cluster_id']: dict(r) for r in primary.execute('SELECT * FROM cluster_management_groups')}
                  if 'cluster_management_groups' in tables else {})
        if not set(groups) <= set(clusters):
            raise ValueError('管理群引用了不存在的集群。')
        proofs = 0
        for cid in sorted(clusters):
            folder = source.parent if cid == 1 else source.with_name(source.name + '.clusters') / str(cid)
            with ExitStack() as cluster_stack:
                db = primary if cid == 1 else open_db(cluster_stack, folder / 'database.sqlite3')
                scoped_tables = tables if cid == 1 else tables_for(db, False)
                proofs += cluster_data(db, scoped_tables, cid, folder, groups.get(cid))
        sequence = 0
        if 'clusters' in tables and primary.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            row = primary.execute("SELECT seq FROM sqlite_sequence WHERE name='clusters'").fetchone()
            if row:
                sequence = row[0]
        emit(complete=True, cluster_sequence=sequence, referenced_proofs=proofs)


if __name__ == '__main__':
    try:
        export(Path(sys.argv[1]))
    except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError, sqlite3.Error) as error:
        print(f'SQLite 导出失败：{error}', file=sys.stderr)
        sys.exit(1)
