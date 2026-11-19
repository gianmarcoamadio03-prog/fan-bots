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
