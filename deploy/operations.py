"""Local maintenance; never execute .env as shell code or delete directory trees."""
import ast
import fcntl
import json
import io
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cpbot.__main__ import load_env, admin_ids
from cpbot.store import cluster_directory

# Explicit inventory: upgrades cannot overwrite config, databases or unknown files.
FILES = ('cpbot/__init__.py', 'cpbot/__main__.py', 'cpbot/api.py', 'cpbot/bot.py',
         'cpbot/identity.py', 'cpbot/comment_buttons.py', 'cpbot/store.py', 'cpbot/reviews.py', 'cpbot/publishing.py', 'cpbot/backup.py', 'deploy/operations.py',
         'deploy/cpbot.service', 'manage.sh', '.env.example', '.gitignore', 'README.md')


def configuration():
    env = Path(os.environ.get('ENV_FILE', '.env')).expanduser().resolve()
    load_env(env)
    db = Path(os.environ.get('DATABASE_PATH', 'data/cpbot.sqlite3')).expanduser().absolute()
    return env, db


def engine():
    value = os.environ.get('CPBOT_ENGINE', 'python')
    if value not in ('python', 'go'):
        raise ValueError('CPBOT_ENGINE 必须是 python 或 go。')
    return value


def go_sources(root):
    paths = {name for name in ('go.mod', 'go.sum', 'deploy/build-go.sh', 'docs/GO_MIGRATION.md')
             if (root / name).is_file()}
    for directory in ('cmd', 'internal'):
        paths.update(path.relative_to(root).as_posix() for path in (root / directory).rglob('*')
                     if path.is_file() and path.suffix in ('.go', '.sql', '.py'))
    return tuple(sorted(paths))


def go_binary(root=ROOT):
    return root / '.runtime' / 'cpbot-go'


def go_command(action, root=ROOT):
    subprocess.run([str(go_binary(root)), action], cwd=root, check=True)


def build_go(root=ROOT):
    subprocess.run(['bash', str(root / 'deploy/build-go.sh')], cwd=root, check=True)


def check():
    configuration()
    if not os.environ.get('BOT_TOKEN'):
        raise ValueError('请先创建 .env，填写 BOT_TOKEN。')
    admin_ids(os.environ)
    if engine() == 'go':
        go_command('check')
    if sys.version_info < (3, 9):
        raise ValueError('需要 Python 3.9 或更新版本。')


def backup():
    env, db = configuration()
    if engine() == 'go':
        return backup_postgres(env, db)
    directory = ROOT / 'backups'
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / ('cpbot-%s-%s.tar.gz' % (time.strftime('%Y%m%d-%H%M%S'), time.time_ns()))
    descriptor, temporary = tempfile.mkstemp(prefix='cpbot-snapshot-', suffix='.sqlite3')
    os.close(descriptor)
    snapshot = Path(temporary)
    def copy_database(source_path):
        with sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True, timeout=10) as source:
            with sqlite3.connect(snapshot) as dest:
                source.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库备份完整性检查失败：%s' % source_path)
                if dest.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='clusters'").fetchone():
                    return [row[0] for row in dest.execute('SELECT id FROM clusters WHERE id>1 ORDER BY id')]
        return []

    def archive_proofs(archive, parent, prefix):
        proofs = parent / 'proofs'
        if proofs.is_dir():
            for image in proofs.iterdir():
                if image.is_file() and image.suffix == '.jpg':
                    archive.add(image, arcname=prefix + 'proofs/' + image.name, recursive=False)

    try:
        cluster_ids = copy_database(db) if db.exists() else []
        with tarfile.open(target, 'x:gz') as archive:
            for name in (*FILES, *go_sources(ROOT)):
                path = ROOT / name
                if path.is_file():
                    archive.add(path, arcname='source/' + name, recursive=False)
            if env.is_file():
                archive.add(env, arcname='config.env', recursive=False)
            if db.exists():
                archive.add(snapshot, arcname='database.sqlite3', recursive=False)
            archive_proofs(archive, db.parent, '')
            for cluster_id in cluster_ids:
                folder = cluster_directory(db) / str(cluster_id)
                copy_database(folder / 'database.sqlite3')
                prefix = 'clusters/%s/' % cluster_id
                archive.add(snapshot, arcname=prefix + 'database.sqlite3', recursive=False)
                archive_proofs(archive, folder, prefix)
            manifest = json.dumps({'database_path': str(db), 'env_path': str(env),
                                    'created': time.time()}, ensure_ascii=False).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size, info.mode = len(manifest), 0o600
            archive.addfile(info, io.BytesIO(manifest))
    finally:
        snapshot.unlink()  # One explicit temporary file, never recursive deletion.
    print('备份完成：%s' % target, flush=True)
    return target


def pg_dump_connection(dsn):
    # libpq expands a DSN in --dbname, but not one obtained from PGDATABASE.
    # Move any password to the child environment before constructing argv.
    child = dict(os.environ)
    if dsn.startswith(('postgresql://', 'postgres://')):
        parts = urlsplit(dsn)
        authority = parts.netloc
        if '@' in authority:
            credentials, host = authority.rsplit('@', 1)
            username, separator, password = credentials.partition(':')
            if separator:
                child['PGPASSWORD'] = unquote(password)
                authority = username + '@' + host
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key == 'password':
                child['PGPASSWORD'] = value
            else:
                query.append((key, value))
        return urlunsplit((parts.scheme, authority, parts.path, urlencode(query, quote_via=quote), '')), child
    if '=' not in dsn:
        return dsn, child
    parameter = re.compile(r"\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*('(?:\\.|[^'\\])*'|(?:\\.|[^\s\\'])*)(?=\s|$)")
    position, options = 0, []
    while position < len(dsn):
        match = parameter.match(dsn, position)
        if not match:
            raise ValueError('DATABASE_URL 的 PostgreSQL 连接参数格式无效。')
        key, value = match.groups()
        position = match.end()
        if key == 'password':
            if value.startswith("'"):
                value = value[1:-1]
            child['PGPASSWORD'] = re.sub(r'\\(.)', r'\1', value)
        else:
            options.append(key + '=' + value)
    return ' '.join(options), child


def backup_postgres(env, db):
    dsn = os.environ.get('DATABASE_URL')
    if not dsn or not dsn.strip():
        raise ValueError('请先设置 DATABASE_URL。')
    pg_db, pg_env = pg_dump_connection(dsn.strip())
    db.parent.mkdir(parents=True, exist_ok=True)
    with open(str(db) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('请先停止 cpbot，再备份 PostgreSQL 和证明图。') from None
        directory = ROOT / 'backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns())
        target = directory / ('cpbot-postgres-' + stamp + '.tar.gz')
        partial = target.with_suffix('.partial')
        with tempfile.TemporaryFile() as dump:
            proc = subprocess.run(['pg_dump', '--format=custom', '--schema=cpbot', '--no-owner', '--no-acl',
                                   '--no-password', '--dbname', pg_db],
                                  env=pg_env, stdout=dump, stderr=subprocess.PIPE)
            if proc.returncode:
                raise ValueError('pg_dump 失败，请检查客户端版本、数据库连接和备份权限。')
            # The custom-format writer can seek back to update the table of contents.
            dump_size = os.fstat(dump.fileno()).st_size
            dump.seek(0)
            try:
                with tarfile.open(partial, 'x:gz') as archive:
                    info = tarfile.TarInfo('cpbot.pgdump')
                    info.size, info.mode = dump_size, 0o600
                    archive.addfile(info, dump)
                    if env.is_file():
                        archive.add(env, arcname='config.env', recursive=False)
                    for name in (*FILES, *go_sources(ROOT)):
                        path = ROOT / name
                        if path.is_file():
                            archive.add(path, arcname='source/' + name, recursive=False)
                    if go_binary().is_file():
                        archive.add(go_binary(), arcname='runtime/cpbot-go', recursive=False)
                    folders = [(db.parent, 'proofs/')]
                    clusters = cluster_directory(db)
                    if clusters.is_dir():
                        folders.extend((item, f'clusters/{item.name}/proofs/') for item in clusters.iterdir()
                                       if item.is_dir() and item.name.isdecimal())
                    for parent, prefix in folders:
                        proofs = parent / 'proofs'
                        if proofs.is_dir():
                            for image in proofs.glob('*.jpg'):
                                if image.is_file() and not image.is_symlink():
                                    archive.add(image, arcname=prefix + image.name, recursive=False)
                    manifest = json.dumps({'format': 'cpbot-postgres', 'schema': 1,
                                           'database_path': str(db), 'created': time.time()}, ensure_ascii=False).encode()
                    info = tarfile.TarInfo('manifest.json')
                    info.size, info.mode = len(manifest), 0o600
                    archive.addfile(info, io.BytesIO(manifest))
                partial.rename(target)
            finally:
                partial.unlink(missing_ok=True)
    print('备份完成：%s' % target, flush=True)
    return target


def install():
    # Environment is loaded by Python exactly as in the bot, including custom paths.
    env, _ = configuration()
    def quoted(value):
        value = str(value)
        if '\n' in value or '\r' in value:
            raise ValueError('服务路径不能包含换行。')
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    unit = (ROOT / 'deploy/cpbot.service').read_text()
    # WorkingDirectory is a single path, not a shell-style argument list.
    # Outer quotes become literal path characters in this directive.
    working_directory = str(ROOT)
    if '\n' in working_directory or '\r' in working_directory:
        raise ValueError('服务路径不能包含换行。')
    working_directory = working_directory.replace('\\', '\\x5c').replace('%', '%%')
    unit = unit.replace('WorkingDirectory=/opt/cpbot', 'WorkingDirectory=' + working_directory)
    unit = unit.replace('Environment=PYTHONUNBUFFERED=1',
                        'Environment=PYTHONUNBUFFERED=1\nEnvironment=' + quoted('ENV_FILE=' + str(env)))
    entry = quoted(go_binary()) + ' run' if engine() == 'go' else quoted(sys.executable) + ' -m cpbot'
    unit = unit.replace('ExecStart=/usr/bin/python3 -m cpbot', 'ExecStart=' + entry)
    Path('/etc/systemd/system/cpbot.service').write_text(unit)


def ctl(*args, **kwargs):
    return subprocess.run(['systemctl', *args, 'cpbot.service'], **kwargs)


def update():
    check()
    def git(*args, **kwargs):
        return subprocess.run(['git', '-C', str(ROOT), *args], **kwargs)
    git('rev-parse', '--show-toplevel', check=True, stdout=subprocess.DEVNULL)
    if git('diff', '--quiet', 'HEAD', '--').returncode:
        raise ValueError('有尚未提交的代码修改，请先处理；未停止服务或覆盖文件。')
    git('rev-parse', '--verify', '@{upstream}', check=True, stdout=subprocess.DEVNULL)
    was_active = ctl('is-active', '--quiet').returncode == 0
    if engine() == 'go':
        ctl('stop', check=True)
        try:
            archive = backup()
        except (OSError, ValueError, subprocess.CalledProcessError):
            if was_active:
                ctl('start', check=True)
            raise
    else:
        archive = backup()
        ctl('stop', check=True)
    try:
        git('pull', '--ff-only', check=True)
    except (OSError, subprocess.CalledProcessError):
        if was_active:
            ctl('start', check=True)
        raise ValueError('Git 拉取失败，已恢复原服务运行状态。请处理上方 Git 报错后重试；备份：%s' % archive) from None
    try:
        subprocess.run(['bash', '-n', str(ROOT / 'manage.sh')], check=True)
        if engine() == 'go':
            build_go()
            go_command('check')
        for path in (ROOT / 'cpbot').glob('*.py'):
            ast.parse(path.read_text(), filename=str(path))
        subprocess.run([sys.executable, '-c', 'import cpbot.bot, cpbot.store, cpbot.__main__'],
                       cwd=ROOT, check=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        if was_active:
            ctl('start', check=True)
            time.sleep(5)
            ctl('is-active', '--quiet', check=True)
    except (OSError, SyntaxError, subprocess.CalledProcessError):
        ctl('stop', check=True)
        raise ValueError('代码已拉取，但校验或启动失败，服务保持停止。请查看日志并按备份恢复；备份：%s' % archive) from None
    print('Git 更新完成。' + ('原服务已重新启动，请查看日志确认 Telegram 连接。' if was_active else '原服务未运行，保持停止。'))


def upgrade(source):
    check()
    source = Path(source).expanduser().resolve()
    if source == ROOT:
        raise ValueError('新版必须放在另一个目录，例如 /opt/cpbot-new。')
    inventory = (*FILES, *go_sources(source))
    for name in inventory:
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('新版缺少普通文件：' + name)
        if name.endswith('.py'):
            ast.parse(path.read_text(), filename=name)
    subprocess.run(['bash', '-n', str(source / 'manage.sh')], check=True)
    # Validate the candidate's real entry points before stopping the current bot.
    subprocess.run([sys.executable, '-c', 'import cpbot.bot, cpbot.store, cpbot.__main__'], cwd=source, check=True,
                   env={**os.environ, 'PYTHONPATH': str(source), 'PYTHONDONTWRITEBYTECODE': '1'})
    _, db = configuration()
    was_active = ctl('is-active', '--quiet').returncode == 0
    if engine() == 'go':
        build_go(source)
        go_command('check', source)
        ctl('stop', check=True)
        try:
            archive = backup()
        except (OSError, ValueError, subprocess.CalledProcessError):
            if was_active:
                ctl('start', check=True)
            raise
    else:
        archive = backup()
        ctl('stop', check=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    with open(str(db) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('另一个手动运行的实例仍占用数据库；代码未升级。') from None
        try:
            for name in inventory:
                (ROOT / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, ROOT / name)
            if engine() == 'go':
                shutil.copy2(go_binary(source), go_binary())
        except BaseException:
            restore_code(archive)
            if was_active:
                ctl('start', check=True)
            raise
    # Keep the installed service definition and its execution identity unchanged.
    # Updating the service itself is an explicit `install` operation.
    try:
        if was_active:
            ctl('start', check=True)
            time.sleep(5)
            ctl('is-active', '--quiet', check=True)
    except BaseException:
        ctl('stop', check=True)
        restore_code(archive)
        if was_active:
            ctl('start', check=True)
        raise ValueError('新版进程启动失败，已恢复旧代码。请查看日志；升级前备份位于 %s' % archive) from None
    print('升级完成；.env 和数据库保留。' + ('服务已启动，请查看日志确认 Telegram 连接。' if was_active else '原服务未运行，升级后保持停止。'))


def restore_code(archive):
    # Only known source paths are read; never extract arbitrary tar paths.
    with tarfile.open(archive) as saved:
        for name in (*FILES, *go_sources(ROOT)):
            if 'source/' + name not in saved.getnames():
                continue
            with saved.extractfile('source/' + name) as content:
                with open(ROOT / name, 'wb') as destination:
                    shutil.copyfileobj(content, destination)
        if engine() == 'go':
            with saved.extractfile('runtime/cpbot-go') as content:
                with open(go_binary(), 'wb') as destination:
                    shutil.copyfileobj(content, destination)
            go_binary().chmod(0o700)
    print('已恢复升级前的代码。', flush=True)


def main():
    os.umask(0o077)
    os.chdir(ROOT)
    if len(sys.argv) == 2 and sys.argv[1] == 'run':
        try:
            configuration()
            if engine() == 'go':
                os.execv(str(go_binary()), [str(go_binary()), 'run'])
            os.execv(sys.executable, [sys.executable, '-m', 'cpbot'])
        except (ValueError, OSError) as error:
            raise SystemExit('操作失败：%s' % error) from None
    # Prevent overlapping maintenance operations without blocking normal bot use.
    with open(ROOT / '.maintenance.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('另一个维护操作正在执行，请稍后再试。') from None
        try:
            action = sys.argv[1]
            if action == 'check':
                check()
            elif action == 'install':
                install()
            elif action == 'backup':
                backup()
            elif action == 'update':
                update()
            elif action == 'upgrade' and len(sys.argv) == 3:
                upgrade(sys.argv[2])
            else:
                raise ValueError('未知维护命令。')
        except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError, SyntaxError) as error:
            raise SystemExit('操作失败：%s' % error) from None


if __name__ == '__main__':
    main()
