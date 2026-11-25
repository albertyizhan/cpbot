"""Loopback Bot API scenarios driven by the actual Go publisher and PostgreSQL."""

from collections import deque
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread


def exercise(base, source, command, sql, dsn):
    requests = []
    planned = deque()
    log = base / 'bot-api.jsonl'

    class BotAPI(BaseHTTPRequestHandler):
        def do_POST(self):
            method = self.path.rsplit('/', 1)[-1]
            data = self.rfile.read(int(self.headers['Content-Length']))
            content_type = self.headers['Content-Type']
            if content_type.startswith('multipart/'):
                message = BytesParser(policy=policy.default).parsebytes(
                    f'Content-Type: {content_type}\r\n\r\n'.encode() + data)
                params = {}
                for part in message.iter_parts():
                    name = part.get_param('name', header='Content-Disposition')
                    value = part.get_payload(decode=True)
                    if name == 'photo':
                        params[name] = {'bytes': len(value), 'head': value[:8].hex()}
                    else:
                        try:
                            params[name] = json.loads(value)
                        except ValueError:
                            params[name] = value.decode()
            else:
                params = json.loads(data)
            expected, status, reply = planned.popleft() if planned else ('no request expected', 500, {'ok': False})
            value = {'method': method, 'params': params, 'expected': expected}
            requests.append(value)
            with log.open('a', encoding='utf-8') as output:
                output.write(json.dumps(value, ensure_ascii=False) + '\n')
            self.send_response(status if expected == method else 500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *_):
            pass

    def success(method, result):
        return method, 200, {'ok': True, 'result': result}

    def failure(method, code, description, **params):
        return method, code, {'ok': False, 'error_code': code, 'description': description, 'parameters': params}

    server = ThreadingHTTPServer(('127.0.0.1', 0), BotAPI)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def drive(*steps, expected=()):
        assert not planned
        start = len(requests)
        planned.extend(expected)
        response = command([base / 'publish-driver'], env_overrides={
            'DATABASE_URL': dsn, 'CPBOT_TEST_SOURCE': str(source),
            'CPBOT_TEST_API': f'http://127.0.0.1:{server.server_port}/bot-test',
        }, input=json.dumps(steps))
        actual = requests[start:]
        assert not planned and [r['method'] for r in actual] == [e[0] for e in expected], actual
        return json.loads(response.stdout), actual

    def timed(op, offset=0):
        stamp = datetime.fromisoformat('2026-11-25T02:30:00+08:00') + timedelta(seconds=offset)
        return {'operation': op, 'now': stamp.isoformat()}

    def q(query):
        return sql('publish', query)

    try:
        proof_id = int(q("SELECT id FROM cpbot.jobs WHERE cluster_id=1 AND kind='proof'"))
        q("UPDATE cpbot.jobs SET state='done' WHERE kind='summary'")
        q("UPDATE cpbot.jobs SET state='sending' WHERE cluster_id=1 AND kind='proof'")
        q("INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation) VALUES(1,'proof',1,1,1,20261125)")
        result, _ = drive({'operation': 'recover'}, timed('tick'), {'operation': 'poll-timeout'})
        assert result[-1] == 1
        assert q(f'SELECT state FROM cpbot.jobs WHERE cluster_id=1 AND id={proof_id}') == 'uncertain'
        assert q("SELECT error FROM cpbot.jobs WHERE cluster_id=1 AND kind='proof' AND generation=20261125") == '等待管理员核对上一次发送结果'
        assert q('SELECT count(*) FROM cpbot.proof_publications WHERE cluster_id=1') == '0'
        _, calls = drive({'operation': 'confirm', 'cluster': 1, 'job': proof_id, 'remote_id': 77}, timed('tick', 12),
                         expected=[success('editMessageCaption', {'message_id': 77})])
        assert calls[0]['params']['message_id'] == 77
        assert q('SELECT message FROM cpbot.proof_publications WHERE cluster_id=1') == '77'

        q("INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,generation) VALUES(1,'checkpost',1,1,20261125)")
        drive(timed('tick', 16), expected=[failure('editMessageText', 400, 'Bad Request: message to edit not found')])
        assert q('SELECT message,root,generation FROM cpbot.posts ORDER BY cluster_id') == '||2\n20|30|1'
        assert q("SELECT state FROM cpbot.jobs WHERE cluster_id=1 AND kind='checkpost'") == 'superseded'
        _, calls = drive(timed('tick', 18), timed('tick', 20), expected=[success('sendMessage', {'message_id': 101})])
        assert calls[0]['params']['chat_id'] == -1000001 and 'start=c1_review_1' in calls[0]['params']['text']
        assert q('SELECT count(*) FROM cpbot.publications WHERE cluster_id=1 AND generation=2') == '0'
        assert q("SELECT error FROM cpbot.jobs WHERE cluster_id=1 AND kind='comment' AND generation=2") == '等待频道自动转发到讨论组'

        fwd = {'operation': 'forward', 'message': {'is_automatic_forward': True, 'chat': {'id': -1000101}, 'message_id': 102,
                'forward_origin': {'chat': {'id': -1000001}, 'message_id': 101}}}
        q("INSERT INTO cpbot.settings VALUES (1,'comment_buttons','{" +
          '"rows":2,"columns":2,"buttons":{"1:1":{"text":"评价当前老师","content":"review_current_teacher","style":null},' +
          '"2:1":{"text":"链接","content":"https://example.invalid/{cluster_id}/{teacher_id}/{review_id}","style":"success"}}}' + "')")
        _, calls = drive(fwd, timed('tick', 32), timed('tick', 33), timed('tick', 34), timed('tick', 39), expected=[
            success('sendMessage', {'message_id': 103}),
            success('editMessageText', {'message_id': 101}),
            failure('sendPhoto', 429, 'Too Many Requests', retry_after=7),
        ])
        comment, _, photo = [r['params'] for r in calls]
        assert comment['reply_parameters'] == {'message_id': 102, 'allow_sending_without_reply': False}
        assert '【作者】' not in comment['text'] and '【综合】：8.0' in comment['text']
        assert comment['reply_markup']['inline_keyboard'][1][0] == {'text': '链接', 'url': 'https://example.invalid/1/1/1', 'style': 'success'}
        assert photo['reply_parameters']['message_id'] == 103 and photo['photo']['head'].startswith('ffd8')
        assert q("SELECT state,error FROM cpbot.jobs WHERE cluster_id=1 AND kind='proof' AND generation=2") == 'pending|Telegram 限流'
        drive(timed('tick', 45), timed('tick', 46), expected=[failure('sendPhoto', 502, 'Bad Gateway')])
        assert q("SELECT state FROM cpbot.jobs WHERE cluster_id=1 AND kind='proof' AND generation=2") == 'uncertain'
        q("INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation) VALUES(1,'proof',1,1,1,20261126)")
        drive(timed('tick', 50))
        q("UPDATE cpbot.jobs SET state='superseded' WHERE cluster_id=1 AND kind='proof' AND generation=20261126")
        uncertain_id = int(q("SELECT id FROM cpbot.jobs WHERE cluster_id=1 AND kind='proof' AND generation=2"))
        _, calls = drive({'operation': 'retry', 'cluster': 1, 'job': uncertain_id}, timed('tick', 58),
                         expected=[success('sendPhoto', {'message_id': 104})])
        assert calls[0]['params']['reply_parameters']['message_id'] == 103
        assert q('SELECT cluster_id,generation,message FROM cpbot.proof_publications ORDER BY 1,2') == '1|1|77\n1|2|104\n2|1|32'
        assert q("SELECT count(*) FROM cpbot.audit WHERE action IN ('登记发送结果','确认未发送，重试任务')") == '2'

        drive(timed('nightly', -1))
        assert q("SELECT count(*) FROM cpbot.settings WHERE key='last_check' AND value='2026-11-25'") == '1'
        drive(timed('nightly'), timed('nightly', 3601))
        assert q("SELECT count(*) FROM cpbot.jobs WHERE cluster_id=2 AND kind='health' AND generation=20261125") == '1'
        assert q("SELECT count(*) FROM cpbot.jobs WHERE cluster_id=3") == '0'
        assert q("SELECT count(*) FROM cpbot.settings WHERE key='last_check' AND value='2026-11-25'") == '2'
        assert drive({'operation': 'poll-timeout'})[0] == [20]  # Only the paused cluster has jobs left.

        q("UPDATE cpbot.reviews SET status='withdrawn' WHERE cluster_id=1 AND id=1")
        q("INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation) VALUES(1,'check',1,1,1,20261126),(1,'proof',1,1,1,20261127)")
        _, calls = drive(timed('tick', 70), timed('tick', 74), expected=[
            success('editMessageText', {'message_id': 103}), success('editMessageMedia', {'message_id': 104})])
        assert calls[0]['params']['text'] == '此评价已由管理员撤下。'
        assert calls[1]['params']['photo']['head'] == '89504e470d0a1a0a'
        assert q('SELECT status FROM cpbot.reviews WHERE cluster_id=1 AND id=1') == 'withdrawn'

        q("INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,generation) VALUES(1,'health',1,0,20261127)")
        drive(timed('tick', 80), expected=[success('getChat', {'id': -1000001, 'type': 'group'})])
        assert q("SELECT state,error FROM cpbot.jobs WHERE cluster_id=1 AND kind='health' AND generation=20261127") == (
            'blocked|目标类型不符合绑定模式')
        assert q('SELECT active,error FROM cpbot.bindings WHERE (cluster_id,id)=(1,1)') == '0|目标类型不符合绑定模式'
        return ['pending_proof_no_blind_retry', 'admin_remote_id_registration', 'replica_rebuild',
                'automatic_forward_wait_and_mapping', 'thread_comment_markup', 'multipart_proof_replies_to_text',
                'telegram_429_and_global_pacing', 'telegram_502_uncertain', 'admin_retry_audit',
                'nightly_once_per_day', 'paused_cluster_excluded', 'withdrawn_proof_replaced', 'invalid_binding_blocked']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
