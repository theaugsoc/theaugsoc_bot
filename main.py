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

TOKEN = os.getenv('BOT_TOKEN', '8883883367:AAFc6zoJaz-K9CgZovzwpuAOHfN1IxUgOaU')
CRITIQUE_TOPIC_ID = 8
PROMPTS_TOPIC_ID = 9  # Update this to your exact "Prompts and Challenges" Topic ID

USER_TICKET_STATE = {}  # { user_id: {"category": str, "draft_text": str} }

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

# ADD HERE:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category TEXT,
            details TEXT,
            status TEXT DEFAULT 'OPEN'
        )
    ''')
    conn.commit()
    conn.close()

# --- PROMPT AUTOMATION TABLE SETUP ---
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS prompts (
            prompt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            challenge_type TEXT DEFAULT 'weekly',
            prompt_text TEXT UNIQUE,
            priority INTEGER DEFAULT 0,
            is_used INTEGER DEFAULT 0
        )
    ''')

    # Submission Cards & Threaded Feedback Tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS submissions (
            sub_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            author_tag TEXT,
            title TEXT,
            genre_tag TEXT,
            post_tag TEXT,
            content TEXT,
            msg_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS submission_reviews (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_id INTEGER,
            reviewer_id INTEGER,
            reviewer_tag TEXT,
            review_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Pre-load initial prompts if queue is empty
    cursor.execute('SELECT COUNT(*) FROM prompts')
    if cursor.fetchone()[0] == 0:
        initial_prompts = [
            ('poetry', 'weekly', 'Write a 14-line sonnet exploring the concept of digital silence.'),
            ('fiction', 'micro', 'Write a 100-word story that starts with: "The package arrived three days late, completely unsealed."'),
            ('non-fiction', 'weekly', 'Draft a reflective piece on an object from your childhood that no longer exists.'),
            ('poetry', 'micro', 'Craft a free verse poem of under 10 lines focusing strictly on sound and tactile imagery.'),
            ('fiction', 'monthly_arc', 'Write a scene featuring two characters forced to negotiate in a place where speaking aloud is dangerous.')
        ]
        cursor.executemany(
            'INSERT OR IGNORE INTO prompts (category, challenge_type, prompt_text) VALUES (?, ?, ?)',
            initial_prompts
        )
    conn.commit()
    conn.close()

# --- PROMPT DATABASE HELPERS ---
def bulk_insert_prompts(prompt_list: list) -> tuple:
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    added = 0
    skipped = 0
    for item in prompt_list:
        cat = item[0].strip().lower()
        if len(item) == 3:
            ctype = item[1].strip().lower()
            text = item[2].strip()
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
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute(
        'SELECT prompt_id, challenge_type, prompt_text, priority FROM prompts WHERE category = ? AND is_used = 0 ORDER BY priority DESC, prompt_id ASC LIMIT ?',
        (category.lower(), limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

def set_prompt_priority(prompt_id: int):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE prompts SET priority = 0 WHERE priority = 1')
    cursor.execute('UPDATE prompts SET priority = 1 WHERE prompt_id = ?', (prompt_id,))
    conn.commit()
    conn.close()

def delete_prompt(prompt_id: int):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('DELETE FROM prompts WHERE prompt_id = ?', (prompt_id,))
    conn.commit()
    conn.close()

def get_next_prompt_to_dispatch() -> tuple:
    conn = sqlite3.connect('critiques.db')
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
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE prompts SET is_used = 1, priority = 0 WHERE prompt_id = ?', (prompt_id,))
    conn.commit()
    conn.close()

# --- SUBMISSION & REVIEW HELPERS ---
ALLOWED_GENRE_TAGS = {'#poetry', '#fiction', '#nonfiction', '#prose', '#essay'}
ALLOWED_POST_TAGS = {'#critique', '#submission', '#feedback', '#wip'}

def parse_and_validate_hashtags(text: str) -> tuple[bool, str, str]:
    tokens = [t.lower() for t in text.split()]
    found_genre = next((t for t in tokens if t in ALLOWED_GENRE_TAGS), None)
    found_post = next((t for t in tokens if t in ALLOWED_POST_TAGS), None)
    is_valid = bool(found_genre and found_post)
    return is_valid, found_genre, found_post

def create_submission(user_id: int, author_tag: str, title: str, genre_tag: str, post_tag: str, content: str) -> int:
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO submissions (user_id, author_tag, title, genre_tag, post_tag, content) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, author_tag, title, genre_tag, post_tag, content)
    )
    sub_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return sub_id

def update_submission_msg_id(sub_id: int, msg_id: int):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE submissions SET msg_id = ? WHERE sub_id = ?', (msg_id, sub_id))
    conn.commit()
    conn.close()

def add_submission_review(sub_id: int, reviewer_id: int, reviewer_tag: str, review_text: str):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO submission_reviews (sub_id, reviewer_id, reviewer_tag, review_text) VALUES (?, ?, ?, ?)',
        (sub_id, reviewer_id, reviewer_tag, review_text)
    )
    conn.commit()
    conn.close()

def get_submission(sub_id: int):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('SELECT sub_id, user_id, author_tag, title, genre_tag, post_tag, content, msg_id FROM submissions WHERE sub_id = ?', (sub_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def get_submission_reviews(sub_id: int) -> list:
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('SELECT reviewer_tag, review_text, created_at FROM submission_reviews WHERE sub_id = ? ORDER BY review_id ASC', (sub_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def create_ticket(user_id: int, category: str, details: str) -> int:
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO tickets (user_id, category, details) VALUES (?, ?, ?)', (user_id, category, details))
    ticket_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return ticket_id

def update_ticket_status(ticket_id: int, status: str):
    conn = sqlite3.connect('critiques.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE tickets SET status = ? WHERE ticket_id = ?', (status, ticket_id))
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

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sync_user(user.id, user.username)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Post Appeal", callback_data="hub_cat_appeal")],
        [InlineKeyboardButton("💳 Credit Dispute", callback_data="hub_cat_credits")],
        [InlineKeyboardButton("🚩 Report Content/User", callback_data="hub_cat_report")],
        [InlineKeyboardButton("❓ General Help", callback_data="hub_cat_other")]
    ])
    
    if update.effective_chat.type != 'private':
        bot_user = await context.bot.get_me()
        await update.message.reply_text(f"Please reach out to me in private DM: https://t.me/{bot_user.username}?start=support")
    else:
        await update.message.reply_text("🛠️ **The Aug Society Support Hub**\nPlease select a category:", reply_markup=keyboard, parse_mode="Markdown")

async def cmd_submitwork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    
    raw_text = msg.text.replace("/submitwork", "").strip()
    lines = raw_text.splitlines()

    if len(lines) < 2:
        await msg.reply_text(
            "✍️ **How to Submit Work for Critique:**\n\n"
            "Format your message like this:\n"
            "`/submitwork Title of Your Piece`\n"
            "`#fiction #critique`\n"
            "`Paste your full poem or story text here...`",
            parse_mode="Markdown"
        )
        return

    title = lines[0].strip()
    tag_line = lines[1].strip()
    content = "\n".join(lines[2:]).strip()

    is_valid, genre_tag, post_tag = parse_and_validate_hashtags(tag_line)
    if not is_valid:
        await msg.reply_text(
            "❌ **Missing Required Hashtags!**\n\n"
            "Your submission must include at least one **Genre** tag (`#poetry`, `#fiction`, `#nonfiction`) "
            "and one **Post Type** tag (`#critique`, `#submission`, `#feedback`).",
            parse_mode="Markdown"
        )
        return

    sub_id = create_submission(user.id, f"@{user.username}" if user.username else user.first_name, title, genre_tag, post_tag, content)
    
    # Format Card
    preview = content[:300] + ("..." if len(content) > 300 else "")
    card_text = (
        f"📖 **SUBMISSION #{sub_id}: {title.upper()}**\n"
        f"✍️ Author: @{user.username if user.username else user.first_name} | Tags: {genre_tag} {post_tag}\n"
        f"--------------------------------------------------\n"
        f"{preview}\n"
        f"--------------------------------------------------\n"
        f"📊 Critiques Received: 0"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 Leave Critique", callback_data=f"sub_rev_{sub_id}"),
            InlineKeyboardButton("🔍 View Stack (0)", callback_data=f"sub_stack_{sub_id}")
        ]
    ])

    sent_msg = await context.bot.send_message(
        chat_id=msg.chat_id,
        message_thread_id=CRITIQUE_TOPIC_ID,
        text=card_text,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    update_submission_msg_id(sub_id, sent_msg.message_id)

    if msg.chat.type != 'private':
        try:
            await msg.delete()
        except Exception:
            pass

async def cmd_addprompts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not await is_admin(msg.chat_id, user.id, context):
        await msg.reply_text("❌ Admin command only.")
        return

    raw_text = msg.text.replace("/addprompts", "").strip()
    if not raw_text:
        await msg.reply_text(
            "Usage:\n`/addprompts\ncategory | challenge_type | prompt text`\n\n"
            "Example:\n`poetry | weekly | Write a sonnet about dusk.`", 
            parse_mode="Markdown"
        )
        return

    entries = []
    for line in raw_text.splitlines():
        if '|' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                entries.append(parts)

    added, skipped = bulk_insert_prompts(entries)
    await msg.reply_text(f"✅ **Ingestion Complete**\nAdded: **{added}** | Duplicates Skipped: **{skipped}**", parse_mode="Markdown")

async def cmd_manageprompts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not await is_admin(msg.chat_id, user.id, context):
        await msg.reply_text("❌ Admin command only.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Poetry Queue", callback_data="q_view_poetry")],
        [InlineKeyboardButton("📖 Fiction Queue", callback_data="q_view_fiction")],
        [InlineKeyboardButton("📝 Non-Fiction Queue", callback_data="q_view_non-fiction")]
    ])
    await msg.reply_text("🗂️ **Prompt Queue Manager**\nSelect a genre category to inspect or rearrange:", reply_markup=keyboard, parse_mode="Markdown")
    
# --- Moderation & Appeal Logic ---
async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return

    # Enforce mandatory hashtags for raw chat messages posted directly in Critique Corner
    if msg.message_thread_id == CRITIQUE_TOPIC_ID and not user.is_bot:
        is_valid, _, _ = parse_and_validate_hashtags(msg.text or "")
        if not is_valid and not msg.text.startswith("/submitwork"):
            try:
                await msg.delete()
                await context.bot.send_message(
                    chat_id=user.id,
                    text=(
                        "⚠️ **Message Removed from #critique-corner**\n\n"
                        "All submissions must include a **Genre** hashtag (`#poetry`, `#fiction`, `#nonfiction`) "
                        "and a **Post Type** hashtag (`#critique`, `#submission`).\n\n"
                        "Please use `/submitwork` to create a structured submission card!"
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            return
            
    # Admin .txt File Upload Handler in DM
    if update.effective_chat.type == 'private' and msg.document and await is_admin(msg.chat_id, user.id, context):
        doc = msg.document
        if doc.file_name.endswith('.txt'):
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

    # Direct Message Reason Handler for Appeals
    if update.effective_chat.type == 'private' and user.id in USER_TICKET_STATE:
        state = USER_TICKET_STATE.pop(user.id)
        category = state.get("category", "General Request")
        details = msg.text or "No text provided."
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

# --- Callback Button Handlers ---
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Submission Cards & Threaded Reviews
    if data.startswith("sub_rev_"):
        sub_id = int(data.split("_")[2])
        context.user_data['reviewing_sub_id'] = sub_id
        await query.message.reply_text(
            f"📝 **Writing Feedback for Submission #{sub_id}**\n"
            f"Reply directly to this message with your critique. It will be attached under the main card!",
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
        
    await query.answer()
    data = query.data
    user = update.effective_user

    if data.startswith("hub_cat_"):
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
        t_id, target_id = int(parts[2]), int(parts[3])
        add_critique(target_id, 2)
        update_ticket_status(t_id, "RESOLVED_GRANTED")
        await query.edit_message_text(f"{query.message.text}\n\n✅ **RESOLVED:** Granted 2 credits to user `{target_id}`.", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text=f"🎉 Ticket #{t_id} resolved! 2 critique credits added to your balance.")
        except Exception:
            pass

    elif data.startswith("tck_dismiss_"):
        parts = data.split("_")
        t_id, target_id = int(parts[2]), int(parts[3])
        update_ticket_status(t_id, "DISMISSED")
        await query.edit_message_text(f"{query.message.text}\n\n❌ **DISMISSED:** Ticket closed.", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text=f"ℹ️ Ticket #{t_id} reviewed and closed by community admins.")
        except Exception:
            pass

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sync_user(user.id, user.username)
    
    if context.args and context.args[0] == "appeal":
        draft = USER_TICKET_STATE.get(user.id, {}).get("draft_text", "[No draft captured]")
        USER_TICKET_STATE[user.id] = {"category": "Post Appeal", "draft_text": draft}
        await update.message.reply_text("📩 **Post Appeal**: Please reply to this message with an explanation for your appeal.")
    elif context.args and context.args[0] == "support":
        await cmd_support(update, context)
    else:
        await update.message.reply_text("Hello! I am The Aug Soc community manager bot. Type /mycredits to check balance or /support for help.")

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
    
def main():
    init_db()
    keep_alive()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("mycredits", cmd_mycredits))
    app.add_handler(CommandHandler("addcredits", cmd_addcredits))
    app.add_handler(CommandHandler("resetcredits", cmd_resetcredits))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("report", cmd_support))
    app.add_handler(CommandHandler("addprompts", cmd_addprompts))
    app.add_handler(CommandHandler("manageprompts", cmd_manageprompts))
    app.add_handler(CommandHandler("submitwork", cmd_submitwork))
    # Set slash commands auto-complete list in Telegram
    import asyncio
    from telegram import BotCommand

    async def register_commands():
        commands = [
            BotCommand("start", "Start the bot and view main menu"),
            BotCommand("mycredits", "Check your current review credits"),
            BotCommand("submitwork", "Submit work for structured critique"),
            BotCommand("support", "Open a support or report ticket"),
            BotCommand("addprompts", "Admin: Add prompts via text"),
            BotCommand("manageprompts", "Admin: Open prompt queue manager"),
            BotCommand("addcredits", "Admin: Add credits to a user"),
            BotCommand("resetcredits", "Admin: Reset credits for a user"),
        ]
        await app.bot.set_my_commands(commands)

    asyncio.get_event_loop().run_until_complete(register_commands())
    GROUP_CHAT_ID = -1001234567890  # Replace with your actual Telegram Group ID
    if app.job_queue:
        app.job_queue.run_repeating(
            send_scheduled_prompt,
            interval=604800,  # 7 days in seconds
            first=10,         # Fires 10s after script boot for testing
            chat_id=GROUP_CHAT_ID,
            name="weekly_prompt_job"
        )
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.ALL, process_chat))

    print("Bot is listening...")
    app.run_polling()

if __name__ == '__main__':
    main()
