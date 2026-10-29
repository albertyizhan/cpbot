import fcntl
import logging
import os
import re
from pathlib import Path
import signal
import threading

from .api import API, APIError
from .bot import Bot
from .store import Store


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def admin_ids(env):
    """Plural startup additions plus backward-compatible singular configuration."""
    values = []
    for key in ('SUPERADMIN_ID', 'SUPERADMIN_IDS'):
        values.extend(re.split(r'[,，\s]+', env.get(key, '').strip()))
    values = [value for value in values if value]
    if not values or any(not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63 for value in values):
        raise ValueError('请设置 SUPERADMIN_IDS（多个数字 ID 用逗号或空格分隔），旧 SUPERADMIN_ID 也可使用。')
    return list(dict.fromkeys(int(value) for value in values))


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    load_env(Path(os.environ.get('ENV_FILE', '.env')))
    token = os.environ.get('BOT_TOKEN', '')
    if not token:
        raise SystemExit('请在 .env 设置 BOT_TOKEN。')
    try:
        configured_admins = admin_ids(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    path = Path(os.environ.get('DATABASE_PATH', 'data/cpbot.sqlite3')).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(path) + '.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('此数据库已有运行中的机器人，请勿重复启动。') from None
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    store, api = Store(str(path), configured_admins[0], configured_admins), API(token)
    os.chmod(path, 0o600)
    try:
        me = api.call('getMe')
        bot = Bot(store, api, me['username'].lower(), me['id'])
        # Validate saved bindings again on startup; do not trust stale administrator rights.
        for binding in store.db.execute('SELECT * FROM bindings WHERE active=1').fetchall():
            try:
                _, discussion = bot.publisher.validate(binding['mode'], binding['chat_id'])
                if discussion != binding['discussion']:
                    raise ValueError('讨论组关联已变化，请重新绑定。')
            except (ValueError, APIError) as error:
                if isinstance(error, APIError) and (error.code == 0 or error.code >= 500 or error.code == 429):
                    raise SystemExit('启动时无法确认频道权限，请稍后重启；未修改绑定。') from None
                store.db.execute('UPDATE bindings SET active=0,error=? WHERE id=?',
                                 (str(error), binding['id']))
        api.call('setMyCommands', commands=[{'command': 'start', 'description': '打开主菜单'},
                                           {'command': 'cancel', 'description': '取消当前操作'}])
        saved = store.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        offset = int(saved[0]) if saved else 0
        logging.info('cpbot started as @%s', me['username'])
        while not stop.is_set():
            try:
                updates = api.call('getUpdates', offset=offset, timeout=1, limit=20,
                                   allowed_updates=['message', 'edited_message', 'callback_query', 'chat_member', 'my_chat_member'])
                bot.publisher.tick()
                for update in updates:
                    if stop.is_set():
                        break
                    try:
                        bot.handle(update)
                    except APIError as error:
                        logging.warning('Update %s: API code %s; response not replayed', update['update_id'], error.code)
                        if error.retry_after:
                            stop.wait(error.retry_after)
                    except ValueError as error:
                        logging.warning('Update %s rejected: %s', update['update_id'], error)
                    offset = update['update_id'] + 1
                    store.db.execute("INSERT OR REPLACE INTO settings VALUES('offset',?)", (str(offset),))
            except APIError as error:
                if error.code in (401, 409):
                    raise SystemExit('Token 无效或存在另一个轮询 / Webhook 实例，请检查配置。') from None
                logging.warning('Polling failed: API code %s', error.code)
                stop.wait(max(3, error.retry_after))
    finally:
        store.db.close()
        lock.close()


if __name__ == '__main__':
    main()
