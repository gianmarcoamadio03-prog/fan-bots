import os
import re
import uuid
import sqlite3
from datetime import datetime
from contextlib import closing
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
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")  # gruppo staff (supergroup id -100...)
DB_PATH = os.getenv("DB_PATH", "message_links.db")

if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("Imposta le variabili d'ambiente BOT_TOKEN e TARGET_CHAT_ID")

SITE_URL = "https://cravattacinese.com"

START_TEXT = (
    "🔥 <b>DIMMI COSA CERCHI!</b>\n\n"
    "📸 Inviami <b>una foto</b> oppure ✍️ <b>una descrizione</b>.\n"
    "💰 Il <b>budget è opzionale</b> (se vuoi scrivilo tipo: <i>budget 50€</i>).\n\n"
    f"🌐 Noi lo caricheremo su: {SITE_URL}\n"
)

AFTER_REQUEST_TEXT = (
    "✅ <b>Richiesta ricevuta!</b>\n"
    "Ti aggiorneremo appena troviamo il prodotto.\n\n"
    f"🔁 Puoi inviare un’altra richiesta quando vuoi.\n"
    f"🌐 {SITE_URL}"
)

# =========================
# DB
# =========================
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL,
                staff_chat_id TEXT NOT NULL,
                staff_message_id INTEGER
            )
            """
        )
        conn.commit()


def save_request(request_id: str, user_id: int, username: str, first_name: str, staff_chat_id: str, staff_message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            INSERT OR REPLACE INTO requests
            (request_id, user_id, username, first_name, created_at, staff_chat_id, staff_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
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


def get_requester_user_id(request_id: str):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT user_id FROM requests WHERE request_id = ?", (request_id,))
        row = cur.fetchone()
        return row[0] if row else None


def mark_staff_message_id(request_id: str, staff_message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "UPDATE requests SET staff_message_id = ? WHERE request_id = ?",
            (int(staff_message_id), request_id),
        )
        conn.commit()


# =========================
# HELPERS
# =========================
BUDGET_RE = re.compile(r"(?:budget|budg|€)\s*[:\-]?\s*(\d{1,6})(?:\s*€)?", re.IGNORECASE)

def extract_budget(text: str | None):
    if not text:
        return None
    m = BUDGET_RE.search(text)
    if not m:
        return None
    return m.group(1)

def clean_text(text: str | None):
    if not text:
        return ""
    return text.strip()

def make_staff_keyboard(request_id: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Aggiunto", callback_data=f"added:{request_id}"),
        InlineKeyboardButton("❌ Non trovato", callback_data=f"notfound:{request_id}"),
    ]])


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        START_TEXT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Accetta:
    - solo foto (anche senza caption)
    - solo testo
    - foto + testo
    Budget opzionale dentro al testo/caption
    """
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    if not user:
        return

    # Prendi testo o caption (se c'è foto)
    text = msg.text if msg.text else msg.caption
    text = clean_text(text)

    budget = extract_budget(text)
    # descrizione: testo senza obbligo budget
    description = text

    # Se non c'è né testo né foto → ignora (es sticker, ecc.)
    has_photo = bool(msg.photo)
    has_text = bool(description)

    if not has_photo and not has_text:
        # non rispondo per non fare rumore su sticker/altro
        return

    request_id = uuid.uuid4().hex[:10]

    # Costruisci messaggio staff
    username = f"@{user.username}" if user.username else "(no username)"
    first_name = user.first_name or ""
    user_id = user.id

    lines = []
    lines.append("🆕 <b>NUOVA RICHIESTA</b>")
    lines.append(f"👤 <b>Richiedente:</b> {first_name} {username}")
    lines.append(f"🆔 <b>User ID:</b> <code>{user_id}</code>")
    lines.append(f"🔖 <b>Request ID:</b> <code>{request_id}</code>")
    if description:
        lines.append(f"📝 <b>Descrizione:</b> {description}")
    if budget:
        lines.append(f"💰 <b>Budget:</b> {budget}€")
    lines.append("")
    lines.append("👉 Usa i pulsanti qui sotto per segnare l’esito.")

    staff_text = "\n".join(lines)

    keyboard = make_staff_keyboard(request_id)

    # Inoltro a staff: se c'è foto invia foto + caption, altrimenti testo
    try:
        if has_photo:
            photo_file_id = msg.photo[-1].file_id  # migliore qualità
            sent = await context.bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=photo_file_id,
                caption=staff_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            sent = await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=staff_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except Exception as e:
        await msg.reply_text(f"⚠️ Errore inoltro al gruppo staff: {e}")
        return

    # salva su DB per poter notificare poi l'utente
    save_request(
        request_id=request_id,
        user_id=user_id,
        username=user.username or "",
        first_name=first_name,
        staff_chat_id=str(TARGET_CHAT_ID),
        staff_message_id=sent.message_id if sent else None
    )

    # Risposta unica (non doppia) all’utente
    await msg.reply_text(
        AFTER_REQUEST_TEXT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Gestisce i click dei bottoni staff:
    - Notifica in privato l'utente
    - Rimuove i bottoni dal messaggio staff e aggiunge un esito
    """
    query = update.callback_query
    if not query:
        return

    await query.answer()  # fondamentale: rende il click "attivo"

    data = query.data or ""
    m = re.match(r"^(added|notfound):([a-f0-9]{6,32})$", data)
    if not m:
        return

    action, request_id = m.group(1), m.group(2)

    requester_id = get_requester_user_id(request_id)
    if not requester_id:
        await query.message.reply_text("⚠️ Non trovo l’utente collegato a questa richiesta.")
        return

    if action == "added":
        user_text = (
            "✅ <b>Aggiornamento:</b> il prodotto è stato <b>AGGIUNTO</b> su CravattaCinese!\n"
            f"🌐 {SITE_URL}"
        )
        staff_tag = "✅ Segnato come: AGGIUNTO"
    else:
        user_text = (
            "❌ <b>Aggiornamento:</b> non siamo riusciti a trovarlo.\n"
            "Se vuoi, puoi fare una nuova richiesta.\n"
            f"🌐 {SITE_URL}"
        )
        staff_tag = "❌ Segnato come: NON TROVATO"

    # Prova a notificare in DM l'utente
    notified = True
    try:
        await context.bot.send_message(
            chat_id=requester_id,
            text=user_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        notified = False
        await query.message.reply_text(f"⚠️ Non riesco a notificare l’utente in privato (deve avviare il bot in DM): {e}")

    # Aggiorna messaggio staff: toglie bottoni e mette esito (estetico e chiaro)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except:
        pass

    try:
        await query.message.reply_text(f"{staff_tag}\n🔔 Utente notificato: {'sì' if notified else 'no'}")
    except:
        pass


def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    # Bottoni staff
    app.add_handler(CallbackQueryHandler(on_button))

    # Messaggi utenti (foto/testo)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_user_message))

    print("Bot in avvio...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
