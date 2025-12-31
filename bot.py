import os
import re
import uuid
import sqlite3
import logging
from datetime import datetime
from contextlib import closing

from dotenv import load_dotenv

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
# LOAD ENV + LOGGING
# =========================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("cravattacinese-bot")

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")          # gruppo staff (supergroup id -100...)
ARCHIVE_CHAT_ID = os.getenv("ARCHIVE_CHAT_ID")        # opzionale (canale/gruppo privato)
DB_PATH = os.getenv("DB_PATH", "message_links.db")

SITE_URL = "www.cravattacinese.com"

if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("Imposta BOT_TOKEN e TARGET_CHAT_ID nel .env")

# =========================
# TEXTS
# =========================
START_TEXT = (
    "✅ <b>Benvenuto!</b>\n\n"
    "📌 <b>Per fare una richiesta corretta</b>:\n"
    "1) Invia <b>una FOTO</b> dell’articolo\n"
    "2) Scrivi una <b>descrizione accurata</b> (marca/modello, colore, taglia, dettagli)\n"
    "3) <b>Budget</b> (opzionale)\n\n"
    "🔔 <b>Per essere notificato</b>:\n"
    "• Devi aver premuto <b>START</b> (ok ✅)\n"
    "• <b>Non silenziare</b> questa chat (attiva notifiche Telegram)\n"
    "• Imposta un <b>@username</b> su Telegram (consigliato)\n\n"
    f"Quando lo troviamo lo carichiamo su <b>{SITE_URL}</b> e ti notifichiamo qui.\n\n"
    "👉 Ora manda <b>FOTO + DESCRIZIONE</b> (anche in due messaggi separati, ti guido io)."
)

AFTER_REQUEST_TEXT = (
    "✅ <b>Richiesta ricevuta!</b>\n\n"
    f"Non appena lo troviamo lo carichiamo su <b>{SITE_URL}</b> e ti notificheremo.\n\n"
    "🔁 Puoi inviare un’altra richiesta quando vuoi: manda un’altra <b>foto + descrizione</b> (budget opzionale)."
)

USER_FOUND_TEXT = (
    f"Il tuo prodotto è stato trovato e caricato su {SITE_URL}"
)

USER_NOTFOUND_TEXT = (
    "❌ Non siamo riusciti a trovarlo al momento.\n"
    "Se vuoi, puoi inviare una nuova richiesta."
)

ASK_FOR_PHOTO_TEXT = (
    "📸 Perfetto! Ora inviami <b>una FOTO</b> dell’articolo.\n"
    "Poi inoltrerò la richiesta allo staff."
)

ASK_FOR_DESCRIPTION_TEXT = (
    "✍️ Perfetto! Ora scrivimi una <b>descrizione accurata</b> (marca/modello, colore, taglia, dettagli).\n"
    "Budget opzionale.\n\n"
    "Poi inoltrerò la richiesta allo staff."
)

# =========================
# REGEX / HELPERS
# =========================
BUDGET_RE = re.compile(r"(?:budget|budg|€)\s*[:\-]?\s*(\d{1,6})(?:\s*€)?", re.IGNORECASE)

def extract_budget(text: str | None):
    if not text:
        return None
    m = BUDGET_RE.search(text)
    return m.group(1) if m else None

def clean_text(text: str | None):
    return (text or "").strip()

def make_staff_keyboard(request_id: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Aggiunto", callback_data=f"added:{request_id}"),
        InlineKeyboardButton("❌ Non trovato", callback_data=f"notfound:{request_id}"),
    ]])

def user_link_html(user_id: int, first_name: str):
    safe_name = (first_name or "User").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

# =========================
# DB
# =========================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(db_connect()) as conn, closing(conn.cursor()) as cur:
        # requests: storico + lock anti-doppio-click
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL,

                staff_chat_id TEXT NOT NULL,
                staff_message_id INTEGER,

                status TEXT NOT NULL DEFAULT 'OPEN',      -- OPEN / ADDED / NOTFOUND
                handled_by INTEGER,
                handled_at TEXT
            )
            """
        )

        # pending: per completare richieste quando l'utente manda prima foto o prima testo
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending (
                user_id INTEGER PRIMARY KEY,
                pending_photo_file_id TEXT,
                pending_text TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

def upsert_pending(user_id: int, photo_file_id: str | None, text: str | None):
    with closing(db_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            INSERT INTO pending (user_id, pending_photo_file_id, pending_text, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                pending_photo_file_id=COALESCE(excluded.pending_photo_file_id, pending.pending_photo_file_id),
                pending_text=COALESCE(excluded.pending_text, pending.pending_text),
                updated_at=excluded.updated_at
            """,
            (user_id, photo_file_id, text, datetime.utcnow().isoformat()),
        )
        conn.commit()

def get_pending(user_id: int):
    with closing(db_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT pending_photo_file_id, pending_text FROM pending WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return None, None
        return row["pending_photo_file_id"], row["pending_text"]

def clear_pending(user_id: int):
    with closing(db_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
        conn.commit()

def save_request(
    request_id: str,
    user_id: int,
    username: str,
    first_name: str,
    staff_chat_id: str,
    staff_message_id: int | None,
):
    with closing(db_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            INSERT OR REPLACE INTO requests
            (request_id, user_id, username, first_name, created_at, staff_chat_id, staff_message_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (
                request_id,
                user_id,
                username,
                first_name,
                datetime.utcnow().isoformat(),
                str(staff_chat_id),
                int(staff_message_id) if staff_message_id else None,
            ),
        )
        conn.commit()

def get_request_row(request_id: str):
    with closing(db_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,))
        return cur.fetchone()

def set_request_status(request_id: str, status: str, handled_by: int):
    with closing(db_connect()) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            UPDATE requests
            SET status = ?, handled_by = ?, handled_at = ?
            WHERE request_id = ?
            """,
            (status, handled_by, datetime.utcnow().isoformat(), request_id),
        )
        conn.commit()

# =========================
# STAFF PERMISSION CHECK
# =========================
async def is_staff_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Consente click solo ad admin nel TARGET_CHAT_ID.
    """
    user = update.effective_user
    if not user:
        return False
    try:
        member = await context.bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        START_TEXT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    DM utenti:
    - Se manda solo testo -> lo salvo e chiedo foto
    - Se manda solo foto -> la salvo e chiedo descrizione
    - Quando ho entrambi -> inoltro allo staff + risposta AFTER_REQUEST_TEXT
    """
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    if not user:
        return

    # SOLO chat privata (evita rumore in staff)
    if update.effective_chat and update.effective_chat.type != "private":
        return

    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""

    incoming_text = clean_text(msg.text if msg.text else msg.caption)

    incoming_photo_file_id = None
    if msg.photo:
        incoming_photo_file_id = msg.photo[-1].file_id  # qualità migliore

    # recupera pending
    pending_photo, pending_text = get_pending(user_id)

    # aggiorna pending con ciò che è arrivato
    if incoming_photo_file_id:
        pending_photo = incoming_photo_file_id
    if incoming_text:
        pending_text = incoming_text

    # Se manca ancora foto -> salva pending e chiedi foto
    if not pending_photo:
        upsert_pending(user_id, None, pending_text)
        await msg.reply_text(ASK_FOR_PHOTO_TEXT, parse_mode=ParseMode.HTML)
        return

    # Se manca ancora descrizione -> salva pending e chiedi descrizione
    if not pending_text:
        upsert_pending(user_id, pending_photo, None)
        await msg.reply_text(ASK_FOR_DESCRIPTION_TEXT, parse_mode=ParseMode.HTML)
        return

    # Ora ho foto + descrizione => creo richiesta
    request_id = uuid.uuid4().hex[:10]
    budget = extract_budget(pending_text)
    description = pending_text

    # Messaggio staff
    user_display = user_link_html(user_id, first_name)
    username_str = f"@{username}" if username else "(no username)"

    lines = []
    lines.append("🆕 <b>NUOVA RICHIESTA</b>")
    lines.append(f"👤 <b>Richiedente:</b> {user_display} {username_str}")
    lines.append(f"🆔 <b>User ID:</b> <code>{user_id}</code>")
    lines.append(f"🔖 <b>Request ID:</b> <code>{request_id}</code>")
    lines.append(f"📝 <b>Descrizione:</b> {description}")
    if budget:
        lines.append(f"💰 <b>Budget:</b> {budget}€")
    lines.append("")
    lines.append("👉 Usa i pulsanti qui sotto per segnare l’esito (il messaggio verrà eliminato).")

    staff_text = "\n".join(lines)
    keyboard = make_staff_keyboard(request_id)

    # invio staff
    try:
        sent = await context.bot.send_photo(
            chat_id=TARGET_CHAT_ID,
            photo=pending_photo,
            caption=staff_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as e:
        log.exception("Errore inoltro staff")
        await msg.reply_text(f"⚠️ Errore inoltro al gruppo staff: {e}")
        return

    # salva DB + pulisci pending
    save_request(
        request_id=request_id,
        user_id=user_id,
        username=username,
        first_name=first_name,
        staff_chat_id=str(TARGET_CHAT_ID),
        staff_message_id=sent.message_id if sent else None,
    )
    clear_pending(user_id)

    # risposta all’utente (ciclo)
    await msg.reply_text(
        AFTER_REQUEST_TEXT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Staff click:
    - Non sporca la chat: niente reply nel gruppo staff
    - Delete del messaggio staff
    - DM all’utente
    """
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    m = re.match(r"^(added|notfound):([a-f0-9]{6,32})$", data)
    if not m:
        await query.answer()
        return

    action, request_id = m.group(1), m.group(2)

    # solo admin
    if not await is_staff_admin(update, context):
        await query.answer("Solo admin.", show_alert=True)
        return

    row = get_request_row(request_id)
    if not row:
        await query.answer("Richiesta non trovata.", show_alert=True)
        return

    # anti doppio click
    if row["status"] != "OPEN":
        await query.answer("Già gestita.", show_alert=False)
        # prova comunque a togliere il messaggio se esiste
        try:
            await context.bot.delete_message(chat_id=TARGET_CHAT_ID, message_id=row["staff_message_id"])
        except Exception:
            pass
        return

    requester_id = int(row["user_id"])
    clicker_id = update.effective_user.id if update.effective_user else 0

    # definisci testo utente
    if action == "added":
        user_text = USER_FOUND_TEXT  # SOLO questo
        new_status = "ADDED"
    else:
        user_text = USER_NOTFOUND_TEXT
        new_status = "NOTFOUND"

    # marca status subito (lock)
    set_request_status(request_id, new_status, handled_by=clicker_id)

    # notifica (DM)
    try:
        await context.bot.send_message(
            chat_id=requester_id,
            text=user_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await query.answer("Utente notificato ✅", show_alert=False)
    except Exception:
        # niente messaggi nel gruppo: solo popup allo staff
        await query.answer("DM fallita: utente non ha premuto START / bot bloccato.", show_alert=True)

    # archivia (opzionale)
    if ARCHIVE_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ARCHIVE_CHAT_ID,
                text=(
                    f"ESITO: {new_status}\n"
                    f"Request ID: {request_id}\n"
                    f"User ID: {requester_id}\n"
                    f"Quando: {datetime.utcnow().isoformat()}Z"
                ),
            )
        except Exception:
            pass

    # chat staff pulita: elimina il messaggio con i bottoni
    try:
        await context.bot.delete_message(chat_id=TARGET_CHAT_ID, message_id=row["staff_message_id"])
    except Exception:
        # se non può eliminare, almeno toglie i bottoni (fallback)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))

    # SOLO DM utenti, niente gruppi
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_user_message))

    log.info("Bot in avvio...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
