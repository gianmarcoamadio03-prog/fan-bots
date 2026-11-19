"""FindBot — invia richieste al gruppo seller con username pubblico.

Dipendenze: python-telegram-bot >= 20, python-dotenv.
Variabili obbligatorie: BOT_TOKEN, TARGET_CHAT_ID.
Variabile opzionale: BEST_FIND_URL.

Ogni seller può contattare l'utente; NON esiste assegnazione esclusiva.
"""
import logging
import os
import re
from html import escape

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("find-request-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip()
BEST_FIND_URL = os.getenv("BEST_FIND_URL", "").strip()
if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("Configura BOT_TOKEN e TARGET_CHAT_ID nelle variabili d'ambiente")

# Le richieste non ancora pubblicate restano in RAM.
pending = {}
BUDGET_RE = re.compile(
    r"(?:\bbudget\b|\bbudg\b)\s*[:=\-]?\s*(?:€\s*)?(\d{1,6})(?:[.,]\d{1,2})?\s*€?|€\s*(\d{1,6})(?:[.,]\d{1,2})?",
    re.IGNORECASE,
)


def username_required_text():
    return (
        "🔒 <b>Per inviare una richiesta devi impostare uno username Telegram.</b>\n\n"
        "Vai su <b>Telegram → Impostazioni → Nome utente</b>, scegli uno username "
        "(per esempio @nomeutente) e salva. Poi torna qui e invia la foto.\n\n"
        "📢 <b>Importante:</b> quando invii la richiesta, il tuo @username sarà "
        "visibile a <b>tutti i seller del gruppo</b>, che potranno contattarti in privato. "
        "Non ci sarà un limite di un solo seller."
    )


def after_request_text():
    reply = (
        "✅ <b>Richiesta ricevuta!</b>\n\n"
        "📢 Il tuo username Telegram è visibile ai seller del gruppo. "
        "<b>Più seller possono contattarti in privato</b> per proporti il link dell'articolo.\n\n"
        "🔁 Puoi inviare un'altra richiesta quando vuoi con una <b>foto</b>. "
        "Descrizione e budget sono opzionali."
    )
    if BEST_FIND_URL:
        reply += f'\n\n🔗 <a href="{escape(BEST_FIND_URL, quote=True)}">Apri Best Find</a>'
    return reply


def extract_budget(text):
    match = BUDGET_RE.search(text or "")
    return next((part for part in match.groups() if part), None) if match else None


def build_caption(username, description, budget):
    user = escape(username)
    contact_link = f"https://t.me/{username}"
    # Lo username e il link di contatto sono intenzionalmente pubblici ai seller.
    header = (
        "🔎 <b>NEW REQUEST</b>\n\n"
        f'👤 <b>Utente:</b> <a href="{contact_link}">@{user}</a>\n'
    )
    desc_line = "📝 <b>Description:</b> "
    if not description:
        return header + desc_line + "Not provided"
    # Il budget resta nella descrizione scritta dall'utente; niente duplicazioni.
    # Budget è mantenuto come dato calcolato per possibili estensioni future.
    _ = budget
    return header + desc_line + escape(description)


def contact_keyboard(username):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Contatta l'utente", url=f"https://t.me/{username}")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    if not update.effective_user.username:
        await update.message.reply_text(username_required_text(), parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        "👋 <b>Benvenuto su FindBot!</b>\n\n"
        "📸 Invia una foto o uno screenshot dell'articolo che cerchi.\n"
        "📝 Descrizione e budget sono opzionali.\n\n"
        "📢 <b>Attenzione:</b> nel gruppo seller sarà pubblicato il tuo "
        "<b>@username Telegram</b> insieme alla richiesta. Tutti i seller del "
        "gruppo potranno aprire la tua chat e contattarti privatamente.\n\n"
        "⚠️ Evita di inviare dati personali nelle foto o nella descrizione.",
        parse_mode=ParseMode.HTML,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not update.message:
        return
    pending.pop(update.effective_chat.id, None)
    await update.message.reply_text("Richiesta incompleta annullata. Puoi inviarne una nuova.")


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or update.effective_chat.type != "private":
        return

    # Verifica lo username per OGNI richiesta, non solo quando si usa /start.
    username = (update.effective_user.username or "").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        pending.pop(msg.chat_id, None)
        await msg.reply_text(username_required_text(), parse_mode=ParseMode.HTML)
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
        await msg.reply_text(
            "📸 Ho ricevuto il testo. Ora inviami una foto dell'articolo; "
            "puoi aggiungere la descrizione anche nella didascalia."
        )
        return

    description = state["description"]
    budget = extract_budget(description)
    caption = build_caption(username, description, budget)
    keyboard = contact_keyboard(username)

    try:
        # La caption delle foto ha un limite Telegram di 1024 caratteri
        # (dopo la decodifica HTML). Per testi lunghi invia un testo separato.
        plain_length = len(re.sub(r"<[^>]+>", "", caption))
        if plain_length <= 1024:
            await context.bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=state["photo"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=state["photo"],
                caption=(
                    "🔎 <b>NEW REQUEST</b>\n\n"
                    f'👤 <b>Utente:</b> <a href="https://t.me/{username}">@{escape(username)}</a>'
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            # Chunking evita tag HTML spezzati e mantiene la descrizione integrale.
            for start_index in range(0, len(description), 3500):
                await context.bot.send_message(
                    chat_id=TARGET_CHAT_ID,
                    text="📝 Description:\n" + description[start_index:start_index + 3500],
                )
    except TelegramError:
        log.exception("Impossibile pubblicare la richiesta nel gruppo seller")
        await msg.reply_text("⚠️ Invio non riuscito. Riprova tra poco.")
        return

    pending.pop(chat_id, None)
    await msg.reply_text(after_request_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_user_message))
    log.info("FindBot avviato; username pubblico nel gruppo seller, senza claim esclusivo")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
