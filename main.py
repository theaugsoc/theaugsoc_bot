import sqlite3
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

TOKEN = '8883883367:AAFc6zoJaz-K9CgZovzwpuAOHfN1IxUgOaU'  # Replace with your Bot Father Token
CRITIQUE_TOPIC_ID = 8          # Thread ID for #critique-corner

def init_db():
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_critiques (
            user_id INTEGER PRIMARY KEY,
            critique_count INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def get_critiques(user_id: int) -> int:
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('SELECT critique_count FROM user_critiques WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def add_critique(user_id: int):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_critiques (user_id, critique_count) VALUES (?, 1)
        ON CONFLICT(user_id) DO UPDATE SET critique_count = critique_count + 1
    ''', (user_id,))
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

def has_user_reviewed_post(user_id: int, target_msg_id: int) -> bool:
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS review_logs (
            user_id INTEGER,
            target_msg_id INTEGER,
            PRIMARY KEY (user_id, target_msg_id)
        )
    ''')
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

async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return

    # Restrict rule enforcement strictly to #critique-corner topic
    if msg.message_thread_id != CRITIQUE_TOPIC_ID:
        return

    text = msg.text or msg.caption or ""
    words = len(text.split())

    # Detect if the message is replying to another chat message
    parent_msg = msg.reply_to_message
    is_real_reply = (
        parent_msg is not None and
        parent_msg.message_id != msg.message_thread_id and
        not parent_msg.forum_topic_created
    )

    # ----------------------------------------------------
    # CASE A: USER IS SUBMITTING A CRITIQUE (#review)
    # ----------------------------------------------------
    if is_real_reply:
        # Condition 1: Must contain the #review hashtag
        if "#review" not in text.lower():
            return

        # Condition 2: Don't count self-reviews
        if parent_msg.from_user.id == user.id:
            return

        # Condition 3: Target message must be a standalone post (NOT a reply/review itself)
        target_is_parent_reply = (
            parent_msg.reply_to_message is not None and
            parent_msg.reply_to_message.message_id != msg.message_thread_id and
            not parent_msg.reply_to_message.forum_topic_created
        )
        if target_is_parent_reply:
            print(f"[REJECTED] {user.first_name} tried to tag #review on a reply instead of a post.")
            return

        # Condition 4: User cannot review the same submission post twice
        if has_user_reviewed_post(user.id, parent_msg.message_id):
            print(f"[REJECTED] {user.first_name} already reviewed post ID {parent_msg.message_id}.")
            return

        # Condition 5: Word count validation (20+ words)
        if words >= 20:
            log_post_review(user.id, parent_msg.message_id)
            add_critique(user.id)
            total = get_critiques(user.id)
            print(f"[CRITIQUE LOGGED] {user.first_name} now has {total}/2 critiques.")
            
            note = await context.bot.send_message(
                chat_id=msg.chat_id,
                message_thread_id=msg.message_thread_id,
                text=f"✅ Valid #review logged for {user.first_name}! ({total}/2 completed)"
            )
            await asyncio.sleep(4)
            await note.delete()
        return

    # ----------------------------------------------------
    # CASE B: USER IS POSTING A STANDALONE SUBMISSION
    # ----------------------------------------------------
    if not is_real_reply and words > 0:
        # Enforce 1,000-Word Cap
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

        # Enforce 2-Critique Requirement
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
                    f"⚠️ **Post Removed for {user.first_name}**\n\n"
                    f"You must leave 20+ word feedback using `#review` on 2 peer posts before submitting work.\n"
                    f"**Current Critiques:** {critiques_done}/2"
                ),
                parse_mode="Markdown"
            )
            await asyncio.sleep(7)
            await warn.delete()
        else:
            use_critiques(user.id, 2)
            print(f"[SUBMISSION APPROVED] Draft published for {user.first_name}. Remaining critiques: {get_critiques(user.id)}")

def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, process_chat))
    print("Bot is listening...")
    app.run_polling()

if __name__ == '__main__':
    main()