#!/usr/bin/env python3
"""
InfoHunt Telegram Bot
Полнофункциональный бот с поиском, профилем, лимитами и реферальной системой.
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
import datetime
from urllib.parse import quote
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
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
PORT = int(os.getenv("PORT", 8080))

# Настройка часового пояса МСК
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
    # Добавляем колонку, если её нет (для старых версий)
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
    # Новому пользователю даём 5 бесплатных запросов
    c.execute(
        "INSERT INTO users (user_id, registered_at, free_queries_today, last_reset_date, balance, referral_balance, referrer_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, now, 5, today, 0.0, 0.0, referrer_id)
    )
    conn.commit()
    conn.close()
    # Если есть реферер, начисляем ему бонус (например, 1 запрос или $0.50)
    if referrer_id:
        # Начисляем рефереру бонус в виде 1 дополнительного запроса (или баланса)
        # Для простоты добавим 1 запрос к free_queries_today (но только если он ещё не истрачен сегодня)
        # Либо увеличим referral_balance на 0.5
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Попробуем увеличить referral_balance
        c.execute("UPDATE users SET referral_balance = referral_balance + 0.5 WHERE user_id=?", (referrer_id,))
        conn.commit()
        conn.close()
    return get_user(user_id)

def reset_daily_queries_if_needed(user_id):
    """Проверяет, был ли уже сброс сегодня. Если нет – сбрасывает до 2."""
    user = get_user(user_id)
    if not user:
        return
    today = datetime.now(MSK_TZ).date().isoformat()
    if user['last_reset_date'] != today:
        # Если пользователь зарегистрирован сегодня, то оставляем 5 (не сбрасываем)
        reg_date = datetime.fromisoformat(user['registered_at']).astimezone(MSK_TZ).date().isoformat()
        if reg_date == today:
            # Только что зарегистрирован, уже есть 5, обновляем last_reset_date, но не меняем количество
            new_queries = user['free_queries_today']  # оставляем как есть
        else:
            # Прошлый день – сбрасываем до 2
            new_queries = 2
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET free_queries_today = ?, last_reset_date = ? WHERE user_id = ?",
                  (new_queries, today, user_id))
        conn.commit()
        conn.close()
        user = get_user(user_id)  # обновляем данные
    return user

def use_free_query(user_id):
    """Уменьшает количество бесплатных запросов на 1, если они есть. Возвращает True/False и остаток."""
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

# ========== FLASK ДЛЯ UPTIMEROBOT ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return jsonify({"status": "ok", "service": "InfoHunt Bot"})

@flask_app.route('/ping')
def ping():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT, debug=False)

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ АВТОСМЕНЫ ТОКЕНОВ ==========
CURRENT_JITLER_TOKEN = None
LAST_JITLER_CHECK = 0
TOKEN_CHECK_INTERVAL = 60

CURRENT_LEAK_TOKEN = None
LAST_LEAK_CHECK = 0

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

# ========== API ФУНКЦИИ (быстрые версии) ==========
def check_jitler_token(token):
    try:
        resp = requests.get(
            "https://api.jitler.top/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5
        )
        if resp.status_code == 200 and resp.json().get("result"):
            return True
        return False
    except:
        return False

def get_working_jitler_token():
    global CURRENT_JITLER_TOKEN, LAST_JITLER_CHECK
    now = time.time()
    if CURRENT_JITLER_TOKEN and (now - LAST_JITLER_CHECK) < TOKEN_CHECK_INTERVAL:
        return CURRENT_JITLER_TOKEN
    for token in JITLER_TOKENS:
        if check_jitler_token(token):
            CURRENT_JITLER_TOKEN = token
            LAST_JITLER_CHECK = now
            return token
    if JITLER_TOKENS:
        CURRENT_JITLER_TOKEN = JITLER_TOKENS[0]
        LAST_JITLER_CHECK = now
        return CURRENT_JITLER_TOKEN
    return None

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
                CURRENT_JITLER_TOKEN = None
                LAST_JITLER_CHECK = 0
                token = get_working_jitler_token()
                if not token:
                    return {"error": "Все токены Jitler недоступны"}
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
    global CURRENT_LEAK_TOKEN, LAST_LEAK_CHECK
    now = time.time()
    if CURRENT_LEAK_TOKEN and (now - LAST_LEAK_CHECK) < TOKEN_CHECK_INTERVAL:
        return CURRENT_LEAK_TOKEN
    for token in LEAKOSINT_TOKENS:
        if check_leakosint_token(token):
            CURRENT_LEAK_TOKEN = token
            LAST_LEAK_CHECK = now
            return token
    if LEAKOSINT_TOKENS:
        CURRENT_LEAK_TOKEN = LEAKOSINT_TOKENS[0]
        LAST_LEAK_CHECK = now
        return CURRENT_LEAK_TOKEN
    return None

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
    # Секция Depsearch
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

    # Jitler
    jit = results.get("jitler")
    if jit and isinstance(jit, dict) and "error" not in jit:
        html += '<div class="section"><h2>Jitler</h2>'
        html += '<pre>' + json.dumps(jit, indent=2, ensure_ascii=False) + '</pre>'
        html += '</div>'

    # LeakOSINT
    leak = results.get("leak")
    if leak and isinstance(leak, dict) and "error" not in leak:
        html += '<div class="section"><h2>LeakOSINT</h2>'
        html += '<pre>' + json.dumps(leak, indent=2, ensure_ascii=False) + '</pre>'
        html += '</div>'

    # Funstat (если есть)
    fun = results.get("funstat")
    if fun:
        html += '<div class="section"><h2>Funstat</h2>'
        html += '<pre>' + json.dumps(fun, indent=2, ensure_ascii=False) + '</pre>'
        html += '</div>'

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

# ========== ГЛАВНОЕ МЕНЮ (инлайн) ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("Поиск по неполным данным", callback_data="search_form")],
        [InlineKeyboardButton("Примеры использования сервиса", callback_data="examples")],
        [InlineKeyboardButton("Мой профиль", callback_data="profile")],
        [InlineKeyboardButton("Партнёрская программа", callback_data="partner")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== СОСТОЯНИЯ ДЛЯ ДИАЛОГА ==========
(FIO, FIRST_NAME, MIDDLE_NAME, BIRTH_DAY, BIRTH_MONTH, BIRTH_YEAR, AGE_FROM, AGE_TO, BIRTH_PLACE) = range(9)

# ========== ОБРАБОТЧИКИ ==========
async def start(update, context):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await require_subscription(update, context)
        return

    # Регистрация пользователя
    user = get_user(user_id)
    if not user:
        # Проверяем реферальный параметр
        referrer_id = None
        if context.args and len(context.args) > 0 and context.args[0].startswith('ref_'):
            try:
                referrer_id = int(context.args[0][4:])
                if referrer_id == user_id:
                    referrer_id = None  # нельзя ссылаться на себя
            except:
                pass
        create_user(user_id, referrer_id)
        # Приветствие новому пользователю
        await update.message.reply_text(
            "Добро пожаловать! Вы получили 5 бесплатных запросов на сегодня.\n"
            "Используйте меню для поиска.",
            reply_markup=main_menu()
        )
    else:
        # Сбрасываем запросы, если нужно
        reset_daily_queries_if_needed(user_id)
        await update.message.reply_text(
            "Приветствую! ты попал в бота кумова.\n\nтут ты сможешь найти инфор#ацию о своем обидчике.\n\nудачного поиска!",
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
        # Начинаем диалог
        await query.edit_message_text(
            "Введите фамилию (или отправьте /skip, чтобы пропустить):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="cancel_search")]])
        )
        return FIO

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
        ref_link = f"https://t.me/{(await context.bot.get_me()).username}?start=ref_{user_id}"
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

    return ConversationHandler.END  # для остальных колбэков не из диалога

# ========== ОБРАБОТЧИКИ ДИАЛОГА ==========
async def search_start(update, context):
    # Этот обработчик уже используется через callback, поэтому здесь просто возвращаем состояние
    pass

async def get_fio(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['fio'] = ''
    else:
        context.user_data['fio'] = text.strip()
    await update.message.reply_text("Введите имя (или /skip):")
    return FIRST_NAME

async def get_first_name(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['first_name'] = ''
    else:
        context.user_data['first_name'] = text.strip()
    await update.message.reply_text("Введите отчество (или /skip):")
    return MIDDLE_NAME

async def get_middle_name(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['middle_name'] = ''
    else:
        context.user_data['middle_name'] = text.strip()
    await update.message.reply_text("Введите день рождения (число, или /skip):")
    return BIRTH_DAY

async def get_birth_day(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['birth_day'] = ''
    else:
        context.user_data['birth_day'] = text.strip()
    await update.message.reply_text("Введите месяц рождения (число, или /skip):")
    return BIRTH_MONTH

async def get_birth_month(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['birth_month'] = ''
    else:
        context.user_data['birth_month'] = text.strip()
    await update.message.reply_text("Введите год рождения (или /skip):")
    return BIRTH_YEAR

async def get_birth_year(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['birth_year'] = ''
    else:
        context.user_data['birth_year'] = text.strip()
    await update.message.reply_text("Введите возраст ОТ (или /skip):")
    return AGE_FROM

async def get_age_from(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['age_from'] = ''
    else:
        context.user_data['age_from'] = text.strip()
    await update.message.reply_text("Введите возраст ДО (или /skip):")
    return AGE_TO

async def get_age_to(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['age_to'] = ''
    else:
        context.user_data['age_to'] = text.strip()
    await update.message.reply_text("Введите место рождения (или /skip):")
    return BIRTH_PLACE

async def get_birth_place(update, context):
    text = update.message.text
    if text.startswith('/skip'):
        context.user_data['birth_place'] = ''
    else:
        context.user_data['birth_place'] = text.strip()

    # Все поля собраны – выполняем поиск
    data = context.user_data
    # Формируем ФИО
    fio_parts = [data.get('fio', ''), data.get('first_name', ''), data.get('middle_name', '')]
    fio = ' '.join([p for p in fio_parts if p]).strip()

    # Дата рождения
    birth_date_parts = []
    if data.get('birth_year'):
        birth_date_parts.append(data['birth_year'])
    if data.get('birth_month'):
        birth_date_parts.append(data['birth_month'].zfill(2))
    if data.get('birth_day'):
        birth_date_parts.append(data['birth_day'].zfill(2))
    birth_date = '-'.join(birth_date_parts) if len(birth_date_parts) >= 3 else ''
    if len(birth_date_parts) == 2:
        birth_date = f"{birth_date_parts[0]}-{birth_date_parts[1]}"
    elif len(birth_date_parts) == 1:
        birth_date = birth_date_parts[0]

    # Возраст
    age_str = ''
    if data.get('age_from') and data.get('age_to'):
        age_str = f"возраст от {data['age_from']} до {data['age_to']}"
    elif data.get('age_from'):
        age_str = f"возраст от {data['age_from']}"
    elif data.get('age_to'):
        age_str = f"возраст до {data['age_to']}"

    place = data.get('birth_place', '')

    query_parts = [fio, birth_date, age_str, place]
    query = ' '.join([p for p in query_parts if p]).strip()
    if not query:
        await update.message.reply_text("Вы не ввели ни одного поля. Начните заново /start")
        return ConversationHandler.END

    # Проверяем, есть ли у пользователя запросы
    user_id = update.effective_user.id
    success, remaining = use_free_query(user_id)
    if not success:
        await update.message.reply_text(
            "У вас закончились бесплатные запросы на сегодня.\n"
            "Пополните баланс в профиле или подождите завтра."
        )
        return ConversationHandler.END

    search_type = detect_search_type(query)
    msg = await update.message.reply_text("Поиск... (осталось запросов: {})".format(remaining))
    results = unified_search(query, search_type)
    formatted = format_result(results, query, search_type)
    html_report = generate_html_report(results, query, search_type)

    context.user_data['html_report'] = html_report
    context.user_data['results'] = results
    context.user_data['query'] = query
    context.user_data['search_type'] = search_type

    keyboard = [
        [InlineKeyboardButton("Полный отчёт", callback_data="full_report")],
        [InlineKeyboardButton("Повторить", callback_data="repeat")],
        [InlineKeyboardButton("Назад", callback_data="main")]
    ]
    await msg.edit_text(formatted, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def cancel_search(update, context):
    await update.message.reply_text("Поиск отменён.", reply_markup=main_menu())
    return ConversationHandler.END

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

    # Проверяем, есть ли пользователь
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Сначала зарегистрируйтесь через /start")
        return

    # Сбрасываем запросы, если нужно
    reset_daily_queries_if_needed(user_id)

    text = update.message.text.strip()
    if not text:
        return

    # Проверяем лимиты
    success, remaining = use_free_query(user_id)
    if not success:
        await update.message.reply_text(
            "У вас закончились бесплатные запросы на сегодня.\n"
            "Пополните баланс в профиле или подождите завтра."
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
    # Инициализация БД
    init_db()

    # Запуск Flask для uptimerobot
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", lambda u, c: menu_callback(
        Update(u.update_id, message=u.message, callback_query=type('', (), {'data': 'profile', 'from_user': u.effective_user})()),
        c
    )))

    # Обработчики колбэков
    app.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="check_sub"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^(search_form|examples|profile|partner|buy|main|cancel_search)$"))

    # Диалог поиска по неполным данным
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_callback, pattern="^search_form$")],
        states={
            FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_fio)],
            FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_first_name)],
            MIDDLE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_middle_name)],
            BIRTH_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birth_day)],
            BIRTH_MONTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birth_month)],
            BIRTH_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birth_year)],
            AGE_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age_from)],
            AGE_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age_to)],
            BIRTH_PLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birth_place)],
        },
        fallbacks=[CommandHandler('cancel', cancel_search)],
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
