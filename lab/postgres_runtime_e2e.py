"""Drive the compiled cpbot polling process over an isolated Bot API."""

from collections import deque
from datetime import datetime
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
from threading import Condition, Thread
from zoneinfo import ZoneInfo
import re


def exercise_runtime(base, source, sql, dsn, image):
    incoming = deque()
    events = []
    files = {}
    condition = Condition()
    update_id, message_id = 890001, 1000
    today = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    sql('runtime', "UPDATE cpbot.jobs SET state='done'; INSERT INTO cpbot.settings(cluster_id,key,value) "
        f"VALUES (1,'last_check','{today}'),(2,'last_check','{today}') ON CONFLICT(cluster_id,key) DO UPDATE SET value=excluded.value")
    sql('runtime', "INSERT INTO cpbot.chat_ownership VALUES(-1003001,1); "
        "INSERT INTO cpbot.bindings(cluster_id,mode,chat_id,title) VALUES(1,'lookup',-1003001,'验收查档群')")
    sql('runtime', "INSERT INTO cpbot.chat_ownership VALUES(-1004001,1); "
        "INSERT INTO cpbot.bindings(cluster_id,mode,chat_id,title) VALUES(1,'feed',-1004001,'验收直发'); "
        "UPDATE cpbot.teachers SET username='teacher_saved' WHERE (cluster_id,id)=(1,1); "
        "INSERT INTO cpbot.people(cluster_id,user_id) VALUES(1,42); "
        "INSERT INTO cpbot.settings VALUES(1,'cluster_member_sources','[-1000101]')")

    class BotAPI(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_bytes(self, data, content_type):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass

        def do_GET(self):
            if self.path == '/file/bot-test/photos/proof.jpg':
                self.send_bytes(image, 'image/jpeg')
                return
            self.send_bytes(files[self.path.rsplit('/', 1)[-1]], 'application/octet-stream')

        def do_POST(self):
            nonlocal message_id
            method = self.path.rsplit('/', 1)[-1]
            payload = self.rfile.read(int(self.headers['Content-Length']))
            content_type = self.headers['Content-Type']
            if content_type.startswith('multipart/'):
                data = BytesParser(policy=policy.default).parsebytes(f'Content-Type: {content_type}\r\n\r\n'.encode() + payload)
                params = {}
                for part in data.iter_parts():
                    name = part.get_param('name', header='Content-Disposition')
                    value = part.get_payload(decode=True)
                    if name in ('photo', 'document'):
                        params[name] = value.hex()
                    else:
                        try:
                            params[name] = json.loads(value)
                        except ValueError:
                            params[name] = value.decode()
            else:
                params = json.loads(payload)
            with condition:
                event = {'method': method, 'params': params}
                if method == 'getMe':
                    result = {'id': 99, 'username': 'review_bot', 'first_name': 'cpbot', 'is_bot': True}
                elif method == 'getUpdates':
                    if not incoming:
                        condition.wait(0.35)
                    result = []
                    while incoming:
                        value = incoming.popleft()
                        if value['update_id'] >= params['offset']:
                            result.append(value)
                elif method == 'getChat':
                    cid = params['chat_id']
                    linked = {-1000001: -1000101, -1000101: -1000001, -1000002: -1000102, -1000102: -1000002}
                    result = {'id': cid, 'title': f'验收 {cid}', 'type': 'channel' if cid in (-1000001, -1000002, -1004001) else 'supergroup', 'linked_chat_id': linked.get(cid, 0)}
                elif method == 'getChatMember':
                    result = {'status': 'administrator' if params['user_id'] == 99 or (params['user_id'] == 777 and params['chat_id'] == -1005001)
                              else 'member' if params['user_id'] == 2 and params['chat_id'] == -1000101 else 'left',
                              'can_post_messages': True, 'can_edit_messages': True}
                elif method == 'getFile':
                    key = params['file_id']
                    result = {'file_path': 'photos/proof.jpg' if key == 'proof-photo' else 'backup/' + key}
                elif method in ('sendMessage', 'sendPhoto', 'sendDocument'):
                    message_id += 1
                    result = {'message_id': message_id}
                elif method == 'editMessageText':
                    result = {'message_id': params['message_id']}
                else:
                    result = True
                event['result'] = result
                events.append(event)
                condition.notify_all()
                if method != 'getUpdates' or result:
                    with (base / 'bot-runtime-api.jsonl').open('a', encoding='utf-8') as out:
                        out.write(json.dumps(event, ensure_ascii=False) + '\n')
            self.send_bytes(json.dumps({'ok': True, 'result': result}).encode(), 'application/json')

    def wait_for(predicate, start=0):
        with condition:
            def ready():
                return next((event for event in events[start:] if predicate(event)), None)
            if not condition.wait_for(ready, timeout=20):
                raise AssertionError(f'Bot API 未出现期望响应。查看 {base / "bot-runtime-api.jsonl"}')
            return ready()

    def submit_update(value):
        nonlocal update_id
        with condition:
            start = len(events)
            value['update_id'] = update_id
            update_id += 1
            incoming.append(value)
            condition.notify_all()
            return start

    def private(text='', user=1, **extra):
        return submit_update({'message': {'chat': {'type': 'private', 'id': user}, 'message_id': 1,
                                         'from': {'id': user, 'first_name': '验收用户'}, 'text': text, **extra}})

    def panel_for(start, text, user=1):
        return wait_for(lambda e: e['method'] in ('sendMessage', 'editMessageText') and e['params'].get('chat_id') == user
                        and text in e['params'].get('text', ''), start)

    def callback(data, mid, user=1):
        return submit_update({'callback_query': {'id': f'c{update_id}', 'data': data,
            'message': {'message_id': mid, 'chat': {'type': 'private', 'id': user}}, 'from': {'id': user, 'first_name': '验收用户'}}})

    def click(panel, label, user=1):
        data = next(button['callback_data'] for row in panel['params']['reply_markup']['inline_keyboard']
                    for button in row if button['text'] == label)
        return callback(data, panel['result']['message_id'], user), data

    local = ThreadingHTTPServer(('127.0.0.1', 0), BotAPI)
    thread = Thread(target=local.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, 'DATABASE_URL': dsn, 'SUPERADMIN_IDS': '1'}
    cmd = [str(base / 'cpbot'), 'run-lab', '--source', str(source), '--api', f'http://127.0.0.1:{local.server_port}']
    with (base / 'bot-runtime.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            poll = wait_for(lambda e: e['method'] == 'getUpdates')
            assert poll['params']['offset'] == 890001
            start = private('/start c1_review_1', user=555)
            panel_for(start, '请先加入', user=555)
            panel = panel_for(private('/start review_1'), '已填写 1/9 项')
            start, _ = click(panel, '提交')
            panel_for(start, '请补齐所有字段')
            start, _ = click(panel, '时间')
            panel_for(start, '请输入评价日期')
            panel_for(private('2026-02-30'), '日期格式应为 YYYY-MM-DD')
            panel = panel_for(private('2026-11-25'), '已填写 2/9 项')
            start, _ = click(panel, '出击证明')
            panel_for(start, '请以照片发送')
            panel_for(private('不是照片'), '请以照片方式发送')
            panel = panel_for(private(photo=[{'file_id': 'proof-photo', 'file_size': len(image)}]), '已上传')
            for i, field in enumerate(('人照', '颜值', '身材', '服务', '态度', '环境')):
                start, _ = click(panel, field)
                scores = panel_for(start, '评分')
                start, _ = click(scores, str(i+4))
                panel = panel_for(start, f'已填写 {i+3}/9 项')
            start, _ = click(panel, '过程')
            panel_for(start, '请发送过程文字')
            panel = panel_for(private('真实进程端到端验收 <&>'), '已填写 9/9 项')
            start, _ = click(panel, '预览')
            preview = panel_for(start, '车评预览')
            assert '【综合】：6.5' in preview['params']['text']
            assert '【老师】：老师1\n@teacher_saved\n' in preview['params']['text']
            start, submit_data = click(preview, '提交')
            panel_for(start, '车评 #2 已提交')
            start = callback(submit_data, preview['result']['message_id'])
            assert wait_for(lambda e: e['method'] == 'answerCallbackQuery' and '已过期' in e['params']['text'], start)
            public = wait_for(lambda e: e['method'] == 'sendPhoto' and e['params']['chat_id'] == -1000101)
            feed = wait_for(lambda e: e['method'] == 'sendPhoto' and e['params']['chat_id'] == -1004001)
            for delivery in (public, feed):
                params = delivery['params']
                assert params['photo'] == image.hex()
                assert '【老师】：老师1\n@teacher_saved\n' in params['caption']
                assert '【综合】：6.5' in params['caption'] and '【作者】' not in params['caption']
                assert '真实进程端到端验收 &lt;&amp;&gt;' in params['caption']
                assert params['reply_markup']['inline_keyboard'] == [[{
                    'text': '评价当前老师', 'url': 'https://t.me/review_bot?start=c1_review_1',
                }]]
            assert public['params']['reply_parameters']['message_id'] == 30
            assert 'reply_parameters' not in feed['params']
            wait_for(lambda e: e['method'] == 'getUpdates' and e['params']['offset'] == update_id, start=0)
            assert sql('runtime', "SELECT author,status,data->>'综合' FROM cpbot.reviews WHERE (cluster_id,id)=(1,2)") == '1|accepted|6.5'
            assert sql('runtime', 'SELECT count(*) FROM cpbot.reviews') == '3'
            assert sql('runtime', "SELECT count(*) FROM cpbot.publications WHERE (cluster_id,review,photo)=(1,2,true)") == '2'
            assert sql('runtime', "SELECT count(*) FROM cpbot.jobs WHERE (cluster_id,review,kind)=(1,2,'proof')") == '0'
            assert sql('runtime', "SELECT count(*) FROM cpbot.drafts WHERE (cluster_id,user_id)=(1,1)") == '0'
            path = sql('runtime', "SELECT data->>'出击证明' FROM cpbot.reviews WHERE (cluster_id,id)=(1,2)")
            assert (Path(source).parent / 'proofs' / path).read_bytes() == image
            home = panel_for(private('/start'), '车评主菜单')
            start, _ = click(home, '📋 我的车评')
            mine = panel_for(start, '我的车评')
            assert f"https://t.me/c/0101/{public['result']['message_id']}" in mine['params']['text']
            start, _ = click(mine, '返回主菜单')
            home = panel_for(start, '车评主菜单')
            start, _ = click(home, '🔎 找老师')
            picker = panel_for(start, '选择集群')
            start, _ = click(picker, '1')
            teachers = panel_for(start, '选择老师')
            start, _ = click(teachers, '老师1 · #1')
            listing = panel_for(start, '老师1 · 车评')
            assert f"#{2} · 2026-11-25" in listing['params']['text']

            panel_for(private('/start', user=42, date=int(datetime.now().timestamp()), **{
                'from': {'id': 42, 'username': 'teacher_live', 'first_name': '老师1'}}), '车评主菜单', user=42)
            sql('runtime', "INSERT INTO cpbot.drafts(cluster_id,user_id,data,token) "
                "SELECT 1,2,data || jsonb_build_object('过程',repeat('长',1200)),'long-draft' "
                "FROM cpbot.reviews WHERE (cluster_id,id)=(1,2)")
            long_draft = panel_for(private('/start c1_review_1', user=2), '已填写 9/9 项', user=2)
            start, _ = click(long_draft, '提交', user=2)
            panel_for(start, '图片说明最多 1024 字', user=2)
            assert sql('runtime', "SELECT count(*) FROM cpbot.reviews WHERE (cluster_id,author)=(1,2)") == '0'
            start, _ = click(long_draft, '出击证明 ✅', user=2)
            proof_panel = panel_for(start, '选填', user=2)
            start, _ = click(proof_panel, '移除证明', user=2)
            clean_draft = panel_for(start, '已移除出击证明', user=2)
            start, _ = click(clean_draft, '提交', user=2)
            submitted = panel_for(start, '已提交', user=2)
            plain_review_id = int(re.search(r'车评 #(\d+) 已提交', submitted['params']['text']).group(1))
            plain_deliveries = []
            for chat in (-1000101, -1004001):
                plain = wait_for(lambda e: e['method'] == 'sendMessage' and e['params']['chat_id'] == chat, start)
                assert '【老师】：老师1\n@teacher_live\n' in plain['params']['text']
                assert '长' * 1200 in plain['params']['text']
                assert plain['params']['reply_markup'] == feed['params']['reply_markup']
                plain_deliveries.append(plain)
            assert plain_deliveries[0]['params']['reply_parameters']['message_id'] == 30
            assert 'reply_parameters' not in plain_deliveries[1]['params']

            start = submit_update({'message': {'chat': {'type': 'supergroup', 'id': -1003001},
                'message_id': 98, 'from': {'id': 1}, 'date': 1760000100, 'text': '@customer'}})
            group_card = wait_for(lambda e: e['method'] == 'sendMessage' and e['params']['chat_id'] == -1003001, start)
            assert '标签1' in group_card['params']['text']
            link = group_card['params']['reply_markup']['inline_keyboard'][0][0]['url']
            panel_for(private('/start ' + link.split('start=')[1]), '发送要追加的标签')
            panel_for(private('新版标签'), '标签1、新版标签')
            assert sql('runtime', 'SELECT cluster_id,tags FROM cpbot.people WHERE user_id=6789123456789 ORDER BY cluster_id') == (
                '1|标签1、新版标签\n2|标签2')
            assert sql('runtime', "SELECT count(*) FROM cpbot.history WHERE action='新增标签'") == '1'
            selector = panel_for(private('/admin'), '选择要管理的集群')
            start, _ = click(selector, '1')
            admin = panel_for(start, '管理后台')
            start, _ = click(admin, '📤 发布任务')
            jobs = panel_for(start, '发布任务')
            start, _ = click(jobs, '暂停任务')
            jobs = panel_for(start, '已暂停')
            start, _ = click(jobs, '返回')
            admin = panel_for(start, '管理后台')
            start, _ = click(admin, '📥 批量导入老师')
            panel_for(start, '每行一位')
            batch = panel_for(private('新增甲 @waitingteacher\n新增乙 88888'), '共 2 位')
            start, _ = click(batch, '确认导入')
            admin = panel_for(start, '已导入 2 位老师')
            panel_for(private('/start', user=99991, **{'from': {'id': 99991, 'username': 'waitingteacher'}}), '车评主菜单', user=99991)
            assert sql('runtime', "SELECT user_id FROM cpbot.teachers WHERE name='新增甲'") == '99991'
            start, _ = click(admin, '👩‍🏫 老师列表')
            roster = panel_for(start, '老师列表')
            new_teacher = next(button['text'] for row in roster['params']['reply_markup']['inline_keyboard']
                               for button in row if button['text'].startswith('新增乙 · #'))
            start, _ = click(roster, new_teacher)
            detail = panel_for(start, '车评用户名：暂无')
            binding = next(line for line in detail['params']['text'].splitlines() if line.startswith('已绑定本人账号'))
            start, _ = click(detail, '登记用户名')
            panel_for(start, '发送老师的 Telegram @用户名')
            panel_for(private('@teacher_live'), '此用户名属于另一个已知账号')
            detail = panel_for(private('@new_teacher_b'), '车评显示：@new_teacher_b')
            assert binding in detail['params']['text'].splitlines()
            start, _ = click(detail, '返回老师列表')
            roster = panel_for(start, '老师列表')
            start, _ = click(roster, '返回管理后台')
            admin = panel_for(start, '管理后台')
            start, _ = click(admin, '评论按钮')
            grid = panel_for(start, '选择格子编辑')
            start, _ = click(grid, '＋')
            cell = panel_for(start, '评论按钮')
            start, _ = click(cell, '填写名称和链接')
            panel_for(start, '分两行')
            cell = panel_for(private('关于评价\nhttps://example.invalid/{review_id}'), '关于评价')
            start, _ = click(cell, '返回格子')
            grid = panel_for(start, '选择格子编辑')
            start, _ = click(grid, '返回')
            admin = panel_for(start, '管理后台')
            start, _ = click(admin, '📤 发布任务')
            jobs = panel_for(start, '发布任务')
            resume, _ = click(jobs, '继续任务')
            jobs = panel_for(resume, '发布任务')
            published = [(2, public), (2, feed)]
            published.extend((plain_review_id, plain) for plain in plain_deliveries)
            for review_id, delivery in published:
                target = delivery['params']['chat_id'], delivery['result']['message_id']
                updated = wait_for(lambda e: e['method'] == 'editMessageReplyMarkup' and
                                   (e['params']['chat_id'], e['params']['message_id']) == target, resume)
                assert updated['params']['reply_markup']['inline_keyboard'] == [[
                    {'text': '评价当前老师', 'url': 'https://t.me/review_bot?start=c1_review_1'},
                    {'text': '关于评价', 'url': f'https://example.invalid/{review_id}'},
                ]]
            start, _ = click(jobs, '返回')
            admin = panel_for(start, '管理后台')
            start, _ = click(admin, '💾 导出数据备份')
            admin = panel_for(start, '备份文件已发送')
            docs = [bytes.fromhex(e['params']['document']) for e in events[start:] if e['method'] == 'sendDocument']
            assert len(docs) == 2
            assert sum(row.get('photo', False) for row in json.loads(docs[0])['tables']['publications']) == 2
            (base/'export.json').write_bytes(docs[0])
            (base/'export.zip').write_bytes(docs[1])
            files.update({'roundtrip.json': docs[0], 'roundtrip.zip': docs[1]})
            start, _ = click(admin, '📥 导入数据备份')
            panel_for(start, '发送同一批')
            panel_for(private(document={'file_id':'roundtrip.zip','file_name':'roundtrip.zip','file_size':len(docs[1])}), '已收到图片 ZIP')
            restored = panel_for(private(document={'file_id':'roundtrip.json','file_name':'roundtrip.json','file_size':len(docs[0])}), '备份校验通过')
            assert sql('runtime', 'SELECT count(*) FROM cpbot.bindings WHERE cluster_id=1 AND active=1') == '3'
            start, _ = click(restored, '确认覆盖并恢复')
            panel_for(start, '备份已恢复')
            assert sql('runtime', 'SELECT count(*) FROM cpbot.bindings WHERE cluster_id=1 AND active=1') == '0'
            assert sql('runtime', 'SELECT count(*) FROM cpbot.bindings WHERE cluster_id=2 AND active=1') == '1'
            assert sql('runtime', 'SELECT count(*) FROM cpbot.publications WHERE cluster_id=1 AND photo') == '2'
            assert len(list((Path(source).parent/'recovery').glob('before-import-*.json'))) == 1

            landing = panel_for(private('/start'), '车评主菜单')
            start, _ = click(landing, '👑 Superadmin 后台')
            super_pick = panel_for(start, '👑 选择集群')
            start, _ = click(super_pick, '＋ 新建集群')
            super_panel = panel_for(start, '集群已创建')
            start, _ = click(super_panel, '生成授权码')
            issue = panel_for(start, '授权码只显示一次')
            code = re.search(r'(?m)^[A-Za-z0-9_-]{32}$', issue['params']['text']).group()
            panel_for(private('/register', user=777), '请输入 Superadmin',user=777)
            panel_for(private(code, user=777), '管理群管理员',user=777)
            panel_for(private('-1005001',user=777), '管理群已绑定',user=777)
            assert sql('runtime', 'SELECT cluster_id FROM cpbot.cluster_management_groups WHERE chat_id=-1005001') == '9'
            return ['running_go_bot_with_persisted_offset', 'member_denied_for_deep_link', 'legacy_review_link',
                    'form_photo_and_score_validation', 'stale_submit_button', 'single_photo_caption_in_thread_and_feed',
                    'saved_username_with_empty_identity', 'observed_username_takes_precedence',
                    'overlong_caption_preserves_draft', 'optional_proof_removal_and_plain_publication',
                    'my_reviews_and_teacher_directory', 'group_lookup_tag_deep_link_and_cluster_isolation',
                    'pause_queue_and_bulk_teacher_identity', 'admin_registers_username_without_rebinding',
                    'default_buttons_on_thread_and_feed_photo_and_text', 'button_config_edits_existing_thread_and_feed_posts',
                    'backup_and_restore_preserve_photo_format', 'register_new_cluster']
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            local.shutdown()
            local.server_close()
            thread.join(5)
