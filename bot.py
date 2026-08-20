#!/usr/bin/env python3
"""
InfoHunt Telegram Bot
Инлайн-форма для поиска по неполным данным с выбором страны.
"""

import os
import re
import logging
import requests
import json
import tempfile
import time
import threading
import sqlite3
from urllib.parse import quote
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes, ConversationHandler
)
from dotenv import load_dotenv
import pytz

load_dotenv()

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

REQUIRED_CHANNEL = "@cumoovwinrar"

DEP_TOKEN = os.getenv("DEP_TOKEN")
DEP_BASE = os.getenv("DEP_BASE")
DADATA_TOKEN = os.getenv("DADATA_TOKEN")
DADATA_SECRET = os.getenv("DADATA_SECRET")

JITLER_TOKENS = []
for i in range(1, 7):
    tok = os.getenv(f"JITLER_TOKEN_{i}")
    if tok:
        JITLER_TOKENS.append(tok)

FUNSTAT_TOKEN = os.getenv("FUNSTAT_TOKEN")
FUNSTAT_BASE = os.getenv("FUNSTAT_BASE", "https://telelog.info/api/v1")

LEAKOSINT_TOKENS = []
for i in range(1, 4):
    tok = os.getenv(f"LEAKOSINT_TOKEN_{i}")
    if tok:
        LEAKOSINT_TOKENS.append(tok)

LEAKOSINT_BASE = "https://leakosintapi.com/"
TIMEOUT = int(os.getenv("TIMEOUT", "12"))
PORT = int(os.getenv("PORT", "8080"))

MSK_TZ = pytz.timezone('Europe/Moscow')

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ========== БАЗА ДАННЫХ (SQLite) ==========
DB_PATH = "users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            registered_at TEXT,
            free_queries_today INTEGER DEFAULT 0,
            last_reset_date TEXT,
            balance REAL DEFAULT 0.0,
            referral_balance REAL DEFAULT 0.0,
            referrer_id INTEGER DEFAULT NULL
        )
    ''')
    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if 'referrer_id' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT NULL")
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "registered_at": row[1],
            "free_queries_today": row[2],
            "last_reset_date": row[3],
            "balance": row[4],
            "referral_balance": row[5],
            "referrer_id": row[6]
        }
    return None

def create_user(user_id, referrer_id=None):
    now = datetime.now(MSK_TZ).isoformat()
    today = datetime.now(MSK_TZ).date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO users (user_id, registered_at, free_queries_today, last_reset_date, balance, referral_balance, referrer_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, now, 5, today, 0.0, 0.0, referrer_id)
    )
    conn.commit()
    conn.close()
    if referrer_id:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET referral_balance = referral_balance + 0.5 WHERE user_id=?", (referrer_id,))
        conn.commit()
        conn.close()
    return get_user(user_id)

def reset_daily_queries_if_needed(user_id):
    user = get_user(user_id)
    if not user:
        return
    today = datetime.now(MSK_TZ).date().isoformat()
    if user['last_reset_date'] != today:
        reg_date = datetime.fromisoformat(user['registered_at']).astimezone(MSK_TZ).date().isoformat()
        if reg_date == today:
            new_queries = user['free_queries_today']
        else:
            new_queries = 2
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET free_queries_today = ?, last_reset_date = ? WHERE user_id = ?",
                  (new_queries, today, user_id))
        conn.commit()
        conn.close()
    return get_user(user_id)

def use_free_query(user_id):
    user = reset_daily_queries_if_needed(user_id)
    if not user:
        return False, 0
    if user['free_queries_today'] > 0:
        new_count = user['free_queries_today'] - 1
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET free_queries_today = ? WHERE user_id = ?", (new_count, user_id))
        conn.commit()
        conn.close()
        return True, new_count
    else:
        return False, 0

def get_profile_text(user_id):
    user = get_user(user_id)
    if not user:
        return "Пользователь не найден."
    reg_date = datetime.fromisoformat(user['registered_at']).astimezone(MSK_TZ)
    days_ago = (datetime.now(MSK_TZ) - reg_date).days
    return (
        f"Ваш ID: {user_id}\n"
        f"Доступно запросов: {user['free_queries_today']}\n"
        f"Ваш баланс: ${user['balance']:.2f}\n"
        f"Реферальный баланс: ${user['referral_balance']:.2f}\n"
        f"Дата регистрации: {reg_date.strftime('%d.%m.%Y %H:%M')}\n"
        f"(Вы агент уже: {days_ago} дней)"
    )

# ========== FLASK ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return jsonify({"status": "ok", "service": "InfoHunt Bot"})

@flask_app.route('/ping')
def ping():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT, debug=False)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def clean_phone(phone):
    return re.sub(r'\D', '', str(phone))

def clean_emojis(text):
    if not text:
        return ""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F700-\U0001F77F"
        "\U0001F780-\U0001F7FF"
        "\U0001F800-\U0001F8FF"
        "\U0001F900-\U0001F9FF"
        "\U0001FA00-\U0001FA6F"
        "\U0001FA70-\U0001FAFF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub('', text).strip()

def format_date(date_str):
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.strftime("%d.%m.%Y %H:%M")
    except:
        return date_str[:16].replace('T', ' ')

def calc_age(birth_date_str):
    try:
        if isinstance(birth_date_str, str) and re.match(r'\d{4}-\d{2}-\d{2}', birth_date_str):
            bd = datetime.strptime(birth_date_str[:10], "%Y-%m-%d")
            today = datetime.now()
            age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
            return age
    except:
        pass
    return None

def get_field_name(key):
    names = {
        'full_name': 'ФИО', 'name': 'Имя', 'first_name': 'Имя',
        'last_name': 'Фамилия', 'middle_name': 'Отчество', 'phone': 'Телефон',
        'email': 'Почта', 'birth_date': 'Дата рождения', 'address': 'Адрес',
        'city': 'Город', 'region': 'Регион', 'country': 'Страна',
        'inn': 'ИНН', 'snils': 'СНИЛС', 'passport': 'Паспорт',
        'card': 'Карта', 'raw_id': 'ID', 'source': 'Источник',
        'original_id': 'ID', 'phone_confirmed_at': 'Телефон подтвержден',
        'created_at': 'Создано', 'updated_at': 'Обновлено', 'fullname': 'ФИО'
    }
    return names.get(key, key)

# ========== API ФУНКЦИИ ==========
def check_jitler_token(token):
    try:
        resp = requests.get("https://api.jitler.top/me", headers={"Authorization": f"Bearer {token}"}, timeout=5)
        return resp.status_code == 200 and resp.json().get("result", False)
    except:
        return False

def get_working_jitler_token():
    for token in JITLER_TOKENS:
        if check_jitler_token(token):
            return token
    return JITLER_TOKENS[0] if JITLER_TOKENS else None

def jitler_search(query, search_type="number"):
    token = get_working_jitler_token()
    if not token:
        return {"error": "Нет токенов Jitler"}
    valid_types = {"number": "number", "sherlock": "sherlock", "phone": "number", "telegram_id": "sherlock"}
    jitler_type = valid_types.get(search_type, "sherlock")
    clean_query = str(query).strip()
    if jitler_type == "number":
        clean_query = re.sub(r'\D', '', clean_query)
        if not clean_query:
            return {"error": "Неверный номер"}
    for _ in range(2):
        try:
            resp = requests.post(
                "https://api.jitler.top/search",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"type": jitler_type, "query": clean_query, "page": 1},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("result"):
                    if "response" in data:
                        return data["response"]
                    if "id" in data:
                        for _ in range(3):
                            time.sleep(1)
                            res = requests.get(f"https://api.jitler.top/search/{data['id']}", headers={"Authorization": f"Bearer {token}"}, timeout=3)
                            if res.status_code == 200:
                                d = res.json()
                                if d.get("result") and "response" in d:
                                    return d["response"]
                        return {"error": "Данные не готовы"}
                    return {"error": "Неожиданный ответ"}
            if resp.status_code in [401, 403, 429]:
                continue
        except:
            time.sleep(1)
            continue
    return {"error": "Jitler не ответил"}

def check_leakosint_token(token):
    try:
        data = {"token": token, "request": "test", "limit": 5, "lang": "ru"}
        resp = requests.post(LEAKOSINT_BASE, json=data, timeout=5)
        return resp.status_code == 200 and "Error code" not in resp.json()
    except:
        return False

def get_working_leakosint_token():
    for token in LEAKOSINT_TOKENS:
        if check_leakosint_token(token):
            return token
    return LEAKOSINT_TOKENS[0] if LEAKOSINT_TOKENS else None

def leakosint_search(query, limit=100):
    if not query or len(query.strip()) < 1:
        return {"error": "Пустой запрос"}
    token = get_working_leakosint_token()
    if not token:
        return {"error": "Нет токенов LeakOSINT"}
    data = {"token": token, "request": query.strip(), "limit": limit, "lang": "ru"}
    for _ in range(2):
        try:
            resp = requests.post(LEAKOSINT_BASE, json=data, timeout=12)
            if resp.status_code == 200:
                result = resp.json()
                if "Error code" not in result:
                    return result
                else:
                    return {"error": f"LeakOSINT: {result['Error code']}"}
            if resp.status_code == 400:
                wait_match = re.search(r"in (\d+) seconds", resp.text)
                if wait_match:
                    time.sleep(int(wait_match.group(1)) + 1)
                    continue
                else:
                    time.sleep(3)
                    continue
            else:
                return {"error": f"LeakOSINT ошибка {resp.status_code}"}
        except:
            time.sleep(2)
            continue
    return {"error": "LeakOSINT не ответил"}

def depsearch_search(query):
    encoded = quote(str(query))
    url = f"{DEP_BASE}/quest={encoded}&token={DEP_TOKEN}&lang=ru"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        else:
            return {"error": f"Depsearch {resp.status_code}"}
    except:
        return {"error": "Depsearch не ответил"}

def dadata_lookup(phone):
    clean = re.sub(r'\D', '', str(phone))
    if not clean or len(clean) < 10:
        return None
    try:
        resp = requests.post(
            "https://dadata.ru/api/v2/clean/phone",
            headers={"Authorization": f"Token {DADATA_TOKEN}", "X-Secret": DADATA_SECRET, "Content-Type": "application/json"},
            json=[clean],
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0 and data[0]:
                return data[0]
        return None
    except:
        return None

def funstat_request(endpoint, params=None):
    if not FUNSTAT_TOKEN:
        return None
    url = f"{FUNSTAT_BASE}{endpoint}"
    try:
        resp = requests.get(url, headers={"accept": "application/json", "Authorization": f"Bearer {FUNSTAT_TOKEN}"}, params=params, timeout=8)
        if resp.status_code == 200:
            return resp.json()
        return None
    except:
        return None

def get_id_by_username(username):
    username = username.replace('@', '').strip()
    result = funstat_request("/users/resolve_username", params={"username": username})
    if result and result.get("success"):
        d = result.get("data", {})
        if isinstance(d, dict):
            return d.get('id')
        elif isinstance(d, list) and d:
            return d[0].get('id')
    return None

def get_user_id(identifier):
    if identifier.isdigit():
        return identifier
    if identifier.startswith('@'):
        identifier = identifier[1:]
    return get_id_by_username(identifier)

def funstat_get_names(user_id):
    result = funstat_request(f"/users/{user_id}/names")
    if result and result.get("success") and result.get("data"):
        return result.get("data", [])
    return None

def funstat_get_usernames(user_id):
    result = funstat_request(f"/users/{user_id}/usernames")
    if result and result.get("success") and result.get("data"):
        return result.get("data", [])
    return None

def funstat_get_gifts(user_id):
    result = funstat_request(f"/users/{user_id}/gifts_relation")
    if result and result.get("success") and result.get("data"):
        return result.get("data", [])
    return None

def funstat_get_user_info(identifier):
    user_id = get_user_id(identifier)
    if not user_id:
        return None
    return {
        "names": funstat_get_names(user_id),
        "usernames": funstat_get_usernames(user_id),
        "gifts": funstat_get_gifts(user_id)
    }

# ========== ОПРЕДЕЛЕНИЕ ТИПА ЗАПРОСА ==========
def detect_search_type(text):
    text = text.strip()
    if re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', text):
        return "email"
    if text.startswith('@') or (not text.isdigit() and re.search(r'[a-zA-Z]', text)):
        return "telegram_id"
    if re.fullmatch(r'\d+', text):
        if 10 <= len(text) <= 15:
            return "phone"
        elif len(text) > 15:
            return "telegram_id"
        else:
            return "fio"
    if re.search(r'[а-яА-Я]', text):
        return "fio"
    if re.search(r'\d+\.\d+\.\d+\.\d+', text):
        return "ip"
    if re.search(r'\w+\.\w+', text) and not re.search(r'\s', text):
        return "domain"
    return "fio"

# ========== УНИВЕРСАЛЬНЫЙ ПОИСК (параллельный) ==========
def unified_search(query, search_type):
    results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        if search_type == "phone":
            clean = clean_phone(query)
            futures["jitler"] = executor.submit(jitler_search, clean, "number")
            futures["depsearch"] = executor.submit(depsearch_search, clean)
            futures["dadata"] = executor.submit(dadata_lookup, clean)
            futures["leak"] = executor.submit(leakosint_search, clean)
            futures["funstat"] = None
        elif search_type == "telegram_id":
            clean = query
            futures["jitler"] = executor.submit(jitler_search, clean, "sherlock")
            futures["depsearch"] = executor.submit(depsearch_search, clean)
            futures["funstat"] = executor.submit(funstat_get_user_info, clean)
            futures["leak"] = executor.submit(leakosint_search, clean)
            futures["dadata"] = None
        elif search_type == "fio":
            futures["jitler"] = executor.submit(jitler_search, query, "sherlock")
            futures["depsearch"] = executor.submit(depsearch_search, query)
            futures["leak"] = executor.submit(leakosint_search, query)
            futures["dadata"] = None
            futures["funstat"] = None
        elif search_type == "email":
            futures["depsearch"] = executor.submit(depsearch_search, query)
            futures["leak"] = executor.submit(leakosint_search, query)
            futures["jitler"] = None
            futures["dadata"] = None
            futures["funstat"] = None
        else:
            futures["depsearch"] = executor.submit(depsearch_search, query)
            futures["leak"] = executor.submit(leakosint_search, query)
            futures["jitler"] = None
            futures["dadata"] = None
            futures["funstat"] = None

        for key, future in futures.items():
            if future:
                try:
                    results[key] = future.result(timeout=12)
                except:
                    results[key] = {"error": "Timeout"}
            else:
                results[key] = None
    return results

# ========== ФОРМАТИРОВАНИЕ КРАТКОГО ОТВЕТА ==========
def format_result(results, query, search_type):
    lines = []
    if search_type == "phone":
        clean = clean_phone(query)
        lines.append(f"Номер телефона: +{clean}")
        dadata = results.get("dadata")
        if dadata:
            operator = dadata.get("operator")
            region = dadata.get("region")
            country = dadata.get("country")
            if operator:
                lines.append(f"Оператор: {operator}")
            if region:
                lines.append(f"Регион: {region}")
            if country:
                lines.append(f"Страна: {country}")
    elif search_type == "telegram_id":
        lines.append(f"Telegram: {query}")
    elif search_type == "email":
        lines.append(f"Email: {query}")
    else:
        lines.append(f"Запрос: {query}")

    dep_data = results.get("depsearch")
    if dep_data and isinstance(dep_data, dict) and "error" not in dep_data:
        recs = dep_data.get("results", [])
        for rec in recs[:1]:
            if isinstance(rec, dict):
                full_name = rec.get("full_name") or rec.get("fullname")
                birth_date = rec.get("birth_date")
                address = rec.get("address") or rec.get("city") or rec.get("region")
                inn = rec.get("inn")
                if full_name or birth_date or address or inn:
                    lines.append("\nЛичные данные")
                    if full_name:
                        lines.append(f"ФИО: {full_name}")
                    if birth_date:
                        lines.append(f"Дата рождения: {birth_date}")
                        age = calc_age(birth_date)
                        if age is not None:
                            lines.append(f"Возраст: {age} лет")
                    if inn:
                        lines.append(f"ИНН: {inn}")
                    if address:
                        lines.append(f"Адрес: {address}")
                    break

    sources = 0
    total_records = 0
    if results.get("depsearch") and "results" in results["depsearch"]:
        total_records += len(results["depsearch"]["results"])
        sources += 1
    if results.get("jitler") and isinstance(results["jitler"], dict) and "error" not in results["jitler"]:
        sources += 1
    if results.get("leak") and isinstance(results["leak"], dict) and "error" not in results["leak"]:
        sources += 1
    if results.get("funstat"):
        sources += 1

    lines.append(f"\nИсточников: {sources}")
    lines.append(f"Всего записей: {total_records}")

    return "\n".join(lines)

# ========== ГЕНЕРАЦИЯ HTML-ОТЧЁТА ==========
def generate_html_report(results, query, search_type):
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Отчёт по поиску</title>
<style>
body {{ font-family: Arial; margin: 20px; background: #f4f4f4; }}
.container {{ max-width: 900px; margin: auto; background: white; padding: 20px; border-radius: 8px; }}
h1 {{ color: #333; }}
.section {{ margin-top: 20px; border-top: 1px solid #ddd; padding-top: 10px; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #f2f2f2; }}
</style>
</head>
<body>
<div class="container">
<h1>Отчёт по поиску</h1>
<p><strong>Запрос:</strong> {query}</p>
<p><strong>Тип:</strong> {search_type}</p>
"""
    dep = results.get("depsearch")
    if dep and isinstance(dep, dict) and "error" not in dep:
        html += '<div class="section"><h2>Depsearch</h2>'
        recs = dep.get("results", [])
        if recs:
            html += '<table><tr>'
            keys = recs[0].keys() if recs else []
            for k in keys:
                html += f'<th>{get_field_name(k)}</th>'
            html += '</tr>'
            for rec in recs[:20]:
                html += '<tr>'
                for k in keys:
                    val = rec.get(k, '')
                    if isinstance(val, list):
                        val = ', '.join(str(v) for v in val)
                    html += f'<td>{val}</td>'
                html += '</tr>'
            html += '</table>'
        else:
            html += '<p>Нет данных</p>'
        html += '</div>'

    jit = results.get("jitler")
    if jit and isinstance(jit, dict) and "error" not in jit:
        html += '<div class="section"><h2>Jitler</h2><pre>' + json.dumps(jit, indent=2, ensure_ascii=False) + '</pre></div>'

    leak = results.get("leak")
    if leak and isinstance(leak, dict) and "error" not in leak:
        html += '<div class="section"><h2>LeakOSINT</h2><pre>' + json.dumps(leak, indent=2, ensure_ascii=False) + '</pre></div>'

    fun = results.get("funstat")
    if fun:
        html += '<div class="section"><h2>Funstat</h2><pre>' + json.dumps(fun, indent=2, ensure_ascii=False) + '</pre></div>'

    html += '</div></body></html>'
    return html

# ========== ПРОВЕРКА ПОДПИСКИ ==========
async def is_subscribed(user_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def require_subscription(update, context):
    keyboard = [
        [InlineKeyboardButton("Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")],
        [InlineKeyboardButton("Проверить подписку", callback_data="check_sub")]
    ]
    await update.message.reply_text(
        f"Для использования бота подпишитесь на канал: {REQUIRED_CHANNEL}\nПосле подписки нажмите «Проверить подписку».",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ========== ГЛАВНОЕ МЕНЮ ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("Поиск по неполным данным", callback_data="search_form")],
        [InlineKeyboardButton("Примеры использования", callback_data="examples")],
        [InlineKeyboardButton("Мой профиль", callback_data="profile")],
        [InlineKeyboardButton("Партнёрская программа", callback_data="partner")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== СОСТОЯНИЯ ДЛЯ ФОРМЫ ==========
EDITING = 1  # состояние ожидания ввода значения для поля

# ========== ФУНКЦИЯ ПОСТРОЕНИЯ ФОРМЫ ==========
def build_form_message(context):
    data = context.user_data.get('search_form', {})
    intro = ("Вы можете указать любое количество данных: фамилию, имя, отчество, "
             "дату или год рождения, возраст, место рождения и т. д.\n"
             "Достаточно заполнить то, что у вас есть — все поля необязательны.\n\n")
    fields = [
        ('Фамилия', 'last_name'),
        ('Имя', 'first_name'),
        ('Отчество', 'middle_name'),
        ('День', 'day'),
        ('Месяц', 'month'),
        ('Год', 'year'),
        ('Возраст от', 'age_from'),
        ('Возраст до', 'age_to'),
        ('Место рождения', 'birth_place')
    ]
    lines = []
    for label, key in fields:
        value = data.get(key, '')
        if not value:
            value = '—'
        lines.append(f"{label}: {value}")
    text = intro + "\n".join(lines) + "\n\nВыберите поле для изменения или действие:"
    return text

def build_form_keyboard(context):
    keyboard = []
    # Кнопки для каждого поля
    field_buttons = [
        ('Фамилия', 'edit_last_name'),
        ('Имя', 'edit_first_name'),
        ('Отчество', 'edit_middle_name'),
        ('День', 'edit_day'),
        ('Месяц', 'edit_month'),
        ('Год', 'edit_year'),
        ('Возраст от', 'edit_age_from'),
        ('Возраст до', 'edit_age_to'),
        ('Место рождения', 'edit_birth_place')
    ]
    # Располагаем в три колонки
    for i in range(0, len(field_buttons), 3):
        row = []
        for label, callback in field_buttons[i:i+3]:
            row.append(InlineKeyboardButton(label, callback_data=callback))
        keyboard.append(row)

    # Нижние кнопки
    keyboard.append([
        InlineKeyboardButton("🇷🇺 Россия", callback_data="choose_country"),
        InlineKeyboardButton("🔄 Сбросить", callback_data="reset_form")
    ])
    keyboard.append([
        InlineKeyboardButton("🔍 Искать", callback_data="do_search"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_search")
    ])
    return InlineKeyboardMarkup(keyboard)

def build_country_keyboard():
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Россия", callback_data="set_country_Россия")],
        [InlineKeyboardButton("🇰🇿 Казахстан", callback_data="set_country_Казахстан")],
        [InlineKeyboardButton("🇧🇾 Беларусь", callback_data="set_country_Беларусь")],
        [InlineKeyboardButton("🔙 Назад к форме", callback_data="back_to_form")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== ОБРАБОТЧИКИ ==========
async def start(update, context):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await require_subscription(update, context)
        return

    user = get_user(user_id)
    if not user:
        referrer_id = None
        if context.args and len(context.args) > 0 and context.args[0].startswith('ref_'):
            try:
                referrer_id = int(context.args[0][4:])
                if referrer_id == user_id:
                    referrer_id = None
            except:
                pass
        create_user(user_id, referrer_id)
        await update.message.reply_text(
            "Добро пожаловать! Вы получили 5 бесплатных запросов на сегодня.\nИспользуйте меню для поиска.",
            reply_markup=main_menu()
        )
    else:
        reset_daily_queries_if_needed(user_id)
        await update.message.reply_text(
            "Приветствую! ты попал в бота кумова.\n\nтут ты сможешь найти информацию о своем обидчике.\n\nудачного поиска!",
            reply_markup=main_menu()
        )

async def check_subscription_callback(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if await is_subscribed(user_id, context):
        await query.edit_message_text("Подписка подтверждена!", reply_markup=main_menu())
    else:
        await query.edit_message_text(
            "Вы ещё не подписаны. Подпишитесь и нажмите «Проверить подписку» снова.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")],
                [InlineKeyboardButton("Проверить подписку", callback_data="check_sub")]
            ])
        )

async def menu_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "search_form":
        if 'search_form' not in context.user_data:
            context.user_data['search_form'] = {}
        text = build_form_message(context)
        keyboard = build_form_keyboard(context)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
        return

    elif data == "examples":
        examples_text = """
Примеры для ввода команд

Личность:
Навальный Алексей Анатольевич 04.06.1976 
(Можно искать и по неполным данным: ФИО, возрасту или части даты рождения.)

Контакты:
79999688666 – номер телефона
79999688666@mail.ru – email

Транспорт:
В395ОК199 – номер автомобиля
XTA211440C5106924 – VIN автомобиля

Социальные сети:
vk.com/sherlock – Вконтакте
tiktok.com/@sherlock – Tiktok
instagram.com/sherlock – Instagram
ok.ru/profile/58460 – Одноклассники

Telegram:
@sherlock, tg123456 – логин или ID

Документы:
/vu 1234567890 – водительские права
/passport 1234567890 – паспорт
/snils 12345678901 – СНИЛС
/inn 123456789012 – ИНН

Онлайн-следы:
/tag хирург москва – поиск по телефонным книгам
sherlock.com или 1.1.1.1 – домен или IP

Недвижимость:
/adr Город, Улица, 1
77:01:0004042:6987 - кадастровый номер

Юридическое лицо:
/inn 2540214547 – ИНН
1107449004464 – ОГРН или ОГРНИП
        """
        await query.edit_message_text(examples_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main")]]))

    elif data == "profile":
        user_id = query.from_user.id
        user = get_user(user_id)
        if user:
            reset_daily_queries_if_needed(user_id)
            text = get_profile_text(user_id)
        else:
            text = "Пользователь не найден. Нажмите /start для регистрации."
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Пополнить баланс", callback_data="buy")],
                [InlineKeyboardButton("Назад", callback_data="main")]
            ])
        )

    elif data == "partner":
        user_id = query.from_user.id
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        await query.edit_message_text(
            f"Партнёрская программа:\n\nПриглашайте друзей и получайте бонусы.\nЗа каждого нового пользователя вы получаете $0.50 на реферальный баланс.\n\nВаша реферальная ссылка:\n{ref_link}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main")]])
        )

    elif data == "buy":
        await query.edit_message_text(
            "Пополнение баланса:\n\nСвяжитесь с @admin для оплаты.\nПосле пополнения баланс будет зачислен автоматически.\n\n(Функция в разработке)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="profile")]])
        )

    elif data == "main":
        await query.edit_message_text("Главное меню:", reply_markup=main_menu())

    elif data == "cancel_search":
        await query.edit_message_text("Поиск отменён.", reply_markup=main_menu())
        return ConversationHandler.END

    elif data == "choose_country":
        # Показываем выбор стран
        text = "Выберите страну для поля «Место рождения»:"
        await query.edit_message_text(text, reply_markup=build_country_keyboard())
        return

    elif data == "back_to_form":
        # Возврат к форме
        text = build_form_message(context)
        keyboard = build_form_keyboard(context)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
        return

    elif data.startswith("set_country_"):
        # Устанавливаем страну
        country = data.replace("set_country_", "")
        if 'search_form' not in context.user_data:
            context.user_data['search_form'] = {}
        context.user_data['search_form']['birth_place'] = country
        # Возвращаем форму
        text = build_form_message(context)
        keyboard = build_form_keyboard(context)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
        return

    elif data == "reset_form":
        context.user_data['search_form'] = {}
        text = build_form_message(context)
        keyboard = build_form_keyboard(context)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
        return

    elif data == "do_search":
        user_id = query.from_user.id
        if not await is_subscribed(user_id, context):
            await query.edit_message_text("Вы не подписаны на канал.", reply_markup=main_menu())
            return

        success, remaining = use_free_query(user_id)
        if not success:
            await query.edit_message_text(
                "У вас закончились бесплатные запросы на сегодня.\nПополните баланс или подождите завтра.",
                reply_markup=main_menu()
            )
            return

        data = context.user_data.get('search_form', {})
        parts = []
        if data.get('last_name'):
            parts.append(data['last_name'])
        if data.get('first_name'):
            parts.append(data['first_name'])
        if data.get('middle_name'):
            parts.append(data['middle_name'])
        birth_parts = []
        if data.get('year'):
            birth_parts.append(data['year'])
        if data.get('month'):
            birth_parts.append(data['month'].zfill(2))
        if data.get('day'):
            birth_parts.append(data['day'].zfill(2))
        if birth_parts:
            parts.append('-'.join(birth_parts))
        age_from = data.get('age_from')
        age_to = data.get('age_to')
        if age_from and age_to:
            parts.append(f"возраст от {age_from} до {age_to}")
        elif age_from:
            parts.append(f"возраст от {age_from}")
        elif age_to:
            parts.append(f"возраст до {age_to}")
        if data.get('birth_place'):
            parts.append(data['birth_place'])

        query_text = ' '.join(parts).strip()
        if not query_text:
            await query.edit_message_text(
                "Вы не заполнили ни одного поля. Заполните хотя бы что-то.",
                reply_markup=build_form_keyboard(context)
            )
            return

        search_type = detect_search_type(query_text)
        msg = await query.edit_message_text(f"Поиск... (осталось запросов: {remaining})")
        results = unified_search(query_text, search_type)
        formatted = format_result(results, query_text, search_type)
        html_report = generate_html_report(results, query_text, search_type)

        context.user_data['html_report'] = html_report
        context.user_data['results'] = results
        context.user_data['query'] = query_text
        context.user_data['search_type'] = search_type

        keyboard = [
            [InlineKeyboardButton("Полный отчёт", callback_data="full_report")],
            [InlineKeyboardButton("Повторить", callback_data="repeat")],
            [InlineKeyboardButton("Назад", callback_data="main")]
        ]
        await msg.edit_text(formatted, reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    return ConversationHandler.END

# ========== ОБРАБОТЧИК ВВОДА ЗНАЧЕНИЙ ДЛЯ ПОЛЯ ==========
async def handle_edit_input(update, context):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Пустое значение не допускается. Попробуйте снова.")
        return EDITING

    field = context.user_data.get('editing_field')
    if not field:
        await update.message.reply_text("Ошибка. Начните заново через /start.")
        return ConversationHandler.END

    if 'search_form' not in context.user_data:
        context.user_data['search_form'] = {}
    context.user_data['search_form'][field] = text

    text_form = build_form_message(context)
    keyboard = build_form_keyboard(context)
    await update.message.reply_text(text_form, reply_markup=keyboard, parse_mode='Markdown')
    return ConversationHandler.END

# ========== ОБРАБОТЧИКИ ОТЧЁТОВ ==========
async def full_report_callback(update, context):
    query = update.callback_query
    await query.answer()
    html = context.user_data.get('html_report')
    if not html:
        await query.edit_message_text("Данные устарели. Начните поиск заново.")
        return
    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
        f.write(html)
        tmp_path = f.name
    try:
        with open(tmp_path, 'rb') as f:
            await query.message.reply_document(document=f, filename=f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
    finally:
        os.unlink(tmp_path)

async def repeat_callback(update, context):
    query = update.callback_query
    await query.answer()
    results = context.user_data.get('results')
    query_text = context.user_data.get('query')
    search_type = context.user_data.get('search_type')
    if not results or not query_text:
        await query.edit_message_text("Нет данных для повтора. Начните новый поиск.")
        return
    formatted = format_result(results, query_text, search_type)
    html_report = generate_html_report(results, query_text, search_type)
    context.user_data['html_report'] = html_report
    keyboard = [
        [InlineKeyboardButton("Полный отчёт", callback_data="full_report")],
        [InlineKeyboardButton("Повторить", callback_data="repeat")],
        [InlineKeyboardButton("Назад", callback_data="main")]
    ]
    await query.edit_message_text(formatted, reply_markup=InlineKeyboardMarkup(keyboard))

# ========== ОБРАБОТКА ОБЫЧНЫХ СООБЩЕНИЙ (быстрый поиск) ==========
async def handle_message(update, context):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await require_subscription(update, context)
        return

    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Сначала зарегистрируйтесь через /start")
        return

    reset_daily_queries_if_needed(user_id)
    text = update.message.text.strip()
    if not text:
        return

    success, remaining = use_free_query(user_id)
    if not success:
        await update.message.reply_text(
            "У вас закончились бесплатные запросы на сегодня.\nПополните баланс или подождите завтра."
        )
        return

    search_type = detect_search_type(text)
    msg = await update.message.reply_text("Поиск... (осталось запросов: {})".format(remaining))
    results = unified_search(text, search_type)
    formatted = format_result(results, text, search_type)
    html_report = generate_html_report(results, text, search_type)

    context.user_data['html_report'] = html_report
    context.user_data['results'] = results
    context.user_data['query'] = text
    context.user_data['search_type'] = search_type

    keyboard = [
        [InlineKeyboardButton("Полный отчёт", callback_data="full_report")],
        [InlineKeyboardButton("Повторить", callback_data="repeat")],
        [InlineKeyboardButton("Назад", callback_data="main")]
    ]
    await msg.edit_text(formatted, reply_markup=InlineKeyboardMarkup(keyboard))

# ========== MAIN ==========
def main():
    init_db()
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="check_sub"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^(search_form|examples|profile|partner|buy|main|cancel_search|choose_country|back_to_form|reset_form|do_search|set_country_.*|edit_.*)$"))

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_callback, pattern="^edit_.*$")],
        states={
            EDITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_input)]
        },
        fallbacks=[],
        allow_reentry=True
    )
    app.add_handler(conv_handler)

    app.add_handler(CallbackQueryHandler(full_report_callback, pattern="full_report"))
    app.add_handler(CallbackQueryHandler(repeat_callback, pattern="repeat"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
