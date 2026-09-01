import os

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_TELEGRAM_ID", "0").split(",") if x.strip().isdigit()]

def set_pen_name(user_id: int, pen_name: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (user_id, pen_name) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET pen_name=?",
        (user_id, pen_name, pen_name)
    )
    conn.commit()
    conn.close()

def get_pen_name(user_id: int) -> str:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT pen_name FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else None

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        await update.message.reply_text(
            f"Welcome {member.mention_markdown()}! 👋\n\n"
            f"Please set your **Pen Name** by replying to this message with: `/setname YourPenName`",
            parse_mode="Markdown"
        )

async def set_name_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("⚠️ Please provide a pen name. Example: `/setname WritingBard`", parse_mode="Markdown")
        return
    pen_name = " ".join(context.args)
    set_pen_name(user.id, pen_name)
    await update.message.reply_text(f"✅ Your pen name has been set to: **{pen_name}**", parse_mode="Markdown")

import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor

import http.server
import socketserver
import threading

def start_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

# Start background health server for Render
threading.Thread(target=start_health_check_server, daemon=True).start()

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')
import logging
import asyncio
import os
from threading import Thread
from telegram import BotCommand, BotCommandScopeAllChatAdministrators
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters
)

DB_FILE = 'critiques.db'

def init_sqlite_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS prompts (
            prompt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            challenge_type TEXT,
            prompt_text TEXT,
            priority INTEGER DEFAULT 0,
            is_used INTEGER DEFAULT 0,
            UNIQUE(prompt_text)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category TEXT,
            message TEXT,
            status TEXT DEFAULT 'OPEN',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS review_logs (
            user_id INTEGER,
            target_msg_id INTEGER,
            UNIQUE(user_id, target_msg_id)
        )
    ''')
    conn.commit()
    conn.close()

# Initialize SQLite tables on startup
init_sqlite_db()

async def auto_delete_messages(bot, chat_id: int, message_ids: list, delay: int = 15):
    """Deletes a list of message IDs after a specified delay in seconds."""
    await asyncio.sleep(delay)
    for msg_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logging.debug(f"Auto-delete failed for message {msg_id}: {e}")


TOKEN = os.getenv('BOT_TOKEN')
CRITIQUE_TOPIC_ID = 8
PROMPTS_TOPIC_ID = 9  # Update this to your exact "Prompts and Challenges" Topic ID
RESOURCE_HUB_TOPIC_ID = 10  # Update this to your exact Resource Hub Topic ID
CHANNEL_ID = os.getenv('CHANNEL_ID', "@theaugustsociety")
LEADERBOARD_TOPIC_ID = 485  # Update with your dedicated Leaderboard Topic ID
LAST_LEADERBOARD_MSG_ID = None

USER_TICKET_STATE = {}  # { user_id: {"category": str, "draft_text": str} }


# --- PROMPT DATABASE HELPERS ---
def bulk_insert_prompts(prompt_list: list) -> tuple:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    added = 0
    skipped = 0
    for item in prompt_list:
        cat = item[0].strip().lower()
        if len(item) >= 3:
            ctype = item[1].strip().lower()
            if ctype == 'daily':
                ctype = 'quick prompt'
            text = '|'.join(item[2:]).strip()
        else:
            ctype = 'weekly'
            text = item[1].strip()
            
        try:
            cursor.execute(
                'INSERT INTO prompts (category, challenge_type, prompt_text) VALUES (?, ?, ?)',
                (cat, ctype, text)
            )
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1
    conn.commit()
    conn.close()
    return added, skipped

def get_prompts_by_category(category: str, limit: int = 5) -> list:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT prompt_id, challenge_type, prompt_text, priority FROM prompts WHERE category = ? AND is_used = 0 ORDER BY priority DESC, prompt_id ASC LIMIT ?',
        (category.lower(), limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

def set_prompt_priority(prompt_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE prompts SET priority = 0 WHERE priority = 1')
    cursor.execute('UPDATE prompts SET priority = 1 WHERE prompt_id = ?', (prompt_id,))
    conn.commit()
    conn.close()

def delete_prompt(prompt_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM prompts WHERE prompt_id = ?', (prompt_id,))
    conn.commit()
    conn.close()

def get_next_prompt_to_dispatch() -> tuple:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Check for admin priority selection first
    cursor.execute('SELECT prompt_id, category, challenge_type, prompt_text FROM prompts WHERE priority = 1 AND is_used = 0 LIMIT 1')
    row = cursor.fetchone()
    if row:
        conn.close()
        return row
    # Fallback to standard oldest unused prompt
    cursor.execute('SELECT prompt_id, category, challenge_type, prompt_text FROM prompts WHERE is_used = 0 ORDER BY prompt_id ASC LIMIT 1')
    row = cursor.fetchone()
    conn.close()
    return row

def mark_prompt_used(prompt_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE prompts SET is_used = 1, priority = 0 WHERE prompt_id = ?', (prompt_id,))
    conn.commit()
    conn.close()

# --- SUBMISSION & REVIEW HELPERS ---
ALLOWED_GENRE_TAGS = {'#poetry', '#fiction', '#nonfiction', '#concept'}
ALLOWED_POST_TAGS = {'#draft', '#submission', '#workinprogress', '#wip'}
ALLOWED_CRITIQUE_TAGS = {'#review', '#feedback', '#critique'}

# --- ADD THIS HELPER FUNCTION HERE ---
def init_restored_table():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS restored_messages (
            message_id INTEGER PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_restored_table()

def mark_message_restored(message_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO restored_messages (message_id) VALUES (?)', (message_id,))
    conn.commit()
    conn.close()

def is_message_restored(message_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM restored_messages WHERE message_id = ?', (message_id,))
    exists = cursor.fetchone()
    conn.close()
    return exists is not None
# ----------------------------------

def split_text_into_chunks(text: str, max_chars: int = 2500) -> list[str]:
    """Safely splits long text into sequential chunks without dropping paragraphs or words."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current_chunk = ""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    for para in paragraphs:
        # Check if adding this paragraph exceeds our safety limit
        if len(current_chunk) + len(para) + 2 > max_chars:
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
                current_chunk = ""

            # If an individual paragraph itself is over max_chars, split by words
            if len(para) > max_chars:
                words = para.split()
                for word in words:
                    if len(current_chunk) + len(word) + 1 > max_chars:
                        chunks.append(current_chunk.strip())
                        current_chunk = word + " "
                    else:
                        current_chunk += word + " "
                current_chunk += "\n\n"
            else:
                current_chunk = para + "\n\n"
        else:
            current_chunk += para + "\n\n"

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks

def parse_and_validate_hashtags(text: str) -> tuple[bool, str, str]:
    tokens = [t.lower() for t in text.split()]
    found_genre = next((t for t in tokens if t in ALLOWED_GENRE_TAGS), None)
    found_post = next((t for t in tokens if t in ALLOWED_POST_TAGS), None)
    is_valid = bool(found_genre and found_post)
    return is_valid, found_genre, found_post

def sync_user(user_id: int, username: str = None):
    clean_username = f"@{username.lstrip('@')}" if username else None
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_critiques (user_id, username, critique_count)
        VALUES (%s, %s, 0)
        ON CONFLICT (user_id) 
        DO UPDATE SET username = COALESCE(EXCLUDED.username, user_critiques.username);
    ''', (user_id, clean_username))
    conn.commit()
    cursor.close()
    conn.close()

def get_critiques(user_id: int) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT critique_count FROM user_critiques WHERE user_id = %s', (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else 0

def add_critique(user_id: int, amount: int = 1, username: str = None):
    clean_username = f"@{username.lstrip('@')}" if username else None
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_critiques (user_id, username, critique_count)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) 
        DO UPDATE SET 
            critique_count = user_critiques.critique_count + EXCLUDED.critique_count,
            username = COALESCE(EXCLUDED.username, user_critiques.username);
    ''', (user_id, clean_username, amount))
    conn.commit()
    cursor.close()
    conn.close()

def set_critiques(user_id: int, amount: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE user_critiques SET critique_count = %s WHERE user_id = %s', (amount, user_id))
    conn.commit()
    cursor.close()
    conn.close()

def use_critiques(user_id: int, count: int = 2):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE user_critiques 
        SET critique_count = GREATEST(0, critique_count - %s) 
        WHERE user_id = %s
    ''', (count, user_id))
    conn.commit()
    cursor.close()
    conn.close()

def get_user_id_by_username(username: str):
    clean_name = username.lstrip('@')
    with_at = f"@{clean_name}"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT user_id FROM user_critiques WHERE LOWER(username) = LOWER(%s) OR LOWER(username) = LOWER(%s)', 
        (clean_name, with_at)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else None

def create_submission(user_id: int, author_tag: str, title: str, genre_tag: str, post_tag: str, content: str) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO submissions (user_id, author_tag, title, genre_tag, post_tag, content)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING sub_id;
    ''', (user_id, author_tag, title, genre_tag, post_tag, content))
    sub_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    conn.close()
    return sub_id
    
def update_submission_msg_id(sub_id: int, msg_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE submissions SET msg_id = %s WHERE sub_id = %s', (msg_id, sub_id))
    conn.commit()
    cursor.close()
    conn.close()

def add_submission_review(sub_id: int, reviewer_id: int, reviewer_tag: str, review_text: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO submission_reviews (sub_id, reviewer_id, reviewer_tag, review_text) VALUES (%s, %s, %s, %s)',
        (sub_id, reviewer_id, reviewer_tag, review_text)
    )
    conn.commit()
    cursor.close()
    conn.close()

def get_submission(sub_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT sub_id, user_id, author_tag, title, genre_tag, post_tag, content, msg_id FROM submissions WHERE sub_id = %s', (sub_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def get_submission_reviews(sub_id: int) -> list:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT reviewer_tag, review_text, created_at FROM submission_reviews WHERE sub_id = %s ORDER BY review_id ASC', (sub_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows

def create_ticket(user_id: int, category: str, details: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO tickets (user_id, category, message) VALUES (?, ?, ?)', (user_id, category, details))
    ticket_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return ticket_id

def update_ticket_status(ticket_id: int, status: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('UPDATE tickets SET status = ? WHERE ticket_id = ?', (status, ticket_id))
    conn.commit()
    conn.close()

def has_user_reviewed_post(user_id: int, target_msg_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM review_logs WHERE user_id = ? AND target_msg_id = ?', (user_id, target_msg_id))
    exists = cursor.fetchone()
    conn.close()
    return exists is not None

def log_post_review(user_id: int, target_msg_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO review_logs (user_id, target_msg_id) VALUES (?, ?)', (user_id, target_msg_id))
    conn.commit()
    conn.close()

def get_top_users(limit: int = 10) -> list:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT username, critique_count FROM user_critiques ORDER BY critique_count DESC LIMIT %s', (limit,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows

# --- Helper Functions ---
async def is_admin(chat_id, user_id, context):
    # Always trust hardcoded environment admin IDs instantly (fixes private chats and group fallback)
    if user_id in ADMIN_IDS and user_id != 0:
        return True
        
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        return chat_member.status in ['creator', 'administrator']
    except Exception:
        return False

# --- COMMAND HANDLERS ---
async def cmd_mycredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    sync_user(user.id, user.username)
    count = get_critiques(user.id)
    
    resp = await msg.reply_text(
        f"📊 **{user.first_name}**, you currently have **{count}** critique credit(s).", 
        parse_mode="Markdown"
    )
    
    # Auto-delete command and bot response after 15 seconds
    asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))


async def cmd_addcredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user

    if not await is_admin(msg.chat_id, user.id, context):
        resp = await msg.reply_text("❌ Admin command only.")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    args = context.args
    if len(args) < 2:
        resp = await msg.reply_text("Usage: `/addcredits @username 2` or `/addcredits <USER_ID> 2`", parse_mode="Markdown")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    target_str, amount_str = args[0], args[1]
    try:
        amount = int(amount_str)
    except ValueError:
        resp = await msg.reply_text("❌ Amount must be a number.")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    target_id = int(target_str) if target_str.isdigit() else get_user_id_by_username(target_str)

    if not target_id:
        resp = await msg.reply_text(f"❌ User `{target_str}` not found in database history.", parse_mode="Markdown")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    add_critique(target_id, amount)
    new_total = get_critiques(target_id)
    resp = await msg.reply_text(f"✅ Added {amount} credit(s). New balance for target: **{new_total}**.", parse_mode="Markdown")
    
    # Clean up after 15 seconds
    asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))


async def cmd_resetcredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    
    if not await is_admin(msg.chat_id, user.id, context):
        resp = await msg.reply_text("❌ Admin command only.")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    args = context.args
    if len(args) < 1:
        resp = await msg.reply_text("Usage: `/resetcredits @username` or `/resetcredits <USER_ID>`", parse_mode="Markdown")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    target_str = args[0]
    target_id = int(target_str) if target_str.isdigit() else get_user_id_by_username(target_str)

    if not target_id:
        resp = await msg.reply_text(f"❌ User `{target_str}` not found in database history.", parse_mode="Markdown")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    set_critiques(target_id, 0)
    resp = await msg.reply_text(f"🔄 Reset critique balance to **0** for user `{target_str}`.", parse_mode="Markdown")
    
    # Clean up after 15 seconds
    asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    sync_user(user.id, user.username)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Post Appeal", callback_data="hub_cat_appeal")],
        [InlineKeyboardButton("💳 Credit Dispute", callback_data="hub_cat_credits")],
        [InlineKeyboardButton("🚩 Report Content/User", callback_data="hub_cat_report")],
        [InlineKeyboardButton("❓ General Help", callback_data="hub_cat_other")]
    ])
    
    if msg.chat.type != 'private':
        bot_user = await context.bot.get_me()
        resp = await msg.reply_text(f"Please reach out to me in private DM: https://t.me/{bot_user.username}?start=support")
        # Automatically clean up both the user command and the bot reply after 15 seconds
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))
    else:
        await msg.reply_text("🛠️ **The August Society Support Hub**\nPlease select a category:", reply_markup=keyboard, parse_mode="Markdown")

async def cmd_submitresource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    sync_user(user.id, user.username)

    if msg.chat.type != 'private':
        bot_user = await context.bot.get_me()
        resp = await msg.reply_text(f"Please submit resources to me in a private DM: https://t.me/{bot_user.username}?start=submit_resource")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Poetry", callback_data="res_tag_poetry"),
             InlineKeyboardButton("Fiction", callback_data="res_tag_fiction"),
             InlineKeyboardButton("Non-Fiction", callback_data="res_tag_nonfiction")]
        ])
        await msg.reply_text(
            "📚 **Resource Hub Submission**\n\n"
            "First, please select the mandatory category for your resource:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    top_users = get_top_users(10)
    
    text = "🏆 **Community Review Leaderboard** 🏆\n\n"
    for idx, (uname, count) in enumerate(top_users, 1):
        display_name = uname or f"User #{idx}"
        text += f"{idx}. {display_name} — **{count} credits**\n"
        
    await msg.reply_text(text, parse_mode="Markdown")

async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    sync_user(user.id, user.username)
    credits = get_critiques(user.id)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Custom Prompt (10 Credits)", callback_data="shop_buy_prompt")],
        [InlineKeyboardButton("🌟 Showcase: Spotlight & Queue (25 Credits)", callback_data="shop_buy_showcase")],
        [InlineKeyboardButton("👑 Permanent Role Badge (50 Credits)", callback_data="shop_buy_badge")]
    ])
    
    await msg.reply_text(
        f"🛍️ **The August Society Credit Shop** (Your Balance: **{credits} Credits**)\n\n"
        "Spend your earned review credits on exclusive community perks:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
        
async def auto_delete_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int = 120):
    """Waits for the specified delay, then deletes the prompt if it still exists."""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # Message was already deleted by user interaction or cleanup

async def cmd_submitwork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    sync_user(user.id, user.username)

    credits = get_critiques(user.id)
    if credits < 1:
        resp = await msg.reply_text(
            f"⚠️ **Insufficient Credits:** You currently have **{credits}** credits.\n"
            f"Please leave a critique on another submission (minimum 20 words) to earn credits before submitting.",
            parse_mode="Markdown"
        )
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎭 Fiction", callback_data="subtag_genre_fiction"),
            InlineKeyboardButton("✍️ Poetry", callback_data="subtag_genre_poetry"),
        ],
        [
            InlineKeyboardButton("📖 Non-Fiction", callback_data="subtag_genre_nonfiction"),
            InlineKeyboardButton("📝 Concept", callback_data="subtag_genre_other"),
        ]
    ])

    sent_msg = await msg.reply_text(
        f"🚀 **Submit Work for Critique (Available Credits: {credits})**\n\n"
        "Select the primary genre for your submission to begin:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    # Schedule prompt message to self-destruct after 30 seconds if ignored
    context.application.create_task(
        auto_delete_prompt(context, sent_msg.chat_id, sent_msg.message_id, delay=30)
    )

    # Delete the user's trigger message to keep the group clean
    try:
        await msg.delete()
    except Exception as e:
        logging.error(f"Failed to delete submitwork trigger message: {e}")


async def cmd_addprompts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    
    # Allow if user is hardcoded in ADMIN_IDS
    is_hardcoded_admin = user.id in ADMIN_IDS and user.id != 0
    
    # Only check group admin status if we are in a group/supergroup chat
    is_chat_admin = False
    if msg.chat.type != 'private':
        is_chat_admin = await is_admin(msg.chat.id, user.id, context)
    
    if not (is_hardcoded_admin or is_chat_admin):
        resp = await msg.reply_text("❌ Admin command only.")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    # Robust multi-line text extraction handling newline or space separation after command
    text = msg.text or ""
    if text.startswith("/addprompts"):
        raw_text = text[len("/addprompts"):].strip()
    else:
        parts = text.split(maxsplit=1)
        raw_text = parts[1].strip() if len(parts) > 1 else ""
        
    if not raw_text:
        resp = await msg.reply_text(
            "Usage:\n`/addprompts\ncategory | challenge_type | prompt text`\n\n"
            "Example:\n`poetry | weekly | Write a sonnet about dusk.`", 
            parse_mode="Markdown"
        )
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))
        return

    entries = []
    for line in raw_text.splitlines():
        if '|' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                entries.append(parts)

    added, skipped = bulk_insert_prompts(entries)
    resp = await msg.reply_text(f"✅ **Ingestion Complete**\nAdded: **{added}** | Duplicates Skipped: **{skipped}**", parse_mode="Markdown")
    asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))


async def cmd_manageprompts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not await is_admin(msg.chat_id, user.id, context):
        resp = await msg.reply_text("❌ Admin command only.")
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 10))
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Poetry Queue", callback_data="q_view_poetry")],
        [InlineKeyboardButton("📖 Fiction Queue", callback_data="q_view_fiction")],
        [InlineKeyboardButton("📝 Non-Fiction Queue", callback_data="q_view_non-fiction")]
    ])
    resp = await msg.reply_text("🗂️ **Prompt Queue Manager**\nSelect a genre category to inspect or rearrange:", reply_markup=keyboard, parse_mode="Markdown")
    asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 20))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    sync_user(user.id, user.username)
    
    if context.args:
        arg = context.args[0].lower()
        if arg == "appeal":
            draft = USER_TICKET_STATE.get(user.id, {}).get("draft_text", "[No draft captured]")
            USER_TICKET_STATE[user.id] = {"category": "Post Appeal", "draft_text": draft}
            await msg.reply_text("📩 **Post Appeal**: Please reply to this message with an explanation for your appeal.")
            return
        elif arg == "support":
            await cmd_support(update, context)
            return
        elif arg == "submit_resource":
            USER_TICKET_STATE[user.id] = {"category": "Resource Submission"}
            await msg.reply_text(
                "📚 **Resource Hub Submission**\n\n"
                "Please reply to this message with the details of the resource you'd like to share (title, link, description, and any tags). "
                "It will be sent to the moderators for review.",
                parse_mode="Markdown"
            )
            return

    resp = await msg.reply_text("Hello! I am The August Society community manager bot. Type /mycredits to check balance or /support for help.")
    if msg.chat.type != 'private':
        asyncio.create_task(auto_delete_messages(context.bot, msg.chat_id, [msg.message_id, resp.message_id], 15))


async def enforce_critique_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or getattr(msg, 'message_thread_id', None) != CRITIQUE_TOPIC_ID:
        return

    user = update.effective_user
    text = msg.text or msg.caption or ""

    # Bypass format enforcement and auto-deletion if user is admin AND contains #mod, 
    # or if it's an admin message with #mod. Let's strictly follow: 
    # 1. Admins AND 2. containing #mod (or check if either bypasses). 
    # The prompt says: "the only messages to bypass autoleteion would be one, from the admins, and two, containing #mod."
    # Wait, let's look closely at: "unless they contain a specific hashtag, like #mod. which means the only messages to bypass autoleteion would be one, from the admins, and two, containing #mod."
    # If it means either admins OR messages containing #mod bypass auto-deletion:
    
    is_admin_user = await is_admin(msg.chat_id, user.id, context)
    has_mod_tag = "#mod" in text.lower()
    has_public_tag = "#public" in text.lower()

    # Always protect manually restored posts from being deleted/filtered
    if is_message_restored(msg.message_id):
        return

    # Admins posting with both #mod and #public will be handled by the public broadcaster below
    if is_admin_user and has_mod_tag and has_public_tag:
        pass  # Let it pass through to the auto-broadcaster
    elif is_admin_user or has_mod_tag:
        return

    parent_msg = msg.reply_to_message
    parent_text = parent_msg.text if parent_msg and parent_msg.text else ""

    # Bypass format enforcement for valid bot interaction workflows
    if "Tags Selected:" in parent_text or "Critique for Submission #" in parent_text or "SUBMISSION #" in parent_text:
        return

    has_genre = any(tag in text.lower() for tag in ['#fiction', '#poetry', '#nonfiction', '#prose'])
    has_type = any(tag in text.lower() for tag in ['#draft', '#workinprogress', '#review'])

    if not (has_genre and has_type):
        try:
            await msg.delete()
            warning = await context.bot.send_message(
                chat_id=msg.chat_id,
                message_thread_id=CRITIQUE_TOPIC_ID,
                text=f"⚠️ @{msg.from_user.username or 'user'}, plain text posts without required tags are auto-removed.\n"
                     f"Please use `/submitwork` to format your submission properly."
            )
            await asyncio.sleep(10)
            await warning.delete()
        except Exception as e:
            logging.error(f"Failed to delete message: {e}")

# Set to track recently processed messages and prevent double-triggering
PROCESSED_MSG_IDS = set()

async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    thread_id = getattr(msg, 'message_thread_id', None)

    # Auto-delete non-admin messages in the Leaderboard topic
    if msg.chat.type in ['supergroup', 'group'] and thread_id == LEADERBOARD_TOPIC_ID:
        user = update.effective_user
        if not await is_admin(msg.chat_id, user.id, context):
            try:
                await msg.delete()
            except Exception:
                pass
            return

    # Strictly block non-admin messages in admin-only topics (Resource Hub & Prompts)
    if thread_id in [RESOURCE_HUB_TOPIC_ID, PROMPTS_TOPIC_ID]:
        user = update.effective_user
        if not await is_admin(msg.chat_id, user.id, context):
            try:
                await msg.delete()
                topic_name = "Resource Hub" if thread_id == RESOURCE_HUB_TOPIC_ID else "Prompts and Challenges"
                warning = await context.bot.send_message(
                    chat_id=msg.chat_id,
                    message_thread_id=thread_id,
                    text=f"⚠️ @{user.username or user.first_name}, direct posts in the **{topic_name}** topic are restricted to administrators."
                )
                await asyncio.sleep(10)
                await warning.delete()
            except Exception as e:
                logging.error(f"Failed to police admin-only topic: {e}")
            return

    # Check if the user is currently in an active submission step or ticket state
    user_id = update.effective_user.id if update.effective_user else None
    is_in_workflow = any([
        context.user_data.get('waiting_for_pen_name'),
        context.user_data.get('waiting_for_title'),
        context.user_data.get('waiting_for_work'),
        context.user_data.get('waiting_for_content'),
        context.user_data.get('waiting_for_text'),
        context.user_data.get('waiting_for_submission'),
        (user_id in USER_TICKET_STATE) if user_id else False
    ])

    # Only enforce topic formatting rules if the user is NOT in an active workflow
    if not is_in_workflow:
        await enforce_critique_format(update, context)
        
    if not msg.text and not msg.document and not msg.photo:
        return

    text = msg.text or msg.caption or ""
    user = update.effective_user

    # Direct Public Admin Post Handler (#mod + #public)
    if text and await is_admin(msg.chat_id, user.id, context) and "#mod" in text.lower() and "#public" in text.lower():
        # Prevent double processing
        if msg.message_id in PROCESSED_MSG_IDS:
            return
        PROCESSED_MSG_IDS.add(msg.message_id)

        # Clean text or extract parts if needed (keeping original text)
        chunks = split_text_into_chunks(text, max_chars=3000)
        total_parts = len(chunks)

        for i, chunk in enumerate(chunks, 1):
            part_suffix = f" ({i}/{total_parts})" if total_parts > 1 else ""
            formatted_post = f"{chunk}{part_suffix}" if total_parts > 1 else chunk

            if CHANNEL_ID:
                try:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=formatted_post,
                        parse_mode=None
                    )
                except Exception as e:
                    logging.error(f"Failed to post public admin message to channel: {e}")
        return

    # Deduplication Guard: If this exact message ID is already being processed, stop!
    if msg.message_id in PROCESSED_MSG_IDS:
        return
    PROCESSED_MSG_IDS.add(msg.message_id)
    
    # Keep the tracking set small so RAM stays low
    if len(PROCESSED_MSG_IDS) > 200:
        PROCESSED_MSG_IDS.clear()

    user = update.effective_user
    parent_msg = msg.reply_to_message
    parent_text = parent_msg.text if parent_msg and parent_msg.text else ""


    # ==========================================
    # WORKFLOW SUBMISSION (Pen Name & Content)
    # ==========================================
    if context.user_data.get('waiting_for_pen_name'):
        # Step 1: Capture the Pen Name provided by the user
        if not msg.text:
            return
        pen_name = msg.text.strip()
        context.user_data['pen_name'] = pen_name
        context.user_data['waiting_for_pen_name'] = False
        context.user_data['waiting_for_content'] = True  # Set next step flag

        # Clean up user's pen name message to keep chat tidy
        try:
            await msg.delete()
        except Exception:
            pass

        # Ask for the actual writing content (Title + Body)
        prompt_msg = await context.bot.send_message(
            chat_id=msg.chat_id,
            message_thread_id=msg.message_thread_id if hasattr(msg, 'message_thread_id') else None,
            text=f"✅ Pen Name recorded as **{pen_name}**.\n\nNow, please reply with your **Title on the first line**, followed by your content:",
            parse_mode="Markdown"
        )
        context.user_data['prompt_msg_id'] = prompt_msg.message_id
        return

    if context.user_data.get('waiting_for_content'):
        # Step 2: Immediately capture and delete the user's input message to keep chat tidy
        user_msg_id = msg.message_id
        try:
            await msg.delete()
        except Exception as e:
            logging.error(f"Could not delete user submission text message: {e}")

        context.user_data['waiting_for_content'] = False
        genre = context.user_data.get('submission_genre', 'general')
        post_type = context.user_data.get('submission_type', 'feedback')
        pen_name = context.user_data.get('pen_name', 'Anonymous')
        
        # Format pen name for hashtag (replace spaces with underscores)
        formatted_pen_hashtag = f"#{pen_name.replace(' ', '_')}"
        author_display = pen_name

        # Extract text from message body or file caption
        full_text = msg.text or msg.caption or ""
        file_doc = msg.document
        file_photo = msg.photo[-1] if msg.photo else None

        # --- PASTE EXTENSION CHECK HERE ---
        ALLOWED_EXTENSIONS = ('.docx', '.pdf', '.txt', '.epub', '.odt', '.rtf', '.md')
        if file_doc:
            file_name = file_doc.file_name or ""
            if not file_name.lower().endswith(ALLOWED_EXTENSIONS):
                # Clean up prompt message and notify user
                prompt_msg_id = context.user_data.pop('prompt_msg_id', None)
                if prompt_msg_id:
                    try:
                        await context.bot.delete_message(chat_id=msg.chat_id, message_id=prompt_msg_id)
                    except:
                        pass
                try:
                    await msg.delete()
                except:
                    pass
                await context.bot.send_message(
                    chat_id=msg.chat_id, 
                    text="❌ **Unsupported file format.** Please upload a supported document format (e.g., PDF, DOCX, TXT).",
                    parse_mode="Markdown"
                )
                return
        # ----------------------------------

        lines = full_text.splitlines()
        if len(lines) > 1:
            title = lines[0].strip()
            content = "\n".join(lines[1:]).strip()
        else:
            words = full_text.split()
            title = " ".join(words[:8]) + "..." if len(words) > 8 else (full_text if words else "Untitled Work")
            content = full_text if words else "[File Attachment Submission]"

        # Extract up to 3 custom tags from the body content and clean them from text
        content_words = content.split()
        custom_tags = []
        cleaned_content_words = []
        for word in content_words:
            if word.startswith('#') and len(word) > 1 and len(custom_tags) < 3:
                if word not in custom_tags:
                    custom_tags.append(word)
            else:
                cleaned_content_words.append(word)
        
        content = " ".join(cleaned_content_words)
        custom_tags_str = " " + " ".join(custom_tags) if custom_tags else ""

        # Deduct credits temporarily upon submission
        use_critiques(user.id, 2)

        # Append file tracking info into the ticket payload if a file/photo is present
        file_meta = ""
        if file_doc:
            file_meta = f"\nATTACHED_DOCUMENT: {file_doc.file_id} | {file_doc.file_name or 'unnamed_file'}"
        elif file_photo:
            file_meta = f"\nATTACHED_PHOTO: {file_photo.file_id}"

        # Package submission data into a review ticket payload
        ticket_payload = (
            f"TITLE: {title}\n"
            f"AUTHOR: {author_display}\n"
            f"GENRE: #{genre}\n"
            f"TYPE: #{post_type}\n"
            f"CUSTOM_TAGS:{custom_tags_str}\n"
            f"PEN_HASHTAG: {formatted_pen_hashtag}\n"
            f"USER_ID: {user.id}\n"
            f"USERNAME: {user.username or 'none'}\n"
            f"{file_meta}\n"
            f"--------------------------------------------------\n"
            f"{content}"
        )

        t_id = create_ticket(user.id, "Work Submission Review", ticket_payload)

        file_notice = "\n📎 **Includes File Attachment**" if (file_doc or file_photo) else ""
        content_preview = content[:1000] if len(content) > 1000 else content

        admin_text = (
            f"📄 **NEW WORK SUBMISSION REVIEW #{t_id}**{file_notice}\n"
            f"**Author:** {author_display} (@{user.username or 'none'} | ID: `{user.id}`)\n"
            f"**Title:** {title}\n"
            f"**Tags:** #{genre} #{post_type}{custom_tags_str} {formatted_pen_hashtag}\n\n"
            f"--------------------------------------------------\n"
            f"{content_preview}"
        )

        buttons = [
            [
                InlineKeyboardButton("✅ Approve & Post", callback_data=f"work_approve_{t_id}_{user.id}"),
                InlineKeyboardButton("❌ Reject & Refund", callback_data=f"work_reject_{t_id}_{user.id}")
            ]
        ]

        # Send review card to all admins (if a file/photo is attached, forward or send file preview to admin as well)
        for admin_id in ADMIN_IDS:
            if admin_id != 0:
                try:
                    if file_doc:
                        await context.bot.send_document(chat_id=admin_id, document=file_doc.file_id, caption=admin_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                    elif file_photo:
                        await context.bot.send_photo(chat_id=admin_id, photo=file_photo.file_id, caption=admin_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                    else:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=admin_text,
                            reply_markup=InlineKeyboardMarkup(buttons),
                            parse_mode="Markdown"
                        )
                except Exception as e:
                    logging.error(f"Failed to send work submission review to admin {admin_id}: {e}")
                    await msg.reply_text(f"⚠️ Warning: Could not send approval card to admin ({admin_id}). Please ensure the admin has opened a DM with the bot and sent /start.")

        # Clean up temporary prompt message
        prompt_msg_id = context.user_data.pop('prompt_msg_id', None)
        if prompt_msg_id:
            try:
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=prompt_msg_id)
            except Exception as e:
                logging.debug(f"Could not delete prompt message {prompt_msg_id}: {e}")

        # Notify user that submission is pending moderation
        sent_ack = await context.bot.send_message(
            chat_id=msg.chat_id,
            text="✅ Submission Received! Your work (and file attachment) has been sent to the moderators for review. You will be notified once it is approved and posted."
        )
        # Automatically remove the confirmation message after 15 seconds to keep the chat clean
        context.application.create_task(
            auto_delete_prompt(context, sent_ack.chat_id, sent_ack.message_id, delay=15)
        )

        # Clear remaining submission context data
        context.user_data.pop('submission_genre', None)
        context.user_data.pop('submission_type', None)
        context.user_data.pop('pen_name', None)
        return
        
    # CHECK FOR NESTED CRITIQUE REPLIES
    if parent_msg and "Critique Submission #" in (parent_msg.text or ""):
        sub_id = parent_msg.text.split("#")[1].split()[0]
        critique_text = (msg.text or msg.caption or "").strip()
        if not critique_text:
            return

        target_reply_id = parent_msg.reply_to_message.message_id if parent_msg.reply_to_message else parent_msg.message_id

        await context.bot.send_message(
            chat_id=msg.chat_id,
            message_thread_id=CRITIQUE_TOPIC_ID,
            reply_to_message_id=target_reply_id,
            text=f"💬 **Critique by @{user.username if user.username else user.first_name}:**\n\n{critique_text}",
            parse_mode="Markdown"
        )

        try:
            await msg.delete()
            await parent_msg.delete()
        except Exception:
            pass
        return

            
    # Admin .txt File Upload Handler in DM
    if update.effective_chat.type == 'private' and msg.document and await is_admin(msg.chat_id, user.id, context):
        doc = msg.document
        if (doc.file_name or "").endswith('.txt'):
            file = await context.bot.get_file(doc.file_id)
            byte_content = await file.download_as_bytearray()
            lines = byte_content.decode('utf-8').splitlines()
            
            entries = []
            for line in lines:
                if '|' in line:
                    parts = [p.strip() for p in line.split('|')]
                    if len(parts) >= 2:
                        entries.append(parts)
            
            added, skipped = bulk_insert_prompts(entries)
            await msg.reply_text(f"📄 **File Processed ({doc.file_name})**\nAdded: **{added}** | Duplicates Skipped: **{skipped}**", parse_mode="Markdown")
            return

    # Direct Message Reason Handler for Appeals & Resource Submissions
    if update.effective_chat.type == 'private' and user.id in USER_TICKET_STATE:
        state = USER_TICKET_STATE.pop(user.id)
        category = state.get("category", "General Request")
        details = msg.text or "No text provided."

        if category == "Resource Submission":
            mandatory_tag = state.get("mandatory_tag", "#fiction")
            
            # Capture caption/text or file attachment info
            caption = msg.caption or msg.text or "No description provided."
            file_info = ""
            
            if msg.document:
                file_info = f"\n📎 **Attached Document:** `{msg.document.file_name}`"
            elif msg.photo:
                file_info = "\n🖼️ **Attached Image**"
            
            full_content = f"**Category:** {mandatory_tag}\n{caption}{file_info}"
            t_id = create_ticket(user.id, category, full_content)

            username_str = f"@{user.username}" if user.username else "No Username"
            admin_text = (
                f"📦 **NEW RESOURCE SUBMISSION #{t_id}**\n"
                f"**Submitted By:** {user.full_name} ({username_str} | ID: `{user.id}`)\n\n"
                f"{full_content}"
            )
            # Truncate caption to stay safely within Telegram's 1024-character limit
            admin_caption = admin_text[:1000] + "..." if len(admin_text) > 1000 else admin_text
            buttons = [
                [
                    InlineKeyboardButton("✅ Approve & Post", callback_data=f"res_approve_{t_id}_{user.id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"res_reject_{t_id}_{user.id}")
                ]
            ]
            
            # Send resource submission review card to all admins
            print(f"DEBUG: Attempting to send submission to ADMIN_IDS: {ADMIN_IDS}")
            for admin_id in ADMIN_IDS:
                if admin_id and admin_id != 0:
                    try:
                        if msg.document:
                            await context.bot.send_document(chat_id=admin_id, document=msg.document.file_id, caption=admin_caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                        elif msg.photo:
                            await context.bot.send_photo(chat_id=admin_id, photo=msg.photo[-1].file_id, caption=admin_caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                        else:
                            await context.bot.send_message(chat_id=admin_id, text=admin_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                        print(f"DEBUG: Successfully sent submission to admin ID {admin_id}")
                    except Exception as e:
                        print(f"ERROR: Failed to send submission to admin ID {admin_id}: {e}")
                        logging.error(f"Failed to send resource submission to admin {admin_id}: {e}")
                        
            await msg.reply_text("✅ Your resource has been submitted to the moderators for review. You'll be notified when it's posted!")
            return

        if category == "Work Rejection Reason":
            target_id = state.get("target_user_id")
            t_id = state.get("ticket_id")
            reason = msg.text or "No reason provided."

            # Refund the 2 credits back to the user
            add_critique(target_id, 2)
            update_ticket_status(t_id, "REJECTED")

            # Send the custom reason directly to the user's DM
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        f"ℹ️ **Work Submission Update (Ticket #{t_id})**\n\n"
                        f"Your submission was declined by the moderators.\n"
                        f"💬 **Reason:** {reason}\n\n"
                        f"✅ Your 2 critique credits have been refunded to your balance."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Failed to DM user {target_id} rejection reason: {e}")

            await msg.reply_text(f"✅ Rejection reason sent to user and 2 credits refunded.")
            return
        
        if category == "Custom Prompt Purchase":
            use_critiques(user.id, 10)
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO prompts (category, challenge_type, prompt_text, priority) VALUES (?, ?, ?, ?)', ('custom', 'weekly', details, 0))
            conn.commit()
            cursor.close()
            conn.close()
            await msg.reply_text("✅ Your custom prompt has been deducted (10 credits) and sent to admins for review before entering the queue!")
            return

        draft = state.get("draft_text", "")
        full_details = f"{details}\n\n[Captured Draft: {draft}]" if draft else details
        t_id = create_ticket(user.id, category, full_details)

        admin_text = (
            f"🎫 **NEW SUPPORT TICKET #{t_id}**\n"
            f"**Category:** {category}\n"
            f"**User:** {user.full_name} (@{user.username} | ID: `{user.id}`)\n\n"
            f"**Details:**\n{full_details}"
        )
        
        buttons = [
            [
                InlineKeyboardButton("✅ Resolve & Grant 2 Credits", callback_data=f"tck_grant_{t_id}_{user.id}"),
                InlineKeyboardButton("❌ Dismiss Ticket", callback_data=f"tck_dismiss_{t_id}_{user.id}")
            ]
        ]
        
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=admin_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        await msg.reply_text(f"✅ Ticket #{t_id} submitted to group admins.")
        return

    if msg.message_thread_id != CRITIQUE_TOPIC_ID:
        return

    sync_user(user.id, user.username)

    if await is_admin(msg.chat_id, user.id, context) or "#mod" in (msg.text or msg.caption or "").lower():
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
        # Check if the message is a reply to a work submission
        parent_msg = msg.reply_to_message
        if not parent_msg:
            return

        # Ensure the review contains at least one explicit critique tag
        text_lower = msg.text.lower() if msg.text else ""
        has_valid_critique_tag = any(tag in text_lower for tag in ALLOWED_CRITIQUE_TAGS)

        if not has_valid_critique_tag:
            try:
                await msg.delete()
                # Optional: Notify user or log why it was deleted
            except Exception:
                pass
            return

        if parent_msg.from_user.id == user.id:
            return
            
        if has_user_reviewed_post(user.id, parent_msg.message_id):
            return

        words = len(text_lower.split())
        if words >= 20:
            log_post_review(user.id, parent_msg.message_id)
            add_critique(user.id, 1, user.username)
            total = get_critiques(user.id)
            
            note = await context.bot.send_message(
                chat_id=msg.chat_id,
                message_thread_id=msg.message_thread_id,
                text=f"✅ Valid critique logged for {user.first_name}! ({total}/2 completed)"
            )
            await asyncio.sleep(4)
            await note.delete()
        return

    # CASE B: STANDALONE DRAFT SUBMISSION
    if not is_real_reply and words > 0 and "Tags Selected:" not in parent_text:
        # --- ADD THIS SAFEGUARD CHECK HERE ---
        if is_message_restored(msg.message_id):
            return
        # -------------------------------------
        
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
            USER_TICKET_STATE[user.id] = {"draft_text": text}
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
            
            # Extract content safely for standalone posts
            submission_text = msg.text or msg.caption or "No text provided."
            preview = submission_text[:500] + "..." if len(submission_text) > 500 else submission_text
            
            admin_text = (
                f"📥 **NEW STANDALONE SUBMISSION FOR REVIEW**\n\n"
                f"**User:** {user.mention_markdown()} (`{user.id}`)\n"
                f"**Word Count:** {words}\n\n"
                f"**Submission:**\n{preview}"
            )
            buttons = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user.id}")
                ]
            ]
            
            for admin_id in ADMIN_IDS:
                if admin_id != 0:
                    try:
                        if msg.document:
                            await context.bot.send_document(chat_id=admin_id, document=msg.document.file_id, caption=admin_text[:1000], reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                        elif msg.photo:
                            await context.bot.send_photo(chat_id=admin_id, photo=msg.photo[-1].file_id, caption=admin_text[:1000], reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                        else:
                            await context.bot.send_message(chat_id=admin_id, text=admin_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Failed to send standalone review to admin {admin_id}: {e}")

# --- Callback Button Handlers ---
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user = update.effective_user

    if data.startswith("subtag_genre_"):
        await query.answer()
        selected_genre = data.replace("subtag_genre_", "")
        context.user_data['submission_genre'] = selected_genre
        
        type_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📄 Draft", callback_data="subtag_type_draft"),
                InlineKeyboardButton("🛠️ Work-in-Progress", callback_data="subtag_type_workinprogress"),
            ]
        ])
        
        # Edit message to show type selection
        sent_message = await query.edit_message_text(
            f"✅ Genre: **#{selected_genre.capitalize()}**\n\nNow select the post type:",
            reply_markup=type_keyboard,
            parse_mode="Markdown"
        )
        
        # Clean up this intermediate step quickly if abandoned halfway (e.g., 15 seconds)
        context.application.create_task(
            auto_delete_prompt(context, sent_message.chat_id, sent_message.message_id, delay=15)
        )
        return

    if data.startswith("subtag_type_"):
        selected_type = data.replace("subtag_type_", "")
        context.user_data['submission_type'] = selected_type
        
        genre = context.user_data.get('submission_genre', 'general')
        pen_name = get_pen_name(user.id)
        
        if not pen_name:
            await query.edit_message_text(
                "⚠️ You have not set a Pen Name yet!\n"
                "Please use `/setname YourPenName` first before submitting work.",
                parse_mode="Markdown"
            )
            return

        context.user_data['pen_name'] = pen_name
        context.user_data['waiting_for_content'] = True
        
        await query.edit_message_text(
            f"✅ **Tags Selected:** `#{genre} | #{selected_type}`\n"
            f"✍️ **Pen Name:** `{pen_name}`\n\n"
            f"Please reply to this message now with your **Title on the first line**, followed by your post text or attachment!",
            parse_mode="Markdown"
        )
        return

    elif data.startswith("subtag_type_"):
        await query.answer()
        selected_type = data.replace("subtag_type_", "")
        context.user_data['submission_type'] = selected_type  
        genre = context.user_data.get('submission_genre', 'general')
        
        # Set a flag telling the message handler the next text input is the pen name
        context.user_data['waiting_for_pen_name'] = True
        
        sent_message = await query.edit_message_text(
            f"✅ **Tags Selected:** #{genre} #{selected_type}\n\n"
            f"Please reply with your **Pen Name** (can be multiple words, e.g., *John Doe*):",
            reply_markup=None,
            parse_mode="Markdown"
        )

        context.application.create_task(
            auto_delete_prompt(context, sent_message.chat_id, sent_message.message_id, delay=300)
        )
        return

    # Submission Cards & Threaded Reviews
    elif data.startswith("sub_rev_"):
        parts = data.split("_")
        sub_id = parts[2]
        author = parts[3]
        await query.answer()

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            message_thread_id=CRITIQUE_TOPIC_ID,
            reply_to_message_id=query.message.message_id,
            text=f"✍️ **Critique for Submission #{sub_id} (Author: @{author})**\n\n"
                 f"Please reply directly to this message with your critique.\n"
                 f"⚠️ **Requirements:** Must include `#review` and be at least **20 words** to earn credit points.",
            parse_mode="Markdown"
        )
        return

    elif data.startswith("sub_stack_"):
        sub_id = int(data.split("_")[2])
        sub = get_submission(sub_id)
        reviews = get_submission_reviews(sub_id)

        if not reviews:
            await query.answer("📭 No critiques have been submitted for this piece yet.", show_alert=True)
            return

        stack_text = f"📚 **Feedback Stack for #{sub_id} ({sub[3]}):**\n\n"
        for reviewer_tag, r_text, r_time in reviews:
            stack_text += f"👤 **{reviewer_tag}** _({r_time[:10]}):_\n{r_text}\n\n---\n"

        await query.message.reply_text(stack_text, parse_mode="Markdown")
        return

    elif data == "shop_buy_prompt":
        credits = get_critiques(user.id)
        if credits < 10:
            await query.answer("❌ You need at least 10 credits to submit a custom prompt.", show_alert=True)
            return
        USER_TICKET_STATE[user.id] = {"category": "Custom Prompt Purchase"}
        await query.message.edit_text("✍️ **Custom Prompt Submission**\nPlease reply to this message with your prompt text. Once approved by admins, it will be queued to appear within the next 3 turns.", parse_mode="Markdown")

    elif data == "shop_buy_showcase":
        credits = get_critiques(user.id)
        if credits < 25:
            await query.answer("❌ You need at least 25 credits for the Showcase perk.", show_alert=True)
            return
        use_critiques(user.id, 25)
        await query.message.edit_text("🌟 **Showcase Purchased!** Your next submission will automatically be pinned to the channel and pushed to the front of the review queue.", parse_mode="Markdown")

    elif data == "shop_buy_badge":
        credits = get_critiques(user.id)
        if credits < 50:
            await query.answer("❌ You need at least 50 credits for a Role Badge.", show_alert=True)
            return
        use_critiques(user.id, 50)
        await query.message.edit_text("👑 **Role Badge Unlocked!** Please contact an admin with your desired custom title.", parse_mode="Markdown")

    elif data.startswith("tck_dismiss_"):
        parts = data.split("_")
        if len(parts) >= 4:
            t_id, target_id = int(parts[2]), int(parts[3])
            update_ticket_status(t_id, "DISMISSED")
            await query.edit_message_text(f"{query.message.text}\n\n❌ **DISMISSED:** Ticket closed.", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=target_id, text=f"ℹ️ Ticket #{t_id} reviewed and closed by community admins.")
            except Exception:
                pass

    elif data.startswith("join_approve_"):
        parts = data.split("_")
        if len(parts) >= 4:
            chat_id, target_user_id = int(parts[2]), int(parts[3])
            try:
                await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=target_user_id)
                await query.edit_message_text(f"{query.message.text}\n\n✅ **APPROVED:** User has been admitted to the group.", parse_mode="Markdown")
                try:
                    await context.bot.send_message(chat_id=target_user_id, text="🎉 Your request to join The August Society has been approved! Welcome.")
                except Exception:
                    pass
            except Exception as e:
                await query.answer(f"Failed to approve: {e}", show_alert=True)

    elif data.startswith("join_decline_"):
        parts = data.split("_")
        if len(parts) >= 4:
            chat_id, target_user_id = int(parts[2]), int(parts[3])
            try:
                await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=target_user_id)
                await query.edit_message_text(f"{query.message.text}\n\n❌ **DECLINED:** Request was rejected.", parse_mode="Markdown")
                try:
                    await context.bot.send_message(chat_id=target_user_id, text="ℹ️ Your request to join The August Society was declined by moderators.")
                except Exception:
                    pass
            except Exception as e:
                await query.answer(f"Failed to decline: {e}", show_alert=True)
     
    elif data.startswith("work_approve_"):
        parts = data.split("_")
        if len(parts) >= 4:
            t_id, target_id = int(parts[2]), int(parts[3])
            
            # Retrieve ticket message payload from SQLite DB
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('SELECT message, status FROM tickets WHERE ticket_id = ?', (t_id,))
            row = cursor.fetchone()
            conn.close()

            if not row:
                await query.answer("❌ Submission ticket not found.", show_alert=True)
                return

            status = row[1]
            if status in ["RESOLVED_APPROVED", "REJECTED"]:
                await query.answer("⚠️ This ticket has already been processed!", show_alert=True)
                return

            # Immediately update ticket status in DB to prevent race conditions/double-clicking
            update_ticket_status(t_id, "RESOLVED_APPROVED")

            # Remove inline buttons immediately so it cannot be clicked again
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            payload = row[0]
            lines = payload.splitlines()
            title = lines[0].replace("TITLE: ", "").strip()
            author_display = lines[1].replace("AUTHOR: ", "").strip()
            genre = lines[2].replace("GENRE: ", "").strip()
            post_type = lines[3].replace("TYPE: ", "").strip()
            custom_tags_str = lines[4].replace("CUSTOM_TAGS:", "").strip()
            formatted_pen_hashtag = lines[5].replace("PEN_HASHTAG: ", "").strip()
            
            content_start_idx = 0
            for idx, line in enumerate(lines):
                if "--------------------------------------------------" in line:
                    content_start_idx = idx + 1
                    break
            content = "\n".join(lines[content_start_idx:]).strip()

            # Create submission in PostgreSQL database to generate sub_id
            sub_id = create_submission(
                target_id, 
                author_display, 
                title, 
                genre, 
                post_type, 
                content
            )

            chunks = split_text_into_chunks(content, max_chars=3000)
            cluster_parts = len(chunks)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Leave Critique", callback_data=f"sub_rev_{sub_id}_{author_display}")]
            ])

            group_chat_id = int(os.getenv("GROUP_CHAT_ID", "-1001234567890"))

            for i, chunk in enumerate(chunks, 1):
                part_suffix = f" ({i}/{cluster_parts})" if cluster_parts > 1 else ""
                
                header = (
                    f"📖 SUBMISSION #{sub_id}: {title.upper()}{part_suffix}\n"
                    f"✍️ Author: {author_display}\n"
                    f"🏷️ Tags: {genre} {post_type} #submission {custom_tags_str} {formatted_pen_hashtag}\n"
                    f"--------------------------------------------------\n\n"
                )
                
                footer = (
                    f"\n\n--------------------------------------------------\n"
                    f"💬 Click 'Leave Critique' below or reply directly to review this work!" 
                    if i == cluster_parts else ""
                )

                formatted_post = f"{header}{chunk}{footer}"
                reply_markup = keyboard if i == cluster_parts else None

                if CHANNEL_ID:
                    try:
                        await context.bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=formatted_post,
                            parse_mode=None
                        )
                    except Exception as e:
                        logging.error(f"Failed to post to channel: {e}")

                await context.bot.send_message(
                    chat_id=group_chat_id,
                    message_thread_id=CRITIQUE_TOPIC_ID,
                    text=formatted_post,
                    reply_markup=reply_markup,
                    parse_mode=None
                )

            await query.message.reply_text(f"✅ **APPROVED & POSTED:** Ticket #{t_id} published as Submission #{sub_id}.", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=target_id, text=f"🎉 Your work submission (#{sub_id}) has been approved and published!")
            except Exception:
                pass

    elif data.startswith("work_reject_"):
        parts = data.split("_")
        if len(parts) >= 4:
            t_id, target_id = int(parts[2]), int(parts[3])
            
            # Check DB to prevent double-clicking or acting on an already resolved ticket
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('SELECT status FROM tickets WHERE ticket_id = ?', (t_id,))
            row = cursor.fetchone()
            conn.close()

            if row and row[0] in ["RESOLVED_APPROVED", "REJECTED"]:
                await query.answer("⚠️ This ticket has already been processed!", show_alert=True)
                return

            # Remove inline buttons immediately so it cannot be double-clicked
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            # Save state so the bot knows the admin is about to type a rejection reason in DM
            USER_TICKET_STATE[user.id] = {
                "category": "Work Rejection Reason",
                "target_user_id": target_id,
                "ticket_id": t_id,
                "admin_msg_id": query.message.message_id
            }
            
            await query.edit_message_text(f"{query.message.text}\n\n⏳ **Awaiting Reason:** Please reply to this message (or send a message here in DM) with the reason for rejection. It will be sent directly to the user along with their credit refund.", parse_mode="Markdown")
            return
    
    # Interactive Queue Manager Callbacks
    if data.startswith("q_view_"):
        cat = data.replace("q_view_", "")
        prompts = get_prompts_by_category(cat)

        if not prompts:
            await query.edit_message_text(f"📭 No queued prompts found for **{cat.capitalize()}**.", parse_mode="Markdown")
            return

        text = f"📋 **Upcoming {cat.capitalize()} Prompts:**\n\n"
        buttons = []
        for p_id, ctype, p_text, prio in prompts:
            flag = "📌 [NEXT UP] " if prio == 1 else ""
            text += f"{flag}**#{p_id}** [{ctype.upper()}]: _{p_text[:60]}..._\n\n"
            buttons.append([
                InlineKeyboardButton(f"🚀 Make #{p_id} Next", callback_data=f"q_next_{p_id}_{cat}"),
                InlineKeyboardButton(f"🗑️ Delete #{p_id}", callback_data=f"q_del_{p_id}_{cat}")
            ])

        buttons.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="q_back")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return

    elif data.startswith("q_next_"):
        parts = data.split("_")
        p_id, cat = int(parts[2]), parts[3]
        set_prompt_priority(p_id)
        await query.edit_message_text(f"✅ **Prompt #{p_id} set as NEXT for broadcast!**")
        return

    elif data.startswith("q_del_"):
        parts = data.split("_")
        p_id, cat = int(parts[2]), parts[3]
        delete_prompt(p_id)
        await query.edit_message_text(f"🗑️ **Prompt #{p_id} deleted from queue.**")
        return

    elif data == "q_back":
        await cmd_manageprompts(update, context)
        return

    elif data.startswith("hub_cat_"):
        cat_map = {
            "hub_cat_appeal": "Post Appeal",
            "hub_cat_credits": "Credit Dispute",
            "hub_cat_report": "Content/User Report",
            "hub_cat_other": "General Inquiry"
        }
        selected = cat_map.get(data, "General Inquiry")
        USER_TICKET_STATE[user.id] = {"category": selected}
        await query.edit_message_text(f"📝 **Category Selected:** {selected}\n\nPlease reply with details for the admin team.")

    elif data.startswith("tck_grant_"):
        parts = data.split("_")
        if len(parts) >= 4:
            t_id, target_id = int(parts[2]), int(parts[3])
            add_critique(target_id, 2)
            update_ticket_status(t_id, "RESTORED")
            await query.edit_message_text(f"{query.message.text}\n\n✅ **RESTORED & RESOLVED:** Granted 2 credits to user `{target_id}`.", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=target_id, text=f"🎉 Ticket #{t_id} resolved! 2 critique credits added to your balance.")
            except Exception:
                pass

    elif data.startswith("res_tag_"):
        tag = data.split("_")[-1]
        mandatory_tag = f"#{tag}"
        USER_TICKET_STATE[user.id] = {
            "category": "Resource Submission",
            "mandatory_tag": mandatory_tag
        }
        await query.message.edit_text(
            f"✅ Selected category: **{mandatory_tag}**\n\n"
            "Now, please upload your file (`.pdf`, `.txt`, `.docx`, `.pptx`, `.xlsx`, `.md`, images) or send a weblink along with a description and any additional tags (e.g., `#academic`, `#research`, `#writingguide`, `#toolkit`, `#history`, etc.).",
            parse_mode="Markdown"
        )
        return
    
    elif data.startswith("res_approve_"):
        parts = data.split("_")
        if len(parts) >= 4:
            t_id, target_id = int(parts[2]), int(parts[3])
            original_text = query.message.text
            
            # Extract content payload from admin ticket message
            content_part = original_text.split("**NEW RESOURCE SUBMISSION**")[-1].split("---")[0] if "**NEW RESOURCE SUBMISSION**" in original_text else original_text

            formatted_resource = (
                f"🌟 **COMMUNITY RESOURCE HUB**\n\n"
                f"{content_part}\n\n"
                f"--- \n"
                f"💡 *Verified Resource Hub Submission*"
            )
            
            group_chat_id = int(os.getenv("GROUP_CHAT_ID", "-1001234567890"))
            await context.bot.send_message(
                chat_id=group_chat_id,
                message_thread_id=RESOURCE_HUB_TOPIC_ID,
                text=formatted_resource,
                parse_mode="Markdown"
            )
            
            update_ticket_status(t_id, "RESOLVED_APPROVED")
            await query.edit_message_text(f"{query.message.text}\n\n✅ **APPROVED & POSTED** to the Resource Hub.", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=target_id, text=f"🎉 Your resource submission (#{t_id}) has been approved and published to the Resource Hub!")
            except Exception:
                pass

    elif data.startswith("res_reject_"):
        parts = data.split("_")
        if len(parts) >= 4:
            t_id, target_id = int(parts[2]), int(parts[3])
            update_ticket_status(t_id, "REJECTED")
            await query.edit_message_text(f"{query.message.text}\n\n❌ **REJECTED**", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=target_id, text=f"ℹ️ Your resource submission (#{t_id}) was reviewed and declined by moderators.")
            except Exception:
                pass

async def send_scheduled_prompt(context: ContextTypes.DEFAULT_TYPE):
    prompt_data = get_next_prompt_to_dispatch()
    if not prompt_data:
        return

    prompt_id, category, challenge_type, prompt_text = prompt_data
    chat_id = context.job.chat_id

    tag_cat = f"#{category.capitalize()}"
    tag_type = f"#{challenge_type.replace('_', ' ').title().replace(' ', '')}"

    formatted_message = (
        f"✍️ **COMMUNITY WRITING PROMPT**\n"
        f"Category: {tag_cat} | Type: {tag_type}\n\n"
        f"{prompt_text}\n\n"
        f"--- \n"
        f"💬 Share your response or post work inspired by this prompt in `#critique-corner`!"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=PROMPTS_TOPIC_ID,
        text=formatted_message,
        parse_mode="Markdown"
    )
    mark_prompt_used(prompt_id)
    
from telegram import BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators

async def post_init(application: Application):
    # Commands visible to all regular users in groups
    user_commands = [
        BotCommand("start", "Start the bot and view main menu"),
        BotCommand("mycredits", "Check your current review credits"),
        BotCommand("submitwork", "Submit work for structured critique"),
        BotCommand("submitresource", "Submit a resource for the Resource Hub"),
        BotCommand("leaderboard", "View review leaderboard"),
        BotCommand("shop", "Spend credits on perks and rewards"),
        BotCommand("support", "Open a support or report ticket"),
    ]
    
    # Commands visible ONLY to group administrators
    admin_commands = user_commands + [
        BotCommand("addprompts", "Admin: Add prompts via text"),
        BotCommand("manageprompts", "Admin: Open prompt queue manager"),
        BotCommand("addcredits", "Admin: Add credits to a user"),
        BotCommand("resetcredits", "Admin: Reset credits for a user"),
    ]

    # Set default user commands globally for everyone
    await application.bot.set_my_commands(user_commands)
    
    # Set admin commands for your specific admin user ID so they appear in private DMs
    for admin_id in ADMIN_IDS:
        if admin_id != 0:
            try:
                from telegram import BotCommandScopeChat
                await application.bot.set_my_commands(
                    admin_commands, 
                    scope=BotCommandScopeChat(chat_id=admin_id)
                )
            except Exception:
                pass

from datetime import datetime

async def update_leaderboard_topic(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    top_users = get_top_users(10)
    current_time = datetime.now().strftime("%B %d, %Y at %H:%M")
    
    text = (
        f"🏆 **Daily Community Review Leaderboard** 🏆\n"
        f"📅 *Updated on: {current_time}*\n\n"
    )
    for idx, (uname, count) in enumerate(top_users, 1):
        display_name = uname or f"User #{idx}"
        text += f"{idx}. {display_name} — **{count} credits**\n"
        
    text += (
        "\n--- \n"
        "💡 Use `/leaderboard` for latest scores and `/mycredits` for your current score."
    )
    
    job_data = context.job.data if context.job.data else {}
    msg_id = job_data.get("leaderboard_msg_id")
    
    try:
        if msg_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="Markdown"
            )
            return
    except Exception:
        pass 
            
    try:
        sent_msg = await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=LEADERBOARD_TOPIC_ID,
            text=text,
            parse_mode="Markdown"
        )
        job_data["leaderboard_msg_id"] = sent_msg.message_id
        context.job.data = job_data
    except Exception as e:
        logging.error(f"Failed to post daily leaderboard: {e}")

async def track_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically records any new member joining the group into the database."""
    result = update.effective_chat
    if not result:
        return
        
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        # Automatically syncs/registers them in your PostgreSQL user_critiques table
        sync_user(member.id, member.username)
        logging.info(f"Recorded new member: {member.full_name} (@{member.username} | ID: {member.id})")
        
async def send_conditional_prompt(context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime
    now = datetime.now()
    weekday = now.weekday() # 0: Mon, 2: Wed, 5: Sat
    day_of_month = now.day

    target_type = None
    if day_of_month == 1:
        target_type = 'monthly'
    elif weekday == 0 or weekday == 2: # Monday or Wednesday
        target_type = 'quick prompt'
    elif weekday == 5: # Saturday
        target_type = 'weekly'
    else:
        return # Do not post on other days

    # Fetch prompt matching the required challenge type from DB queue
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT prompt_id, category, challenge_type, prompt_text FROM prompts WHERE is_used = 0 AND challenge_type = ? ORDER BY priority DESC, prompt_id ASC LIMIT 1',
        (target_type,)
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return

    prompt_id, category, challenge_type, prompt_text = row
    chat_id = context.job.chat_id

    tag_cat = f"#{category.capitalize()}"
    tag_type = f"#{challenge_type.replace('_', ' ').title().replace(' ', '')}"

    formatted_message = (
        f"✍️ **COMMUNITY WRITING PROMPT**\n"
        f"Category: {tag_cat} | Type: {tag_type}\n\n"
        f"{prompt_text}\n\n"
        f"--- \n"
        f"💬 Share your response or post work inspired by this prompt in `#critique-corner`!"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=PROMPTS_TOPIC_ID,
        text=formatted_message,
        parse_mode="Markdown"
    )
    mark_prompt_used(prompt_id)

from telegram import ChatJoinRequest

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures a user's request to join via invite link and notifies admins."""
    req: ChatJoinRequest = update.chat_join_request
    user = req.from_user
    chat = req.chat

    # Automatically sync user to database if needed
    sync_user(user.id, user.username)

    admin_text = (
        f"🚪 **NEW JOIN REQUEST**\n\n"
        f"**User:** {user.full_name} (@{user.username or 'none'} | ID: `{user.id}`)\n"
        f"**Group:** {chat.title}"
    )
    
    buttons = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"join_approve_{chat.id}_{user.id}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"join_decline_{chat.id}_{user.id}")
        ]
    ]

    # Send the approval card to all hardcoded admins or post it to an admin channel/chat
    # Here we send it to your hardcoded ADMIN_IDS
    for admin_id in ADMIN_IDS:
        if admin_id != 0:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=admin_text,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Failed to send join request to admin {admin_id}: {e}")

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # Register Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("mycredits", cmd_mycredits))
    app.add_handler(CommandHandler("addcredits", cmd_addcredits))
    app.add_handler(CommandHandler("resetcredits", cmd_resetcredits))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("report", cmd_support))
    app.add_handler(CommandHandler("addprompts", cmd_addprompts))
    app.add_handler(CommandHandler("manageprompts", cmd_manageprompts))
    app.add_handler(CommandHandler("submitwork", cmd_submitwork))
    app.add_handler(CommandHandler("submitresource", cmd_submitresource))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CommandHandler("setname", set_name_command))

    # Register Job Queue
    group_chat_id = int(os.getenv("GROUP_CHAT_ID", "-1001234567890"))
    if app.job_queue:
        from zoneinfo import ZoneInfo
        from datetime import time

        app.job_queue.run_daily(
            send_conditional_prompt,
            time=time(hour=9, minute=0, second=0, tzinfo=ZoneInfo("Asia/Kolkata")),
            chat_id=group_chat_id,
            name="conditional_prompt_job"
        )
        app.job_queue.run_daily(
            update_leaderboard_topic,
            time=time(hour=9, minute=0, second=0, tzinfo=ZoneInfo("Asia/Kolkata")),
            chat_id=group_chat_id,
            name="daily_leaderboard_job"
        )

    # Register Handlers
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    
    # Track any new members joining the group
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, track_new_members))
    
    # Handle incoming join requests from invite links
    from telegram.ext import ChatJoinRequestHandler
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    
    # Process incoming text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_chat))

    print("Bot is listening...")
    app.run_polling()
    
if __name__ == '__main__':
    main()
