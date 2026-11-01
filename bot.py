import asyncio
import html
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("rapira_bot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/rapira.sqlite3")
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip()
}
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")

WELCOME = (
    "🏆 <b>Добро пожаловать в RAPIRA COMMUNITY!</b>\n\n"
    "Для получения доступа к нашему сообществу необходимо пройти небольшую "
    "регистрацию. Заполни данные ниже и дождись подтверждения администрации."
)


def validate_config() -> None:
    missing = [
        name
        for name, value in (
            ("BOT_TOKEN", TOKEN),
            ("ADMIN_IDS", ADMIN_IDS),
            ("GROUP_CHAT_ID", GROUP_CHAT_ID),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Не заданы обязательные переменные: {', '.join(missing)}")


async def initialize_database() -> None:
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                user_id INTEGER PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                step TEXT NOT NULL,
                nickname TEXT,
                rapira_id TEXT,
                username TEXT
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                nickname TEXT NOT NULL,
                rapira_id TEXT NOT NULL,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
                decided_at TEXT,
                decided_by INTEGER
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_application_per_user
            ON applications(user_id) WHERE status = 'pending';
            """
        )
        await db.commit()


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отправить заявку", callback_data="submit")],
            [InlineKeyboardButton(text="Изменить данные", callback_data="restart")],
        ]
    )


def moderation_keyboard(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="☑ ПОДТВЕРДИТЬ", callback_data=f"approve:{application_id}"
                ),
                InlineKeyboardButton(
                    text="❌ ОТКЛОНИТЬ", callback_data=f"reject:{application_id}"
                ),
            ]
        ]
    )


def escape(value: str) -> str:
    return html.escape(value, quote=True)


async def get_application_for_user(user_id: int) -> tuple | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT id, status FROM applications WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        return await cursor.fetchone()


async def send_approved_invite(bot: Bot, user_id: int, application_id: int) -> bool:
    invite = None
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=GROUP_CHAT_ID,
            member_limit=1,
            expire_date=datetime.now(timezone.utc) + timedelta(days=7),
            name=f"RAPIRA application {application_id}",
        )
        await bot.send_message(
            user_id,
            "🎉 <b>Ваша заявка одобрена!</b>\n\n"
            "Добро пожаловать в RAPIRA COMMUNITY!\n\n"
            "Ваша регистрация успешно пройдена. Теперь вы можете присоединиться "
            "к нашему сообществу по ссылке ниже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="ПРИСОЕДИНИТЬСЯ К ГРУППЕ", url=invite.invite_link)]
                ]
            ),
        )
        return True
    except Exception:
        logger.exception("Не удалось выдать ссылку для заявки %s", application_id)
        if invite:
            try:
                await bot.revoke_chat_invite_link(GROUP_CHAT_ID, invite.invite_link)
            except Exception:
                logger.exception("Не удалось отозвать ссылку для заявки %s", application_id)
        return False


async def start_registration(message: Message, bot: Bot, user_id: int | None = None) -> None:
    user_id = user_id or message.from_user.id
    application = await get_application_for_user(user_id)
    if application and application[1] == "pending":
        await message.answer("⏳ Ваша заявка уже находится на рассмотрении. Дождитесь решения администрации.")
        return
    if application and application[1] == "approved":
        if not await send_approved_invite(bot, user_id, application[0]):
            await message.answer(
                "✅ Ваша заявка одобрена, но сейчас не удалось выдать ссылку. "
                "Попробуйте отправить /start позже или обратитесь к администрации."
            )
        return

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO drafts(user_id, token, step) VALUES (?, ?, 'nickname') "
            "ON CONFLICT(user_id) DO UPDATE SET token = excluded.token, "
            "step = 'nickname', nickname = NULL, rapira_id = NULL, username = NULL",
            (user_id, str(uuid.uuid4())),
        )
        await db.commit()
    await message.answer(f"{WELCOME}\n\n<b>1/3</b> Укажи игровой ник (Nickname):")


async def deliver_application(bot: Bot, application: tuple) -> None:
    app_id, user_id, nickname, rapira_id, username, created_at = application
    text = (
        f"📥 <b>Заявка RAPIRA COMMUNITY №{app_id}</b>\n\n"
        f"🎮 Игровой ник: <b>{escape(nickname)}</b>\n"
        f"🆔 RAPIRA ID: <code>{escape(rapira_id)}</code>\n"
        f"✈️ Telegram: <b>{escape(username)}</b>\n"
        f"👤 Telegram ID: <code>{user_id}</code>\n"
        f"📅 Дата подачи: <code>{created_at}</code>\n\n"
        "Статус: <b>ОЖИДАНИЕ</b>"
    )
    targets = [int(ADMIN_CHAT_ID)] if ADMIN_CHAT_ID else sorted(ADMIN_IDS)
    for target in targets:
        try:
            await bot.send_message(
                target,
                text,
                reply_markup=moderation_keyboard(app_id),
            )
        except Exception:
            logger.exception("Не удалось доставить заявку %s в чат %s", app_id, target)


async def handle_text(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.text:
        return
    value = message.text.strip()
    if not value:
        await message.answer("Поле не должно быть пустым. Попробуй ещё раз.")
        return

    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT token, step, nickname, rapira_id, username FROM drafts WHERE user_id = ?",
            (message.from_user.id,),
        )
        draft = await cursor.fetchone()
        if not draft:
            return

        token, step, nickname, rapira_id, username = draft
        if step == "nickname":
            if len(value) > 64:
                await message.answer("Игровой ник должен быть не длиннее 64 символов.")
                return
            await db.execute(
                "UPDATE drafts SET nickname = ?, step = 'rapira_id' WHERE user_id = ? AND token = ?",
                (value, message.from_user.id, token),
            )
            response = "<b>2/3</b> Укажи свой RAPIRA ID:"
        elif step == "rapira_id":
            if len(value) > 64:
                await message.answer("RAPIRA ID должен быть не длиннее 64 символов.")
                return
            await db.execute(
                "UPDATE drafts SET rapira_id = ?, step = 'username' WHERE user_id = ? AND token = ?",
                (value, message.from_user.id, token),
            )
            response = "<b>3/3</b> Укажи Telegram Username (например, @username):"
        elif step == "username":
            normalized = value.removeprefix("@").strip()
            if not USERNAME_PATTERN.fullmatch(normalized):
                await message.answer(
                    "Введи корректный Telegram username: от 5 до 32 символов, "
                    "латинские буквы, цифры и подчёркивание."
                )
                return
            await db.execute(
                "UPDATE drafts SET username = ?, step = 'review' WHERE user_id = ? AND token = ?",
                (f"@{normalized}", message.from_user.id, token),
            )
            response = (
                "<b>Проверь данные перед отправкой:</b>\n\n"
                f"🎮 Игровой ник: <b>{escape(nickname or '')}</b>\n"
                f"🆔 RAPIRA ID: <code>{escape(rapira_id or '')}</code>\n"
                f"✈️ Telegram Username: <b>@{escape(normalized)}</b>"
            )
        else:
            return
        await db.commit()

    if step == "username":
        await message.answer(response, reply_markup=main_keyboard())
    else:
        await message.answer(response)


async def submit_application(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return

    application = None
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT token, step, nickname, rapira_id, username FROM drafts WHERE user_id = ?",
            (callback.from_user.id,),
        )
        draft = await cursor.fetchone()
        if draft and draft[1] == "review":
            token, _, nickname, rapira_id, username = draft
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                cursor = await db.execute(
                    "INSERT INTO applications(token, user_id, nickname, rapira_id, username, "
                    "created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                    (token, callback.from_user.id, nickname, rapira_id, username, created_at),
                )
                application_id = cursor.lastrowid
                await db.execute(
                    "DELETE FROM drafts WHERE user_id = ? AND token = ?",
                    (callback.from_user.id, token),
                )
                application = (
                    application_id,
                    callback.from_user.id,
                    nickname,
                    rapira_id,
                    username,
                    created_at,
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                await db.rollback()

    if not application:
        await callback.answer("Заявка уже отправлена или данные устарели.", show_alert=True)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "⏳ <b>Ваша заявка успешно отправлена!</b>\n\n"
        "Администрация RAPIRA COMMUNITY проверит ваши данные. Пожалуйста, дождитесь решения.\n\n"
        "Статус заявки: <b>ОЖИДАНИЕ</b>."
    )
    await deliver_application(bot, application)
    await callback.answer("Заявка отправлена")


async def decide_application(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это действие доступно только администраторам.", show_alert=True)
        return

    action, _, raw_id = callback.data.partition(":")
    if action not in {"approve", "reject"} or not raw_id.isdigit():
        await callback.answer("Некорректное действие.", show_alert=True)
        return
    application_id = int(raw_id)
    status = "approved" if action == "approve" else "rejected"
    decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE applications SET status = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, decided_at, callback.from_user.id, application_id),
        )
        changed = cursor.rowcount
        if changed:
            cursor = await db.execute(
                "SELECT user_id FROM applications WHERE id = ?", (application_id,)
            )
            row = await cursor.fetchone()
            user_id = row[0]
        await db.commit()

    if not changed:
        await callback.answer("По этой заявке уже принято решение.", show_alert=True)
        return

    if action == "approve":
        delivered = await send_approved_invite(bot, user_id, application_id)
        if delivered:
            decision_text = "☑ Заявка одобрена. Ссылка отправлена пользователю."
        else:
            decision_text = (
                "⚠ Заявка одобрена, но ссылка не отправлена. Пользователь сможет "
                "повторить запрос командой /start после устранения ошибки."
            )
    else:
        try:
            await bot.send_message(
                user_id,
                "❌ <b>Ваша заявка не была одобрена администрацией RAPIRA COMMUNITY.</b>\n\n"
                "При необходимости вы можете обратиться к администрации за дополнительной информацией.",
            )
            decision_text = "❌ Заявка отклонена. Пользователь уведомлён."
        except Exception:
            logger.exception("Не удалось отправить уведомление об отклонении %s", application_id)
            decision_text = "❌ Заявка отклонена. Не удалось уведомить пользователя."

    if callback.message:
        original = callback.message.text or "Заявка"
        await callback.message.edit_text(f"{original}\n\n<b>{decision_text}</b>", reply_markup=None)
    await callback.answer("Решение сохранено")


async def main() -> None:
    validate_config()
    await initialize_database()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()

    @dispatcher.message(CommandStart())
    async def on_start(message: Message) -> None:
        await start_registration(message)

    @dispatcher.message(Command("cancel"))
    async def on_cancel(message: Message) -> None:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("DELETE FROM drafts WHERE user_id = ?", (message.from_user.id,))
            await db.commit()
        await message.answer("Регистрация отменена. Чтобы начать заново, отправь /start.")

    @dispatcher.message(F.text)
    async def on_text(message: Message) -> None:
        await handle_text(message, bot)

    @dispatcher.callback_query(F.data == "submit")
    async def on_submit(callback: CallbackQuery) -> None:
        await submit_application(callback, bot)

    @dispatcher.callback_query(F.data == "restart")
    async def on_restart(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            await callback.answer()
            return
        await callback.message.delete()
        await start_registration(callback.message, bot, callback.from_user.id)
        await callback.answer()

    @dispatcher.callback_query(F.data.startswith(("approve:", "reject:")))
    async def on_decision(callback: CallbackQuery) -> None:
        await decide_application(callback, bot)

    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())