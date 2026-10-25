import fcntl
import logging
import os
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


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    load_env(Path(os.environ.get('ENV_FILE', '.env')))
    token, owner = os.environ.get('BOT_TOKEN', ''), os.environ.get('SUPERADMIN_ID', '')
    if not token or not owner.isdigit() or not 0 < int(owner) < 2**63:
        raise SystemExit('请在 .env 设置 BOT_TOKEN 和有效的 SUPERADMIN_ID。')
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
    store, api = Store(str(path), int(owner)), API(token)
    os.chmod(path, 0o600)
    try:
        me = api.call('getMe')
        bot = Bot(store, api, me['username'].lower())
        api.call('setMyCommands', commands=[{'command': 'start', 'description': '打开车评后台'},
                                           {'command': 'cancel', 'description': '取消当前操作'}])
        saved = store.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
        offset = int(saved[0]) if saved else 0
        logging.info('cpbot started as @%s', me['username'])
        while not stop.is_set():
            try:
                updates = api.call('getUpdates', offset=offset, timeout=25, limit=20,
                                   allowed_updates=['message', 'edited_message', 'callback_query', 'chat_member', 'my_chat_member'])
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
