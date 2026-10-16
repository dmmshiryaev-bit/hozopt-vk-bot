import copy
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from bot import (ApiError, Bot, Store, VK, apply_message_history, flush, handle_update,
                 load_config, phone, poll_url, sync_message_history, validate)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
        self.cfg.update(manager_ids=[2], price_url='https://disk.yandex.ru/d/demo')
        self.store = Store(':memory:')
        self.bot = Bot(self.store, self.cfg)
        self.seq = 0

    def tearDown(self):
        self.store.db.close()

    def send(self, value, uid=1):
        self.seq += 1
        self.bot.process(uid, value, f'test:{self.seq}')
        return self.store.db.execute('SELECT * FROM outbox WHERE peer_id=? ORDER BY id DESC LIMIT 1', (uid,)).fetchone()

    def registered(self):
        for value in ['Начать', 'Согласен', 'Нижний Новгород', 'Анна', '+7 (999) 123-45-67', 'Всё верно']:
            self.send(value)

    def test_full_path_price_order_notification(self):
        self.registered()
        self.assertEqual(self.store.user(1)[0], 'menu')
        self.assertEqual(self.store.user(1)[1]['phone'], '+79991234567')
        contacts = self.store.db.execute('SELECT * FROM outbox WHERE peer_id=2').fetchall()
        self.assertEqual(len(contacts), 1)
        self.assertIn('Анна', contacts[0]['body'])
        self.assertIn(self.cfg['price_url'], self.send('Актуальный прайс')['body'])
        self.send('Составить заказ'); self.send('ХО-001 — 8 коробок'); self.send('Подтвердить')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)
        self.assertIn('№1', self.store.db.execute('SELECT body FROM outbox WHERE peer_id=1 ORDER BY id DESC').fetchone()[0])

    def test_refusal_stops_collection_and_menu(self):
        self.send('Начать'); self.send('Не согласен'); self.send('Актуальный прайс')
        self.assertEqual(self.store.user(1), ('consent', {}))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox WHERE peer_id=2').fetchone()[0], 0)

    def test_invalid_phone_and_menu_cannot_bypass(self):
        for value in ['Начать', 'Согласен', 'Москва', 'Анна']:
            self.send(value)
        for value in ['', '123', 'abc79991234567', '+1 555 123 4567', 'Актуальный прайс', 'Меню']:
            msg = self.send(value)
            self.assertEqual(self.store.user(1)[0], 'phone')
            self.assertNotIn(self.cfg['price_url'], msg['body'])
            self.assertEqual(json.loads(msg['keyboard'])['buttons'], [])

    def test_empty_name_and_buttons_not_accepted_as_name(self):
        for value in ['Начать', 'Согласен', 'Казань']:
            self.send(value)
        for value in ['', '   ', '123', 'Меню', 'Актуальный прайс']:
            self.send(value)
            self.assertEqual(self.store.user(1)[0], 'name')
        self.send('Анна')
        self.assertEqual(self.store.user(1)[0], 'phone')

    def test_menu_in_city_is_not_a_city(self):
        self.send('Согласен'); self.send('Меню')
        self.assertEqual(self.store.user(1)[0], 'city')

    def test_returning_buyer_no_duplicate_contact(self):
        self.registered()
        self.send('Начать'); self.send('Актуальный прайс')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox WHERE peer_id=2').fetchone()[0], 1)

    def test_duplicate_event_no_duplicate_order(self):
        self.registered(); self.send('Составить заказ'); self.send('ХО-001 — 8 коробок')
        self.bot.process(1, 'Подтвердить', 'duplicate')
        count = self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        self.assertFalse(self.bot.process(1, 'Подтвердить', 'duplicate'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], count)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)

    def test_cancel_draft_no_order(self):
        self.registered(); self.send('Составить заказ'); self.send('ХО-001 — 8 коробок'); self.send('Отменить')
        self.assertEqual(self.store.user(1)[0], 'menu')
        self.assertNotIn('draft', self.store.user(1)[1])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)

    def test_stop_does_not_trigger_order(self):
        self.registered(); self.send('Стоп'); self.send('Составить заказ')
        self.assertEqual(self.store.user(1)[0], 'stopped')
        self.send('Начать'); self.assertEqual(self.store.user(1)[0], 'menu')

    def test_handoff_and_resume(self):
        self.registered(); self.send('Связь с менеджером'); self.send('Когда отправка?'); self.send('Подтвердить')
        self.assertEqual(self.store.user(1)[0], 'human')
        self.send('Уточнение')
        self.send('/resume 1', uid=2)
        self.assertEqual(self.store.user(1)[0], 'menu')

    def test_unapproved_user_cannot_resume_somebody(self):
        self.registered(); self.send('Связь с менеджером'); self.send('Вопрос'); self.send('Подтвердить')
        self.send('/resume 1', uid=3)
        self.assertEqual(self.store.user(1)[0], 'human')

    def test_latest_price_from_current_config(self):
        self.registered()
        self.cfg.update(price_url='https://disk.yandex.ru/d/new', price_date='21.10.2026')
        msg=self.send('Актуальный прайс')['body']
        self.assertIn('21.10.2026', msg); self.assertIn('/new', msg)

    def test_restart_preserves_phone_step(self):
        temp_root=Path(__file__).resolve().parents[1]/'data'/'test-temp'
        temp_root.mkdir(parents=True,exist_ok=True)
        path=temp_root/('test-'+uuid.uuid4().hex+'.sqlite3')
        try:
            first=Store(path); b=Bot(first,self.cfg)
            for i,v in enumerate(['Начать','Согласен','Москва','Анна']):b.process(1,v,f'persist:{i}')
            first.db.close()
            second=Store(path)
            try:
                self.assertEqual(second.user(1)[0],'phone')
                Bot(second,self.cfg).process(1,'Меню','persist:5')
                self.assertEqual(second.user(1)[0],'phone')
            finally:second.db.close()
        finally:
            path.unlink(missing_ok=True)

    def test_failed_notification_retry_and_redaction(self):
        with self.store.db:self.store.queue('retry',2,'Контакт')
        class Failing:
            def send(self,item):raise ApiError(901)
        flush(self.store,Failing())
        item=self.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(item['sent'],0);self.assertEqual(item['last_error'],'VK:901')
        with self.store.db:self.store.db.execute('UPDATE outbox SET next_try=0')
        class Working:
            def send(self,item):return 100
        flush(self.store,Working())
        item=self.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(item['sent'],1);self.assertEqual(item['body'],'')

    def test_random_id_stable_for_retry(self):
        with self.store.db:self.store.queue('stable',2,'Контакт')
        item=self.store.db.execute('SELECT * FROM outbox').fetchone()
        vk=VK(self.cfg)
        with patch.object(vk,'call',return_value=1) as call:
            vk.send(item);vk.send(item)
        self.assertEqual(call.call_args_list[0].kwargs['random_id'],call.call_args_list[1].kwargs['random_id'])

    def test_only_incoming_private_events(self):
        update={'type':'message_new','event_id':'vk1','object':{'message':{'from_id':1,'peer_id':2000000001,'text':'Согласен'}}}
        handle_update(self.bot,update)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
        update['object']['message']['peer_id']=1
        handle_update(self.bot,update)
        self.assertEqual(self.store.user(1)[0],'city')

    def test_config_rejects_placeholder(self):
        with self.assertRaises(ValueError):validate(self.cfg)
        self.cfg.update(vk_token='test',group_id=1)
        validate(self.cfg)

    def test_phone_formats(self):
        for value in ['+7 (999) 123-45-67','89991234567','7 999 123 45 67']:
            self.assertEqual(phone(value),'+79991234567')

    def test_history_processes_full_messages_in_order(self):
        items=[{'id':3,'date':3,'from_id':1,'peer_id':1,'out':0,'text':'Москва'},
               {'id':1,'date':1,'from_id':1,'peer_id':1,'out':0,'text':'Начать'},
               {'id':2,'date':2,'from_id':1,'peer_id':1,'out':0,'text':'Согласен'}]
        apply_message_history(self.bot,{'messages':{'items':items}},123)
        self.assertEqual(self.store.user(1)[0],'name')
        self.assertEqual(self.store.user(1)[1]['city'],'Москва')

    def test_history_deduplicates_zero_global_id_using_cmid(self):
        item={'id':0,'conversation_message_id':1,'from_id':1,'peer_id':1,'out':0,'text':'Начать'}
        history={'messages':{'items':[item]}}
        apply_message_history(self.bot,history,123)
        count=self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        apply_message_history(self.bot,history,123)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],count)

    def test_history_filters_outgoing_and_chats(self):
        items=[{'id':1,'from_id':1,'peer_id':1,'out':1,'text':'Согласен'},
               {'id':2,'from_id':1,'peer_id':2000000001,'out':0,'text':'Согласен'}]
        apply_message_history(self.bot,{'messages':{'items':items}},123)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_history_cursor_commits_after_processing_and_drains_pages(self):
        responses=[{'new_pts':11,'more':1,'messages':{'items':[
            {'id':1,'from_id':1,'peer_id':1,'out':0,'text':'Начать'}]}},
            {'new_pts':12,'more':0,'messages':{'items':[
            {'id':2,'from_id':1,'peer_id':1,'out':0,'text':'Согласен'}]}}]
        class HistoryApi:
            def call(self,*args,**kwargs):return responses.pop(0)
        lp={'ts':'5','pts':'10'}
        sync_message_history(self.bot,HistoryApi(),self.store,lp,123)
        self.assertEqual(lp['pts'],'12')
        self.assertEqual(self.store.meta('messages_lp_pts'),'12')
        self.assertEqual(self.store.user(1)[0],'city')

    def test_history_bad_message_does_not_commit_cursor(self):
        class HistoryApi:
            def call(self,*args,**kwargs):return {'new_pts':11,'messages':{'items':[
                {'from_id':1,'peer_id':1,'text':'Начать'}]}}
        with self.assertRaises(ValueError):
            sync_message_history(self.bot,HistoryApi(),self.store,{'ts':'5','pts':'10'},123)
        self.assertIsNone(self.store.meta('messages_lp_pts'))

    def test_poll_url_normalizes_https_and_keeps_existing_query(self):
        from urllib.parse import urlparse,parse_qs
        url=poll_url({'server':'api.vk.com/longpoll?extra=1','key':'test','ts':'5'},'messages')
        parsed=urlparse(url);params=parse_qs(parsed.query)
        self.assertEqual(parsed.scheme,'https');self.assertEqual(parsed.hostname,'api.vk.com')
        self.assertEqual(params['extra'],['1']);self.assertEqual(params['version'],['3'])
        with self.assertRaises(ValueError):
            poll_url({'server':'https://evil.test','key':'test','ts':'5'},'messages')


if __name__=='__main__':unittest.main()
