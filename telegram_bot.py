import asyncio
import html
import os
import sqlite3
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
DEFAULT_CATEGORY = os.getenv("DEFAULT_TASK_CATEGORY", "study")
DEFAULT_PRIORITY = os.getenv("DEFAULT_TASK_PRIORITY", "normal")
MAX_TASKS_PER_MESSAGE = int(os.getenv("MAX_TASKS_PER_MESSAGE", "25"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://taskboard7.up.railway.app").rstrip("/")

class TelegramTokenStore:
    """Persistent, single-use Telegram login-token storage.

    SQLite is sufficient here because tokens are short-lived and writes are tiny.
    Set TELEGRAM_TOKEN_DB_PATH to a persistent Railway volume or another shared
    filesystem when running more than one process.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS telegram_login_tokens (
                    token TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    telegram_id INTEGER,
                    display_name TEXT,
                    telegram_username TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    verify_chat_id INTEGER,
                    verify_message_id INTEGER
                )"""
            )
            conn.commit()

    def create(self, token: str, created_at: float) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO telegram_login_tokens(token, created_at) VALUES (?, ?)",
                (token, created_at),
            )
            conn.commit()

    def get(self, token: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM telegram_login_tokens WHERE token = ?",
                (token,),
            ).fetchone()
        return dict(row) if row else None

    def consume_verified(self, token: str, now: float, max_age: float = 300) -> dict[str, Any] | None:
        """Atomically validate and consume a verified token exactly once."""
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM telegram_login_tokens WHERE token = ? AND verified = 1",
                (token,),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            if now - float(row["created_at"]) > max_age:
                conn.execute("DELETE FROM telegram_login_tokens WHERE token = ?", (token,))
                conn.commit()
                return None
            conn.execute("DELETE FROM telegram_login_tokens WHERE token = ?", (token,))
            conn.commit()
            return dict(row)

    def update(self, token: str, values: dict[str, Any]) -> None:
        allowed = {
            "telegram_id", "display_name", "telegram_username", "verified",
            "verify_chat_id", "verify_message_id",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [int(v) if key == "verified" else v for key, v in values.items()]
        params.append(token)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                f"UPDATE telegram_login_tokens SET {assignments} WHERE token = ?",
                params,
            )
            conn.commit()

    def delete(self, token: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM telegram_login_tokens WHERE token = ?", (token,))
            conn.commit()

    def purge_expired(self, cutoff: float) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM telegram_login_tokens WHERE created_at < ?", (cutoff,))
            conn.commit()


TELEGRAM_TOKEN_DB_PATH = os.getenv(
    "TELEGRAM_TOKEN_DB_PATH",
    os.path.join(os.getenv("TMPDIR", "/tmp"), "taskboard_telegram_tokens.sqlite3"),
)
telegram_token_store = TelegramTokenStore(TELEGRAM_TOKEN_DB_PATH)


class SupabaseError(RuntimeError):
    pass


class SupabaseClient:
    def __init__(self, url: str, key: str) -> None:
        self.base_url = f"{url}/rest/v1"
        self.client = httpx.AsyncClient(
            timeout=20,
            headers={
                "apikey": key,
                "authorization": f"Bearer {key}",
                "content-type": "application/json",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, f"{self.base_url}/{path}", **kwargs)
        if response.status_code >= 400:
            raise SupabaseError(response.text)
        if not response.content:
            return None
        return response.json()

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        rows = await self.request(
            "GET",
            "users",
            params={
                "email": f"eq.{email}",
                "select": "id,name,email,telegram_chat_id,study_subjects,study_date,today_study_seconds,total_study_seconds,study_timer",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def get_user_by_chat_id(self, chat_id: int) -> dict[str, Any] | None:
        rows = await self.request(
            "GET",
            "users",
            params={
                "telegram_chat_id": f"eq.{chat_id}",
                "select": "id,name,email,telegram_chat_id,study_subjects,study_date,today_study_seconds,total_study_seconds,study_timer",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def update_study_data(self, user_id: Any, study_subjects: list[dict[str, Any]], today_study_seconds: int, total_study_seconds: int, study_date: str, study_timer: dict[str, Any] | None) -> None:
        await self.request(
            "PATCH",
            "users",
            params={"id": f"eq.{user_id}"},
            headers={"prefer": "return=minimal"},
            json={
                "study_subjects": study_subjects,
                "today_study_seconds": today_study_seconds,
                "total_study_seconds": total_study_seconds,
                "study_date": study_date,
                "study_timer": study_timer
            },
        )

    async def link_chat(self, user_id: Any, chat_id: int) -> None:
        await self.request(
            "PATCH",
            "users",
            params={"id": f"eq.{user_id}"},
            headers={"prefer": "return=minimal"},
            json={"telegram_chat_id": str(chat_id)},
        )

    async def unlink_chat(self, chat_id: int) -> None:
        await self.request(
            "PATCH",
            "users",
            params={"telegram_chat_id": f"eq.{chat_id}"},
            headers={"prefer": "return=minimal"},
            json={"telegram_chat_id": None},
        )

    async def pending_tasks(self, user_id: Any) -> list[dict[str, Any]]:
        return await self.request(
            "GET",
            "tasks",
            params={
                "user_id": f"eq.{user_id}",
                "done": "eq.false",
                "select": "id,title,category,priority,date,time,created_at",
                "order": "date.asc,time.asc,created_at.asc",
                "limit": "50",
            },
        )

    async def add_tasks(self, user_id: Any, titles: list[str], date_key: str) -> list[dict[str, Any]]:
        payload = [
            {
                "user_id": user_id,
                "title": title,
                "description": "",
                "category": DEFAULT_CATEGORY,
                "priority": DEFAULT_PRIORITY,
                "date": date_key,
                "time": None,
                "subtasks": [],
                "done": False,
                "pinned": False,
            }
            for title in titles
        ]
        return await self.request(
            "POST",
            "tasks",
            headers={"prefer": "return=representation"},
            json=payload,
        )

    async def study_history_for_today(self, user_id: Any, date_key: str) -> dict[str, Any] | None:
        rows = await self.request(
            "GET",
            "study_history",
            params={
                "user_id": f"eq.{user_id}",
                "date": f"eq.{date_key}",
                "select": "subjects",
                "limit": "1",
            },
        )
        return rows[0] if rows else None


sb = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)


def today_key() -> str:
    return datetime.now(ZoneInfo(APP_TIMEZONE)).date().isoformat()


def escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def format_duration(seconds: float | int | None) -> str:
    total = max(0, int(seconds or 0))
    hours = total // 3600
    minutes = (total % 3600) // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def parse_task_titles(raw: str) -> list[str]:
    cleaned = raw.strip()
    if not cleaned:
        return []
    pieces: list[str] = []
    for line in cleaned.replace(";", "\n").splitlines():
        title = line.strip().lstrip("-*0123456789. ").strip()
        if title:
            pieces.append(title)
    return pieces[:MAX_TASKS_PER_MESSAGE]


def safe_active_timer(active_timer: Any) -> dict[str, Any] | None:
    """Validate a study_timer blob from the DB before trusting it.

    Stale/legacy or partially-written timer JSON (e.g. missing
    started_at) used to cause an uncaught TypeError deep in /stats and
    /study, which python-telegram-bot's error handler then reported as a
    generic "Something went wrong." This normalizes bad data into "no
    active timer" instead of crashing.
    """
    if not isinstance(active_timer, dict):
        return None
    if not active_timer.get("subject_id"):
        return None
    started_at = active_timer.get("started_at")
    if not isinstance(started_at, (int, float)):
        return None
    return active_timer


def missing_link_column_message() -> str:
    return (
        "Telegram linking needs this Supabase column:\n\n"
        "<code>alter table public.users add column if not exists telegram_chat_id text;</code>\n\n"
        "Run that once in Supabase SQL editor, then use /link again."
    )


def build_commands_keyboard() -> InlineKeyboardMarkup:
    """Buttons for the bot's main commands with color styling (PTB v22.7+)."""
    # Green buttons (success)
    stats_btn = InlineKeyboardButton("📊 Stats", callback_data="cmd_stats", style="success")
    whoami_btn = InlineKeyboardButton("👤 Who am I", callback_data="cmd_whoami", style="success")
    study_btn = InlineKeyboardButton("⏱️ Study Status", callback_data="cmd_study_status", style="success")
    addtask_btn = InlineKeyboardButton("➕ Add Task", callback_data="cmd_task_help", style="success")
    
    # Blue buttons (primary)
    webapp_btn = InlineKeyboardButton("🌐 Open TaskBoard", url=WEBAPP_URL, style="primary")
    
    # Red buttons (danger)
    unlink_btn = InlineKeyboardButton("🔓 Unlink Account", callback_data="cmd_unlink_ask", style="danger")

    return InlineKeyboardMarkup([
        [stats_btn, whoami_btn],
        [study_btn, addtask_btn],
        [webapp_btn],
        [unlink_btn],
    ])


def get_unlink_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Confirmation keyboard for unlinking."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, unlink", callback_data="cmd_unlink_confirm", style="primary"),
            InlineKeyboardButton("❌ Cancel", callback_data="cmd_cancel", style="danger"),
        ]
    ])


def get_login_keyboard(callback_url: str) -> InlineKeyboardMarkup:
    """Login keyboard with web app button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Sign in to TaskBoard", url=callback_url, style="success")]
    ])


async def send_welcome_message(bot: Any, chat_id: int, display_name: str) -> None:
    """Send the post-login welcome message with the command buttons attached."""
    text = (
        "🎉 <b>Login Successful!</b>\n\n"
        f"Welcome to <b>TaskBoard Toolkit</b>, {escape(display_name)}! 🚀\n\n"
        "Your Telegram is now linked to your account. Use the buttons below, "
        "or these commands anytime:\n\n"
        "📊 <code>/stats</code> — pending tasks &amp; today's study hours\n"
        "➕ <code>/task</code> — add a daily task\n"
        "⏱️ <code>/study</code> — start / stop / check your study timer\n"
        "👤 <code>/whoami</code> — check your linked account\n"
        "🔓 <code>/unlink</code> — disconnect this chat\n\n"
        "Let's get to work. 💪"
    )
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=build_commands_keyboard(),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle taps on the command buttons attached to the welcome message."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cmd_stats":
        await stats(update, context)
    elif data == "cmd_whoami":
        await whoami(update, context)
    elif data == "cmd_study_status":
        context.args = ["status"]
        await study(update, context)
    elif data == "cmd_task_help":
        await query.message.reply_text(
            "Use:\n<code>/task Revise electrostatics</code>\n\n"
            "Or add many at once:\n"
            "<code>/task Physics numericals; Chemistry notes; Maths practice</code>",
            parse_mode=ParseMode.HTML,
        )
    elif data == "cmd_unlink_ask":
        await query.message.reply_text(
            "Are you sure you want to unlink this chat from TaskBoard?",
            reply_markup=get_unlink_confirmation_keyboard(),
        )
    elif data == "cmd_unlink_confirm":
        await unlink(update, context)
    elif data == "cmd_cancel":
        try:
            await query.edit_message_text("Cancelled.")
        except Exception:
            pass


async def require_linked_user(update: Update) -> dict[str, Any] | None:
    chat_id = update.effective_chat.id
    try:
        user = await sb.get_user_by_chat_id(chat_id)
    except SupabaseError as exc:
        message = str(exc)
        if "telegram_chat_id" in message:
            await update.effective_message.reply_text(missing_link_column_message(), parse_mode=ParseMode.HTML)
            return None
        # Any other Supabase failure here (e.g. a column referenced in the
        # SELECT — like study_timer — not existing yet in the actual
        # table) used to bubble up as a bare "Something went wrong."
        # Surface the real reason so it's fixable without digging through
        # Railway logs.
        if "does not exist" in message.lower() or "column" in message.lower():
            await update.effective_message.reply_text(
                "⚠️ Supabase schema error while loading your profile:\n\n"
                f"<code>{escape(message[:300])}</code>\n\n"
                "This usually means a column the bot expects (e.g. study_timer) "
                "hasn't been added to the `users` table yet. Run the pending "
                "migration in the Supabase SQL editor, then try again.",
                parse_mode=ParseMode.HTML,
            )
            return None
        raise
    if not user:
        await update.effective_message.reply_text(
            "This Telegram chat is not linked yet.\n\nUse:\n"
            "<code>/link your-email@example.com</code>",
            parse_mode=ParseMode.HTML,
        )
        return None
    return user


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Deep-link login flow: /start <token>
    token = context.args[0] if context.args else ""
    entry = telegram_token_store.get(token) if token else None
    if entry:
        now = time.time()
        if now - float(entry["created_at"]) > 300:
            telegram_token_store.delete(token)
            await update.message.reply_text("This login link has expired. Please try again from the TaskBoard website.")
            return

        tg_user = update.effective_user
        display_name = tg_user.first_name or "User"
        if tg_user.last_name:
            display_name += f" {tg_user.last_name}"
        username = tg_user.username or ""
        telegram_token_store.update(token, {
            "telegram_id": tg_user.id,
            "display_name": display_name,
            "telegram_username": username,
            "verified": True,
        })

        callback_url = f"{WEBAPP_URL}/api/auth/telegram/callback?token={token}"
        sent = await update.message.reply_text(
            f"Welcome, {display_name}! ✨\n\n"
            f"Tap the button below to sign in:",
            reply_markup=get_login_keyboard(callback_url),
        )
        # Remember this message so it can be removed after login completes.
        telegram_token_store.update(token, {
            "verify_chat_id": update.effective_chat.id,
            "verify_message_id": sent.message_id,
        })
        return

    # Normal /start — show help, as buttons
    await update.message.reply_text(
        "TaskBoard bot is ready. Tap a button below or use the commands directly.",
        reply_markup=build_commands_keyboard(),
    )


async def link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Use: /link your-email@example.com")
        return
    email = context.args[0].strip().lower()
    try:
        user = await sb.get_user_by_email(email)
        if not user:
            await update.message.reply_text("No TaskBoard account was found for that email.")
            return
        await sb.link_chat(user["id"], update.effective_chat.id)
    except SupabaseError as exc:
        if "telegram_chat_id" in str(exc):
            await update.message.reply_text(missing_link_column_message(), parse_mode=ParseMode.HTML)
            return
        raise
    await update.message.reply_text(f"Linked to TaskBoard user: {user.get('name') or email}")


async def unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await sb.unlink_chat(update.effective_chat.id)
    await update.effective_message.reply_text("This Telegram chat is no longer linked to TaskBoard.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_linked_user(update)
    if not user:
        return
    date_key = today_key()
    pending = await sb.pending_tasks(user["id"])
    subjects = []

    # Get live timer info if exists to make stats precise
    active_timer = safe_active_timer(user.get("study_timer"))
    active_id = None
    elapsed = 0
    if active_timer:
        active_id = active_timer.get("subject_id")
        elapsed = max(0, int(time.time() - active_timer.get("started_at")))

    if user.get("study_date") == date_key and isinstance(user.get("study_subjects"), list):
        subjects = user["study_subjects"]
    else:
        try:
            history = await sb.study_history_for_today(user["id"], date_key)
            subjects = list((history or {}).get("subjects", {}).values())
        except SupabaseError:
            subjects = []

    study_rows = []
    total_seconds = 0
    for subject in subjects:
        seconds = subject.get("seconds", subject.get("secs", 0)) if isinstance(subject, dict) else 0
        sid = subject.get("id") if isinstance(subject, dict) else None
        # Add live elapsed time if this is the active subject
        if sid and sid == active_id:
            seconds += elapsed
        
        name = subject.get("name", "Subject") if isinstance(subject, dict) else "Subject"
        if seconds or (sid and sid == active_id):
            total_seconds += int(seconds)
            study_rows.append(f"- {escape(name)}: {format_duration(seconds)}")

    # Handle active subject not in subjects list
    if active_id and not any(isinstance(s, dict) and s.get("id") == active_id for s in subjects):
        name = active_timer.get("subject_name") or "Subject"
        total_seconds += elapsed
        study_rows.append(f"- {escape(name)}: {format_duration(elapsed)} (active)")

    task_rows = []
    for i, task in enumerate(pending, 1):
        date_text = task.get("date") or "No date"
        time_text = f" {task['time']}" if task.get("time") else ""
        task_rows.append(
            f"{i}. {escape(task.get('title'))} "
            f"({escape(task.get('category'))}, {escape(task.get('priority'))}, {escape(date_text)}{escape(time_text)})"
        )

    message = [
        f"<b>TaskBoard stats for {escape(user.get('name') or 'you')}</b>",
        f"<b>Date:</b> {escape(date_key)}",
        "",
        f"<b>Study today:</b> {format_duration(total_seconds)}",
        *(study_rows or ["- No study time logged today."]),
        "",
        f"<b>Pending tasks:</b> {len(pending)}",
        *(task_rows or ["- No pending tasks."]),
    ]
    await update.effective_message.reply_text("\n".join(message), parse_mode=ParseMode.HTML)


async def task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_linked_user(update)
    if not user:
        return
    raw = update.message.text.partition(" ")[2]
    titles = parse_task_titles(raw)
    if not titles:
        await update.effective_message.reply_text(
            "Use:\n"
            "<code>/task Revise electrostatics</code>\n\n"
            "Or add many:\n"
            "<code>/task Physics numericals; Chemistry notes; Maths practice</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    added = await sb.add_tasks(user["id"], titles, today_key())
    rows = [f"- {escape(row.get('title'))}" for row in added]
    await update.effective_message.reply_text(
        f"Added {len(added)} task(s) for today:\n" + "\n".join(rows),
        parse_mode=ParseMode.HTML,
    )


async def study(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_linked_user(update)
    if not user:
        return

    args = context.args
    if not args:
        active_timer = safe_active_timer(user.get("study_timer"))
        if active_timer:
            subject_name = active_timer.get("subject_name") or "Subject"
            started_at = active_timer.get("started_at")
            elapsed = int(time.time() - started_at)
            await update.effective_message.reply_text(
                f"⏱️ You are currently studying <b>{escape(subject_name)}</b> for <b>{format_duration(elapsed)}</b>.\n\n"
                f"To stop the timer, use:\n"
                f"<code>/study stop</code>",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_message.reply_text(
                "📚 No active study timer.\n\n"
                "To start studying a subject, use:\n"
                "<code>/study Physics</code>\n\n"
                "To stop studying, use:\n"
                "<code>/study stop</code>",
                parse_mode=ParseMode.HTML
            )
        return

    arg = " ".join(args).strip()
    today_date = today_key()
    
    study_subjects = user.get("study_subjects") or []
    if not isinstance(study_subjects, list):
        study_subjects = []
    
    study_date = user.get("study_date")
    today_study_seconds = int(user.get("today_study_seconds") or 0)
    total_study_seconds = int(user.get("total_study_seconds") or 0)
    active_timer = safe_active_timer(user.get("study_timer"))

    # Handle day boundary reset if active
    if study_date and study_date != today_date:
        # Archive yesterday's subjects to study_history
        subjects_dict = {}
        for s in study_subjects:
            if not isinstance(s, dict) or s.get("id") is None:
                continue
            subjects_dict[str(s["id"])] = {
                "secs": int(s.get("secs") or 0),
                "name": s.get("name") or "Subject",
                "color": s.get("color", "#38c9a8"),
            }
        total_secs = sum(int(s.get("secs") or 0) for s in study_subjects if isinstance(s, dict))
        if total_secs > 0:
            try:
                await sb.request("POST", "study_history", json={
                    "user_id": str(user["id"]),
                    "date": study_date,
                    "subjects": subjects_dict,
                    "total_secs": total_secs,
                    "updated_at": datetime.now().isoformat()
                })
            except Exception:
                pass
        
        # Reset subjects secs for the new day
        for s in study_subjects:
            if isinstance(s, dict):
                s['secs'] = 0
        today_study_seconds = 0
        study_date = today_date

    if not study_date:
        study_date = today_date

    if arg.lower() == "stop":
        if not active_timer or not active_timer.get("subject_id"):
            await update.effective_message.reply_text("No active study timer is running.")
            return

        subject_id = active_timer.get("subject_id")
        started_at = active_timer.get("started_at")
        subject_name = active_timer.get("subject_name") or "Subject"
        elapsed = max(0, int(time.time() - started_at))

        # Add to the subject secs
        subject_found = False
        for s in study_subjects:
            if s.get("id") == subject_id:
                s["secs"] = int(s.get("secs") or 0) + elapsed
                subject_found = True
                break
        
        if not subject_found:
            study_subjects.append({
                "id": subject_id,
                "name": subject_name,
                "secs": elapsed,
                "color": "#38c9a8"
            })

        today_study_seconds += elapsed
        total_study_seconds += elapsed

        # Save to DB
        await sb.update_study_data(user["id"], study_subjects, today_study_seconds, total_study_seconds, study_date, None)
        await update.effective_message.reply_text(
            f"⏹️ Stopped studying <b>{escape(subject_name)}</b>.\n"
            f"Session duration: <b>{format_duration(elapsed)}</b>.\n"
            f"Total study today: <b>{format_duration(today_study_seconds)}</b>.",
            parse_mode=ParseMode.HTML
        )
        return

    if arg.lower() == "status":
        if active_timer and active_timer.get("subject_id"):
            subject_name = active_timer.get("subject_name") or "Subject"
            started_at = active_timer.get("started_at")
            elapsed = int(time.time() - started_at)
            await update.effective_message.reply_text(
                f"⏱️ You are currently studying <b>{escape(subject_name)}</b> for <b>{format_duration(elapsed)}</b>.\n\n"
                f"To stop the timer, use:\n"
                f"<code>/study stop</code>",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_message.reply_text(
                "📚 No active study timer.",
                parse_mode=ParseMode.HTML
            )
        return

    # Start a new timer for subject 'arg'
    subject_name = arg
    subject_id = None
    
    # Case-insensitive check
    for s in study_subjects:
        if s.get("name", "").lower() == subject_name.lower():
            subject_name = s.get("name")
            subject_id = s.get("id")
            break

    if not subject_id:
        if len(study_subjects) >= 10:
            await update.effective_message.reply_text("Cannot create new subject. Maximum of 10 subjects allowed on TaskBoard.")
            return
        subject_id = int(time.time() * 1000)
        colors = ["#38c9a8", "#3b82f6", "#ef4444", "#f59e0b", "#10b981", "#8b5cf6", "#ec4899", "#14b8a6", "#f43f5e"]
        color = colors[len(study_subjects) % len(colors)]
        study_subjects.append({
            "id": subject_id,
            "name": subject_name,
            "secs": 0,
            "color": color
        })

    # If there is already a running timer, stop it first and credit the time
    stop_msg = ""
    if active_timer and active_timer.get("subject_id"):
        prev_id = active_timer.get("subject_id")
        prev_start = active_timer.get("started_at")
        prev_name = active_timer.get("subject_name") or "Subject"
        prev_elapsed = max(0, int(time.time() - prev_start))
        if prev_elapsed > 0:
            for s in study_subjects:
                if s.get("id") == prev_id:
                    s["secs"] = int(s.get("secs") or 0) + prev_elapsed
                    break
            today_study_seconds += prev_elapsed
            total_study_seconds += prev_elapsed
            stop_msg = f"⏹️ Stopped previous session for <b>{escape(prev_name)}</b> ({format_duration(prev_elapsed)}).\n"

    # Start new timer
    new_timer = {
        "subject_id": subject_id,
        "started_at": int(time.time()),
        "subject_name": subject_name
    }

    # Save to DB
    await sb.update_study_data(user["id"], study_subjects, today_study_seconds, total_study_seconds, study_date, new_timer)
    
    await update.effective_message.reply_text(
        f"{stop_msg}▶️ Started studying <b>{escape(subject_name)}</b>! Timer is running.\n\n"
        f"Use <code>/study stop</code> when you are done.",
        parse_mode=ParseMode.HTML
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await require_linked_user(update)
    if not user:
        return
    await update.effective_message.reply_text(
        f"Linked as {user.get('name') or 'TaskBoard user'}\n"
        f"Email: {user.get('email') or 'unknown'}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram bot error:", repr(context.error))
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Please try again in a moment.")


async def post_shutdown(application: Application) -> None:
    await sb.close()


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Add it in Railway Variables before running telegram_bot.py directly.")
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("link", link))
    application.add_handler(CommandHandler("unlink", unlink))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("task", task))
    application.add_handler(CommandHandler("study", study))
    application.add_handler(CommandHandler("timer", study))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        asyncio.run(sb.close())
