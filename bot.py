"""VK wholesale bot. Python 3.12+, standard library only; no token in code/logs."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MENU = [["Актуальный прайс"], ["Составить заказ", "Связь с менеджером"],
        ["О нас", "Доставка"], ["Изменить данные", "Стоп"]]
COMMANDS = {x.casefold() for row in MENU for x in row} | {
    "меню", "начать", "старт", "назад", "отменить", "согласен", "не согласен",
    "всё верно", "изменить", "подтвердить", "вернуться к боту"}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(value):
    return format(value, ",").replace(",", " ")


def phone(value):
    if not re.fullmatch(r"[+\d\s()\-]+", value):
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78" and digits[1] in "349":
        return "+7" + digits[1:]
    return None


def valid_text(value, limit=80):
    return (2 <= len(value) <= limit and value.casefold() not in COMMANDS
            and any(c.isalpha() for c in value) and not any(ord(c) < 32 for c in value))


def keyboard(rows):
    return {"one_time": False, "buttons": [[{
        "action": {"type": "text", "label": label}, "color": "secondary"
    } for label in row] for row in rows]}


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          kind TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
          peer_id INTEGER NOT NULL, body TEXT NOT NULL, keyboard TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
          sent INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def user(self, uid):
        row = self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return (row["state"], json.loads(row["payload"])) if row else ("consent", {})

    def save(self, uid, state, data):
        self.db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)",
                        (uid, state, json.dumps(data, ensure_ascii=False)))

    def queue(self, key, uid, body, rows=None):
        kb = keyboard(rows) if rows is not None else {"buttons": []}
        self.db.execute("""INSERT OR IGNORE INTO outbox
            (event_key,peer_id,body,keyboard) VALUES (?,?,?,?)""",
            (key, uid, body, json.dumps(kb, ensure_ascii=False)))

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
            return str(value)
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


class Bot:
    def __init__(self, store, config):
        self.s = store
        self.cfg = config

    @staticmethod
    def complete(data):
        return bool(data.get("consent_at") and valid_text(data.get("city", ""))
                    and valid_text(data.get("name", ""), 60) and phone(data.get("phone", "")))

    def contact(self, uid, data):
        return (f"Имя: {data['name']}\nГород: {data['city']}\nТелефон: {data['phone']}"
                f"\nПрофиль: https://vk.ru/id{uid}")

    def process(self, uid, text, event_id):
        text = text.strip()
        key = str(event_id)
        if self.s.db.execute("SELECT 1 FROM events WHERE id=?", (key,)).fetchone():
            return False
        with self.s.db:
            self.s.db.execute("INSERT INTO events VALUES (?,?)", (key, stamp()))
            state, data = self.s.user(uid)
            def reply(body, rows=None):
                self.s.queue(f"{key}:reply", uid, body, rows)
            def save(next_state):
                self.s.save(uid, next_state, data)
            def menu(body="Выберите раздел. Прайс и заявка доступны в любое время."):
                save("menu")
                reply(body, MENU)
            def notify(body, suffix):
                for manager in self.cfg.get("manager_ids", []):
                    self.s.queue(f"{key}:{suffix}:{manager}", manager, body)
            def consent():
                save("consent")
                prefix = ""
                url = self.cfg.get("privacy_url", "")
                reply(prefix + "Помогу получить оптовый прайс и отправить заявку. "
                      f"Минимальный заказ — {money(self.cfg['minimum_order_rub'])} ₽. "
                      "Доставка — по России. "
                      "Сначала — три вопроса: город, имя и телефон.\n\n"
                      + self.cfg["consent_text"] + ("\n" + url if url else ""),
                      [["Согласен", "Не согласен"]])

            # Administrative commands do not grant buyers access to other buyers' data.
            if uid in self.cfg.get("manager_ids", []) and text.startswith("/resume "):
                target = text.removeprefix("/resume ").strip()
                if not target.isdigit():
                    reply("Формат: /resume VK_ID")
                else:
                    target_id = int(target)
                    _, target_data = self.s.user(target_id)
                    if not self.complete(target_data):
                        reply("Анкета покупателя не завершена.")
                    else:
                        self.s.save(target_id, "menu", target_data)
                        self.s.queue(f"{key}:resume", target_id, "Бот снова доступен.", MENU)
                        reply("Автоматические ответы восстановлены.")
                return True
            if text.casefold() == "стоп":
                data.pop("draft", None)
                save("stopped")
                reply("Сценарий остановлен. Для продолжения напишите «Начать». "
                      "Для запроса удаления данных обратитесь администратору сообщества.", [["Начать"]])
                return True
            if state == "stopped":
                if text.casefold() in {"начать", "старт"}:
                    if self.complete(data):
                        menu()
                    else:
                        consent()
                else:
                    reply("Сценарий остановлен. Для продолжения — «Начать».", [["Начать"]])
                return True
            if state == "consent":
                if text == "Не согласен":
                    data.clear()
                    save("consent")
                    reply("Без согласия анкету не собираем. Если передумаете, нажмите «Согласен».",
                          [["Согласен", "Не согласен"]])
                elif text == "Согласен":
                    data["consent_at"] = stamp()
                    data["consent_version"] = self.cfg["consent_version"]
                    save("city")
                    reply("1 из 3. В каком городе находится ваш магазин? Город нужен для уточнения доставки.")
                else:
                    consent()
                return True
            # Prevent accidental bypass via menu buttons, typed commands or corrupt state.
            if state in {"city", "name", "phone", "confirm"}:
                if text.casefold() in {"начать", "старт", "изменить", "изменить данные"}:
                    for field in ("city", "name", "phone"):
                        data.pop(field, None)
                    save("city")
                    reply("1 из 3. В каком городе находится ваш магазин? Город нужен для уточнения доставки.")
                elif state == "city":
                    if valid_text(text):
                        data["city"] = text
                        save("name")
                        reply("2 из 3. Как к вам обращаться? Напишите имя.")
                    else:
                        reply("Напишите название города, от 2 до 80 символов.")
                elif state == "name":
                    if valid_text(text, 60):
                        data["name"] = text
                        save("phone")
                        reply("3 из 3. Укажите телефон для связи по вашей заявке. Формат: +7 999 123-45-67. Можно начать с 8.")
                    else:
                        reply("Имя не может быть пустым. Напишите от 2 до 60 символов, например «Анна».")
                elif state == "phone":
                    number = phone(text)
                    if number:
                        data["phone"] = number
                        save("confirm")
                        reply("Проверьте данные:\n" + self.contact(uid, data), [["Всё верно", "Изменить"]])
                    else:
                        reply("Не удалось распознать телефон. Пример: +7 999 123-45-67. Без телефона меню недоступно.")
                elif text == "Всё верно" and self.complete(data):
                    if not data.get("contact_notified"):
                        notify("Новый оптовый покупатель\n" + self.contact(uid, data), "contact")
                        data["contact_notified"] = True
                    menu("Данные сохранены. Получите прайс или составьте заявку.")
                else:
                    reply("Подтвердите данные кнопкой «Всё верно» или нажмите «Изменить».",
                          [["Всё верно", "Изменить"]])
                return True
            if not self.complete(data):
                data.clear()
                consent()
                return True
            if state == "human":
                if text in {"Вернуться к боту", "Меню", "Начать"}:
                    menu()
                else:
                    notify("Дополнение к обращению\n" + self.contact(uid, data) + "\n\n" + text[:2500], "human")
                return True
            if text in {"Меню", "Назад", "Отменить", "Начать", "Старт"}:
                data.pop("draft", None)
                menu()
            elif text == "Изменить данные":
                data.pop("contact_notified", None)
                for field in ("city", "name", "phone"):
                    data.pop(field, None)
                save("city")
                reply("1 из 3. В каком городе находится ваш магазин? Город нужен для уточнения доставки.")
            elif state in {"order", "question"}:
                if not text or len(text) > 2500 or text.casefold() in COMMANDS:
                    reply("Напишите текст от 1 до 2500 символов или нажмите «Отменить».", [["Отменить"]])
                else:
                    data["draft"] = text
                    data["draft_kind"] = "order" if state == "order" else "question"
                    save("draft_confirm")
                    reply("Проверьте обращение:\n" + text, [["Подтвердить", "Изменить"], ["Отменить"]])
            elif state == "draft_confirm":
                if text == "Изменить":
                    save("order" if data["draft_kind"] == "order" else "question")
                    reply("Напишите исправленный текст.", [["Отменить"]])
                elif text == "Подтвердить":
                    kind = data.pop("draft_kind")
                    body = data.pop("draft")
                    cursor = self.s.db.execute("INSERT INTO orders(user_id,kind,body,created) VALUES (?,?,?,?)",
                                               (uid, kind, body, stamp()))
                    oid = cursor.lastrowid
                    notify(f"Обращение №{oid} ({kind})\n" + self.contact(uid, data) + "\n\n" + body,
                           f"order:{oid}")
                    if kind == "question":
                        save("human")
                        reply(f"Обращение №{oid} сохранено. График менеджера: {self.cfg['manager_hours']}. "
                              "Вне рабочего времени ответит в ближайший рабочий период. "
                              "Можно дописать вопрос или вернуться к боту.", [["Вернуться к боту"]])
                    else:
                        menu(f"Заявка №{oid} сохранена. Менеджер подтвердит наличие, сумму и доставку. "
                             f"График: {self.cfg['manager_hours']}.")
                else:
                    reply("Выберите «Подтвердить», «Изменить» или «Отменить».",
                          [["Подтвердить", "Изменить"], ["Отменить"]])
            elif text == "Актуальный прайс":
                url = self.cfg.get("price_url", "")
                if url:
                    menu(f"Прайс от {self.cfg['price_date']}:\n{url}\n"
                         f"Минимальный заказ — {money(self.cfg['minimum_order_rub'])} ₽. "
                         "Проверьте единицы цены и кратность в таблице. "
                         "Наличие и итоговую сумму подтвердит менеджер. "
                         "Готовы заказать? Нажмите «Составить заказ».")
                else:
                    menu("Ссылка на прайс пока не настроена. Напишите менеджеру через меню.")
            elif text == "Составить заказ":
                save("order")
                reply(self.cfg["order_intro"], [["Отменить"]])
            elif text == "Связь с менеджером":
                save("question")
                reply("Напишите вопрос менеджеру одним сообщением.", [["Отменить"]])
            elif text == "О нас":
                menu(self.cfg["about"])
            elif text == "Доставка":
                menu(self.cfg["delivery"])
            else:
                menu("Выберите действие кнопкой ниже.")
        return True


class ApiError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"VK API error {code}")  # Never include request parameters/token.


class VK:
    def __init__(self, config):
        self.config = config

    def call(self, method, **params):
        params.update(access_token=self.config["vk_token"], v=self.config["api_version"])
        req = urllib.request.Request("https://api.vk.com/method/" + method,
                                     data=urllib.parse.urlencode(params).encode())
        with urllib.request.urlopen(req, timeout=40) as response:
            result = json.load(response)
        if "error" in result:
            raise ApiError(result["error"].get("error_code", 0))
        return result["response"]

    def send(self, item):
        random_id = int.from_bytes(hashlib.sha256(item["event_key"].encode()).digest()[:4], "big") & 0x7fffffff
        return self.call("messages.send", peer_id=item["peer_id"], message=item["body"],
                         random_id=random_id or 1, keyboard=item["keyboard"])


def flush(store, api):
    items = store.db.execute("""SELECT * FROM outbox WHERE sent=0 AND next_try<=?
        ORDER BY id LIMIT 30""", (time.time(),)).fetchall()
    for item in items:
        try:
            api.send(item)
        except (ApiError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            code = f"VK:{exc.code}" if isinstance(exc, ApiError) else type(exc).__name__
            delay = min(3600, 5 * 2 ** min(item["attempts"], 10))
            with store.db:
                store.db.execute("UPDATE outbox SET attempts=attempts+1,next_try=?,last_error=? WHERE id=?",
                                 (time.time() + delay, code, item["id"]))
            logging.warning("Уведомление %s ожидает повторной отправки (%s)", item["id"], code)
        else:
            with store.db:
                # Remove transmitted personal data from queue; users/orders remain authoritative.
                store.db.execute("UPDATE outbox SET sent=1,body='',keyboard='{}' WHERE id=?", (item["id"],))


def handle_update(bot, update):
    if update.get("type") != "message_new":
        return
    obj = update.get("object", {})
    msg = obj.get("message", obj)
    uid = msg.get("from_id", 0)
    if uid <= 0 or msg.get("out") or msg.get("peer_id") != uid:
        return
    event_id = update.get("event_id") or f"{uid}:{msg.get('conversation_message_id', msg.get('id'))}"
    if event_id.endswith(":None"):
        logging.warning("Пропущено событие без идентификатора")
        return
    bot.process(uid, msg.get("text", ""), event_id)


def apply_message_history(bot, history, group_id):
    """Consume authoritative full messages, never truncated Long Poll previews."""
    messages = history.get("messages") or {}
    if not isinstance(messages, dict) or not isinstance(messages.get("items", []), list):
        raise ValueError("Unexpected messages history")
    items = sorted(messages.get("items", []), key=lambda m: (
        int(m.get("date", 0)), int(m.get("id") or m.get("conversation_message_id") or 0)))
    for msg in items:
        mid = msg.get("conversation_message_id") or msg.get("id")
        if not mid:
            raise ValueError("History message has no stable identifier")
        handle_update(bot, {"type": "message_new", "object": {"message": msg},
                           "event_id": f"messages:{group_id}:{msg.get('peer_id')}:{mid}"})


def sync_message_history(bot, api, store, longpoll, group_id):
    """Commit a history cursor only after its messages are durably processed."""
    while True:
        previous = str(longpoll["pts"])
        history = api.call("messages.getLongPollHistory", ts=longpoll["ts"],
                           pts=longpoll["pts"], group_id=group_id, lp_version=3,
                           preview_length=0)
        if "new_pts" not in history:
            raise ValueError("History response has no cursor")
        apply_message_history(bot, history, group_id)
        current = str(history["new_pts"])
        if history.get("more") and current == previous:
            raise ValueError("History cursor did not advance")
        store.meta("messages_lp_pts", current)
        longpoll["pts"] = current
        if not history.get("more"):
            return


def poll_url(longpoll, mode):
    server_url = longpoll["server"]
    if "://" not in server_url:
        server_url = "https://" + server_url
    server = urllib.parse.urlparse(server_url)
    if server.scheme != "https" or not (server.hostname or "").endswith((".vk.com", ".vk.ru")):
        raise ValueError("Unexpected Long Poll address")
    params = dict(urllib.parse.parse_qsl(server.query))
    params.update(act="a_check", key=longpoll["key"], ts=longpoll["ts"], wait=25)
    if mode == "messages":
        params.update(mode=32, version=3)
    return urllib.parse.urlunparse(server._replace(query=urllib.parse.urlencode(params)))


def validate(config):
    missing = []
    if not config.get("vk_token"):
        missing.append("vk_token")
    if not isinstance(config.get("group_id"), int) or config["group_id"] <= 0:
        missing.append("group_id")
    if not config.get("manager_ids") or not all(isinstance(x, int) and x > 0 for x in config["manager_ids"]):
        missing.append("manager_ids")
    if config.get("long_poll_mode", "messages") not in {"messages", "groups"}:
        missing.append("long_poll_mode (messages / groups)")
    url = urllib.parse.urlparse(config.get("price_url", ""))
    if url.scheme != "https" or not url.netloc:
        missing.append("price_url (публичная HTTPS-ссылка)")
    if not config.get("demo_mode") and (not config.get("privacy_url") or "учебн" in config.get("consent_text", "").lower()):
        missing.append("реальные документы и текст согласия")
    if missing:
        raise ValueError("Заполните config.json: " + ", ".join(missing))


def run(path, db_path):
    config = load_config(path)
    validate(config)
    db_path.parent.mkdir(exist_ok=True, parents=True)
    store = Store(db_path)
    bot = Bot(store, config)
    api = VK(config)
    longpoll = None
    mode = config.get("long_poll_mode", "messages")
    ts_key = "messages_lp_ts" if mode == "messages" else "longpoll_ts"
    logging.info("Бот запущен. Остановить: Ctrl+C")
    while True:
        try:
            fresh = load_config(path)
            validate(fresh)
            if fresh["group_id"] != config["group_id"]:
                raise ValueError("Смена сообщества требует отдельной базы и перезапуска")
            if fresh.get("long_poll_mode", "messages") != mode:
                raise ValueError("Смена режима Long Poll требует перезапуска")
            config = fresh
            bot.cfg = config
            api.config = config
            flush(store, api)
            if longpoll is None:
                if mode == "messages":
                    longpoll = api.call("messages.getLongPollServer", group_id=config["group_id"],
                                        need_pts=1, lp_version=3)
                    longpoll["pts"] = store.meta("messages_lp_pts") or longpoll["pts"]
                    store.meta("messages_lp_pts", longpoll["pts"])
                else:
                    longpoll = api.call("groups.getLongPollServer", group_id=config["group_id"])
                longpoll["ts"] = store.meta(ts_key) or longpoll["ts"]
                logging.info("Long Poll подключён: %s", mode)
            with urllib.request.urlopen(poll_url(longpoll, mode), timeout=35) as response:
                result = json.load(response)
            failed = result.get("failed")
            if failed:
                if failed == 1:
                    logging.error("VK сообщил об устаревшем курсоре: возможен пропуск старых событий")
                    longpoll["ts"] = result["ts"]
                    store.meta(ts_key, result["ts"])
                else:
                    if failed == 3:
                        logging.error("История Long Poll потеряна; проверьте обращения в сообществе")
                        with store.db:
                            store.db.execute("DELETE FROM meta WHERE key=?", (ts_key,))
                            if mode == "messages":
                                store.db.execute("DELETE FROM meta WHERE key='messages_lp_pts'")
                    longpoll = None
                continue
            if mode == "messages":
                sync_message_history(bot, api, store, longpoll, config["group_id"])
            else:
                for update in result.get("updates", []):
                    handle_update(bot, update)
            longpoll["ts"] = result["ts"]
            store.meta(ts_key, result["ts"])
            flush(store, api)
        except (ApiError, urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            logging.error("Цикл приостановлен: %s", str(exc) if isinstance(exc, ApiError) else type(exc).__name__)
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.simulate:
        cfg = load_config(args.config if args.config.exists() else ROOT / "config.example.json")
        cfg["manager_ids"] = [2]
        store = Store(":memory:")
        bot = Bot(store, cfg)
        print("Локальный тренажёр. Сеть не используется. /quit — выход. Используйте вымышленные данные.")
        seq = 0
        while True:
            value = input("Вы: ")
            if value == "/quit":
                break
            seq += 1
            bot.process(1, value, f"simulation:{seq}")
            for item in store.db.execute("SELECT * FROM outbox WHERE sent=0").fetchall():
                print(("Бот: " if item["peer_id"] == 1 else "Менеджеру (симуляция): ") + item["body"])
                if item["peer_id"] == 1:
                    labels = [[x["action"]["label"] for x in row]
                              for row in json.loads(item["keyboard"]).get("buttons", [])]
                    if labels:
                        print("Кнопки:", labels)
            with store.db:
                store.db.execute("UPDATE outbox SET sent=1")
    elif args.check:
        validate(load_config(args.config))
        print("Конфигурация заполнена. Соединение с ВК этой командой не проверяется.")
    else:
        run(args.config, ROOT / "data" / "bot.sqlite3")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Остановлен.")
    except (ValueError, OSError) as exc:
        # Configuration parser errors can contain source snippets; never print them.
        print("Запуск не выполнен. Проверьте наличие и заполнение конфигурации. Тип:", type(exc).__name__)
        raise SystemExit(1)
