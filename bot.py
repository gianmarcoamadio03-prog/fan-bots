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
    with connect_db() as db:
        return db.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()


def claim_request(request_id, seller_id):
    """Primo claim vince: UPDATE condizionale atomico nel database SQLite."""
    with connect_db() as db:
        result = db.execute(
            "UPDATE requests SET claimed_by = ?, claimed_at = ? "
            "WHERE request_id = ? AND claimed_by IS NULL",
            (seller_id, int(time.time()), request_id),
        )
        won = result.rowcount == 1
        request = db.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        return won, request


def seller_claims(seller_id):
    with connect_db() as db:
        return db.execute(
            "SELECT request_id FROM requests WHERE claimed_by = ? ORDER BY claimed_at DESC LIMIT 5",
            (seller_id,),
        ).fetchall()


def set_seller_session(seller_id, request_id):
    with connect_db() as db:
        db.execute(
            "INSERT INTO seller_sessions (seller_id, request_id) VALUES (?, ?) "
            "ON CONFLICT(seller_id) DO UPDATE SET request_id = excluded.request_id",
            (seller_id, request_id),
        )


def get_seller_session(seller_id):
    with connect_db() as db:
        return db.execute(
            "SELECT request_id FROM seller_sessions WHERE seller_id = ?", (seller_id,)
        ).fetchone()


def clear_seller_session(seller_id):
    with connect_db() as db:
        db.execute("DELETE FROM seller_sessions WHERE seller_id = ?", (seller_id,))


def split_description_and_budget(text):
    text = (text or "").strip()
    match = BUDGET_RE.search(text)
    budget = next((x for x in match.groups() if x), None) if match else None
    return text, budget


def after_request_text():
    reply = (
        "✅ <b>Richiesta ricevuta!</b>\n\n"
        "Un solo seller potrà prenderla in carico. "
        "Se hai uno username Telegram, quel seller potrà vederlo per contattarti; "
        "altrimenti potrà inviarti il link tramite questo bot.\n\n"
        "🔁 Puoi inviare un'altra richiesta quando vuoi con una <b>foto</b>. "
        "Descrizione e budget sono opzionali."
    )
    if BEST_FIND_URL:
        reply += f'\n\n🔗 <a href="{escape(BEST_FIND_URL, quote=True)}">Apri Best Find</a>'
    return reply


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    claims = seller_claims(update.effective_user.id)
    if claims:
        ids = ", ".join(row["request_id"] for row in claims)
        await update.message.reply_text(
            "👋 Hai delle richieste già assegnate: " + ids + "\n"
            "Per inviare un link tramite il bot scrivi /claim seguito dal numero richiesta, "
            "ad esempio /claim " + claims[0]["request_id"]
        )
        return
    await update.message.reply_text(
        "👋 <b>Benvenuto su Find Bot!</b>\n\n"
        "📸 Invia una foto o uno screenshot dell'articolo che cerchi.\n"
        "📝 Descrizione e budget sono opzionali.\n\n"
        "La richiesta apparirà nel gruppo seller senza nome, username o ID. "
        "<b>Il primo seller che la prende in carico</b> potrà vedere il tuo username "
        "Telegram (se lo hai) per scriverti in privato. "
        "Se non hai uno username, potrà inviarti il link attraverso il bot.\n\n"
        "⚠️ Evita di inviare dati personali o screenshot che li mostrano.",
        parse_mode=ParseMode.HTML,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    pending.pop(update.effective_chat.id, None)
    clear_seller_session(update.effective_user.id)
    await update.message.reply_text("Operazione corrente annullata.")


async def seller_claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    if not context.args:
        await update.message.reply_text("Utilizzo: /claim CODICE_RICHIESTA")
        return
    request_id = context.args[0].upper().strip()
    request = get_request(request_id)
    if not request or request["claimed_by"] != update.effective_user.id:
        await update.message.reply_text("Questa richiesta non è assegnata a te.")
        return
    set_seller_session(update.effective_user.id, request_id)
    if request["username"]:
        await update.message.reply_text(
            f"✅ Richiesta {request_id}. Puoi contattare direttamente @{request['username']}.\n"
            "Oppure manda qui un link: lo inoltrerò all'utente tramite FindBot."
        )
    else:
        await update.message.reply_text(
            f"✅ Richiesta {request_id}. L'utente non ha uno username pubblico.\n"
            "Mandami ora il link del prodotto: lo invierò privatamente all'utente tramite FindBot."
        )


async def on_claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    if str(query.message.chat_id) != TARGET_CHAT_ID:
        await query.answer("Pulsante non valido in questa chat.", show_alert=True)
        return
    request_id = (query.data or "").removeprefix("claim:")
    if not re.fullmatch(r"[A-F0-9]{10}", request_id):
        await query.answer("Richiesta non valida.", show_alert=True)
        return
    if update.effective_user.is_bot:
        await query.answer("Operazione non consentita.", show_alert=True)
        return
    try:
        won, request = claim_request(request_id, update.effective_user.id)
    except sqlite3.Error:
        log.exception("Errore database durante claim")
        await query.answer("Errore temporaneo, riprova.", show_alert=True)
        return
    if not request:
        await query.answer("Richiesta non trovata.", show_alert=True)
        return
    if not won:
        if request["claimed_by"] == update.effective_user.id:
            await query.answer("Hai già preso questa richiesta. Apri FindBot e usa /claim " + request_id, show_alert=True)
        else:
            await query.answer("Questa richiesta è già stata presa da un altro seller.", show_alert=True)
        return

    # Il nome dell'utente non viene mai inserito nel messaggio del gruppo.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        log.exception("Impossibile rimuovere il pulsante dopo il claim %s", request_id)

    if request["username"]:
        notice = (
            f"✅ Richiesta {request_id} assegnata a te!\n"
            f"Contatta: https://t.me/{request['username']}\n"
            f"Oppure apri FindBot e usa /claim {request_id}."
        )
    else:
        notice = (
            f"✅ Richiesta {request_id} assegnata a te! L'utente non ha username.\n"
            f"Apri FindBot in privato e invia /claim {request_id} per mandargli il link."
        )
    await query.answer(notice[:200], show_alert=True)
    try:
        await context.bot.send_message(
            chat_id=request["user_id"],
            text=f"🔔 Un seller ha preso in carico la tua richiesta {request_id}. "
            "Potrà contattarti su Telegram o inviarti un link qui tramite FindBot.",
        )
    except TelegramError:
        log.exception("Impossibile notificare l'utente per %s", request_id)


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or update.effective_chat.type != "private":
        return

    seller_id = update.effective_user.id
    session = get_seller_session(seller_id)
    if session:
        request_id = session["request_id"]
        request = get_request(request_id)
        if not request or request["claimed_by"] != seller_id:
            clear_seller_session(seller_id)
            await msg.reply_text("Sessione seller scaduta. Usa /claim CODICE_RICHIESTA.")
            return
        link = (msg.text or "").strip()
        if not re.search(r"https?://\S+", link, flags=re.IGNORECASE):
            await msg.reply_text("Inviami un messaggio di testo contenente il link del prodotto (http:// o https://).")
            return
        try:
            await context.bot.send_message(
                chat_id=request["user_id"],
                text=f"🔗 <b>Un seller ha trovato un articolo per la tua richiesta {request_id}:</b>\n\n{escape(link[:3500])}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError:
            log.exception("Invio link all'utente fallito per %s", request_id)
            await msg.reply_text("⚠️ Invio fallito. Riprova, oppure usa /cancel.")
            return
        clear_seller_session(seller_id)
        await msg.reply_text("✅ Link inviato in privato all'utente.")
        return

    if not msg.photo and not (msg.text or msg.caption):
        await msg.reply_text("Invia una foto o uno screenshot. Descrizione e budget sono opzionali.")
        return

    chat_id = msg.chat_id
    state = pending.get(chat_id, {"photo": None, "description": ""})
    if msg.photo:
        state["photo"] = msg.photo[-1].file_id
    incoming_text = (msg.text or msg.caption or "").strip()
    if incoming_text:
        state["description"] = incoming_text
    pending[chat_id] = state

    if not state["photo"]:
        await msg.reply_text("📸 Ho ricevuto il testo. Ora inviami una foto dell'articolo.")
        return

    description, budget = split_description_and_budget(state["description"])
    request_id = uuid.uuid4().hex[:10].upper()
    caption = f"🔎 <b>NEW REQUEST #{request_id}</b>"
    if description:
        caption += "\n\n📝 <b>Description:</b> " + escape(description)
    else:
        caption += "\n\n📝 <b>Description:</b> Not provided"
    if budget and "budget" not in description.lower() and "budg" not in description.lower():
        caption += f"\n💰 <b>Budget:</b> €{escape(budget)}"

    button = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Prendi in carico (1 seller)", callback_data=f"claim:{request_id}")]
    ])
    try:
        save_request(request_id, update.effective_user.id, update.effective_user.username)
        if len(caption) <= 1024:
            sent = await context.bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=state["photo"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=button,
            )
        else:
            # Evita di tagliare un'entità HTML o un tag a metà.
            # Pubblica prima il testo, poi la foto con il pulsante.
            plain_caption = (
                f"🔎 NEW REQUEST #{request_id}\n\n"
                f"📝 Description: {description or 'Not provided'}"
            )
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=plain_caption[:4000],
            )
            sent = await context.bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=state["photo"],
                reply_markup=button,
            )
        set_group_message_id(request_id, sent.message_id)
    except (TelegramError, sqlite3.Error):
        log.exception("Impossibile pubblicare la richiesta nel gruppo seller")
        delete_request(request_id)
        await msg.reply_text("⚠️ Invio non riuscito. Riprova tra poco.")
        return

    pending.pop(chat_id, None)
    await msg.reply_text(after_request_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def main():
    setup_db()
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("claim", seller_claim_command))
    application.add_handler(CallbackQueryHandler(on_claim_callback, pattern=r"^claim:[A-F0-9]{10}$"))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_user_message))
    log.info("Find Bot avviato; richieste con claim esclusivo e DB %s", DB_PATH)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
