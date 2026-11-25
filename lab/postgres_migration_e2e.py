#!/usr/bin/env python3
"""Exercise the Go importer against a temporary PostgreSQL and real cpbot SQLite data."""

from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import tarfile
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpbot.reviews import SCORES
from cpbot.store import ClusterStores, Store
from deploy.operations import FILES, go_sources
from postgres_publish_e2e import exercise
from postgres_runtime_e2e import exercise_runtime


USER_ID = 6789123456789
PROOF = '1234567890abcdef1234567890abcdef.jpg'
JPEG = bytes.fromhex('ffd8ffe000104a46494600010100000100010000ffd9')


def fixture(root):
    source = root / 'source' / 'review.sqlite3'
    source.parent.mkdir()
    primary = Store(str(source), 1)
    clusters = ClusterStores(source, primary)
    secondary = clusters.create(1)
    cluster3 = clusters.create(1)
    code, _ = clusters.issue_activation(1, secondary.cluster_id)
    clusters.register(1, clusters.activation_digest(code), {'id': -1002001, 'title': '二号管理群'})
    clusters.issue_activation(1, cluster3.cluster_id)
    clusters.rename(1, 2, '二号集群')
    secondary.set_membership_target(1, {'id': -1002001, 'title': '二号管理群'})
    secondary.toggle_member_source(1, -1002001)
    primary.db.execute('UPDATE sqlite_sequence SET seq=8 WHERE name=\'clusters\'')
    primary.db.execute("INSERT INTO settings VALUES ('offset','890001'),('password','retired'),('last_check','2026-11-25')")
    primary.db.execute('INSERT INTO grants VALUES (50,\'retired\',0)')
    primary.db.execute('INSERT INTO admins VALUES (50)')
    secondary.db.execute("INSERT INTO settings VALUES ('publishing_paused','1')")

    for store in (primary, secondary):
        cid = store.cluster_id
        teacher = store.save_teacher(1, f'老师{cid}', 42)
        assert teacher == 1
        post_chat, discussion = -1000000 - cid, -1000100 - cid
        binding = store.bind(1, 'thread', {'id': post_chat, 'title': f'频道{cid}'}, discussion)
        assert binding == 1
        user = {'id': USER_ID, 'username': 'customer', 'first_name': '客户'}
        person = store.observe_member(user, {'id': discussion}, True, 123.25, 123)
        store.save(person['id'], person['version'], {'id': 1}, f'标签{cid}')
        image = store.proof_path(PROOF)
        image.parent.mkdir(exist_ok=True)
        image.write_bytes(JPEG)
        form = {'老师': teacher, '时间': '2026-11-22', '过程': f'车评{cid}', '匿名': True, **{f: 8 for f in SCORES}, '出击证明': PROOF}
        store.save_draft(USER_ID, form, 'same-submission-token')
        assert store.submit(user, 'same-submission-token') == (1, 'accepted')
        if cid == 2:
            store.save_draft(USER_ID, form, 'unsubmitted-draft')
        store.db.execute('UPDATE posts SET message=20,root=30 WHERE binding=1 AND teacher=1')
        store.db.execute('INSERT INTO publications VALUES(1,1,1,31,?)', (discussion,))
        store.db.execute("UPDATE jobs SET state='done' WHERE kind IN ('post','comment')")
        if cid == 1:
            # Materialize the SQLite boundary at a process exit during sendPhoto.
            store.db.execute("UPDATE jobs SET state='sending' WHERE kind='proof'")
        else:
            store.db.execute('INSERT INTO proof_publications VALUES(1,1,1,32,?)', (discussion,))
            store.db.execute("UPDATE jobs SET state='done' WHERE kind='proof'")
    return source, primary, clusters


def legacy_fixture(root):
    source = root / 'legacy' / 'legacy.sqlite3'
    source.parent.mkdir()
    with closing(Store(str(source), 1).db) as db:
        db.execute('INSERT INTO teachers(id,name) VALUES(9,\'旧老师\')')
        db.execute('DROP INDEX teachers_username')
        db.execute('ALTER TABLE teachers DROP COLUMN username')
        db.execute('ALTER TABLE teachers DROP COLUMN deleted_at')
        db.execute('DROP TABLE proof_publications')
        db.execute("INSERT INTO people(id,user_id,note,tags) VALUES (17,32,'旧群备注','保留标签')")
        db.execute('INSERT INTO history VALUES (1,17,1,\'管理员\',\'修改\',?,?,123)', (
            json.dumps(['旧群备注', ''], ensure_ascii=False), json.dumps(['旧群备注', '保留标签'], ensure_ascii=False)))
        data = json.dumps({'老师': 9, '时间': '2025-03-02 04:05', '留名': 'legacy', '人照': '旧评分文本',
                           '综合': 7, '过程': '旧车评', **{f: 8 for f in SCORES if f != '人照'}}, ensure_ascii=False)
        db.execute('''INSERT INTO reviews(id,author,identity,teacher,data,anonymous,status,created,submission)
                      VALUES(15,32,'{"id":32}',9,?,1,'accepted',123,'old')''', (data,))
    return source


def exercise_server(base, source, executable, pg_bin, environment, command, sql, dsn):
    server_root = base / 'server-copy'
    for name in (*FILES, *go_sources(ROOT)):
        path = server_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, path)
    executable_path = server_root / '.runtime' / 'cpbot-go'
    executable_path.parent.mkdir()
    shutil.copy2(executable, executable_path)
    environment = {
        'ENV_FILE': str(base / 'no-production-env'), 'CPBOT_ENGINE': 'go', 'DATABASE_URL': dsn,
        'DATABASE_PATH': str(source), 'PATH': str(pg_bin) + os.pathsep + environment['PATH'],
        'BOT_TOKEN': '1:isolated-fixture', 'SUPERADMIN_IDS': '1', 'SUPERADMIN_ID': '',
    }
    maintenance = [sys.executable, server_root / 'deploy' / 'operations.py']
    with Path(str(source) + '.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = command([*maintenance, 'backup'], ok=False, env_overrides=environment)
        assert '请先停止 cpbot' in refused.stderr
        assert not (server_root / 'backups').exists()

    result = command([*maintenance, 'backup'], env_overrides=environment)
    archive_path = Path(result.stdout.strip().split('备份完成：', 1)[1])
    with tarfile.open(archive_path) as archive:
        assert archive.extractfile('proofs/' + PROOF).read() == JPEG
        assert archive.extractfile('clusters/2/proofs/' + PROOF).read() == JPEG
        manifest = json.load(archive.extractfile('manifest.json'))
        assert manifest['format'] == 'cpbot-postgres'
        with archive.extractfile('cpbot.pgdump') as dump, (base / 'cpbot.pgdump').open('wb') as target:
            shutil.copyfileobj(dump, target)
    command([pg_bin / 'pg_restore', '--single-transaction', '--no-owner', '--no-acl', '-d', 'restore', base / 'cpbot.pgdump'])
    assert sql('restore', 'SELECT count(*) FROM cpbot.reviews') == '3'
    assert sql('restore', 'SELECT count(*) FROM cpbot.cluster_management_groups') == '2'
    assert sql('restore', "SELECT value FROM cpbot.settings WHERE (cluster_id,key)=(1,'offset')") == (
        sql('runtime', "SELECT value FROM cpbot.settings WHERE (cluster_id,key)=(1,'offset')"))

    # systemd is unavailable on macOS. Model its start/stop boundary while
    # building, archiving, replacing and checking the actual binaries.
    candidate = base / 'upgrade-candidate'
    shutil.copytree(server_root, candidate, ignore=shutil.ignore_patterns('.runtime', 'backups', '.maintenance.lock'))
    candidate_main = candidate / 'cmd' / 'cpbot' / 'main.go'
    original_main = candidate_main.read_text()
    start = '\tcase "run", "check":\n'
    assert original_main.count(start) == 1
    candidate_main.write_text(original_main.replace(start, start +
        '\t\tif args[0] == "run" { return errors.New("验收：新版启动失败") }\n'))
    fake_service = base / 'service-bin'
    fake_service.mkdir()
    active, trace = base / 'service-active', base / 'service.log'
    active.touch()
    systemctl = fake_service / 'systemctl'
    systemctl.write_text(f'''#!/bin/sh
set -eu
active={shlex.quote(str(active))}
trace={shlex.quote(str(trace))}
binary={shlex.quote(str(executable_path))}
original={shlex.quote(str(executable))}
printf '%s\\n' "$1" >> "$trace"
case "$1" in
    is-active) test -f "$active" ;;
    stop) if test -f "$active"; then unlink "$active"; fi ;;
    start)
        if cmp -s "$binary" "$original"; then
            "$binary" check >> "$trace" 2>&1
            touch "$active"
        else
            "$binary" run >> "$trace" 2>&1
        fi ;;
    *) exit 2 ;;
esac
''')
    systemctl.chmod(0o700)
    failed = command([*maintenance, 'upgrade', candidate], ok=False, env_overrides={
        **environment, 'PATH': str(fake_service) + os.pathsep + environment['PATH'],
    })
    assert '新版进程启动失败，已恢复旧代码' in failed.stderr
    assert active.exists() and trace.read_text().splitlines() == [
        'is-active', 'stop', 'start', '验收：新版启动失败', 'stop', 'start', 'cpbot PostgreSQL schema 1 可用',
    ]
    assert (server_root / 'cmd' / 'cpbot' / 'main.go').read_text() == original_main
    assert sql('runtime', 'SELECT count(*) FROM cpbot.reviews') == '3'
    return ['postgres_backup_refuses_run_lock', 'postgres_server_backup_restore', 'failed_upgrade_restores_binary']


def main():
    base = Path(tempfile.mkdtemp(prefix='cpbot-pg-e2e-', dir='/tmp')).resolve()
    print(f'Artifacts: {base}', flush=True)
    go = os.environ.get('CPBOT_GO', 'go')
    pg_bin = Path(os.environ.get('CPBOT_PG_BIN', ''))
    db_root, socket, executable = base / 'pgdata', base / 'sock', base / 'cpbot'
    socket.mkdir(mode=0o700)
    logfile = base / 'commands.log'
    environment = {**os.environ, 'LC_ALL': 'C', 'PGHOST': str(socket), 'PGPORT': '5432', 'PGUSER': 'cpbot_e2e'}

    def command(argv, *, ok=True, env_overrides=None, input=None):
        argv = [str(a) for a in argv]
        result = subprocess.run(argv, cwd=ROOT, env={**environment, **(env_overrides or {})}, input=input, capture_output=True, text=True, encoding='utf-8', timeout=120)
        with logfile.open('a', encoding='utf-8') as output:
            output.write(f'$ {subprocess.list2cmdline(argv)}\nexit={result.returncode}\n{result.stdout}{result.stderr}\n')
            if input is not None:
                output.write(f'stdin={input}\n')
        if (result.returncode == 0) != ok:
            raise RuntimeError(f'意外的返回值，查看 {logfile}')
        return result

    def sql(db, query, *, ok=True):
        return command([pg_bin / 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1', '-d', db, '-c', query], ok=ok).stdout.strip()

    def dsn(db):
        return f'host={socket} port=5432 user={environment["PGUSER"]} dbname={db} sslmode=disable'

    def migrate(db, source, *, check=False, ok=True):
        args = [executable, 'migrate-sqlite', '--source', source] + (['--check'] if check else [])
        result = command(args, ok=ok, env_overrides={'DATABASE_URL': dsn(db)})
        return json.loads(result.stdout) if ok else result.stderr

    command([go, 'build', '-o', executable, './cmd/cpbot'])
    command([go, 'build', '-o', base / 'publish-driver', './lab/publish-driver'])
    command([pg_bin / 'initdb', '-D', db_root, '-U', 'cpbot_e2e', '-A', 'trust', '-E', 'UTF8', '--no-locale'])
    command([pg_bin / 'pg_ctl', '-D', db_root, '-l', base / 'postgres.log', '-o', f"-h '' -p 5432 -k {socket}", '-w', 'start'])
    try:
        for db in ('migration', 'conflict', 'legacy', 'publish', 'runtime', 'restore'):
            command([pg_bin / 'createdb', db])
        source, primary, clusters = fixture(base)
        try:
            wal = Path(str(source) + '-wal')
            assert wal.stat().st_size > 0
            proof = clusters.stores[2].proof_path(PROOF)
            lockpath = Path(str(source) + '.lock')
            with lockpath.open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                assert '无法取得 SQLite 运行锁' in migrate('migration', source, ok=False)

            proof.rename(proof.with_suffix('.missing'))
            try:
                assert '缺少证明图' in migrate('migration', source, ok=False)
                assert sql('migration', "SELECT to_regnamespace('cpbot') IS NULL") == 't'
            finally:
                proof.with_suffix('.missing').rename(proof)

            dry_run = migrate('migration', source, check=True)
            assert not dry_run['applied'] and dry_run['referenced_proofs'] == 2
            assert sql('migration', "SELECT to_regnamespace('cpbot') IS NULL") == 't'
            report = migrate('migration', source)
            assert report['applied'] and report['rows'] == dry_run['rows']
            assert '目标已存在 cpbot schema' in migrate('migration', source, ok=False)
            assert sql('migration', 'SELECT version FROM cpbot.schema_version') == '1'
            assert sql('migration', 'SELECT id,name FROM cpbot.clusters ORDER BY id') == '1|1\n2|二号集群\n3|3'
            assert sql('migration', 'SELECT cluster_id FROM cpbot.cluster_authorizations ORDER BY cluster_id') == '1\n2'
            assert sql('migration', 'SELECT count(*) FROM cpbot.cluster_activation_codes WHERE redeemed IS NULL') == '1'
            assert sql('migration', 'SELECT cluster_id,chat_id FROM cpbot.cluster_management_groups') == '2|-1002001'
            assert sql('migration', 'SELECT cluster_id,chat_id FROM cpbot.membership_targets') == '2|-1002001'
            assert sql('migration', "SELECT cluster_id,key,value FROM cpbot.settings WHERE key IN ('offset','publishing_paused','last_check','cluster_member_sources') ORDER BY 1,2") == (
                '1|last_check|2026-11-25\n1|offset|890001\n2|cluster_member_sources|[-1002001]\n2|publishing_paused|1')
            assert sql('migration', "SELECT count(*) FROM cpbot.settings WHERE key='password'") == '0'
            assert sql('migration', "SELECT cluster_id,id,author,data->>'过程' FROM cpbot.reviews ORDER BY cluster_id") == (
                f'1|1|{USER_ID}|车评1\n2|1|{USER_ID}|车评2')
            assert sql('migration', 'SELECT cluster_id,tags,version FROM cpbot.people ORDER BY cluster_id') == '1|标签1|1\n2|标签2|1'
            assert sql('migration', "SELECT count(*) FROM cpbot.memberships WHERE joined=123 AND observed=123.25") == '2'
            assert sql('migration', "SELECT cluster_id,token,data->>'出击证明' FROM cpbot.drafts") == f'2|unsubmitted-draft|{PROOF}'
            assert sql('migration', 'SELECT cluster_id,message,root FROM cpbot.posts ORDER BY cluster_id') == '1|20|30\n2|20|30'
            assert sql('migration', 'SELECT cluster_id,message FROM cpbot.publications ORDER BY cluster_id') == '1|31\n2|31'
            assert sql('migration', 'SELECT cluster_id,message FROM cpbot.proof_publications') == '2|32'
            assert sql('migration', "SELECT cluster_id,state FROM cpbot.jobs WHERE kind='proof' ORDER BY cluster_id") == '1|uncertain\n2|done'
            assert sql('migration', "SELECT nextval('cpbot.clusters_id_seq'), nextval('cpbot.reviews_id_seq')") == '9|2'
            assert proof.read_bytes() == JPEG
            for table, count in report['rows'].items():
                assert sql('migration', f'SELECT count(*) FROM cpbot.{table}') == str(count), table
            assert primary.db.execute("SELECT state FROM jobs WHERE kind='proof'").fetchone()[0] == 'sending'

            migrate('publish', source)
            publish_checks = exercise(base, source, command, sql, dsn('publish'))

            migrate('runtime', source)
            runtime_checks = exercise_runtime(base, source, sql, dsn('runtime'), JPEG)

            server_checks = exercise_server(base, source, executable, pg_bin, environment, command, sql, dsn('runtime'))

            secondary = clusters.stores[2]
            # Inject a cross-cluster route that the live application would reject.
            secondary.db.execute("UPDATE bindings SET discussion=-1000101 WHERE id=1")
            assert 'SQLSTATE 23505' in migrate('conflict', source, ok=False)
            assert sql('conflict', "SELECT to_regnamespace('cpbot') IS NULL") == 't'
        finally:
            clusters.close()
            primary.db.close()

        legacy = migrate('legacy', legacy_fixture(base))
        assert legacy['applied'] and legacy['referenced_proofs'] == 0
        assert sql('legacy', 'SELECT cluster_id FROM cpbot.cluster_authorizations') == '1'
        assert sql('legacy', "SELECT cluster_id,id,data->>'时间',data->>'综合' FROM cpbot.reviews") == '1|15|2025-03-02 04:05|7'
        assert sql('legacy', 'SELECT tags FROM cpbot.people') == '保留标签'
        assert sql('legacy', 'SELECT after_value FROM cpbot.history') == '["", "保留标签"]'
        evidence = {'result': 'passed', 'publish_checks': publish_checks, 'runtime_checks': runtime_checks,
                    'server_checks': server_checks, 'current': report, 'dry_run': dry_run, 'legacy': legacy,
                    'checks': ['wal', 'single_instance_lock', 'missing_proof_rollback', 'dry_run_rollback',
                               'cluster_routes', 'cross_cluster_conflict_rollback', 'activation_state',
                               'overlapping_review_ids', 'offset_and_schedule', 'proof_and_message_mapping',
                               'sending_to_uncertain', 'retired_authorization_removed', 'legacy_date_and_score',
                               'source_preserved', 'duplicate_target_rejected', 'sequence_floor']}
        (base / 'result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False), flush=True)
    finally:
        command([pg_bin / 'pg_ctl', '-D', db_root, '-m', 'fast', '-w', 'stop'])


if __name__ == '__main__':
    main()
