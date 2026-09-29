import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from html.parser import HTMLParser
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ['BOT_TOKEN'] = '123456:offline_test_token'
os.environ['OWNER_TG_ID'] = '101'
os.environ['DEVELOPER_TG_IDS'] = ''
os.environ.pop('OPENAI_API_KEY', None)
os.environ.pop('BACKUP_DIR', None)

from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import Message, Update, User
import ai as advisor
import bot as app
from backups import create_backup, verify_backup, restore_backup, runtime_lock, list_backups
from db import DB, PostingRestricted
from moderation import moderate
from text_utils import text_length, text_pages


class Plain(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.parts = []
        self.feed(text)
    def handle_data(self, data):
        self.parts.append(data)


def plain(text):
    return ''.join(Plain(text).parts)


class Recorder(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
    async def close(self):
        pass
    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name == 'getMe':
            return User(id=123456, is_bot=True, first_name='Offline', username='AnonVerdictBot')
        if name in {'sendMessage', 'editMessageText'}:
            text = method.text or ''
            if text_length(plain(text)) > 4096:
                raise AssertionError(f'Telegram message exceeds budget: {text_length(plain(text))}')
            markup = method.reply_markup
            for row in getattr(markup, 'inline_keyboard', []):
                for button in row:
                    if button.callback_data and len(button.callback_data.encode()) > 64:
                        raise AssertionError('Callback exceeds 64 bytes')
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat={'id': int(method.chat_id), 'type': 'private'},
                           from_user={'id': 123456, 'is_bot': True, 'first_name': 'Offline'}, text=text)
        if name == 'answerCallbackQuery':
            return True
        raise AssertionError(f'Unexpected offline API call: {name}')
    async def stream_content(self, url, **kwargs):
        raise AssertionError('Network is forbidden in tests')
        yield b''


class ProjectTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='anon-tests-')
        self.path = Path(self.tmp.name) / 'db.sqlite3'
        self.db = DB(str(self.path), staff_roles={101: 'CEO Anon Verdict'})
        await self.db.init()
        app.db = self.db
        app.dp.fsm.storage = MemoryStorage()
        app.dp.fsm.events_isolation = SimpleEventIsolation()
        self.recorder = Recorder()
        app.bot.session = self.recorder
        # Exceptions must fail tests instead of being converted into a friendly response.
        self.error_handlers = app.dp.errors.handlers[:]
        app.dp.errors.handlers.clear()
        self.counter = 0
        for uid in (101, 202, 303, 404, 505):
            await self.db.ensure_user(uid, f'private_username_{uid}')

    async def asyncTearDown(self):
        app.dp.errors.handlers[:] = self.error_handlers
        await app.dp.storage.close()
        await app.dp.fsm.events_isolation.close()
        await app.bot.session.close()
        self.tmp.cleanup()

    async def message(self, uid, text):
        self.counter += 1
        return await app.dp.feed_update(app.bot, Update.model_validate({
            'update_id': self.counter,
            'message': {'message_id': self.counter, 'date': 1790690000,
                        'chat': {'id': uid, 'type': 'private'},
                        'from': {'id': uid, 'is_bot': False, 'first_name': 'Test'}, 'text': text}}))

    async def callback(self, uid, data):
        self.counter += 1
        return await app.dp.feed_update(app.bot, Update.model_validate({
            'update_id': self.counter,
            'callback_query': {'id': str(self.counter), 'chat_instance': 'offline', 'data': data,
                'from': {'id': uid, 'is_bot': False, 'first_name': 'Test'},
                'message': {'message_id': self.counter, 'date': 1790690000,
                            'chat': {'id': uid, 'type': 'private'},
                            'from': {'id': 123456, 'is_bot': True, 'first_name': 'Bot'}, 'text': 'Test'}}}))

    def texts(self):
        return '\n'.join(str(getattr(call, 'text', '') or '') for call in self.recorder.calls)

    async def story(self, owner=101, body=None):
        return await self.db.create_story(owner, app.CATS[0], 'Название', body or 'Описание ситуации достаточно длинное для публикации.')

    async def test_deep_link_and_share(self):
        sid = await self.story()
        await self.message(303, f'/start case_{sid}')
        self.assertIn(f'Дело №{sid}', self.texts())
        await self.callback(303, f'share:{sid}')
        markup = self.recorder.calls[-2].reply_markup
        urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
        self.assertTrue(any(url.startswith('https://t.me/share/url?') for url in urls))
        self.assertFalse(any(b.switch_inline_query_chosen_chat for row in markup.inline_keyboard for b in row))

    async def test_best_answer_award_once_after_reopen_and_parallel_close(self):
        sid = await self.story()
        cid = (await self.db.comment(202, sid, 'Полезный ответ'))['comment_id']
        results = await asyncio.gather(*(self.db.close_story_with_best(101, sid, cid) for _ in range(6)))
        self.assertEqual(sum(r.get('best_reputation_reward', 0) for r in results), 5)
        self.assertEqual((await self.db.get_user(202))['reputation'], 7)
        await self.db.change_own_story_status(101, sid, 'open')
        await self.db.close_story_with_best(101, sid, cid)
        self.assertEqual((await self.db.get_user(202))['reputation'], 7)
        await self.db.init()
        await self.db.change_own_story_status(101, sid, 'open')
        await self.db.close_story_with_best(101, sid, cid)
        self.assertEqual((await self.db.get_user(202))['reputation'], 7)

    async def test_concurrent_likes_and_advice_reward(self):
        sid = await self.story()
        comments = await asyncio.gather(*(self.db.comment(202, sid, 'Ответ') for _ in range(5)))
        self.assertEqual((await self.db.get_user(202))['reputation'], 2)
        cid = comments[0]['comment_id']
        await asyncio.gather(*(self.db.react(303, cid, 1) for _ in range(8)))
        self.assertEqual((await self.db.get_user(202))['reputation'], 3)
        await asyncio.gather(*(self.db.react(303, cid, -1) for _ in range(8)))
        self.assertEqual((await self.db.get_user(202))['reputation'], 2)

    async def test_start_home_and_commands_cancel_pending_input(self):
        for uid, navigation in ((303, '/start'), (404, 'home'), (505, '/profile')):
            sid = await self.story()
            await self.callback(uid, f'advice:{sid}:0:all:new:feed')
            if navigation == 'home':
                await self.callback(uid, 'home')
            else:
                await self.message(uid, navigation)
            state = app.dp.fsm.get_context(bot=app.bot, chat_id=uid, user_id=uid)
            if navigation == '/profile':
                self.assertIn('До следующего звания', self.texts())
            self.assertIsNone(await state.get_state())
            self.assertIsNone(await self.db.pending_input(uid))
            await self.message(uid, 'Этот текст уже не должен публиковаться.')
            self.assertEqual(await self.db.comment_count(sid), 0)

    async def test_draft_preview_edit_publish_once_and_restart(self):
        await self.message(303, '📝 Подать дело')
        await self.callback(303, 'cat:' + app.CATS[0])
        await self.message(303, 'Первое название')
        # Simulate loss of in-memory state; pending input and the draft survive.
        app.dp.fsm.storage = MemoryStorage()
        await self.message(303, 'Я хочу разобраться в ситуации и получить подробный совет.')
        self.assertEqual(await self.db.user_story_count(303), 0)
        draft = await self.db.draft(303)
        old_revision = draft['revision']
        self.assertEqual(draft['stage'], 'preview')
        await self.callback(303, 'draft:edit:title')
        await self.message(303, 'Новое название')
        self.assertEqual((await self.db.publish_draft(303, old_revision))['status'], 'stale')
        draft = await self.db.draft(303)
        await asyncio.gather(*(self.db.publish_draft(303, draft['revision']) for _ in range(5)))
        self.assertEqual(await self.db.user_story_count(303), 1)
        self.assertIsNone(await self.db.draft(303))
        row = await self.db.user_story_item(303)
        self.assertEqual(row['title'], 'Новое название')

    async def test_draft_survives_exit_without_publishing(self):
        await self.callback(303, 'new')
        await self.callback(303, 'cat:' + app.CATS[0])
        await self.message(303, 'Мой черновик')
        await self.message(303, '/start')
        await self.message(303, 'Это обычное сообщение, а не текст дела.')
        self.assertEqual((await self.db.draft(303))['body'], '')
        self.assertEqual(await self.db.user_story_count(303), 0)
        await self.callback(303, 'draft:resume')
        await self.message(303, 'Новый подробный текст ситуации, который я хочу опубликовать.')
        revision = (await self.db.draft(303))['revision']
        await self.callback(303, f'draft:publish:{revision}')
        await self.callback(303, f'draft:publish:{revision}')
        self.assertEqual(await self.db.user_story_count(303), 1)

    async def test_long_cases_history_advice_and_full_reader(self):
        sid = await self.story(body='🦊' * 2000)
        cid = (await self.db.comment(202, sid, '🌟' * 2000))['comment_id']
        await self.db.add_story_update(101, sid, '💭' * 2000)
        await self.db.add_story_update(101, sid, '🤔' * 2000)
        await self.db.close_story_with_best(101, sid, cid)
        await self.db.set_story_outcome(101, sid, '🔥' * 2000)
        await self.callback(303, f'case:{sid}:0:all:new')
        await self.callback(303, f'updates:{sid}:0:feed:all:new')
        await self.callback(303, f'up:{sid}:0:f:all:new:0:1')
        await self.callback(303, f'read:{sid}:body:1:0:f:all:new')
        await self.callback(303, f'read:{sid}:best:0:0:f:all:new')
        await self.callback(303, f'read:{sid}:outcome:0:0:f:all:new')
        await self.callback(303, f'comments:{sid}:0:0:all:new:feed')
        await self.callback(303, f'read:{sid}:c{cid}:1:0:f:all:new')
        await self.callback(101, f'admin:story:{sid}:0')
        self.assertLessEqual(text_length(plain(app.case_text(await self.db.story(sid)))), 4096)
        text = '🦊<>&' * 2000
        self.assertEqual(''.join(text_pages(text)), text)

    async def test_anonymous_author_role_and_moderation_privacy(self):
        sid = await self.story()
        await self.db.add_discussion_message(101, sid, 'Сообщение автора')
        text = app.discussion_window_text(await self.db.discussion_page(sid), 0, 1)
        self.assertIn('👑 Автор', text)
        self.assertNotIn('CEO', text)
        self.assertNotIn('private_username_101', text)
        report = await self.db.report_target(303, 'story', sid, 'Спам')
        await self.db.set_staff_role(404, 'Moderator Anon Verdict')
        self.recorder.calls.clear()
        await self.callback(404, f"admin:report:{report['report_id']}:0")
        self.assertNotIn('private_username', self.texts())
        self.assertNotIn('Telegram ID', self.texts())
        await self.callback(404, 'admin:home')
        self.assertIn('Нет доступа', self.texts())

    async def test_moderation_restrictions_and_protected_roles(self):
        await self.db.set_staff_role(404, 'Moderator Anon Verdict')
        await self.db.set_staff_role(505, 'Developer Anon Verdict')
        self.assertFalse(await self.db.restrict_user(303, 202, 'ban', 101))
        self.assertFalse(await self.db.restrict_user(404, 101, 'ban', 101))
        self.assertFalse(await self.db.restrict_user(404, 505, 'ban', 101))
        self.assertTrue(await self.db.restrict_user(404, 202, 'mute24', 101))
        sid = await self.story()
        with self.assertRaises(PostingRestricted):
            await self.db.comment(202, sid, 'Нельзя публиковать')
        await self.callback(202, f'case:{sid}:0:all:new')
        self.assertIn(f'Дело №{sid}', self.texts())
        self.assertTrue(await self.db.restrict_user(101, 202, 'ban', 101))
        self.assertFalse(await self.db.restrict_user(404, 202, 'mute24', 101))
        self.assertFalse(await self.db.restrict_user(404, 202, 'ban', 101))
        self.assertFalse(await self.db.restrict_user(404, 202, 'clear', 101))
        self.recorder.calls.clear()
        await self.message(202, '/start')
        self.assertIn('заблокирован', self.texts())
        self.assertTrue(await self.db.restrict_user(101, 202, 'clear', 101))
        self.assertEqual((await self.db.comment(202, sid, 'Теперь можно'))['status'], 'created')

    async def test_per_case_notifications_cover_owner_followers_and_participants(self):
        sid = await self.story()
        await self.db.toggle_favorite(202, sid)
        await self.db.comment(202, sid, 'Совет')
        await self.db.toggle_case_notifications(202, sid)
        self.assertNotIn(202, await self.db.favorite_subscribers(sid))
        self.assertNotIn(202, await self.db.story_participant_subscribers(sid))
        self.assertFalse(await app.safe_notify(202, 'Тест', sid=sid))
        await self.db.toggle_case_notifications(101, sid)
        self.assertFalse(await app.upsert_advice_notification(101, sid, 'Тест', 'Друг', 1))
        await self.db.toggle_case_notifications(202, sid)
        self.assertIn(202, await self.db.favorite_subscribers(sid))
        await self.db.set_notifications(202, False)
        self.assertFalse(await self.db.notifications_allowed(202, sid))

    async def test_first_touch_including_direct_never_changes(self):
        await self.message(303, '/start')
        await self.message(303, '/start src_tiktok_video01')
        self.assertEqual((await self.db.get_user(303))['acquisition_source'], 'direct')
        await self.message(404, '/start src_tiktok_video01')
        await self.message(404, '/start src_reels_video02')
        user = await self.db.get_user(404)
        self.assertEqual((user['acquisition_source'], user['acquisition_campaign']), ('tiktok', 'video01'))

    async def test_ai_receives_latest_context_and_answers_callback_first(self):
        sid = await self.story()
        await self.db.add_story_update(101, sid, 'Первое обновление')
        await self.db.add_story_update(101, sid, 'Последнее обновление')
        await self.db.close_story_with_best(101, sid)
        await self.db.set_story_outcome(101, sid, 'В итоге всё получилось хорошо')
        async def fake_review(title, body, updates=(), outcome='', user_id=None):
            self.assertEqual(updates, ['Первое обновление', 'Последнее обновление'])
            self.assertEqual(outcome, 'В итоге всё получилось хорошо')
            self.assertEqual(self.recorder.calls[-1].__api_method__, 'answerCallbackQuery')
            return 'Разбор ' * 1000
        with patch.object(app, 'review', fake_review):
            await self.callback(303, f'ai:{sid}:0:all:new:feed')

    async def test_backup_restore_and_running_instance_lock(self):
        sid = await self.story()
        path, info = await asyncio.to_thread(create_backup, self.path)
        self.assertEqual(info['counts']['stories'], 6)
        await self.db.create_story(101, app.CATS[0], 'Лишнее', 'Следующее дело достаточно длинное для сохранения.')
        with runtime_lock(self.path):
            with self.assertRaises(RuntimeError):
                await asyncio.to_thread(restore_backup, path, self.path)
        await asyncio.to_thread(restore_backup, path, self.path)
        self.assertEqual(await self.db.user_story_count(101), 1)
        self.assertEqual(verify_backup(self.path)['counts']['stories'], 6)
        self.assertEqual(len(list_backups(self.path)), 2)

    async def test_corrupt_backup_cannot_replace_database(self):
        bad = Path(self.tmp.name) / 'bad.sqlite3'
        bad.write_bytes(b'broken backup')
        before = verify_backup(self.path)
        with self.assertRaises(sqlite3.DatabaseError):
            restore_backup(bad, self.path)
        self.assertEqual(verify_backup(self.path)['sha256'], before['sha256'])

    async def test_minimum_and_foreign_case_permissions(self):
        with self.assertRaises(ValueError):
            await self.db.create_story(101, app.CATS[0], 'Название', 'Коротко')
        sid = await self.story()
        self.assertEqual((await self.db.close_story_with_best(303, sid))['status'], 'not_found')
        self.assertEqual((await self.db.add_story_update(303, sid, 'Чужое'))['status'], 'not_found')
        self.assertEqual((await self.db.comment(101, sid, 'Себе'))['status'], 'self_or_demo')
        self.assertEqual((await self.db.comment(202, 1, 'К демо'))['status'], 'self_or_demo')

    async def test_legacy_best_award_and_direct_source_migration(self):
        sid = await self.story()
        cid = (await self.db.comment(202, sid, 'Совет'))['comment_id']
        await self.db.close_story_with_best(101, sid, cid)
        async with self.db.connect() as conn:
            await conn.execute('DELETE FROM best_answer_awards')
            await conn.execute("DELETE FROM app_meta WHERE key='reliability_v17'")
            await conn.execute("UPDATE users SET acquisition_source='',acquisition_locked=0 WHERE tg_id=303")
            await conn.commit()
        await self.db.init()
        await self.db.record_acquisition(303, 'tiktok', 'late')
        self.assertEqual((await self.db.get_user(303))['acquisition_source'], 'direct')
        await self.db.change_own_story_status(101, sid, 'open')
        await self.db.close_story_with_best(101, sid, cid)
        self.assertEqual((await self.db.get_user(202))['reputation'], 7)


class TextAndModerationTest(unittest.TestCase):
    def test_reports_of_threats_are_not_blocked(self):
        for text in ['Он сказал мне: я тебя убью. Мне страшно, что делать?',
                     'Мне угрожают и говорят: убью тебя.', 'Я не знаю, как пережить ссору.']:
            self.assertEqual(moderate(text)[1], [])
        self.assertIn('угрозы', moderate('Я тебя убью!')[1])
        self.assertIn('телефон/номер', moderate('Телефон +7 999 123 45 67')[1])
        self.assertIn('электронная почта', moderate('name@example.com')[1])


class AIClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_cache_refresh_and_rate_limit_without_network(self):
        advisor._cache = OrderedDict()
        advisor._recent = OrderedDict()
        advisor._locks = [asyncio.Lock() for _ in range(32)]
        advisor._capacity = asyncio.Semaphore(3)
        create = AsyncMock(return_value=SimpleNamespace(output_text='Готовый разбор'))
        client = AsyncMock()
        client.responses.create = create
        client.__aenter__.return_value = client
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'offline-key'}), patch.object(advisor, 'AsyncOpenAI', return_value=client):
            await advisor.review('Title', 'Body', ['Update 1'], user_id=1)
            await advisor.review('Title', 'Body', ['Update 1'], user_id=1)
            self.assertEqual(create.await_count, 1)
            with self.assertRaises(advisor.ReviewUnavailable):
                await advisor.review('Title', 'Body', ['Update 2'], user_id=1)
            await advisor.review('Title', 'Body', ['Update 2'], user_id=2)
            self.assertEqual(create.await_count, 2)
            self.assertIn('Update 2', create.call_args.kwargs['input'])
            self.assertFalse(create.call_args.kwargs['store'])


if __name__ == '__main__':
    unittest.main()
