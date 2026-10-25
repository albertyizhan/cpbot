import json
import tempfile
import time
import unittest
from pathlib import Path

from cpbot.api import APIError
from cpbot.bot import Bot
from cpbot.identity import targets
from cpbot.store import Store


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.error = None

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == 'getChatMember':
            if self.error:
                raise APIError(self.error)
            return {'status': 'member', 'user': {'id': params['user_id'], 'first_name': '目标', 'username': 'target_a'}}
        return {'message_id': len(self.calls)}


def message(uid=2, text='', group=False, **extra):
    return {'from': {'id': uid, 'first_name': '用户%s' % uid},
            'chat': {'id': -100 if group else uid, 'type': 'supergroup' if group else 'private', 'title': '小群'},
            'text': text, 'date': int(time.time()), 'message_id': 10, **extra}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'db.sqlite3')
        self.store = Store(self.path, 1)
        self.api = FakeAPI()
        self.bot = Bot(self.store, self.api, 'review_bot')
        self.store.password_set(1, 'test-password')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def grant(self, uid=2):
        self.store.unlock({'id': uid, 'first_name': '编辑者'}, 'test-password')

    def click(self, label, uid=2, data=None):
        if data is None:
            keyboard = [p['reply_markup'] for m, p in self.api.calls if m == 'sendMessage'][-1]['inline_keyboard']
            data = next(b['callback_data'] for row in keyboard for b in row if b['text'] == label)
        self.bot.handle({'callback_query': {'id': 'cb', 'from': {'id': uid, 'first_name': '编辑者'},
                                          'message': {'chat': {'id': uid, 'type': 'private'}}, 'data': data}})
        return data

    def test_private_password_edit_delete_history_flow(self):
        self.bot.message(message(text='/start'))
        self.bot.message(message(text='test-password'))
        self.assertTrue(self.store.allowed(2))
        self.click('🔎 查找人员')
        self.bot.message(message(text='@target_a'))
        p = self.store.lookup(username='target_a')
        self.click('编辑标签')
        self.bot.message(message(text='统一备注\n已记录'))
        p = self.store.person(p['id'])
        self.assertEqual((p['note'], p['tags']), ('统一备注', '已记录'))
        self.click('删除标签')
        self.click('确认删除')
        p = self.store.person(p['id'])
        self.assertEqual((p['note'], p['tags']), ('统一备注', ''))
        self.click('修改记录')
        self.click('查看记录 #2')
        self.assertIn('已记录', self.api.calls[-2][1]['text'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM history').fetchone()[0], 2)

    def test_unauthorized_group_silent_and_authorized_shared_across_groups(self):
        self.bot.message(message(text='@target_a', group=True))
        self.assertEqual(self.api.calls, [])
        self.assertIsNone(self.store.lookup(username='target_a'))
        self.grant()
        self.bot.message(message(text='@target_a', group=True))
        first = self.store.lookup(username='target_a')
        self.store.save(first['id'], 0, {'id': 2}, '共同备注', '共同标签')
        other = message(text='@target_a', group=True)
        other['chat']['id'] = -200
        self.bot.message(other)
        sent = self.api.calls[-1][1]
        self.assertIn('共同标签', sent['text'])
        self.assertEqual([b['text'] for b in sent['reply_markup']['inline_keyboard'][0]], ['编辑标签', '删除标签'])
        self.assertIn('start=delete_', sent['reply_markup']['inline_keyboard'][0][1]['url'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM people WHERE username=?', ('target_a',)).fetchone()[0], 1)

    def test_forward_hidden_and_private_deep_links_require_auth(self):
        self.bot.message(message(text='/start edit_1'))
        self.assertFalse(self.store.allowed(2))
        self.grant()
        self.bot.message(message(group=True, forward_origin={'type': 'hidden_user', 'sender_user_name': '某人'}))
        self.assertIn('隐藏', self.api.calls[-1][1]['text'])
        self.bot.message(message(group=True, forward_origin={'type': 'user', 'date': 10,
                                 'sender_user': {'id': 50, 'first_name': '旧名', 'username': 'target_a'}}))
        p = self.store.lookup(user_id=50)
        self.assertEqual(p['name'], '目标')
        self.bot.message(message(text='/start delete_%s' % p['id']))
        self.assertIn('确定删除', self.api.calls[-1][1]['text'])

    def test_username_recycling_never_transfers_marks(self):
        self.grant()
        p = self.store.track('@target_a')
        self.store.save(p['id'], 0, {'id': 2}, '原备注', '原标签')
        self.store.observe({'id': 50, 'username': 'target_a', 'first_name': '原用户'}, 100, True)
        other = self.store.observe({'id': 51, 'username': 'target_a', 'first_name': '新用户'}, 200, True)
        self.assertEqual(other['tags'], '')
        self.assertEqual(self.store.lookup(user_id=50)['tags'], '原标签')
        self.assertIsNone(self.store.lookup(user_id=50)['username'])
        self.store.observe({'id': 50, 'username': 'target_a'}, 50, True)
        self.assertEqual(self.store.lookup(username='target_a')['user_id'], 51)

    def test_unresolved_and_confirmed_records_keep_separate_marks(self):
        self.grant()
        a, b = self.store.track('50'), self.store.track('@target_a')
        self.store.save(a['id'], 0, {'id': 2}, '甲', 'A')
        self.store.save(b['id'], 0, {'id': 2}, '乙', 'B')
        self.store.observe({'id': 50, 'username': 'target_a'}, 100, True)
        self.assertEqual(self.store.person(a['id'])['tags'], 'A')
        self.assertEqual(self.store.person(b['id'])['tags'], 'B')
        self.assertIsNone(self.store.person(b['id'])['username'])

    def test_two_connections_concurrent_version_and_atomic_history(self):
        self.grant()
        p = self.store.track('50')
        second = Store(self.path, 1)
        try:
            self.store.save(p['id'], 0, {'id': 2}, '先提交', '标签')
            with self.assertRaisesRegex(ValueError, '别人修改'):
                second.save(p['id'], 0, {'id': 2}, '后提交', '覆盖')
            self.assertEqual(second.person(p['id'])['note'], '先提交')
            self.assertEqual(second.db.execute('SELECT count(*) FROM history').fetchone()[0], 1)
        finally:
            second.db.close()

    def test_revoke_old_button_expiry_and_replayed_confirmation(self):
        self.grant()
        p = self.store.track('50')
        self.bot.show_person(2, p['id'])
        self.click('编辑标签')
        self.store.revoke(1, 2)
        self.bot.message(message(text='不应写入\n不应写入'))
        self.assertEqual(self.store.person(p['id'])['version'], 0)
        self.grant()
        self.bot.show_person(2, p['id'], 'delete')
        data = self.click('确认删除')
        self.click('', data=data)
        self.assertEqual(self.store.person(p['id'])['version'], 1)
        self.assertTrue(self.api.calls[-1][1]['show_alert'])
        self.bot.home(2)
        self.bot.sessions[2]['until'] = 0
        self.click('📋 浏览车评')
        self.assertTrue(self.api.calls[-1][1]['show_alert'])

    def test_password_rate_limit_rotation_persistence_and_admin_only(self):
        for _ in range(5):
            with self.assertRaisesRegex(ValueError, '不正确'):
                self.store.unlock({'id': 3}, 'wrong')
        with self.assertRaisesRegex(ValueError, '10 分钟'):
            self.store.unlock({'id': 3}, 'test-password')
        self.grant()
        self.store.password_set(1, 'new-password')
        second = Store(self.path, 1)
        try:
            self.assertTrue(second.allowed(2))
            self.assertTrue(second.allowed(1))
            with self.assertRaises(ValueError):
                second.password_set(2, 'hijacked-password')
            with self.assertRaises(ValueError):
                second.revoke(2, 1)
            value = second.db.execute("SELECT value FROM settings WHERE key='password'").fetchone()[0]
            self.assertNotIn('new-password', value)
        finally:
            second.db.close()

    def test_member_dates_promotions_restrictions_and_out_of_order(self):
        event = {'chat': {'id': -100, 'type': 'supergroup', 'title': '小群'}, 'date': 100,
                 'old_chat_member': {'status': 'left'},
                 'new_chat_member': {'status': 'member', 'user': {'id': 50, 'first_name': '目标'}}}
        self.bot.member_update(event)
        event.update(date=200, old_chat_member={'status': 'member'},
                     new_chat_member={'status': 'administrator', 'user': {'id': 50, 'first_name': '目标'}})
        self.bot.member_update(event)
        row = self.store.db.execute('SELECT * FROM memberships').fetchone()
        self.assertEqual(row['joined'], 100)
        event.update(date=300, new_chat_member={'status': 'restricted', 'is_member': False, 'user': {'id': 50}})
        self.bot.member_update(event)
        self.store.member(row['person'], event['chat'], True, 150, 150)
        row = self.store.db.execute('SELECT * FROM memberships').fetchone()
        self.assertEqual((row['present'], row['joined']), (0, 100))

    def test_limits_invalid_input_and_api_failure(self):
        self.grant()
        for invalid in ('@ab', '-1', str(2**64)):
            with self.assertRaises(ValueError):
                self.store.track(invalid)
        p = self.store.track('50')
        with self.assertRaises(ValueError):
            self.store.save(p['id'], 0, {'id': 2}, 'a' * 501, '')
        self.api.error = 403
        self.bot.message(message(text='x', group=True, forward_origin={'type': 'user', 'date': 10, 'sender_user': {'id': 50}}))
        self.assertIn('是否在群：未知', self.api.calls[-1][1]['text'])
        self.bot.show_person(2, p['id'])
        self.click('编辑标签')
        self.bot.message(message(text='只有一行'))
        self.assertEqual(self.store.person(p['id'])['version'], 0)
        self.assertEqual(len(targets(message(text='@target_a @target_a'))), 1)


if __name__ == '__main__':
    unittest.main()
