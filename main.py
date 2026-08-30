import sqlite3
import logging
import asyncio
import os
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters
)

TOKEN = os.getenv('BOT_TOKEN', '8883883367:AAFc6zoJaz-K9CgZovzwpuAOHfN1IxUgOa')
CRITIQUE_TOPIC_ID = 8

# Pending appeals tracker: { user_id: deleted_post_text }
PENDING_APPEALS = {}
AWAITING_REASON = set()

# --- Flask Keep-Alive Server ---
app_flask = Flask('')

@app_flask.route('/')
def home():
    return "The Aug Soc Bot is awake and running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# --- Database Functions ---
def init_db():
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_critiques (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            critique_count INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS review_logs (
            user_id INTEGER,
            target_msg_id INTEGER,
            PRIMARY KEY (user_id, target_msg_id)
        )
    ''')
    conn.commit()
    conn.close()

def sync_user(user_id: int, username: str = None):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_critiques (user_id, username, critique_count) VALUES (?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET username = COALESCE(EXCLUDED.username, user_critiques.username)
    ''', (user_id, username))
    conn.commit()
    conn.close()

def get_critiques(user_id: int) -> int:
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('SELECT critique_count FROM user_critiques WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def add_critique(user_id: int, amount: int = 1, username: str = None):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_critiques (user_id, username, critique_count) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
            critique_count = critique_count + EXCLUDED.critique_count,
            username = COALESCE(EXCLUDED.username, user_critiques.username)
    ''', (user_id, username, amount))
    conn.commit()
    conn.close()

def set_critiques(user_id: int, amount: int):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE user_critiques SET critique_count = ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def use_critiques(user_id: int, count: int = 2):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE user_critiques SET critique_count = MAX(0, critique_count - ?) WHERE user_id = ?
    ''', (count, user_id))
    conn.commit()
    conn.close()

def get_user_id_by_username(username: str):
    clean_name = username.lstrip('@')
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM user_critiques WHERE LOWER(username) = LOWER(?)', (clean_name,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def has_user_reviewed_post(user_id: int, target_msg_id: int) -> bool:
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM review_logs WHERE user_id = ? AND target_msg_id = ?', (user_id, target_msg_id))
    exists = cursor.fetchone()
    conn.close()
    return exists is not None

def log_post_review(user_id: int, target_msg_id: int):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO review_logs (user_id, target_msg_id) VALUES (?, ?)', (user_id, target_msg_id))
    conn.commit()
    conn.close()

# --- Helper Functions ---
async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False

# --- Command Handlers ---
async def cmd_mycredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sync_user(user.id, user.username)
    count = get_critiques(user.id)
    await update.message.reply_text(f"📊 **{user.first_name}**, you currently have **{count}** critique credit(s).", parse_mode="Markdown")

async def cmd_addcredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not await is_admin(msg.chat_id, user.id, context):
        await msg.reply_text("❌ Admin command only.")
        return

    args = context.args
    if len(args) < 2:
        await msg.reply_text("Usage: `/addcredits @username 2` or `/addcredits <USER_ID> 2`", parse_mode="Markdown")
        return

    target_str, amount_str = args[0], args[1]
    try:
        amount = int(amount_str)
    except ValueError:
        await msg.reply_text("❌ Amount must be a number.")
        return

    target_id = None
    if target_str.isdigit():
        target_id = int(target_str)
    else:
        target_id = get_user_id_by_username(target_str)

    if not target_id:
        await msg.reply_text(f"❌ User `{target_str}` not found in database history.", parse_mode="Markdown")
        return

    add_critique(target_id, amount)
    new_total = get_critiques(target_id)
    await msg.reply_text(f"✅ Added {amount} credit(s). New balance for target: **{new_total}**.", parse_mode="Markdown")

async def cmd_resetcredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not await is_admin(msg.chat_id, user.id, context):
        await msg.reply_text("❌ Admin command only.")
        return

    args = context.args
    if len(args) < 1:
        await msg.reply_text("Usage: `/resetcredits @username` or `/resetcredits <USER_ID>`", parse_mode="Markdown")
        return

    target_str = args[0]
    target_id = int(target_str) if target_str.isdigit() else get_user_id_by_username(target_str)

    if not target_id:
        await msg.reply_text(f"❌ User `{target_str}` not found in database history.", parse_mode="Markdown")
        return

    set_critiques(target_id, 0)
    await msg.reply_text(f"🔄 Reset critique balance to **0** for user `{target_str}`.", parse_mode="Markdown")

# --- Moderation & Appeal Logic ---
async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return

    # Direct Message Reason Handler for Appeals
    if update.effective_chat.type == 'private' and user.id in AWAITING_REASON:
        AWAITING_REASON.remove(user.id)
        reason = msg.text or "No reason provided."
        deleted_text = PENDING_APPEALS.pop(user.id, "[Content Unavailable]")

        admin_text = (
            f"📥 **NEW MODERATION APPEAL**\n\n"
            f"**User:** {user.full_name} (@{user.username} | ID: `{user.id}`)\n"
            f"**User's Appeal Reason:** {reason}\n\n"
            f"**Deleted Draft Excerpt:**\n_{deleted_text[:500]}_"
        )
        buttons = [
            [
                InlineKeyboardButton("✅ Grant 2 Credits & Notify", callback_data=f"app_grant_{user.id}"),
                InlineKeyboardButton("❌ Reject Appeal", callback_data=f"app_rej_{user.id}")
            ]
        ]
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=admin_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        await msg.reply_text("✅ Your appeal has been submitted to group admins for review.")
        return

    if msg.message_thread_id != CRITIQUE_TOPIC_ID:
        return

    sync_user(user.id, user.username)

    if await is_admin(msg.chat_id, user.id, context):
        return

    text = msg.text or msg.caption or ""
    words = len(text.split())

    parent_msg = msg.reply_to_message
    is_real_reply = (
        parent_msg is not None and
        parent_msg.message_id != msg.message_thread_id and
        not parent_msg.forum_topic_created
    )

    # CASE A: CRITIQUE SUBMISSION (#review)
    if is_real_reply:
        if "#review" not in text.lower() or parent_msg.from_user.id == user.id:
            return
        if has_user_reviewed_post(user.id, parent_msg.message_id):
            return

        if words >= 20:
            log_post_review(user.id, parent_msg.message_id)
            add_critique(user.id, 1, user.username)
            total = get_critiques(user.id)
            
            note = await context.bot.send_message(
                chat_id=msg.chat_id,
                message_thread_id=msg.message_thread_id,
                text=f"✅ Valid #review logged for {user.first_name}! ({total}/2 completed)"
            )
            await asyncio.sleep(4)
            await note.delete()
        return

    # CASE B: STANDALONE DRAFT SUBMISSION
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
            PENDING_APPEALS[user.id] = text
            try:
                await msg.delete()
            except Exception:
                pass
            
            bot_username = (await context.bot.get_me()).username
            appeal_url = f"https://t.me/{bot_username}?start=appeal"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Appeal Deletion", url=appeal_url)]
            ])

            warn = await context.bot.send_message(
                chat_id=msg.chat_id,
                message_thread_id=msg.message_thread_id,
                text=(
                    f"⚠️ **Post Removed for {user.first_name}**\n\n"
                    f"You must leave 20+ word feedback using `#review` on 2 peer posts before submitting work.\n"
                    f"**Current Critiques:** {critiques_done}/2"
                ),
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            await asyncio.sleep(10)
            await warn.delete()
        else:
            use_critiques(user.id, 2)

# --- Callback Button Handlers ---
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("app_grant_"):
        target_id = int(data.split("_")[2])
        add_critique(target_id, 2)
        await query.edit_message_text(f"{query.message.text}\n\n✅ **APPROVED:** Granted 2 credits to user `{target_id}`.", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text="🎉 Your appeal was approved! 2 critique credits have been added to your balance.")
        except Exception:
            pass

    elif data.startswith("app_rej_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_text(f"{query.message.text}\n\n❌ **REJECTED:** Appeal declined.", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text="❌ Your appeal was reviewed and declined by community admins.")
        except Exception:
            pass

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sync_user(user.id, user.username)
    if context.args and context.args[0] == "appeal":
        if user.id in PENDING_APPEALS:
            AWAITING_REASON.add(user.id)
            await update.message.reply_text("Please reply to this message with a short explanation for your appeal:")
        else:
            await update.message.reply_text("No recent deleted posts found eligible for appeal.")
    else:
        await update.message.reply_text("Hello! I am The Aug Soc community manager bot. Type /mycredits to check your balance.")

def main():
    init_db()
    keep_alive()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("mycredits", cmd_mycredits))
    app.add_handler(CommandHandler("addcredits", cmd_addcredits))
    app.add_handler(CommandHandler("resetcredits", cmd_resetcredits))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.ALL, process_chat))

    print("Bot is listening...")
    app.run_polling()

if __name__ == '__main__':
    main()
