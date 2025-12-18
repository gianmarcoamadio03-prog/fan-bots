import os
import time
import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ReactionHandler,
    filters,
)

# ===============================
# CONFIG
# ===============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
DB_PATH = os.getenv("DB_PATH", "message_links.db")

if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("BOT_TOKEN o TARGET_CHAT_ID mancanti")

# ===============================
# DATABASE
# ===============================
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn, closing(conn.cursor()) as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS links (
                target_chat_id TEXT,
                target_msg_id INTEGER,
                source_chat_id TEXT,
                source_msg_id INTEGER,
                PRIMARY KEY (target_chat_id, target_msg_id)
            )
        """)

def save_link(t_chat, t_msg, s_chat, s_msg):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn, closing(conn.cursor()) as cur:
        cur.execute(
            "INSERT OR REPLACE INTO links VALUES (?,?,?,?)",
            (str(t_chat), int(t_msg), str(s_chat), int(s_msg))
        )

def lookup_source(t_chat, t_msg):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT source_chat_id, source_msg_id FROM links WHERE target_chat_id=? AND target_msg_id=?",
            (str(t_chat), int(t_msg))
        )
        return cur.fetchone()

# ===============================
# UI
# ===============================
def get_cta_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Vai alla Spreadsheet", url="https://www.cravattacinese.com")]]
    )

# ===============================
# START COMMAND
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "DIMMI COSA CERCHI!\n\n"
        "📸 Invia una FOTO\n"
        "📝 Una DESCRIZIONE\n"
        "💰 Il tuo BUDGET\n\n"
        "E noi lo caricheremo su CravattaCinese.com"
    )
    await update.message.reply_text(text, reply_markup=get_cta_keyboard())

# ===============================
# PRIVATE MESSAGE HANDLER
# ===============================
async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user

    header = (
        f"<b>NUOVA RICHIESTA FIND</b>\n"
        f"👤 {user.full_name}\n"
        f"🆔 <code>{user.id}</code>\n"
        f"🕒 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )

    await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text=header,
        parse_mode=ParseMode.HTML
    )

    sent = await context.bot.copy_message(
        chat_id=TARGET_CHAT_ID,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id
    )

    save_link(TARGET_CHAT_ID, sent.message_id, msg.chat_id, msg.message_id)

    await msg.reply_text(
        "Richiesta ricevuta ✅\n"
        "Ti aggiorneremo appena troviamo il prodotto.",
        reply_markup=get_cta_keyboard()
    )

# ===============================
# REACTION HANDLER ❤️ / 👎
# ===============================
async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction = update.message_reaction
    if not reaction:
        return

    mapped = lookup_source(str(reaction.chat.id), reaction.message_id)
    if not mapped:
        return

    source_chat_id, source_msg_id = mapped
    emojis = [r.emoji for r in reaction.new_reaction]

    if "❤️" in emojis:
        text = (
            "❤️ *PRODOTTO TROVATO!*\n\n"
            "Il prodotto che cercavi è stato trovato ed inserito "
            "nella spreadsheet su *CravattaCinese.com*."
        )
    elif "👎" in emojis:
        text = (
            "👎 *PRODOTTO NON TROVATO*\n\n"
            "Al momento non siamo riusciti a trovare il prodotto richiesto."
        )
    else:
        return

    await context.bot.send_message(
        chat_id=source_chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_to_message_id=source_msg_id
    )

# ===============================
# MAIN
# ===============================
def main():
    print(
        "DEBUG avvio →",
        "TARGET_CHAT_ID:", TARGET_CHAT_ID,
        "| BOT_TOKEN settato:", bool(BOT_TOKEN)
    )

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private))
    app.add_handler(ReactionHandler(handle_reaction))

    app.run_polling()

if __name__ == "__main__":
    main()
