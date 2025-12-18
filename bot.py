import os
import sqlite3
import hashlib
from contextlib import closing
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")  # gruppo staff dove arrivano le richieste
DB_PATH = os.getenv("DB_PATH", "requests.db")

if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("Mancano BOT_TOKEN o TARGET_CHAT_ID nelle variabili d'ambiente")

SITE_URL = "https://cravattacinese.com"

START_PROMPT = (
    f"🔥 <b>DIMMI COSA CERCHI!</b>\n"
    f"Inviami <b>foto</b>, <b>descrizione</b> e <b>budget</b> dell’articolo.\n"
    f"Noi lo caricheremo su <b>CravattaCinese</b>: {SITE_URL}\n\n"
    f"✅ Puoi fare richieste quante volte vuoi."
)

AFTER_REQUEST_PROMPT = (
    f"✅ <b>Richiesta inviata!</b>\n"
    f"Se vuoi, puoi già farne un’altra.\n\n"
    f"{START_PROMPT}"
)

USER_ADDED_MSG = (
    f"✅ <b>AGGIORNAMENTO:</b> il prodotto che hai richiesto è stato "
    f"<b>trovato ed inserito</b> su CravattaCinese: {SITE_URL}\n"
)

USER_NOT_FOUND_MSG = (
    f"❌ <b>AGGIORNAMENTO:</b> per ora <b>non siamo riusciti a trovarlo</b>.\n"
    f"Se vuoi, manda una nuova foto/descrizione/budget per riprovare: {SITE_URL}\n"
)

# =========================
# DB
# =========================
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                req_id TEXT PRIMARY KEY,
                user_chat_id TEXT NOT NULL,
                user_id TEXT,
                username TEXT,
                original_text TEXT,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_messages (
                admin_message_id INTEGER PRIMARY KEY,
                admin_chat_id TEXT NOT NULL,
                req_id TEXT NOT NULL
            )
            """
        )
        conn.commit()


def make_req_id(user_chat_id: int, content: str) -> str:
    base = f"{user_chat_id}|{datetime.utcnow().isoformat()}|{content}".encode("utf-8", "ignore")
    return hashlib.sha256(base).hexdigest()[:16]


def save_request(req_id: str, user_chat_id: int, user_id: int | None, username: str | None, original_text: str):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            INSERT OR REPLACE INTO requests (req_id, user_chat_id, user_id, username, original_text, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (req_id, str(user_chat_id), str(user_id) if user_id else None, username, original_text, datetime.utcnow().isoformat()),
        )
        conn.commit()


def link_admin_message(admin_chat_id: int, admin_message_id: int, req_id: str):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            INSERT OR REPLACE INTO admin_messages (admin_message_id, admin_chat_id, req_id)
            VALUES (?, ?, ?)
            """,
            (admin_message_id, str(admin_chat_id), req_id),
        )
        conn.commit()


def get_req_by_admin_message(admin_chat_id: int, admin_message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            SELECT r.req_id, r.user_chat_id, r.status
            FROM admin_messages a
            JOIN requests r ON r.req_id = a.req_id
            WHERE a.admin_chat_id = ? AND a.admin_message_id = ?
            """,
            (str(admin_chat_id), admin_message_id),
        )
        return cur.fetchone()


def set_status(req_id: str, status: str):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute("UPDATE requests SET status = ? WHERE req_id = ?", (status, req_id))
        conn.commit()


# =========================
# BOT LOGIC
# =========================
def admin_keyboard(req_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Aggiunto", callback_data=f"ok:{req_id}"),
                InlineKeyboardButton("❌ Non trovato", callback_data=f"no:{req_id}"),
            ]
        ]
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        START_PROMPT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def format_user_header(update: Update) -> str:
    u = update.effective_user
    name = (u.full_name or "").strip()
    username = f"@{u.username}" if u.username else "—"
    return f"<b>Richiedente:</b> {name}\n<b>Username:</b> {username}\n<b>User ID:</b> <code>{u.id}</code>"


async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Accettiamo: testo, foto, caption
    chat = update.effective_chat
    user = update.effective_user

    text = ""
    has_photo = False
    file_id = None

    if update.message is None:
        return

    if update.message.photo:
        has_photo = True
        file_id = update.message.photo[-1].file_id
        text = (update.message.caption or "").strip()
    else:
        text = (update.message.text or "").strip()

    if not has_photo and not text:
        await chat.send_message("Mandami una foto oppure una descrizione + budget 🙂")
        await chat.send_message(START_PROMPT, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return

    req_id = make_req_id(chat.id, (text or "photo"))
    save_request(req_id, chat.id, user.id if user else None, user.username if user else None, text)

    # Messaggio per staff
    header = format_user_header(update)
    body = f"{header}\n<b>Request ID:</b> <code>{req_id}</code>\n\n<b>Richiesta:</b>\n{text if text else '📷 (solo foto)'}"

    try:
        if has_photo and file_id:
            admin_msg = await context.bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=file_id,
                caption=body,
                parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard(req_id),
            )
        else:
            admin_msg = await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=body,
                parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard(req_id),
                disable_web_page_preview=True,
            )

        link_admin_message(int(TARGET_CHAT_ID), admin_msg.message_id, req_id)

    except Exception as e:
        await chat.send_message(f"⚠️ Errore inoltro al gruppo staff: {e}")
        return

    # Conferma + “riprompt” immediato
    await chat.send_message(AFTER_REQUEST_PROMPT, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    if ":" not in data:
        return

    action, req_id = data.split(":", 1)

    # Troviamo l'utente legato a quella richiesta (tramite msg admin)
    admin_chat_id = query.message.chat_id if query.message else None
    admin_message_id = query.message.message_id if query.message else None
    if admin_chat_id is None or admin_message_id is None:
        return

    row = get_req_by_admin_message(admin_chat_id, admin_message_id)
    if not row:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    _req_id, user_chat_id, status = row
    if status in ("added", "not_found"):
        # Già gestito: non reinviare
        await query.answer("Già aggiornato.", show_alert=False)
        return

    user_chat_id_int = int(user_chat_id)

    if action == "ok":
        set_status(req_id, "added")
        # Notifica utente
        await context.bot.send_message(
            chat_id=user_chat_id_int,
            text=USER_ADDED_MSG + "\n" + START_PROMPT,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        # Aggiorna messaggio staff
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=admin_chat_id, text=f"✅ Notificato l’utente per Request ID {req_id}")

    elif action == "no":
        set_status(req_id, "not_found")
        await context.bot.send_message(
            chat_id=user_chat_id_int,
            text=USER_NOT_FOUND_MSG + "\n" + START_PROMPT,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=admin_chat_id, text=f"❌ Notificato l’utente (non trovato) per Request ID {req_id}")

    else:
        await query.answer("Azione non valida.", show_alert=False)


async def healthcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("✅ Bot online.")


def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", healthcheck))

    # Pulsanti staff
    app.add_handler(CallbackQueryHandler(on_admin_button))

    # Richieste utenti: testo o foto
    app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT, handle_request))

    print(f"DEBUG avvio → TARGET_CHAT_ID: '{TARGET_CHAT_ID}' | BOT_TOKEN settato: {bool(BOT_TOKEN)}")
    print("Bot in avvio...")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
