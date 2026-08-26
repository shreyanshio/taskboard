import asyncio
import base64
import hashlib
import os
import time
import uuid
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from telegram_bot import (
    BOT_TOKEN,
    sb,
    button_handler,
    error_handler,
    link,
    telegram_token_store,
    send_welcome_message,
    start,
    stats,
    study,
    task,
    today_key,
    unlink,
    whoami,
)


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]


app = FastAPI(title="TaskBoard Railway API")
telegram_app: Application | None = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # No cookies are used anywhere in this app — auth is a bearer token in the
    # Authorization header, so allow_credentials must be False. Combining
    # allow_credentials=True with a wildcard "*" origin is invalid per the
    # CORS spec and browsers will silently reject those responses; keeping
    # this False lets ALLOWED_ORIGINS stay "*" safely, or be locked down to
    # specific origins later without touching this line again.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginBody(BaseModel):
    email: str
    password: str


class SignupBody(BaseModel):
    name: str
    email: str
    password: str


class TaskBody(BaseModel):
    title: str
    description: str = ""
    category: str = "study"
    priority: str = "normal"
    date: str
    time: str | None = None
    subtasks: list[dict[str, Any]] = []
    pinned: bool = False


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    priority: str | None = None
    date: str | None = None
    time: str | None = None
    subtasks: list[dict[str, Any]] | None = None
    done: bool | None = None
    pinned: bool | None = None
    done_at: str | None = None


class ProfilePatch(BaseModel):
    """Fields a user may edit directly; progression is server-owned."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    status_message: str | None = None
    workspace_title: str | None = None
    layout_sort: str | None = None
    avatar_url: str | None = None


class StudySubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | str
    name: str
    secs: int = 0
    color: str = "#38c9a8"


class StudyTimer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: int | str
    started_at: int
    subject_name: str


class StudySyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    study_subjects: list[StudySubject] = []
    study_timer: StudyTimer | None = None





async def fetch_telegram_avatar_data_url(telegram_id: int) -> str | None:
    """Best-effort fetch of a user's current Telegram profile photo, returned
    as a small base64 data: URL (same format the "upload from device" avatar
    picker already produces client-side), so it can be stored directly in
    the existing `avatar_url` column.
    """
    if not telegram_app or not BOT_TOKEN:
        return None
    try:
        photos = await telegram_app.bot.get_user_profile_photos(telegram_id, limit=1)
        if not photos or not photos.photos:
            return None
        # Smallest available size is first in the list — plenty for an avatar
        # and keeps the resulting data: URL compact.
        file_id = photos.photos[0][0].file_id
        tg_file = await telegram_app.bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(file_url)
        if resp.status_code >= 400 or not resp.content:
            return None
        content_type = resp.headers.get("content-type", "image/jpeg")
        encoded = base64.b64encode(resp.content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
    except Exception:
        return None  # Avatar fetch is best-effort; login must still succeed.


async def supabase_auth_request(method: str, path: str, **kwargs: Any) -> Any:
    headers = kwargs.pop("headers", {})
    headers.update({"apikey": SUPABASE_ANON_KEY})
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.request(
            method,
            f"{SUPABASE_URL}/auth/v1/{path}",
            headers=headers,
            **kwargs,
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json() if response.content else None


async def current_auth_user(authorization: str = Header(default="")) -> dict[str, Any]:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    return await supabase_auth_request(
        "GET",
        "user",
        headers={"authorization": f"Bearer {token}"},
    )


async def current_profile(auth_user: dict[str, Any] = Depends(current_auth_user)) -> dict[str, Any]:
    rows = await sb.request(
        "GET",
        "users",
        params={
            "auth_id": f"eq.{auth_user['id']}",
            "select": "*",
            "limit": "1",
        },
    )
    if rows:
        return rows[0]
    email = auth_user.get("email") or ""
    name = (auth_user.get("user_metadata") or {}).get("name") or email.split("@")[0] or "User"
    # If the user signed in via Google, Supabase surfaces their Google
    # profile photo inside user_metadata (as "avatar_url" or "picture"
    # depending on provider version) — reuse it as the TaskBoard avatar.
    avatar_url = (auth_user.get("user_metadata") or {}).get("avatar_url") or (
        auth_user.get("user_metadata") or {}
    ).get("picture")
    payload: dict[str, Any] = {"name": name, "email": email, "auth_id": auth_user["id"]}
    if avatar_url:
        payload["avatar_url"] = avatar_url
    created = await sb.request(
        "POST",
        "users",
        headers={"prefer": "return=representation"},
        json=payload,
    )
    return created[0]


def clean_payload(payload: Any) -> Any:
    if isinstance(payload, list):
        return [clean_payload(item) for item in payload]
    if not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if v is not None}


def restricted_params(table: str, raw_params: dict[str, str], profile: dict[str, Any]) -> dict[str, str]:
    params = dict(raw_params)
    if table in {"tasks", "quick_notes", "routines", "routine_logs"}:
        params["user_id"] = f"eq.{profile['id']}"
    elif table == "study_history":
        params["user_id"] = f"eq.{profile['id']}"
    elif table == "users":
        params["id"] = f"eq.{profile['id']}"
    return params


PROFILE_EDIT_FIELDS = {
    "name", "status_message", "workspace_title", "layout_sort", "avatar_url",
}
PROTECTED_PROFILE_FIELDS = {
    "auth_id", "xp_total", "current_level", "total_study_seconds",
    "today_study_seconds", "study_date", "study_subjects", "study_timer",
}
REST_PATCH_FIELDS: dict[str, set[str]] = {
    "users": PROFILE_EDIT_FIELDS,
    "tasks": {"title", "description", "category", "priority", "date", "time", "subtasks", "done", "pinned", "done_at"},
    "quick_notes": {"content"},
    "study_history": {"date", "subjects", "total_secs", "updated_at"},
}
REST_POST_FIELDS: dict[str, set[str]] = {
    "tasks": {"user_id", "title", "description", "category", "priority", "date", "time", "subtasks", "done", "pinned", "done_at"},
    "quick_notes": {"user_id", "content"},
    "study_history": {"user_id", "date", "subjects", "total_secs", "updated_at"},
    "routines": {"user_id", "name", "icon", "order", "archived"},
    "routine_logs": {"user_id", "routine_id", "date", "done"},
}
REST_PATCH_FIELDS.update({
    "routines": {"name", "icon", "order", "archived"},
    "routine_logs": {"done"},
})


def validate_payload_fields(payload: Any, allowed: set[str], label: str) -> Any:
    """Reject unknown keys instead of forwarding arbitrary user-controlled columns."""
    items = payload if isinstance(payload, list) else [payload]
    if not all(isinstance(item, dict) for item in items):
        raise HTTPException(status_code=422, detail=f"{label} payload must be an object or list of objects")
    unknown = sorted({str(key) for item in items for key in item if key not in allowed})
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported {label} fields: {', '.join(unknown)}")
    return payload


def sanitize_study_subjects(subjects: list[StudySubject]) -> list[dict[str, Any]]:
    """Normalize study data and cap impossible values before persisting it."""
    if len(subjects) > 10:
        raise HTTPException(status_code=422, detail="A maximum of 10 study subjects is allowed")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for subject in subjects:
        subject_id = str(subject.id)
        if subject_id in seen_ids:
            raise HTTPException(status_code=422, detail="Study subject IDs must be unique")
        seen_ids.add(subject_id)
        name = subject.name.strip()
        if not name or len(name) > 80:
            raise HTTPException(status_code=422, detail="Study subject names must be 1–80 characters")
        secs = int(subject.secs)
        if secs < 0 or secs > 86400:
            raise HTTPException(status_code=422, detail="Study seconds must be between 0 and 86400 per subject")
        normalized.append({"id": subject.id, "name": name, "secs": secs, "color": subject.color})
    return normalized


async def study_history_total(profile_id: Any, exclude_date: str | None = None) -> int:
    rows = await sb.request(
        "GET",
        "study_history",
        params={
            "user_id": f"eq.{profile_id}",
            "select": "date,total_secs,subjects",
            "limit": "5000",
        },
    )
    total = 0
    for row in rows or []:
        if exclude_date and row.get("date") == exclude_date:
            continue
        value = row.get("total_secs")
        if value is None:
            subjects = row.get("subjects") or {}
            value = sum(int(item.get("secs", item.get("seconds", 0)) or 0) for item in subjects.values() if isinstance(item, dict))
        total += max(0, int(value or 0))
    return total


async def persist_validated_study(profile: dict[str, Any], body: StudySyncBody) -> dict[str, Any]:
    today = today_key()
    subjects = sanitize_study_subjects(body.study_subjects)
    subject_ids = {str(subject["id"]) for subject in subjects}
    timer = body.study_timer.model_dump() if body.study_timer else None
    if timer:
        if str(timer["subject_id"]) not in subject_ids:
            raise HTTPException(status_code=422, detail="The active timer must reference an existing subject")
        now = int(time.time())
        if timer["started_at"] > now + 60 or timer["started_at"] < now - 172800:
            raise HTTPException(status_code=422, detail="The active timer timestamp is outside the allowed range")
        timer["subject_name"] = next(s["name"] for s in subjects if str(s["id"]) == str(timer["subject_id"]))
    today_total = sum(subject["secs"] for subject in subjects)
    historic_total = await study_history_total(profile["id"], exclude_date=today)
    patch = {
        "study_subjects": subjects,
        "study_timer": timer,
        "study_date": today,
        "today_study_seconds": today_total,
        "total_study_seconds": historic_total + today_total,
    }
    rows = await sb.request(
        "PATCH",
        "users",
        params={"id": f"eq.{profile['id']}"},
        headers={"prefer": "return=representation"},
        json=patch,
    )
    return rows[0] if rows else {**profile, **patch}


@app.on_event("startup")
async def startup() -> None:

    global telegram_app
    index_path = os.path.join(BASE_DIR, "index.html")
    print(f"[startup] index.html: {'found' if os.path.isfile(index_path) else 'MISSING'} at {index_path}")
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set; website API will run without Telegram bot polling.")
        return
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", start))
    telegram_app.add_handler(CommandHandler("link", link))
    telegram_app.add_handler(CommandHandler("unlink", unlink))
    telegram_app.add_handler(CommandHandler("stats", stats))
    telegram_app.add_handler(CommandHandler("task", task))
    telegram_app.add_handler(CommandHandler("study", study))
    telegram_app.add_handler(CommandHandler("timer", study))
    telegram_app.add_handler(CommandHandler("whoami", whoami))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_error_handler(error_handler)
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()


@app.on_event("shutdown")
async def shutdown() -> None:
    if telegram_app:
        # On Railway, a redeploy sends SIGTERM with a short grace period.
        # Telegram's getUpdates long-poll can take a moment to cancel, and
        # if the network is slow right at that instant python-telegram-bot
        # raises TimedOut — harmless (the process is exiting anyway), but
        # left unhandled it prints a scary traceback on every deploy. Give
        # each shutdown step a bounded timeout and swallow failures here.
        for step in (
            telegram_app.updater.stop,
            telegram_app.stop,
            telegram_app.shutdown,
        ):
            try:
                await asyncio.wait_for(step(), timeout=5)
            except Exception as e:  # noqa: BLE001 - best-effort cleanup
                print(f"Telegram bot shutdown step {step.__qualname__} failed: {e}")
    await sb.close()


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"ok": "true"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.post("/api/auth/login")
async def login(body: LoginBody) -> dict[str, Any]:
    return await supabase_auth_request(
        "POST",
        "token?grant_type=password",
        json={"email": body.email, "password": body.password},
    )


@app.get("/api/auth/google/start")
async def google_start(request: Request) -> RedirectResponse:
    redirect_to = str(request.url_for("index"))
    authorize_url = (
        f"{SUPABASE_URL}/auth/v1/authorize"
        f"?provider=google"
        f"&redirect_to={quote(redirect_to, safe='')}"
    )
    return RedirectResponse(authorize_url)


@app.get("/api/auth/telegram/start")
async def telegram_start() -> RedirectResponse:
    """Create a persistent, single-use token and redirect to the Telegram bot."""
    now = time.time()
    token = uuid.uuid4().hex
    telegram_token_store.create(token, now)
    telegram_token_store.purge_expired(now - 300)
    return RedirectResponse(f"https://t.me/taskboard7_bot?start={token}")


@app.get("/api/auth/telegram/callback")
async def telegram_callback(token: str, request: Request) -> RedirectResponse:
    """Verify the bot-filled token, create/sign-in the Supabase user, redirect with session hash."""
    entry = telegram_token_store.consume_verified(token, time.time())
    if not entry:
        raise HTTPException(status_code=400, detail="Invalid or expired token. Please try again.")

    # The token is consumed atomically before external calls, so it cannot be replayed.
    telegram_id = entry["telegram_id"]
    display_name = entry["display_name"] or "User"
    telegram_username = entry.get("telegram_username") or ""
    verify_chat_id = entry.get("verify_chat_id")
    verify_message_id = entry.get("verify_message_id")

    email = f"tg_{telegram_id}@telegram.local"
    password = hashlib.sha256(
        f"tg_{telegram_id}_{SUPABASE_SERVICE_ROLE_KEY}".encode()
    ).hexdigest()
    redirect_to = str(request.url_for("index"))

    admin_headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "content-type": "application/json",
    }

    auth_user_id: str | None = None
    session_fragment: str | None = None

    async with httpx.AsyncClient(timeout=20) as client:
        # 1. Ensure auth user exists (ignore duplicate / 422 errors)
        await client.post(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers=admin_headers,
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {
                    "name": display_name,
                    "full_name": display_name,
                    "telegram_username": telegram_username,
                },
            },
        )

        # 2. Try generate_link for a fresh, guaranteed-valid session
        link_resp = await client.post(
            f"{SUPABASE_URL}/auth/v1/admin/generate_link",
            headers=admin_headers,
            json={
                "type": "magiclink",
                "email": email,
                "redirect_to": redirect_to,
            },
        )

        if link_resp.status_code < 400:
            link_data = link_resp.json()
            props = link_data.get("properties") or {}
            action_link = (
                props.get("action_link")
                or link_data.get("action_link")
            )
            hashed_token = (
                props.get("hashed_token")
                or link_data.get("hashed_token")
            )
            auth_user_id = link_data.get("id")

            # Build action_link from hashed_token if needed
            if not action_link and hashed_token:
                action_link = (
                    f"{SUPABASE_URL}/auth/v1/verify"
                    f"?token={hashed_token}"
                    f"&type=magiclink"
                    f"&redirect_to={quote(redirect_to, safe='')}"
                )

            if action_link:
                verify_resp = await client.get(action_link, follow_redirects=False)
                location = verify_resp.headers.get("location", "")
                if "#" in location:
                    session_fragment = location.split("#", 1)[1]

    # 3. Fallback: password-based sign-in if generate_link didn't work
    if not session_fragment:
        try:
            session = await supabase_auth_request(
                "POST",
                "token?grant_type=password",
                json={"email": email, "password": password},
            )
        except HTTPException:
            raise HTTPException(
                status_code=500,
                detail="Telegram login failed. Please try again.",
            )
        access_token = session.get("access_token", "")
        refresh_token = session.get("refresh_token", "")
        expires_in = session.get("expires_in", 3600)
        token_type = session.get("token_type", "bearer")
        session_fragment = (
            f"access_token={access_token}"
            f"&token_type={token_type}"
            f"&expires_in={expires_in}"
            f"&refresh_token={refresh_token}"
        )
        auth_user_id = auth_user_id or (session.get("user") or {}).get("id")

    # 4. Ensure profile row exists with Telegram info
    if auth_user_id:
        try:
            existing = await sb.request(
                "GET",
                "users",
                params={
                    "auth_id": f"eq.{auth_user_id}",
                    "select": "id,avatar_url",
                    "limit": "1",
                },
            )
            # Best-effort fetch of the user's current Telegram profile photo.
            # Only applied when the profile doesn't already have a custom
            # avatar, so it never clobbers something the user picked on the
            # web dashboard.
            needs_avatar = not existing or not existing[0].get("avatar_url")
            avatar_url = (
                await fetch_telegram_avatar_data_url(telegram_id)
                if needs_avatar
                else None
            )
            if existing:
                patch: dict[str, Any] = {
                    "telegram_username": telegram_username,
                    "telegram_chat_id": str(telegram_id),
                }
                if avatar_url:
                    patch["avatar_url"] = avatar_url
                await sb.request(
                    "PATCH",
                    "users",
                    params={"auth_id": f"eq.{auth_user_id}"},
                    headers={"prefer": "return=minimal"},
                    json=patch,
                )
            else:
                create_payload: dict[str, Any] = {
                    "name": display_name,
                    "email": email,
                    "auth_id": auth_user_id,
                    "telegram_username": telegram_username,
                    "telegram_chat_id": str(telegram_id),
                }
                if avatar_url:
                    create_payload["avatar_url"] = avatar_url
                await sb.request(
                    "POST",
                    "users",
                    headers={"prefer": "return=minimal"},
                    json=create_payload,
                )
        except Exception:
            pass  # Profile update is best-effort; login still works

    # 4b. Clean up the "Sign in to TaskBoard" message and greet the user
    # in the bot chat now that login has actually gone through.
    if telegram_app:
        if verify_chat_id and verify_message_id:
            try:
                await telegram_app.bot.delete_message(
                    chat_id=verify_chat_id, message_id=verify_message_id
                )
            except Exception:
                pass  # Message may already be gone; not critical.
        try:
            await send_welcome_message(telegram_app.bot, telegram_id, display_name)
        except Exception:
            pass  # Best-effort; login must still succeed even if this fails.

    # 5. Redirect with session hash — same pattern as Google OAuth
    return RedirectResponse(f"/#{session_fragment}")


@app.get("/api/auth/user")
async def auth_user(user: dict[str, Any] = Depends(current_auth_user)) -> dict[str, Any]:
    return user


@app.post("/api/auth/signup")
async def signup(body: SignupBody) -> dict[str, Any]:
    result = await supabase_auth_request(
        "POST",
        "signup",
        json={
            "email": body.email,
            "password": body.password,
            "data": {"name": body.name, "full_name": body.name},
        },
    )
    user_id = result.get("id") or (result.get("user") or {}).get("id")
    if user_id:
        await sb.request(
            "POST",
            "users",
            headers={"prefer": "return=minimal"},
            json={"name": body.name, "email": body.email, "auth_id": user_id},
        )
    return result


@app.get("/api/me")
async def me(profile: dict[str, Any] = Depends(current_profile)) -> dict[str, Any]:
    return profile


@app.patch("/api/me")
async def update_me(
    body: ProfilePatch,
    profile: dict[str, Any] = Depends(current_profile),
) -> dict[str, Any]:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return profile
    rows = await sb.request(
        "PATCH",
        "users",
        params={"id": f"eq.{profile['id']}"},
        headers={"prefer": "return=representation"},
        json=patch,
    )
    return rows[0] if rows else {**profile, **patch}


@app.post("/api/study/sync")
async def sync_study(
    body: StudySyncBody,
    profile: dict[str, Any] = Depends(current_profile),
) -> dict[str, Any]:
    return await persist_validated_study(profile, body)


@app.get("/api/bootstrap")
async def bootstrap(profile: dict[str, Any] = Depends(current_profile)) -> dict[str, Any]:

    tasks = await sb.request(
        "GET",
        "tasks",
        params={
            "user_id": f"eq.{profile['id']}",
            "select": "*",
            "order": "created_at.desc",
        },
    )
    quick_notes = await sb.request(
        "GET",
        "quick_notes",
        params={"user_id": f"eq.{profile['id']}", "select": "id,content", "limit": "1"},
    )
    history = await sb.request(
        "GET",
        "study_history",
        params={"user_id": f"eq.{profile['id']}", "select": "date,subjects"},
    )
    return {
        "profile": profile,
        "tasks": tasks,
        "quick_note": quick_notes[0] if quick_notes else None,
        "study_history": history,
    }


@app.get("/api/tasks")
async def list_tasks(profile: dict[str, Any] = Depends(current_profile)) -> list[dict[str, Any]]:
    return await sb.request(
        "GET",
        "tasks",
        params={"user_id": f"eq.{profile['id']}", "select": "*", "order": "created_at.desc"},
    )


@app.post("/api/tasks")
async def create_task(
    body: TaskBody,
    profile: dict[str, Any] = Depends(current_profile),
) -> dict[str, Any]:
    rows = await sb.request(
        "POST",
        "tasks",
        headers={"prefer": "return=representation"},
        json={**body.model_dump(), "user_id": profile["id"], "done": False},
    )
    return rows[0]


@app.patch("/api/tasks/{task_id}")
async def update_task(
    task_id: str,
    body: TaskPatch,
    profile: dict[str, Any] = Depends(current_profile),
) -> dict[str, Any]:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    rows = await sb.request(
        "PATCH",
        "tasks",
        params={"id": f"eq.{task_id}", "user_id": f"eq.{profile['id']}"},
        headers={"prefer": "return=representation"},
        json=patch,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Task not found")
    return rows[0]


@app.delete("/api/tasks/{task_id}")
async def delete_task(
    task_id: str,
    profile: dict[str, Any] = Depends(current_profile),
) -> dict[str, bool]:
    await sb.request(
        "DELETE",
        "tasks",
        params={"id": f"eq.{task_id}", "user_id": f"eq.{profile['id']}"},
        headers={"prefer": "return=minimal"},
    )
    return {"ok": True}


@app.get("/api/routines/analytics")
async def routine_analytics(
    month: int | None = None,
    year: int | None = None,
    profile: dict[str, Any] = Depends(current_profile),
) -> dict[str, Any]:
    target = datetime.now().date()
    month = month or target.month
    year = year or target.year
    if month < 1 or month > 12 or year < 2000 or year > 2100:
        raise HTTPException(status_code=422, detail="Invalid analytics month or year")
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"
    routines = await sb.request(
        "GET", "routines", params={
            "user_id": f"eq.{profile['id']}",
            "select": "id,name,icon,order",
            "order": "order.asc",
        }
    )
    # Use a bounded PostgREST range for the selected month.
    logs = await sb.request(
        "GET", "routine_logs", params={
            "user_id": f"eq.{profile['id']}",
            "date": f"and(gte.{start},lte.{end})",
            "select": "routine_id,date,done",
        }
    )
    done_by_routine: dict[str, set[str]] = {}
    for row in logs or []:
        if row.get("done"):
            done_by_routine.setdefault(str(row["routine_id"]), set()).add(row["date"])
    days_in_month = monthrange(year, month)[1]
    today = target.isoformat()
    routine_stats: list[dict[str, Any]] = []
    for routine in routines or []:
        rid = str(routine["id"])
        done_dates = done_by_routine.get(rid, set())
        streak = 0
        cursor = min(target, datetime(year, month, days_in_month).date())
        while cursor >= datetime(year, month, 1).date() and cursor.isoformat() in done_dates:
            streak += 1
            cursor -= timedelta(days=1)
        days_done = len(done_dates)
        routine_stats.append({
            "id": routine["id"],
            "name": routine.get("name") or "Habit",
            "icon": routine.get("icon") or "✅",
            "days_done": days_done,
            "completion_pct": round(days_done / days_in_month * 100, 1),
            "streak": streak,
        })
    daily_series = []
    for day in range(1, days_in_month + 1):
        date_key = f"{year:04d}-{month:02d}-{day:02d}"
        done = sum(date_key in done_by_routine.get(str(r["id"]), set()) for r in routines or [])
        daily_series.append({"date": date_key, "done": done, "total": len(routines or []), "pct": round(done / len(routines) * 100, 1) if routines else 0})
    week_end = min(target, datetime(year, month, days_in_month).date())
    week = []
    for offset in range(6, -1, -1):
        day = week_end - timedelta(days=offset)
        date_key = day.isoformat()
        done = sum(date_key in done_by_routine.get(str(r["id"]), set()) for r in routines or [])
        week.append({"label": day.strftime("%a"), "date": date_key, "done": done, "total": len(routines or [])})
    strongest = max(routine_stats, key=lambda row: row["days_done"], default=None)
    weakest = min(routine_stats, key=lambda row: row["days_done"], default=None)
    return {"month": month, "year": year, "routines": routine_stats, "daily_series": daily_series, "week": week, "strongest": strongest, "weakest": weakest}


@app.get("/api/leaderboard")
async def leaderboard(
    date: str | None = None,
    profile: dict[str, Any] = Depends(current_profile),
) -> list[dict[str, Any]]:
    """Daily leaderboard by default (today, Asia/Kolkata) — pass ?date=all
    for the all-time totals board, or ?date=YYYY-MM-DD for any specific day.
    Daily rankings are built from `study_history`, which every device syncs
    to roughly every 30s while a timer is running (see syncStudyToCloud in
    script.js) — so scores here update within ~30s of someone studying,
    without needing a separate realtime/websocket pipeline.

    Requires a valid bearer token (any logged-in user) — previously this was
    open with no auth check, so anyone with the URL could pull every user's
    name, avatar, and study stats.
    """
    if date == "all":
        try:
            return await sb.request(
                "GET",
                "leaderboard_view",
                params={
                    "select": "id,name,total_study_seconds,today_study_seconds,study_date,xp_total,current_level,avatar_url",
                    "order": "total_study_seconds.desc",
                    "limit": "50",
                },
            )
        except Exception as e:
            print(f"[leaderboard] all-time query failed: {type(e).__name__}: {e}")
            raise HTTPException(status_code=500, detail=f"leaderboard(all) failed: {e}")

    target_date = date or today_key()

    try:
        hist_rows = await sb.request(
            "GET",
            "study_history",
            params={
                "date": f"eq.{target_date}",
                "select": "user_id,total_secs,updated_at",
                "order": "total_secs.desc",
                "limit": "50",
            },
        )
    except Exception as e:
        print(f"[leaderboard] study_history query failed for date={target_date}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"study_history query failed: {e}")
    if not hist_rows:
        return []

    user_ids = sorted({str(r["user_id"]) for r in hist_rows})
    try:
        users_rows = await sb.request(
            "GET",
            "users",
            params={
                "id": f"in.({','.join(user_ids)})",
                "select": "id,name,avatar_url",
            },
        )
    except Exception as e:
        print(f"[leaderboard] users query failed for ids={user_ids}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"users query failed: {e}")
    users_by_id = {str(u["id"]): u for u in users_rows}

    result: list[dict[str, Any]] = []
    for row in hist_rows:
        u = users_by_id.get(str(row["user_id"]))
        if not u or not u.get("name"):
            continue
        result.append(
            {
                "id": u["id"],
                "name": u["name"],
                "avatar_url": u.get("avatar_url"),
                "today_study_seconds": row.get("total_secs") or 0,
                "study_date": target_date,
            }
        )
    result.sort(key=lambda r: r["today_study_seconds"], reverse=True)
    return result


@app.api_route("/api/rest/{table}", methods=["GET", "POST", "PATCH", "DELETE"])
async def rest_proxy(
    table: str,
    request: Request,
    profile: dict[str, Any] = Depends(current_profile),
) -> Any:
    allowed = {"users", "tasks", "quick_notes", "study_history", "leaderboard_view", "routines", "routine_logs"}
    if table not in allowed:
        raise HTTPException(status_code=403, detail="Table is not allowed")

    method = request.method
    raw_params = dict(request.query_params)
    params = restricted_params(table, raw_params, profile)

    if table == "leaderboard_view" and method != "GET":
        raise HTTPException(status_code=405, detail="Leaderboard is read-only")

    if method == "GET":
        return await sb.request("GET", table, params=params)

    if method in {"POST", "PATCH"}:
        try:
            raw_payload = await request.json()
        except Exception as e:
            print(f"[rest_proxy] {method} {table}: invalid/empty JSON body — {type(e).__name__}: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")
    else:
        raw_payload = None
    # Validate every write before forwarding it to Supabase. In particular,
    # the users table never accepts auth_id or progression fields here.
    if method == "POST":
        if table == "users":
            validate_payload_fields(raw_payload, PROFILE_EDIT_FIELDS | {"email", "auth_id"}, "users")
        elif table not in REST_POST_FIELDS:
            raise HTTPException(status_code=405, detail=f"POST is not supported for {table}")
        else:
            validate_payload_fields(raw_payload, REST_POST_FIELDS[table], table)
    elif method == "PATCH":
        if table not in REST_PATCH_FIELDS:
            raise HTTPException(status_code=405, detail=f"PATCH is not supported for {table}")
        validate_payload_fields(raw_payload, REST_PATCH_FIELDS[table], table)
    payload = clean_payload(raw_payload) if method == "POST" else raw_payload

    if method == "POST":
        if table == "users":
            return [profile]
        if table in {"tasks", "quick_notes", "routines", "routine_logs"}:
            if isinstance(payload, list):
                payload = [{**item, "user_id": profile["id"]} for item in payload]
            else:
                payload = {**payload, "user_id": profile["id"]}
        if table == "study_history":
            if isinstance(payload, list):
                payload = [{**item, "user_id": str(profile["id"])} for item in payload]
            else:
                payload = {**payload, "user_id": str(profile["id"])}
        allow_upsert = table in {"study_history", "routine_logs"} and request.headers.get("x-upsert") == "true"
        prefer = "resolution=merge-duplicates,return=representation" if allow_upsert else "return=representation"
        return await sb.request("POST", table, params=params, headers={"prefer": prefer}, json=payload)

    if method == "PATCH":
        return await sb.request(
            "PATCH",
            table,
            params=params,
            headers={"prefer": "return=representation"},
            json=payload,
        )

    if method == "DELETE":
        if table == "users":
            raise HTTPException(status_code=405, detail="Deleting users is disabled")
        await sb.request("DELETE", table, params=params, headers={"prefer": "return=minimal"})
        return {"ok": True}
