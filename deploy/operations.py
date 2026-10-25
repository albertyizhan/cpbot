"""Local maintenance; never execute .env as shell code or delete directory trees."""
import ast
import fcntl
import json
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cpbot.__main__ import load_env

# Explicit inventory: upgrades cannot overwrite config, databases or unknown files.
FILES = ('cpbot/__init__.py', 'cpbot/__main__.py', 'cpbot/api.py', 'cpbot/bot.py',
         'cpbot/identity.py', 'cpbot/store.py', 'deploy/operations.py',
         'deploy/cpbot.service', 'manage.sh', '.env.example', '.gitignore', 'README.md')


def configuration():
    env = Path(os.environ.get('ENV_FILE', '.env')).expanduser().resolve()
    load_env(env)
    db = Path(os.environ.get('DATABASE_PATH', 'data/cpbot.sqlite3')).expanduser().resolve()
    return env, db


def check():
    configuration()
    uid = os.environ.get('SUPERADMIN_ID', '')
    if not os.environ.get('BOT_TOKEN') or not uid.isdigit() or not 0 < int(uid) < 2**63:
        raise ValueError('请先创建 .env，填写 BOT_TOKEN 和有效的 SUPERADMIN_ID。')
    if sys.version_info < (3, 9):
        raise ValueError('需要 Python 3.9 或更新版本。')


def backup():
    env, db = configuration()
    directory = ROOT / 'backups'
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / ('cpbot-%s-%s.tar.gz' % (time.strftime('%Y%m%d-%H%M%S'), time.time_ns()))
    descriptor, temporary = tempfile.mkstemp(prefix='cpbot-snapshot-', suffix='.sqlite3')
    os.close(descriptor)
    snapshot = Path(temporary)
    try:
        if db.exists():
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=10) as source:
                with sqlite3.connect(snapshot) as dest:
                    source.backup(dest)
                    if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('数据库备份完整性检查失败。')
        with tarfile.open(target, 'x:gz') as archive:
            for name in FILES:
                path = ROOT / name
                if path.is_file():
                    archive.add(path, arcname='source/' + name, recursive=False)
            if env.is_file():
                archive.add(env, arcname='config.env', recursive=False)
            if db.exists():
                archive.add(snapshot, arcname='database.sqlite3', recursive=False)
            manifest = json.dumps({'database_path': str(db), 'env_path': str(env),
                                    'created': time.time()}, ensure_ascii=False).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size, info.mode = len(manifest), 0o600
            archive.addfile(info, io.BytesIO(manifest))
    finally:
        snapshot.unlink()  # One explicit temporary file, never recursive deletion.
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
    unit = unit.replace('ExecStart=/usr/bin/python3 -m cpbot', 'ExecStart=' + quoted(sys.executable) + ' -m cpbot')
    Path('/etc/systemd/system/cpbot.service').write_text(unit)


def ctl(*args, **kwargs):
    return subprocess.run(['systemctl', *args, 'cpbot.service'], **kwargs)


def upgrade(source):
    check()
    source = Path(source).expanduser().resolve()
    if source == ROOT:
        raise ValueError('新版必须放在另一个目录，例如 /opt/cpbot-new。')
    for name in FILES:
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
    archive = backup()
    ctl('stop', check=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    with open(str(db) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('另一个手动运行的实例仍占用数据库；代码未升级。') from None
        try:
            for name in FILES:
                (ROOT / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, ROOT / name)
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
        for name in FILES:
            with saved.extractfile('source/' + name) as content:
                with open(ROOT / name, 'wb') as destination:
                    shutil.copyfileobj(content, destination)
    print('已恢复升级前的代码。', flush=True)


def main():
    os.umask(0o077)
    os.chdir(ROOT)
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
            elif action == 'upgrade' and len(sys.argv) == 3:
                upgrade(sys.argv[2])
            else:
                raise ValueError('未知维护命令。')
        except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError, SyntaxError) as error:
            raise SystemExit('操作失败：%s' % error) from None


if __name__ == '__main__':
    main()
