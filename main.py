if words >= 20:
            log_post_review(user.id, parent_msg.message_id)
            add_critique(user.id)
            total = get_critiques(user.id)
            
            note = await context.bot.send_message(
                chat_id=msg.chat_id,
                message_thread_id=msg.message_thread_id,
                text=f"✅ Valid #review logged for {user.first_name}! ({total}/2 completed)"
            )
            await asyncio.sleep(4)
            await note.delete()
        return

    # CASE B: USER IS POSTING A STANDALONE SUBMISSION
    if not is_real_reply and words > 0:
        if words > 1000:
            try:
                await msg.delete()
            except Exception:
                pass
            warn = await context.bot.send_message(
                chat_id=msg.chat_id,
                message_thread_id=msg.message_thread_id,
                text=f"⚠️ {user.first_name}, post removed: Exceeds 1,000 words limit ({words} words)."
            )
            await asyncio.sleep(7)
            await warn.delete()
            return

        critiques_done = get_critiques(user.id)
        if critiques_done < 2:
            try:
                await msg.delete()
            except Exception as e:
                print(f"Deletion failed: {e}")
            
            warn = await context.bot.send_message(
                chat_id=msg.chat_id,
                message_thread_id=msg.message_thread_id,
                text=(
                    f"⚠️ Post Removed for {user.first_name}\n\n"
                    f"You must leave 20+ word feedback using #review on 2 peer posts before submitting work.\n"
                    f"Current Critiques: {critiques_done}/2"
                ),
                parse_mode="Markdown"
            )
            await asyncio.sleep(7)
            await warn.delete()
        else:
            use_critiques(user.id, 2)
            print(f"[SUBMISSION APPROVED] Draft published for {user.first_name}.")

def main():
    init_db()
    keep_alive()  # Starts the Flask web server to satisfy Render's HTTP check
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, process_chat))
    print("Bot is listening...")
    app.run_polling()

if name == 'main':
    main()
