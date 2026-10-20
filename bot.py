"""Find Bot — richieste anonime verso il gruppo seller (solo Telegram).

Compatibile con python-telegram-bot 20+.
Variabili obbligatorie: BOT_TOKEN, TARGET_CHAT_ID.
"""
import logging
import os
import re
from html import escape

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("find-request-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "").strip()
BEST_FIND_URL = os.getenv("BEST_FIND_URL", "").strip()
if not BOT_TOKEN or not TARGET_CHAT_ID:
    raise RuntimeError("Configura BOT_TOKEN e TARGET_CHAT_ID nelle variabili d'ambiente")

# Il bot conserva temporaneamente in RAM solo i dati necessari a completare
# la richiesta. Non salva identificativi, messaggi o foto su un database.
# Le richieste incompiute vanno perse se il processo si riavvia.
pending = {}
BUDGET_RE = re.compile(r"(?:\bbudget\b|\bbudg\b)\s*[:=\-]?\s*(?:€\s*)?(\d{1,6})(?:[.,]\d{1,2})?\s*€?|€\s*(\d{1,6})(?:[.,]\d{1,2})?", re.IGNORECASE)


def split_description_and_budget(text):
    text = (text or "").strip()
    match = BUDGET_RE.search(text)
    budget = next((x for x in match.groups() if x), None) if match else None
    # Manteniamo la descrizione originale, incluso il budget scritto dall'utente.
    return text, budget


def after_request_text():
    reply = (
        "✅ <b>Richiesta ricevuta!</b>\n\n"
        "I seller cercheranno l'articolo e, se disponibile, lo pubblicheranno nel gruppo Best Find.\n\n"
        "🔁 Puoi inviare un'altra richiesta quando vuoi: <b>foto + descrizione</b> "
        "(budget opzionale)."
    )
    if BEST_FIND_URL:
        reply += f'\n\n🔗 <a href="{escape(BEST_FIND_URL, quote=True)}">Apri Best Find</a>'
    return reply


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "👋 <b>Benvenuto su Find Bot!</b>\n\n"
        "📸 Invia una foto o uno screenshot dell'articolo che cerchi.\n"
        "📝 Aggiungi una descrizione (marca, modello, colore, taglia).\n"
        "💰 Puoi specificare un budget, ma è opzionale.\n\n"
        "Puoi mandare foto e testo insieme oppure in due messaggi separati. "
        "La richiesta sarà pubblicata <b>senza nome, username e ID Telegram</b> "
        "nel gruppo privato dei seller.\n\n"
        "⚠️ Evita di inserire dati personali o screenshot che li mostrano.",
        parse_mode=ParseMode.HTML,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    pending.pop(update.effective_chat.id, None)
    await update.message.reply_text("Richiesta incompleta annullata. Puoi inviarne una nuova.")


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or update.effective_chat.type != "private":
        return
    # Non accetta documenti/video/audio: solo foto Telegram e testo.
    if not msg.photo and not (msg.text or msg.caption):
        await msg.reply_text("Invia una foto o uno screenshot, con una descrizione. Il budget è opzionale.")
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
        await msg.reply_text("📸 Ho ricevuto la descrizione. Ora inviami una foto dell'articolo.")
        return
    if not state["description"]:
        await msg.reply_text("✍️ Ho ricevuto la foto. Ora inviami una descrizione dell'articolo (budget opzionale).")
        return

    description, budget = split_description_and_budget(state["description"])
    caption = "🔎 <b>NEW REQUEST</b>\n\n📝 <b>Description:</b> " + escape(description)
    # Se l'utente specifica un budget, è già incluso nel testo originale.
    if budget and "budget" not in description.lower() and "budg" not in description.lower():
        caption += f"\n💰 <b>Budget:</b> €{escape(budget)}"

    # Una didascalia Telegram ha limite di 1024 caratteri: per testi lunghi
    # mandiamo la foto senza caption e il testo separato (sempre senza autore).
    try:
        if len(caption) <= 1024:
            await context.bot.send_photo(chat_id=TARGET_CHAT_ID, photo=state["photo"], caption=caption, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_photo(chat_id=TARGET_CHAT_ID, photo=state["photo"])
            await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=caption[:4000], parse_mode=ParseMode.HTML)
    except Exception:
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
    log.info("Find Bot avviato; gruppo seller configurato")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
