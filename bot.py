import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone

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
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip()  # staff group/supergroup id (es. -100...)
DB_PATH = os.getenv("DB_PATH", "message_links.db").strip()  # su Railway puoi lasciarlo così

if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("Imposta le variabili d'ambiente BOT_TOKEN e TARGET_CHAT_ID")

SITE_URL = "https://cravattacinese.com"

WELCOME_TEXT = (
    "🧵 *DIMMI COSA CERCHI!*\n\n"
    "Inviami:\n"
    "📸 una foto (se ce l’hai)\n"
    "📝 descrizione\n"
    "💰 budget\n\n"
    "Noi lo caricheremo su *CravattaCinese*.\n"
    f"🌐 {SITE_URL}"
)

CONFIRM_TEXT = (
    "✅ *Richiesta ricevuta!*\n"
    "Ti aggiorniamo qui appena troviamo il prodotto.\n\n"
    f"🌐 {SITE_URL}"
)

FOUND_TEXT = (
    "✅ *Aggiornamento:* il prodotto è stato trovato e inserito su CravattaCinese!\n"
    f"🌐 {SITE_URL}"
)

NOT_FOUND_TEXT = (
    "❌ *Aggiornamento:* non siamo riusciti a trovare il prodotto.\n"
    "Se vuoi, manda più dettagli o un’altra foto."
)

# =========================
# DB
# =========================
def db_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db_conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
              request_id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              user_chat_id INTEGER NOT NULL,
              username TEXT,
              created_at TEXT NOT NULL,
              description TEXT,
              budget TEXT,
              photo_file_id TEXT,
              staff_chat_id TEXT NOT NULL,
              staff_message_id INTEGER,
              status TEXT DEFAULT 'pending'
            )
            """
        )
        con.commit()

# =========================
# HELPERS
# =========================
BUDGET_RE = re.compile(r"(€\s*\d+[\d.,]*|\d+[\d.,]*\s*€|\b\d{1,6}[\d.,]*\b)", re.IGNORECASE)

def extract_budget(text: str) -> str | None:
    if not text:
        return None
    m = BUDGET_RE.search(text)
    if not m:
        return None
    return m.group(0).strip()

def clean_text(text: str) -> str:
    if not text:
        return ""
    return text.strip()

def keyboard_welcome():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Apri CravattaCinese", url=SITE_URL)],
    ])

def keyboard_after_request():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Apri CravattaCinese", url=SITE_URL)],
        [InlineKeyboardButton("➕ Fai un'altra richiesta", callback_data="new_request")],
    ])

def keyboard_staff(request_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Trovato", callback_data=f"found:{request_id}"),
            InlineKeyboardButton("❌ Non trovato", callback_data=f"notfound:{request_id}"),
        ],
    ])

def staff_message_text(request_id: str, user_id: int, username: str | None, description: str, budget: str):
    u = f"@{username}" if username else "(senza username)"
    return (
        "📩 *NUOVA RICHIESTA*\n"
        f"🆔 Request ID: `{request_id}`\n"
        f"👤 Utente: `{user_id}` {u}\n"
        f"🕒 Ora: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`\n\n"
        f"📝 *Descrizione:*\n{description or '_(manca descrizione)_'}\n\n"
        f"💰 *Budget:* {budget or '_(manca budget)_'}\n"
    )

async def safe_dm(app, chat_id: int, text: str):
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return True
    except Exception:
        return False

# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        WELCOME_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=keyboard_welcome(),
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if data == "new_request":
        await q.message.reply_text("Ok ✅ Inviami *foto, descrizione e budget*.", parse_mode=ParseMode.MARKDOWN)
        return

    # staff decision buttons
    if data.startswith("found:") or data.startswith("notfound:"):
        action, request_id = data.split(":", 1)

        with db_conn() as con:
            row = con.execute(
                "SELECT user_chat_id, status FROM requests WHERE request_id=?",
                (request_id,)
            ).fetchone()

        if not row:
            await q.message.reply_text("⚠️ Request ID non trovato nel database.")
            return

        user_chat_id, status = row
        if status in ("found", "notfound"):
            await q.message.reply_text("ℹ️ Questa richiesta è già stata aggiornata.")
            return

        new_status = "found" if action == "found" else "notfound"
        notify_text = FOUND_TEXT if new_status == "found" else NOT_FOUND_TEXT

        # salva stato
        with db_conn() as con:
            con.execute(
                "UPDATE requests SET status=? WHERE request_id=?",
                (new_status, request_id)
            )
            con.commit()

        # notifica utente in privato
        ok = await safe_dm(context.application, int(user_chat_id), notify_text)

        # aggiorna messaggio staff (edit + nota)
        try:
            suffix = "\n\n✅ *Aggiornato:* TROVATO" if new_status == "found" else "\n\n❌ *Aggiornato:* NON TROVATO"
            await q.message.edit_text(
                q.message.text_markdown_v2 if False else (q.message.text + suffix),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=None,
                disable_web_page_preview=True
            )
        except Exception:
            # se non si può editare, manda solo una reply
            pass

        if ok:
            await q.message.reply_text("📨 Utente notificato in privato.")
        else:
            await q.message.reply_text("⚠️ Non riesco a scrivere all’utente (deve aver avviato il bot in privato).")
        return

async def submit_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Accettiamo richieste SOLO in privato (così non sporchi il gruppo)
    if update.effective_chat.type != "private":
        return

    user = update.effective_user
    user_id = user.id
    username = user.username

    photo_file_id = None
    text = ""

    # Caso foto
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
        text = update.message.caption or ""
    else:
        text = update.message.text or ""

    text = clean_text(text)
    budget = extract_budget(text)
    description = text

    # Se non c'è budget, chiediamo di reinviare
    if not budget:
        await update.effective_chat.send_message(
            "❗ Mi serve anche il *budget* (es: `50€`).\n"
            "Rimanda la richiesta con budget incluso.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    request_id = uuid.uuid4().hex[:10]
    created_at = datetime.now(timezone.utc).isoformat()

    # invia allo staff
    staff_text = staff_message_text(
        request_id=request_id,
        user_id=user_id,
        username=username,
        description=description,
        budget=budget
    )

    sent = None
    try:
        if photo_file_id:
            sent = await context.application.bot.send_photo(
                chat_id=int(TARGET_CHAT_ID),
                photo=photo_file_id,
                caption=staff_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard_staff(request_id),
            )
        else:
            sent = await context.application.bot.send_message(
                chat_id=int(TARGET_CHAT_ID),
                text=staff_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard_staff(request_id),
                disable_web_page_preview=True
            )
    except Exception as e:
        await update.effective_chat.send_message(f"⚠️ Errore inoltro al gruppo staff: {e}")
        return

    staff_message_id = sent.message_id if sent else None

    # salva nel DB
    with db_conn() as con:
        con.execute(
            """
            INSERT INTO requests
            (request_id, user_id, user_chat_id, username, created_at, description, budget, photo_file_id,
             staff_chat_id, staff_message_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                request_id, user_id, update.effective_chat.id, username, created_at,
                description, budget, photo_file_id, TARGET_CHAT_ID, staff_message_id
            )
        )
        con.commit()

    # conferma all'utente (UNA SOLA VOLTA, niente doppioni)
    await update.effective_chat.send_message(
        CONFIRM_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=keyboard_after_request(),
    )

async def admin_found(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # fallback: /trovato <request_id>
    if update.effective_chat.id != int(TARGET_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text("Uso: /trovato <request_id>")
        return
    request_id = context.args[0].strip()
    with db_conn() as con:
        row = con.execute("SELECT user_chat_id, status FROM requests WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        await update.message.reply_text("Request ID non trovato.")
        return
    user_chat_id, status = row
    if status in ("found", "notfound"):
        await update.message.reply_text("Già aggiornato.")
        return
    with db_conn() as con:
        con.execute("UPDATE requests SET status='found' WHERE request_id=?", (request_id,))
        con.commit()
    ok = await safe_dm(context.application, int(user_chat_id), FOUND_TEXT)
    await update.message.reply_text("OK ✅ notificato" if ok else "⚠️ Non posso scrivere all’utente (deve avviare il bot in privato).")

async def admin_notfound(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # fallback: /nontrovato <request_id>
    if update.effective_chat.id != int(TARGET_CHAT_ID):
        return
    if not context.args:
        await update.message.reply_text("Uso: /nontrovato <request_id>")
        return
    request_id = context.args[0].strip()
    with db_conn() as con:
        row = con.execute("SELECT user_chat_id, status FROM requests WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        await update.message.reply_text("Request ID non trovato.")
        return
    user_chat_id, status = row
    if status in ("found", "notfound"):
        await update.message.reply_text("Già aggiornato.")
        return
    with db_conn() as con:
        con.execute("UPDATE requests SET status='notfound' WHERE request_id=?", (request_id,))
        con.commit()
    ok = await safe_dm(context.application, int(user_chat_id), NOT_FOUND_TEXT)
    await update.message.reply_text("OK ✅ notificato" if ok else "⚠️ Non posso scrivere all’utente (deve avviare il bot in privato).")

# =========================
# MAIN
# =========================
def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(CommandHandler("trovato", admin_found))
    app.add_handler(CommandHandler("nontrovato", admin_notfound))

    # richieste utenti: testo o foto, SOLO in privato
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, submit_request))

    print(f"DEBUG avvio → TARGET_CHAT_ID: '{TARGET_CHAT_ID}' | BOT_TOKEN settato: {bool(BOT_TOKEN)}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
