"""Use an isolated PostgreSQL database and the real Mini App HTTP service."""

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zlib

ROOT = Path(__file__).resolve().parents[1]
TOKEN = str(99) + chr(58) + "cpbot-mini-app-test"
PROOF_NAME = '1' * 32 + '.jpg'
MISSING_NAME = '2' * 32 + '.jpg'

FIXTURE = """
INSERT INTO cpbot.clusters(id,name) VALUES (1,'海湾评组'),(2,'私密评组');
INSERT INTO cpbot.cluster_authorizations VALUES (1);
INSERT INTO cpbot.chat_ownership VALUES(-100123,1),(-100124,1),(-100234,2);
INSERT INTO cpbot.bindings(id,cluster_id,mode,chat_id,title) VALUES
    (1,1,'feed',-100123,'车评发布'),(2,2,'feed',-100234,'私密发布'),(3,1,'feed',-100124,'转载');
INSERT INTO cpbot.teachers(id,cluster_id,name,username,deleted_at) VALUES
    (1,1,'林老师','lin_review',NULL),(2,1,'陈老师','chen_review',NULL),
    (1,2,'林老师','private_lin',NULL),(3,1,'离开老师','gone',5);
INSERT INTO cpbot.reviews(id,cluster_id,teacher,author,identity,data,anonymous,status,created,submission) VALUES
    (1,1,1,10,'{}','{"时间":"2026-11-24 23:59","综合":8.8,"过程":"提前约好了时间。当天确认很顺利，沟通清楚，环境整洁。整体体验和事先说的一致。"}',1,'accepted',100,'a'),
    (2,1,1,10,'{}','{"时间":"2026-11-22","综合":8.2,"过程":"约了晚上，路好找，回复也及时。"}',1,'accepted',300,'b'),
    (3,1,1,10,'{}','{"时间":"2026-11-25 00:00","综合":9,"过程":"这次是第二次。流程还是很清楚，提前确认细节也很省心。"}',1,'accepted',400,'c'),
    (4,1,2,10,'{}','{"时间":"2026-11-23","综合":8.5,"过程":"到达前有提醒，整个过程都比较从容。"}',1,'accepted',200,'d'),
    (5,1,1,10,'{}','{"时间":"2026-11-23","过程":"发布暂缓的内容"}',1,'accepted',50,'e'),
    (6,1,1,10,'{}','{"时间":"2026-11-24","过程":"不应出现的草稿内容"}',1,'pending',500,'f'),
    (7,1,3,10,'{}','{"时间":"2026-11-24","过程":"不应出现的已删除老师"}',1,'accepted',500,'g'),
    (1,2,1,10,'{}','{"时间":"2026-11-24","过程":"不应出现的未授权集群内容"}',1,'accepted',500,'h');
INSERT INTO cpbot.publications(cluster_id,binding,review,generation,message,chat_id) VALUES
    (1,1,1,1,101,-100123),(1,1,2,1,102,-100123),(1,1,3,1,103,-100123),
    (1,1,4,1,104,-100123),(1,3,3,1,903,-100124),(2,2,1,1,201,-100234);
INSERT INTO cpbot.reviews(id,cluster_id,teacher,author,identity,data,anonymous,status,created,submission)
    SELECT i,1,1,10,'{}',jsonb_build_object('时间','2026-11-10','综合',8.1,'过程','早期历史记录'),1,'accepted',10,'history-'||i
    FROM generate_series(10,30) AS i;
INSERT INTO cpbot.publications(cluster_id,binding,review,generation,message,chat_id)
    SELECT 1,1,i,1,1000+i,-100123 FROM generate_series(10,30) AS i;
"""

LIVE = """INSERT INTO cpbot.reviews(id,cluster_id,teacher,author,identity,data,anonymous,status,created,submission) VALUES
    (40,1,2,11,'{}','{"时间":"2026-11-20","综合":9.1,"过程":"这条车评来自一次使用真实数据库的实时更新。"}',1,'accepted',600,'new');
INSERT INTO cpbot.publications(cluster_id,binding,review,generation,message,chat_id) VALUES (1,1,40,1,140,-100123);"""


def proof_image(colors):
    """Encode a synthetic 640 by 400 PNG using only the standard library."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))

    rows = b''.join(b'\0' + colors[y // 100] * 640 for y in range(400))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 640, 400, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def init_data(uid=42, age=0):
    pairs = {'auth_date': str(int(time.time()) - age), 'user': json.dumps({'id': uid, 'first_name': '验收'})}
    key = hmac.digest(b'WebAppData', TOKEN.encode(), hashlib.sha256)
    pairs['hash'] = hmac.digest(key, '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items())).encode(), hashlib.sha256).hex()
    return urlencode(pairs)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--browser', action='store_true', help='keep the isolated app available for browser verification')
    options = cli.parse_args()
    pg = Path(os.environ['CPBOT_PG_BIN'])
    go = os.environ.get('CPBOT_GO', 'go')
    out = Path(tempfile.mkdtemp(prefix='cpbot-mini-e2e-', dir='/tmp')).resolve()
    print(f'Artifacts: {out}', flush=True)
    (out / 'runner.pid').write_text(str(os.getpid()))
    data = out / 'db'
    env = {**os.environ, 'LC_ALL': 'C', 'PGHOST': str(out), 'PGUSER': 'fixture', 'PGPORT': '5432', 'PGDATABASE': 'postgres'}

    def run(*args, sql=None):
        result = subprocess.run(args, cwd=ROOT, env=env, input=sql, text=True, capture_output=True, timeout=120)
        with (out / 'commands.log').open('a') as log:
            log.write(f'{args}\n{result.stdout}{result.stderr}')
        result.check_returncode()

    def sql(statement):
        run(pg / 'psql', '-X', '-v', 'ON_ERROR_STOP=1', sql=statement)

    binary = out / 'cpbot'
    run(go, 'build', '-o', binary, './cmd/cpbot')
    run(pg / 'initdb', '-D', data, '-U', 'fixture', '-A', 'trust', '-E', 'UTF8', '--no-locale')
    run(pg / 'pg_ctl', '-D', data, '-l', out / 'postgres.log', '-o', f"-h '' -p 5432 -k {out}", '-w', 'start')
    app = None
    checks = {}
    try:
        schema = ROOT / 'internal/pgstore'
        sql('BEGIN;\n' + (schema / 'schema.sql').read_text() + '\n' +
            (schema / 'photo_publications.sql').read_text() + '\n' + FIXTURE + '\nCOMMIT;')
        sql(f"""
            UPDATE cpbot.reviews SET data=data||'{{"出击证明":"{PROOF_NAME}"}}'
                WHERE (cluster_id=1 AND id IN(1,3,5,6,7,20)) OR cluster_id=2;
            UPDATE cpbot.reviews SET data=data||'{{"出击证明":"{MISSING_NAME}"}}' WHERE (cluster_id,id)=(1,2);
            UPDATE cpbot.publications SET photo=true WHERE (cluster_id,review) IN ((1,2),(1,3),(1,20),(2,1));
            INSERT INTO cpbot.publications(cluster_id,binding,review,generation,message,chat_id,photo) VALUES
                (1,1,6,1,106,-100123,true),(1,1,7,1,107,-100123,true);
            INSERT INTO cpbot.proof_publications(cluster_id,binding,review,generation,message,chat_id)
                VALUES (1,1,1,1,201,-100123);
        """)
        source = out / 'media' / 'bot.sqlite3'
        image = proof_image([b'\x19\x36\x52', b'\x3c\x71\x8a', b'\xa5\xc7\xba', b'\xe7\xb0\x70'])
        private_image = proof_image([b'\x5b\x30\x4f'] * 4)
        for directory, content in [(source.parent, image), (Path(str(source) + '.clusters') / '2', private_image)]:
            (directory / 'proofs').mkdir(parents=True)
            (directory / 'proofs' / PROOF_NAME).write_bytes(content)
        config = {**env, 'BOT_TOKEN': TOKEN, 'SUPERADMIN_IDS': '42,44', 'ENV_FILE': str(out / 'no-production-env'),
                  'DATABASE_PATH': str(source),
                  'CPBOT_MINI_APP_LISTEN': '127.0.0.1:0', 'DATABASE_URL': f'host={out} user=fixture dbname=postgres sslmode=disable'}
        with (out / 'app.log').open('w') as log:
            app = subprocess.Popen([binary, 'serve-mini-app'], env=config, cwd=out, stdout=log, stderr=log)
        for _ in range(100):
            log = (out / 'app.log').read_text()
            if 'listen=' in log:
                base = 'http://' + log.strip().rsplit('listen=', 1)[-1]
                break
            if app.poll() is not None:
                raise RuntimeError(log)
            time.sleep(.1)
        else:
            raise TimeoutError('Mini App did not start')

        def get(path, auth=None, code=200):
            request = Request(base + path, headers={'Authorization': 'Telegram ' + auth} if auth else {})
            try:
                response = urlopen(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                body = response.read()
                content_type = response.headers.get('Content-Type', '')
                assert response.status == code, (path, response.status, body[:512])
                if content_type.startswith('image/'):
                    assert response.headers.get('Cache-Control') == 'no-store'
                    assert content_type == 'image/png'
                    return body
                return json.loads(body) if 'application/json' in content_type else body.decode()

        signed = init_data()
        assert '<h1 id="home-title">最新车评</h1>' in get('/')
        get('/api/feed', code=401)
        get('/api/feed', signed.replace('42','41'), code=401)
        get('/api/feed', init_data(age=9*3600), code=401)
        sql('UPDATE cpbot.bindings SET active=0 WHERE cluster_id=1;')
        get('/api/feed', init_data(uid=43), code=403)
        sql('UPDATE cpbot.bindings SET active=1 WHERE cluster_id=1;')
        get('/api/teachers/2/1', signed, code=404)
        checks['authentication_and_cluster_access'] = 'passed'
        feed = get('/api/feed', signed)
        assert [item['id'] for item in feed['items'][:5]] == ['3','2','4','1','5'], feed
        assert len(feed['items']) == 20 and feed['items'][0]['link'].endswith('/123/103'), feed
        assert 'text' not in feed['items'][4] and 'link' not in feed['items'][4], feed
        assert {i['id'] for i in feed['items'] if i.get('hasProof')} == {'1','2','3','20'}, feed
        assert PROOF_NAME not in json.dumps(feed)
        assert all('author' not in i and i['clusterId'] == '1' for i in feed['items']), feed
        more = get('/api/feed?' + urlencode({'cursor': feed['next']}), signed)
        assert len(more['items']) == 6
        assert len({item['id'] for item in feed['items'] + more['items']}) == 26
        checks['global_order_access_dedup_and_cursor'] = 'passed'
        for word in ('林', '@lin_re'):
            teachers = get('/api/teachers?' + urlencode({'q': word}), signed)['items']
            assert [(item['clusterId'], item['id']) for item in teachers] == [('1','1')], teachers
        assert not get('/api/teachers?q=gone', signed)['items']
        teacher = '/api/teachers/1/1'
        assert [item['id'] for item in get(teacher, signed)['items'][:4]] == ['3','1','5','2']
        assert get(teacher, signed)['items'][1]['hasProof']
        first_old = get(teacher+'?order=asc', signed)
        assert [item['id'] for item in first_old['items'][:2]] == ['10','11']
        old_more = get(teacher+'?'+urlencode({'order': 'asc', 'cursor': first_old['next']}), signed)
        assert [item['id'] for item in old_more['items']] == ['30','2','5','1','3'], old_more
        assert [item['id'] for item in get(teacher+'?from=2026-11-24&to=2026-11-24', signed)['items']] == ['1']
        assert [item['id'] for item in get(teacher+'?from=2026-11-22&to=2026-11-24&order=asc', signed)['items']] == ['2','5','1']
        get(teacher+'?from=2026-11-25&to=2026-11-24', signed, code=400)
        assert not get(teacher+'?from=2026-11-26', signed)['items']
        checks['teacher_search_dates_and_both_orders'] = 'passed'
        proof = '/api/reviews/1/3/proof'
        get(proof, code=401)
        assert get(proof, signed) == image
        assert get('/api/reviews/1/1/proof', signed) == image
        for hidden in (2, 4, 5, 6, 7):
            get(f'/api/reviews/1/{hidden}/proof', signed, code=404)
        sql("UPDATE cpbot.reviews SET status='withdrawn' WHERE (cluster_id,id)=(1,3);")
        get(proof, signed, code=404)
        sql("UPDATE cpbot.reviews SET status='accepted' WHERE (cluster_id,id)=(1,3);")
        sql('INSERT INTO cpbot.cluster_authorizations VALUES (2);')
        get('/api/reviews/2/1/proof', signed, code=404)
        assert get('/api/reviews/2/1/proof', init_data(uid=44)) == private_image
        sql('DELETE FROM cpbot.cluster_authorizations WHERE cluster_id=2;')
        get('/api/reviews/2/1/proof', init_data(uid=44), code=404)
        checks['local_proofs_inline_and_legacy_without_telegram'] = 'passed'
        checks['proof_auth_cluster_withdrawal_and_missing_file'] = 'passed'
        (out / 'live.sql').write_text(LIVE)
        def publish_live(*_):
            if 'new_review_visible_without_restart' in checks:
                return
            sql(LIVE)
            assert get('/api/feed', signed)['items'][0]['id'] == '40'
            checks['new_review_visible_without_restart'] = 'passed'
            (out / 'result.json').write_text(json.dumps(checks, indent=2))
            print('Live review committed', flush=True)

        if not options.browser:
            publish_live()
        (out / 'result.json').write_text(json.dumps(checks, indent=2))
        if options.browser:
            signal.signal(signal.SIGUSR1, publish_live)
            # The official Telegram script consumes tgWebAppData from the URL fragment.
            browser_url = base + '/#' + urlencode({'tgWebAppData': signed, 'tgWebAppVersion': '8.0', 'tgWebAppPlatform': 'web'})
            (out / 'browser-url.txt').write_text(browser_url)
            print(base, flush=True)
            while True:
                signal.pause()
        print(json.dumps(checks), flush=True)
    finally:
        if app and app.poll() is None:
            app.terminate()
            app.wait(timeout=15)
        run(pg / 'pg_ctl', '-D', data, '-m', 'fast', '-w', 'stop')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: exit(0))
    main()
