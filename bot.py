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
    filters,
)

# ===============================
# Config da variabili d'ambiente
# ===============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")  # es. -1001234567890
DB_PATH = os.getenv("DB_PATH", "message_links.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # se presente, usa webhook; altrimenti polling
PORT = int(os.getenv("PORT", "8080"))
LISTEN = os.getenv("LISTEN", "0.0.0.0")

if not BOT_TOKEN:
    raise RuntimeError("Imposta la variabile d'ambiente BOT_TOKEN")

# ===============================
# Anti-spam & de-dup
# ===============================
RATE_LIMIT_SEC = 3          # 1 messaggio ogni 3s per utente
DEDUP_WINDOW_SEC = 300      # deduplica identici entro 5 minuti
_last_msg_ts = {}           # user_id -> epoch
_recent_hashes = {}         # user_id -> {hash: ts}

def _msg_fingerprint(msg) -> str:
    text = (msg.text or msg.caption or "")[:1000]
    media_uid = ""
    if msg.photo:
        media_uid = getattr(msg.photo[-1], "file_unique_id", "")
    elif msg.document:
        media_uid = getattr(msg.document, "file_unique_id", "")
    elif msg.video:
        media_uid = getattr(msg.video, "file_unique_id", "")
    elif msg.voice:
        media_uid = getattr(msg.voice, "file_unique_id", "")
    return hashlib.sha1((text + "|" + media_uid).encode("utf-8", "ignore")).hexdigest()

def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    last = _last_msg_ts.get(user_id, 0)
    if now - last < RATE_LIMIT_SEC:
        _last_msg_ts[user_id] = now
        return True
    _last_msg_ts[user_id] = now
    return False

def is_duplicate(user_id: int, fp: str) -> bool:
    now = time.time()
    user_map = _recent_hashes.setdefault(user_id, {})
    # purge
    for h, ts in list(user_map.items()):
        if now - ts > DEDUP_WINDOW_SEC:
            del user_map[h]
    if fp in user_map:
        user_map[fp] = now
        return True
    user_map[fp] = now
    return False

# ===============================
# Tagging automatico (keyword -> tag)
# ===============================
TAG_KEYWORDS = {
    "scarpe": "shoes", "sneaker": "shoes", "jordan": "shoes", "yeezy": "shoes",
    "felpa": "hoodie", "hoodie": "hoodie", "maglia": "top", "tshirt": "top", "t-shirt": "top",
    "borsa": "bag", "bag": "bag", "cintura": "belt", "giacca": "jacket",
    "pantalone": "pants", "jeans": "pants", "orologio": "watch", "collana": "jewelry",
    "balenciaga": "balenciaga", "lv": "louis_vuitton", "louis vuitton": "louis_vuitton",
    "gucci": "gucci", "prada": "prada", "dior": "dior",
}

def infer_tags(text: str) -> list[str]:
    if not text:
        return []
    t = text.lower()
    seen = []
    for kw, tg in TAG_KEYWORDS.items():
        if kw in t and tg not in seen:
            seen.append(tg)
    return seen[:5]

# ===============================
# DB mapping: (target_chat, target_msg) -> (source_chat, source_msg)
# ===============================
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn, closing(conn.cursor()) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                target_chat_id TEXT NOT NULL,
                target_msg_id INTEGER NOT NULL,
                source_chat_id TEXT NOT NULL,
                source_msg_id INTEGER NOT NULL,
                PRIMARY KEY (target_chat_id, target_msg_id)
            )
            """
        )

def save_link(target_chat_id: str, target_msg_id: int, source_chat_id: str, source_msg_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn, closing(conn.cursor()) as cur:
        cur.execute(
            "INSERT OR REPLACE INTO links (target_chat_id, target_msg_id, source_chat_id, source_msg_id) VALUES (?,?,?,?)",
            (str(target_chat_id), int(target_msg_id), str(source_chat_id), int(source_msg_id)),
        )

def lookup_source(target_chat_id: str, target_msg_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn, closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT source_chat_id, source_msg_id FROM links WHERE target_chat_id=? AND target_msg_id=?",
            (str(target_chat_id), int(target_msg_id)),
        )
        row = cur.fetchone()
        return row if row else None

# ===============================
# Album (media group) support
# ===============================
ALBUM_FLUSH_MS = 1500
_album_buf = {}  # key=(src_chat_id, media_group_id) -> list[(message, ts)]

def _to_input_media(m):
    if m.photo:
        return InputMediaPhoto(m.photo[-1].file_id, caption=m.caption_html, parse_mode=ParseMode.HTML)
    if m.video:
        return InputMediaVideo(m.video.file_id, caption=m.caption_html, parse_mode=ParseMode.HTML)
    return None

# ===============================
# Helpers UI
# ===============================
def user_ref(u) -> str:
    if not u:
        return "Utente"
    name = (u.full_name or u.first_name or "Utente").strip()
    return f"{name} (@{u.username})" if u.username else name

def get_cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="Spreadsheet", url="https://www.cravattacinese.com/category/all-products")]]
    )

# ===============================
# Handlers
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Dimmi cosa cerchi e a breve lo caricheremo su CravattaCinese"
    await update.message.reply_text(text, reply_markup=get_cta_keyboard())

async def whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"chat_id: <code>{chat.id}</code>\nchat_type: {chat.type}",
        parse_mode=ParseMode.HTML,
    )

async def flush_album(context: ContextTypes.DEFAULT_TYPE):
    key = context.job.data  # (src_chat_id, media_group_id)
    items = _album_buf.pop(key, [])
    if not items:
        return
    messages = [m for m, _ in sorted(items, key=lambda x: x[1])]
    media = []
    for m in messages:
        im = _to_input_media(m)
        if im:
            media.append(im)
    if not media:
        return
    sent_list = await context.bot.send_media_group(chat_id=TARGET_CHAT_ID, media=media)
    # Collega la prima foto per reply-back
    first = sent_list[0]
    src_chat_id = messages[0].chat_id
    src_msg_id = messages[0].id
    save_link(str(TARGET_CHAT_ID), first.message_id, str(src_chat_id), src_msg_id)

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inoltra ogni messaggio DM al gruppo/canale e salva mappatura per le reply."""
    msg = update.effective_message
    src_chat = update.effective_chat

    # Protezione configurazione
    if not TARGET_CHAT_ID:
        await msg.reply_text("Configurazione mancante: TARGET_CHAT_ID non impostato. Contatta l'amministratore.")
        return

    # Anti-spam
    uid = msg.from_user.id
    if is_rate_limited(uid):
        await msg.reply_text("Rallenta un attimo 🙂 (anti-spam attivo)")
        return
    fp = _msg_fingerprint(msg)
    if is_duplicate(uid, fp):
        await msg.reply_text("Messaggio già ricevuto 👍 (evitiamo duplicati)")
        return

    # Album (media_group): bufferizza e flusha
    if msg.media_group_id:
        key = (src_chat.id, msg.media_group_id)
        _album_buf.setdefault(key, []).append((msg, time.time()))
        context.job_queue.run_once(flush_album, when=ALBUM_FLUSH_MS/1000, data=key)
        await msg.reply_text(
            "Album ricevuto, lo stiamo inoltrando allo staff 🚀",
            reply_markup=get_cta_keyboard()
        )
        return

    # Tagging
    user_txt = (msg.text or msg.caption or "")[:400]
    tags = infer_tags(user_txt)
    tags_line = ("Tags: " + ", ".join(f"#{t}" for t in tags)) if tags else "Tags: #unlabeled"

    # Header informativo
    header = (
        f"\n<b>Nuova richiesta</b>\n"
        f"Da: {user_ref(msg.from_user)}\n"
        f"User ID: <code>{msg.from_user.id}</code>\n"
        f"Chat ID: <code>{src_chat.id}</code>\n"
        f"Ora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{tags_line}"
    )
    try:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=header,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print("Errore invio header:", e)

    # Copia 1:1 il messaggio dell'utente
    try:
        sent = await context.bot.copy_message(
            chat_id=TARGET_CHAT_ID,
            from_chat_id=src_chat.id,
            message_id=msg.id,
        )
        save_link(str(TARGET_CHAT_ID), sent.message_id, str(src_chat.id), msg.id)
    except Exception as e:
        print("Errore copy_message:", e)
        await msg.reply_text("Errore nell'inoltro della richiesta. Riprova tra poco.")
        return

    # Conferma all'utente con CTA
    await msg.reply_text(
        "Dimmi cosa cerchi e a breve lo caricheremo su CravattaCinese",
        reply_markup=get_cta_keyboard()
    )

async def handle_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Se qualcuno risponde nel gruppo/canale al messaggio copiato dal bot, inoltra la risposta all’utente originale."""
    msg = update.effective_message
    if not msg.reply_to_message:
        return

    replied_id = msg.reply_to_message.id
    mapped = lookup_source(str(update.effective_chat.id), int(replied_id))
    if not mapped:
        return  # non è una reply a un messaggio gestito dal bot

    source_chat_id, source_msg_id = mapped

    try:
        await context.bot.copy_message(
            chat_id=source_chat_id,
            from_chat_id=update.effective_chat.id,
            message_id=msg.id,
            reply_to_message_id=source_msg_id,
        )
    except Exception:
        # Fallback solo testo
        if msg.text or msg.caption:
            await context.bot.send_message(
                chat_id=source_chat_id,
                text=f"Risposta dello staff:\n\n{msg.text or msg.caption}",
                reply_to_message_id=source_msg_id,
            )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comando non riconosciuto. Usa /start")

# ===============================
# Main
# ===============================
def main():
    print(
        "DEBUG avvio →",
        "TARGET_CHAT_ID:", repr(TARGET_CHAT_ID),
        "| BOT_TOKEN settato:", bool(BOT_TOKEN),
        "| WEBHOOK_URL:", repr(WEBHOOK_URL)
    )

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Comandi
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whereami", whereami))

    # DM → gruppo/canale
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private))

    # Reply nel gruppo/canale → ritorno all’utente
    app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, handle_group_reply))

    # Catch-all comandi
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # Avvio
    if WEBHOOK_URL:
        app.run_webhook(
            listen=LISTEN,
            port=PORT,
            webhook_url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)

# ===============================
# Main
# ===============================
from dotenv import load_dotenv
load_dotenv()  # CARICA IL .env AUTOMATICAMENTE

def main():
    print(
        "DEBUG avvio →",
        "TARGET_CHAT_ID:", repr(TARGET_CHAT_ID),
        "| BOT_TOKEN settato:", bool(BOT_TOKEN),
        "| WEBHOOK_URL:", repr(WEBHOOK_URL)
    )

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Comandi
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whereami", whereami))

    # DM → gruppo/canale
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private))

    # Reply nel gruppo/canale → ritorno all’utente
    app.add_handler(MessageHandler(filters.REPLY & ~filters.COMMAND, handle_group_reply))

    # Catch-all comandi sconosciuti
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # Avvio
    if WEBHOOK_URL:
        app.run_webhook(
            listen=LISTEN,
            port=PORT,
            webhook_url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

