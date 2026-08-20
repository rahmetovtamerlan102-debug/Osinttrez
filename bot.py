#!/usr/bin/env python3
"""
InfoHunt Telegram Bot
С Flask для UptimeRobot, без смайликов, с HTML-отчётом
"""

import os
import re
import logging
import requests
import json
import tempfile
import time
import threading
from urllib.parse import quote
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv

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

SEON_TOKEN = os.getenv("SEON_TOKEN")
SEON_URL = "https://api.seon.io/SeonRestService/phone-api/v2"

SNUSBASE_TOKEN = os.getenv("SNUSBASE_TOKEN")
SNUSBASE_URL = "https://api.snusbase.com/data/search"

LEAKOSINT_BASE = "https://leakosintapi.com/"
TIMEOUT = int(os.getenv("TIMEOUT", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ========== FLASK ДЛЯ UPTIMEROBOT ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return jsonify({"status": "ok", "service": "InfoHunt Bot"})

@flask_app.route('/ping')
def ping():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)), debug=False)

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

# ========== API ФУНКЦИИ ==========
# Jitler
def check_jitler_token(token):
    try:
        resp = requests.get(
            "https://api.jitler.top/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        if not data.get("result"):
            return False
        plan = data.get("plan", {})
        daily = plan.get("daily", {})
        monthly = plan.get("monthly", {})
        parallel = plan.get("parallel_tasks", {})
        daily_limit = daily.get("limit")
        daily_current = daily.get("current")
        monthly_limit = monthly.get("limit")
        monthly_current = monthly.get("current")
        parallel_limit = parallel.get("limit")
        parallel_current = parallel.get("current")
        daily_ok = (daily_limit is None) or (daily_current is not None and daily_current < daily_limit)
        monthly_ok = (monthly_limit is None) or (monthly_current is not None and monthly_current < monthly_limit)
        parallel_ok = (parallel_limit is None) or (parallel_current is not None and parallel_current < parallel_limit)
        return daily_ok and monthly_ok and parallel_ok
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
    global CURRENT_JITLER_TOKEN, LAST_JITLER_CHECK
    token = get_working_jitler_token()
    if not token:
        return {"error": "Нет доступных токенов Jitler"}
    valid_types = {"number": "number", "sherlock": "sherlock", "phone": "number", "telegram_id": "sherlock"}
    jitler_type = valid_types.get(search_type, "sherlock")
    clean_query = str(query).strip()
    if jitler_type == "number":
        clean_query = re.sub(r'\D', '', clean_query)
        if not clean_query:
            return {"error": "Неверный номер телефона"}
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.jitler.top/search",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"type": jitler_type, "query": clean_query, "page": 1},
                timeout=20
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("result"):
                    if "response" in data:
                        return data["response"]
                    if "id" in data:
                        return jitler_wait_result(data["id"], token)
                    if data.get("response") == []:
                        return {"error": "Данные не найдены"}
                    return {"error": "Неожиданный ответ"}
                else:
                    if resp.status_code in [401, 403, 429]:
                        CURRENT_JITLER_TOKEN = None
                        LAST_JITLER_CHECK = 0
                        token = get_working_jitler_token()
                        if not token:
                            return {"error": "Все токены Jitler недоступны"}
                        continue
                    else:
                        return {"error": data.get("error", "Неизвестная ошибка")}
            else:
                if resp.status_code in [401, 403, 429]:
                    CURRENT_JITLER_TOKEN = None
                    LAST_JITLER_CHECK = 0
                    token = get_working_jitler_token()
                    if not token:
                        return {"error": "Все токены Jitler недоступны"}
                    continue
                else:
                    return {"error": f"HTTP {resp.status_code}"}
        except:
            time.sleep(2)
            continue
    return {"error": "Не удалось выполнить запрос"}

def jitler_wait_result(task_id, token, attempts=10):
    for _ in range(attempts):
        try:
            resp = requests.get(f"https://api.jitler.top/search/{task_id}", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            if resp.status_code == 501:
                time.sleep(2)
                continue
            if resp.status_code == 200:
                data = resp.json()
                if data.get("result") and "response" in data:
                    return data["response"]
            return {"error": f"Ошибка {resp.status_code}"}
        except:
            time.sleep(2)
            continue
    return {"error": "Таймаут"}

# LeakOSINT
def check_leakosint_token(token):
    try:
        data = {"token": token, "request": "test", "limit": 10, "lang": "ru"}
        resp = requests.post(LEAKOSINT_BASE, json=data, timeout=10)
        if resp.status_code == 200 and "Error code" not in resp.json():
            return True
        return False
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

def leakosint_search(query, limit=300, retries=3):
    if not query or len(query.strip()) < 1:
        return {"error": "Пустой запрос"}
    token = get_working_leakosint_token()
    if not token:
        return {"error": "Нет доступных токенов LeakOSINT"}
    data = {"token": token, "request": query.strip(), "limit": limit, "lang": "ru"}
    for _ in range(retries):
        try:
            resp = requests.post(LEAKOSINT_BASE, json=data, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                if "Error code" in result:
                    return {"error": f"LeakOSINT: {result['Error code']}"}
                return result
            if resp.status_code == 400:
                wait_match = re.search(r"in (\d+) seconds", resp.text)
                if wait_match:
                    wait_seconds = int(wait_match.group(1)) + 1
                    time.sleep(wait_seconds)
                    continue
                else:
                    time.sleep(30)
                    continue
            else:
                return {"error": f"Ошибка {resp.status_code}"}
        except:
            time.sleep(5)
            continue
    return {"error": "Не удалось выполнить запрос"}

# Depsearch
def depsearch_search(query):
    encoded = quote(str(query))
    url = f"{DEP_BASE}/quest={encoded}&token={DEP_TOKEN}&lang=ru"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        if resp.status_code == 404:
            return {"error": "Данные не найдены"}
        if resp.status_code != 200:
            return {"error": f"Ошибка {resp.status_code}"}
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

# DaData
def dadata_lookup(phone):
    clean = re.sub(r'\D', '', str(phone))
    if not clean or len(clean) < 10:
        return None
    try:
        resp = requests.post(
            "https://dadata.ru/api/v2/clean/phone",
            headers={"Authorization": f"Token {DADATA_TOKEN}", "X-Secret": DADATA_SECRET, "Content-Type": "application/json"},
            json=[clean],
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0 and data[0]:
                return data[0]
        return None
    except:
        return None

# Seon
def seon_lookup(phone):
    clean_phone = re.sub(r'\D', '', str(phone))
    if not clean_phone or len(clean_phone) < 10:
        return None
    headers = {"X-API-KEY": SEON_TOKEN, "Content-Type": "application/json"}
    payload = {"phone": clean_phone}
    try:
        resp = requests.post(SEON_URL, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            return None
    except:
        return None

# Snusbase
def snusbase_search(query):
    clean_query = str(query).strip()
    if not clean_query:
        return {"error": "Пустой запрос"}
    headers = {"Auth": SNUSBASE_TOKEN, "Content-Type": "application/json"}
    payload = {"terms": [clean_query], "types": ["email"], "wildcard": False}
    try:
        resp = requests.post(SNUSBASE_URL, json=payload, headers=headers, timeout=20)
        if resp.status_code == 200:
            return resp.json()
        else:
            return None
    except:
        return None

# Funstat
def funstat_request(endpoint, params=None):
    if not FUNSTAT_TOKEN:
        return None
    url = f"{FUNSTAT_BASE}{endpoint}"
    try:
        resp = requests.get(url, headers={"accept": "application/json", "Authorization": f"Bearer {FUNSTAT_TOKEN}"}, params=params, timeout=10)
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

# ========== УНИВЕРСАЛЬНЫЙ ПОИСК ==========
def unified_search(query, search_type):
    results = {}
    if search_type == "phone":
        clean = clean_phone(query)
        with ThreadPoolExecutor(max_workers=6) as executor:
            jitler_future = executor.submit(jitler_search, clean, "number")
            dep_future = executor.submit(depsearch_search, clean)
            dadata_future = executor.submit(dadata_lookup, clean)
            leak_future = executor.submit(leakosint_search, clean)
            seon_future = executor.submit(seon_lookup, clean)
            results["jitler"] = jitler_future.result(timeout=TIMEOUT) if jitler_future else {"error": "Timeout"}
            results["depsearch"] = dep_future.result(timeout=TIMEOUT) if dep_future else {"error": "Timeout"}
            results["dadata"] = dadata_future.result(timeout=10) if dadata_future else None
            results["leak"] = leak_future.result(timeout=30) if leak_future else {"error": "Timeout"}
            results["seon"] = seon_future.result(timeout=15) if seon_future else None
            results["snusbase"] = None
            results["funstat"] = None
    elif search_type == "telegram_id":
        clean = clean_phone(query)
        with ThreadPoolExecutor(max_workers=4) as executor:
            jitler_future = executor.submit(jitler_search, clean, "sherlock")
            dep_future = executor.submit(depsearch_search, clean)
            funstat_future = executor.submit(funstat_get_user_info, clean)
            leak_future = executor.submit(leakosint_search, clean)
            results["jitler"] = jitler_future.result(timeout=TIMEOUT) if jitler_future else {"error": "Timeout"}
            results["depsearch"] = dep_future.result(timeout=TIMEOUT) if dep_future else {"error": "Timeout"}
            results["funstat"] = funstat_future.result(timeout=15) if funstat_future else None
            results["leak"] = leak_future.result(timeout=30) if leak_future else {"error": "Timeout"}
            results["dadata"] = None
            results["seon"] = None
            results["snusbase"] = None
    elif search_type == "fio":
        with ThreadPoolExecutor(max_workers=4) as executor:
            jitler_future = executor.submit(jitler_search, query, "sherlock")
            dep_future = executor.submit(depsearch_search, query)
            leak_future = executor.submit(leakosint_search, query)
            results["jitler"] = jitler_future.result(timeout=TIMEOUT) if jitler_future else {"error": "Timeout"}
            results["depsearch"] = dep_future.result(timeout=TIMEOUT) if dep_future else {"error": "Timeout"}
            results["leak"] = leak_future.result(timeout=30) if leak_future else {"error": "Timeout"}
            results["dadata"] = None
            results["seon"] = None
            results["snusbase"] = None
            results["funstat"] = None
    elif search_type == "email":
        with ThreadPoolExecutor(max_workers=3) as executor:
            dep_future = executor.submit(depsearch_search, query)
            leak_future = executor.submit(leakosint_search, query)
            snusbase_future = executor.submit(snusbase_search, query)
            results["depsearch"] = dep_future.result(timeout=TIMEOUT) if dep_future else {"error": "Timeout"}
            results["leak"] = leak_future.result(timeout=30) if leak_future else {"error": "Timeout"}
            results["snusbase"] = snusbase_future.result(timeout=20) if snusbase_future else None
            results["jitler"] = None
            results["dadata"] = None
            results["seon"] = None
            results["funstat"] = None
    return results

# ========== ГЕНЕРАЦИЯ HTML-ОТЧЁТА (без ошибок) ==========
def generate_html_report(results, query, search_type):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean = clean_phone(query) if search_type == "phone" else query

    # Подсчёт источников и записей
    sources = 0
    total_records = 0
    if results.get("depsearch") and "results" in results["depsearch"]:
        total_records += len(results["depsearch"]["results"])
        sources += 1
    if results.get("jitler") and isinstance(results["jitler"], dict) and "error" not in results["jitler"]:
        sources += 1
    if results.get("leak") and isinstance(results["leak"], dict) and "error" not in results["leak"]:
        sources += 1
    if results.get("seon"):
        sources += 1
    if results.get("snusbase"):
        sources += 1
    if results.get("funstat"):
        sources += 1

    # Основные данные
    phone_display = ""
    operator = ""
    region = ""
    country = ""
    dadata = results.get("dadata")
    if dadata:
        operator = dadata.get("operator", "")
        region = dadata.get("region", "")
        country = dadata.get("country", "")

    # Личные данные
    full_name = ""
    birth_date = ""
    age = ""
    inn = ""
    address = ""
    dep_data = results.get("depsearch")
    if dep_data and isinstance(dep_data, dict) and "error" not in dep_data:
        recs = dep_data.get("results", [])
        if recs and isinstance(recs[0], dict):
            full_name = recs[0].get("full_name") or recs[0].get("fullname") or ""
            birth_date = recs[0].get("birth_date") or ""
            inn = recs[0].get("inn") or ""
            address = recs[0].get("address") or recs[0].get("city") or recs[0].get("region") or ""
            if birth_date:
                age_val = calc_age(birth_date)
                age = f"{age_val} лет" if age_val is not None else ""

    # Jitler данные
    jitler_phonebooks = []
    jitler_vk = []
    jitler_telegram = []
    jitler_data = results.get("jitler")
    if jitler_data and isinstance(jitler_data, dict) and "error" not in jitler_data:
        if "phonebooks" in jitler_data:
            jitler_phonebooks = jitler_data["phonebooks"][:10]
        if "profiles" in jitler_data:
            for platform, items in jitler_data["profiles"].items():
                if platform.upper() == "VK":
                    for item in items[:5]:
                        if isinstance(item, dict):
                            name = item.get("name", "")
                            url = item.get("url", "")
                            if name:
                                jitler_vk.append({"name": name, "url": url})
        if "telegram" in jitler_data:
            for tg in jitler_data["telegram"][:5]:
                if isinstance(tg, dict):
                    username = tg.get("username", "")
                    tg_id = tg.get("id", "")
                    if username or tg_id:
                        jitler_telegram.append({"username": username, "id": tg_id})

    # LeakOSINT
    leak_lines = []
    leak_data = results.get("leak")
    if leak_data and isinstance(leak_data, dict) and "error" not in leak_data:
        for db_name, db_data in leak_data.get("List", {}).items():
            if db_name != "No results found":
                for item in db_data.get("Data", [])[:5]:
                    for key, value in item.items():
                        if value:
                            leak_lines.append(f"{get_field_name(key)}: {value}")
                break

    # Seon
    seon_lines = []
    seon_data = results.get("seon")
    if seon_data:
        cnam = seon_data.get("cnam_details", {})
        if cnam and cnam.get("name"):
            seon_lines.append(f"Владелец (CNAM): {cnam.get('name')}")
        if seon_data.get("score") is not None:
            seon_lines.append(f"Риск: {seon_data.get('score')}")
        if seon_data.get("email"):
            seon_lines.append(f"Email: {seon_data.get('email')}")

    # Snusbase (только для email)
    snusbase_lines = []
    if search_type == "email":
        snusbase_data = results.get("snusbase")
        if snusbase_data and isinstance(snusbase_data, dict):
            for db_name, records in snusbase_data.get("results", {}).items():
                if records:
                    snusbase_lines.append(f"База: {db_name}")
                    for rec in records[:5]:
                        for key, value in rec.items():
                            if value and key != '_domain':
                                snusbase_lines.append(f"  {get_field_name(key)}: {value}")
                    break

    # Funstat
    funstat_lines = []
    if search_type == "telegram_id":
        funstat_data = results.get("funstat")
        if funstat_data:
            names = funstat_data.get("names")
            if names:
                funstat_lines.append("Имена:")
                for item in names[:5]:
                    name = item.get("name", "")
                    date = item.get("date_time", "")
                    if name:
                        date_str = format_date(date) if date else ""
                        funstat_lines.append(f"  {name} {date_str}")
            usernames = funstat_data.get("usernames")
            if usernames:
                funstat_lines.append("Username:")
                for item in usernames[:5]:
                    uname = item.get("name", "")
                    date = item.get("date_time", "")
                    if uname:
                        date_str = format_date(date) if date else ""
                        funstat_lines.append(f"  @{uname} {date_str}")
            gifts = funstat_data.get("gifts")
            if gifts:
                funstat_lines.append("Подарки:")
                for item in gifts[:5]:
                    from_name = item.get("from_first_name", "")
                    to_name = item.get("to_first_name", "")
                    date = item.get("last_gift_date", "")
                    if from_name and to_name:
                        funstat_lines.append(f"  {from_name} -> {to_name} {format_date(date) if date else ''}")

    # Формируем HTML с экранированием фигурных скобок
    html_template = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InfoHunt отчёт</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #0f0f1a;
            color: #e0e0e0;
            font-family: 'Segoe UI', system-ui, sans-serif;
            padding: 20px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            background: #1a1a2e;
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.6);
        }}
        h1 {{
            font-size: 24px;
            font-weight: 700;
            color: #a78bfa;
            border-bottom: 2px solid #2d2d44;
            padding-bottom: 12px;
            margin-bottom: 20px;
        }}
        .meta {{
            display: flex;
            justify-content: space-between;
            font-size: 13px;
            color: #8888aa;
            margin-bottom: 20px;
        }}
        .section {{
            background: #12121f;
            border-radius: 10px;
            padding: 16px;
            margin-bottom: 16px;
            border: 1px solid #2a2a40;
        }}
        .section-title {{
            font-size: 16px;
            font-weight: 600;
            color: #c4b5fd;
            margin-bottom: 10px;
        }}
        .row {{
            display: flex;
            padding: 4px 0;
            border-bottom: 1px solid #1e1e30;
        }}
        .row:last-child {{
            border-bottom: none;
        }}
        .label {{
            color: #8b94a8;
            min-width: 140px;
            font-weight: 500;
        }}
        .value {{
            color: #e6edf3;
            word-break: break-word;
        }}
        .badge {{
            display: inline-block;
            background: #2d2d44;
            border-radius: 12px;
            padding: 2px 12px;
            font-size: 12px;
            color: #a78bfa;
            margin-right: 6px;
        }}
        .stat-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
            gap: 12px;
            margin-top: 8px;
        }}
        .stat-item {{
            background: #0f0f1a;
            border-radius: 8px;
            padding: 10px;
            text-align: center;
        }}
        .stat-number {{
            font-size: 22px;
            font-weight: 700;
            color: #a78bfa;
        }}
        .stat-label {{
            font-size: 11px;
            color: #8888aa;
        }}
        .list-item {{
            padding: 4px 0;
            border-bottom: 1px solid #1e1e30;
        }}
        .list-item:last-child {{
            border-bottom: none;
        }}
        .url-link {{
            color: #60a5fa;
            text-decoration: none;
        }}
        .url-link:hover {{
            text-decoration: underline;
        }}
        .footer {{
            margin-top: 24px;
            padding-top: 12px;
            border-top: 1px solid #2d2d44;
            font-size: 12px;
            color: #666688;
            text-align: center;
        }}
    </style>
</head>
<body>
<div class="container">
    <h1>InfoHunt отчёт</h1>
    <div class="meta">
        <span>Запрос: {search_type} – {clean}</span>
        <span>{now}</span>
    </div>

    <div class="section">
        <div class="section-title">Основная информация</div>
        <div class="row"><span class="label">Тип</span><span class="value">{search_type}</span></div>
        <div class="row"><span class="label">Запрос</span><span class="value">{clean}</span></div>
        {operator_row}
        {region_row}
        {country_row}
    </div>

    {personal_section}

    {jitler_section}

    {leak_section}

    {seon_section}

    {snusbase_section}

    {funstat_section}

    <div class="section">
        <div class="section-title">Статистика</div>
        <div class="stat-grid">
            <div class="stat-item"><div class="stat-number">{sources}</div><div class="stat-label">Источников</div></div>
            <div class="stat-item"><div class="stat-number">{total_records}</div><div class="stat-label">Всего записей</div></div>
        </div>
    </div>

    <div class="footer">InfoHunt • {now}</div>
</div>
</body>
</html>"""

    # Подстановка динамических блоков
    operator_row = f'<div class="row"><span class="label">Оператор</span><span class="value">{operator}</span></div>' if operator else ''
    region_row = f'<div class="row"><span class="label">Регион</span><span class="value">{region}</span></div>' if region else ''
    country_row = f'<div class="row"><span class="label">Страна</span><span class="value">{country}</span></div>' if country else ''

    personal_section = ""
    if full_name or birth_date or inn or address:
        personal_section = f"""
    <div class="section">
        <div class="section-title">Личные данные</div>
        {f'<div class="row"><span class="label">ФИО</span><span class="value">{full_name}</span></div>' if full_name else ''}
        {f'<div class="row"><span class="label">Дата рождения</span><span class="value">{birth_date}</span></div>' if birth_date else ''}
        {f'<div class="row"><span class="label">Возраст</span><span class="value">{age}</span></div>' if age else ''}
        {f'<div class="row"><span class="label">ИНН</span><span class="value">{inn}</span></div>' if inn else ''}
        {f'<div class="row"><span class="label">Адрес</span><span class="value">{address}</span></div>' if address else ''}
    </div>"""

    jitler_section = ""
    if jitler_phonebooks or jitler_vk or jitler_telegram:
        jitler_section = f"""
    <div class="section">
        <div class="section-title">Jitler данные</div>"""
        if jitler_phonebooks:
            jitler_section += f'<div class="row"><span class="label">Телефонные книги</span><span class="value">{", ".join(jitler_phonebooks)}</span></div>'
        if jitler_vk:
            vk_links = "<br>".join([f"{item['name']} (<a href='{item['url']}' class='url-link'>{item['url']}</a>)" for item in jitler_vk if item.get('name')])
            jitler_section += f'<div class="row"><span class="label">VK</span><span class="value">{vk_links}</span></div>'
        if jitler_telegram:
            tg_links = "<br>".join([f"{item['username']} (ID: {item['id']})" for item in jitler_telegram if item.get('username')])
            jitler_section += f'<div class="row"><span class="label">Telegram</span><span class="value">{tg_links}</span></div>'
        jitler_section += """
    </div>"""

    leak_section = ""
    if leak_lines:
        leak_section = f"""
    <div class="section">
        <div class="section-title">LeakOSINT (утечки)</div>
        {"<br>".join([f'<div class="row"><span class="label">•</span><span class="value">{line}</span></div>' for line in leak_lines])}
    </div>"""

    seon_section = ""
    if seon_lines:
        seon_section = f"""
    <div class="section">
        <div class="section-title">Seon (цифровой след)</div>
        {"<br>".join([f'<div class="row"><span class="label">•</span><span class="value">{line}</span></div>' for line in seon_lines])}
    </div>"""

    snusbase_section = ""
    if snusbase_lines:
        snusbase_section = f"""
    <div class="section">
        <div class="section-title">Snusbase (утечки)</div>
        {"<br>".join([f'<div class="row"><span class="label">•</span><span class="value">{line}</span></div>' for line in snusbase_lines])}
    </div>"""

    funstat_section = ""
    if funstat_lines:
        funstat_section = f"""
    <div class="section">
        <div class="section-title">Funstat (история)</div>
        {"<br>".join([f'<div class="row"><span class="label">•</span><span class="value">{line}</span></div>' for line in funstat_lines])}
    </div>"""

    # Собираем HTML
    html = html_template.format(
        search_type=search_type,
        clean=clean,
        now=now,
        operator_row=operator_row,
        region_row=region_row,
        country_row=country_row,
        personal_section=personal_section,
        jitler_section=jitler_section,
        leak_section=leak_section,
        seon_section=seon_section,
        snusbase_section=snusbase_section,
        funstat_section=funstat_section,
        sources=sources,
        total_records=total_records
    )
    return html

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
        lines.append(f"ID Telegram: {clean_phone(query)}")
    elif search_type == "email":
        lines.append(f"Email: {query}")
    else:
        lines.append(f"Запрос: {query}")

    # Личные данные
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

    # Источники и записи
    sources = 0
    total_records = 0
    if results.get("depsearch") and "results" in results["depsearch"]:
        total_records += len(results["depsearch"]["results"])
        sources += 1
    if results.get("jitler") and isinstance(results["jitler"], dict) and "error" not in results["jitler"]:
        sources += 1
    if results.get("leak") and isinstance(results["leak"], dict) and "error" not in results["leak"]:
        sources += 1
    if results.get("seon"):
        sources += 1
    if results.get("snusbase"):
        sources += 1
    if results.get("funstat"):
        sources += 1

    lines.append(f"\nИсточников: {sources}")
    lines.append(f"Всего записей: {total_records}")

    return "\n".join(lines)

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
    if update.message:
        await update.message.reply_text(
            f"Для использования бота подпишитесь на канал: {REQUIRED_CHANNEL}\nПосле подписки нажмите Проверить подписку.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.edit_message_text(
            f"Для использования бота подпишитесь на канал: {REQUIRED_CHANNEL}\nПосле подписки нажмите Проверить подписку.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ========== ГЛАВНОЕ МЕНЮ ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("Поиск по неполным данным", callback_data="search")],
        [InlineKeyboardButton("Примеры использования сервиса", callback_data="examples")],
        [InlineKeyboardButton("Мой аккаунт", callback_data="account")],
        [InlineKeyboardButton("Партнёрская программа", callback_data="partner")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== ОБРАБОТЧИКИ ==========
async def start(update, context):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await require_subscription(update, context)
        return
    await update.message.reply_text(
        "Приветствую! ты попал в бота кумова.\n\n.\n\n.\n\nтут ты сможешь найти инфор#ацию о своем обидчике.\n\n.\n\n.\n\nудачного поиска!",
        reply_markup=main_menu()
    )

async def check_subscription_callback(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if await is_subscribed(user_id, context):
        await query.edit_message_text("Подписка подтверждена! Выберите действие:", reply_markup=main_menu())
    else:
        await query.edit_message_text(
            "Вы ещё не подписаны. Подпишитесь и нажмите Проверить подписку снова.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")],
                [InlineKeyboardButton("Проверить подписку", callback_data="check_sub")]
            ])
        )

async def menu_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "search":
        await query.edit_message_text(
            "Выберите тип поиска:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("По номеру телефона", callback_data="search_phone")],
                [InlineKeyboardButton("По ID Telegram", callback_data="search_telegram")],
                [InlineKeyboardButton("По ФИО", callback_data="search_fio")],
                [InlineKeyboardButton("По email", callback_data="search_email")],
                [InlineKeyboardButton("Назад", callback_data="main")]
            ])
        )
    elif data == "examples":
        await query.edit_message_text(
            "Примеры использования:\n\n"
            "1. Номер телефона: 79271234567\n"
            "2. ID Telegram: 123456789\n"
            "3. ФИО: Иванов Иван Иванович\n"
            "4. Email: example@gmail.com\n\n"
            "После выбора типа поиска отправьте данные."
        )
    elif data == "account":
        user_id = query.from_user.id
        await query.edit_message_text(
            f"Ваш ID: {user_id}\nБаланс запросов: 0\nПодписка: активна",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main")]])
        )
    elif data == "partner":
        await query.edit_message_text(
            "Партнёрская программа:\n\n"
            "Приглашайте друзей и получайте бонусы.\n"
            "Ваша реферальная ссылка: https://t.me/ваш_бот?start=ref_123",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main")]])
        )
    elif data == "main":
        await query.edit_message_text("Главное меню:", reply_markup=main_menu())

async def search_type_callback(update, context):
    query = update.callback_query
    await query.answer()
    search_type = query.data.replace("search_", "")
    context.user_data['search_type'] = search_type
    await query.edit_message_text(f"Введите данные для поиска по {search_type}.\n(например: 79271234567)")

async def handle_message(update, context):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await require_subscription(update, context)
        return

    text = update.message.text.strip()
    if not text:
        return

    search_type = context.user_data.get('search_type')
    if not search_type:
        await update.message.reply_text("Сначала выберите тип поиска в меню.")
        return

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
    await update.message.reply_text(formatted, reply_markup=InlineKeyboardMarkup(keyboard))

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
        await query.edit_message_text("Нет данных для повтора. Начните новый поиск.", reply_markup=main_menu())
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

# ========== MAIN ==========
def main():
    # Запускаем Flask в отдельном потоке для UptimeRobot
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Запускаем Telegram бота
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="check_sub"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^(search|examples|account|partner|main)$"))
    app.add_handler(CallbackQueryHandler(search_type_callback, pattern="^search_"))
    app.add_handler(CallbackQueryHandler(full_report_callback, pattern="full_report"))
    app.add_handler(CallbackQueryHandler(repeat_callback, pattern="repeat"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
