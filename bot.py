"""Find Bot — richieste private e presa in carico esclusiva dei seller.

Requisiti: python-telegram-bot >= 20, python-dotenv.
Variabili: BOT_TOKEN, TARGET_CHAT_ID, BEST_FIND_URL (opzionale), DB_PATH (opzionale).
Per Railway, montare un volume persistente e impostare DB_PATH=/data/findbot.sqlite3.
"""
import logging
import os
import re
import sqlite3
import time
import uuid
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("find-request-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip()
BEST_FIND_URL = os.getenv("BEST_FIND_URL", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "findbot.sqlite3")).expanduser()
if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("Configura BOT_TOKEN e TARGET_CHAT_ID nelle variabili d'ambiente")

# Solo richieste non ancora inviate: un riavvio le cancella.
pending = {}
BUDGET_RE = re.compile(
    r"(?:\bbudget\b|\bbudg\b)\s*[:=\-]?\s*(?:€\s*)?(\d{1,6})(?:[.,]\d{1,2})?\s*€?|€\s*(\d{1,6})(?:[.,]\d{1,2})?",
    re.IGNORECASE,
)


def connect_db():
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    return db


def setup_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT,
                group_message_id INTEGER,
                claimed_by INTEGER,
                claimed_at INTEGER,
                created_at INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS seller_sessions (
                seller_id INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_claimed_by ON requests(claimed_by)")


def save_request(request_id, user_id, username):
    with connect_db() as db:
        db.execute(
            "INSERT INTO requests (request_id, user_id, username, created_at) VALUES (?, ?, ?, ?)",
            (request_id, user_id, username, int(time.time())),
        )


def delete_request(request_id):
    with connect_db() as db:
        db.execute("DELETE FROM requests WHERE request_id = ? AND claimed_by IS NULL", (request_id,))


def set_group_message_id(request_id, message_id):
    with connect_db() as db:
        db.execute("UPDATE requests SET group_message_id = ? WHERE request_id = ?", (message_id, request_id))


def get_request(request_id):
