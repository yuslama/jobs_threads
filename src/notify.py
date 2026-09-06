"""Telegram delivery and button callbacks (R7).

One message per lead, self-contained enough to triage without opening
anything. A lead that cannot be delivered is queued, not lost: Telegram being
unreachable at 2am must not cost her a vacancy.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from .config import Config
from .store import Store

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import acyclic
    from .pipeline import Lead

log = logging.getLogger(__name__)

CAPTION_PREVIEW_CHARS = 300
SOURCE_LABELS = {"source_a": "watch list", "source_b": "search"}

CB_APPLIED = "applied"
CB_DISMISS = "dismiss"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def preview(caption: str, limit: int = CAPTION_PREVIEW_CHARS) -> str:
    caption = (caption or "").strip()
    if len(caption) <= limit:
        return caption
    return caption[:limit].rstrip() + "..."


def build_lead_message(lead: "Lead") -> str:
    """The notification body. Everything needed to triage, nothing else."""
    post = lead.post
    extraction = lead.extraction
    role = extraction.role or "role unclear"
    lines = [
        f"<b>{_esc(role)}</b>",
        f"@{_esc(post.username)} · via {_esc(SOURCE_LABELS.get(lead.source, lead.source))}",
        "",
        f"Apply: {_esc(extraction.apply_method)}",
    ]
    if extraction.deadline:
        lines.append(f"Deadline: {_esc(extraction.deadline)}")
    if post.taken_at:
        lines.append(f"Posted: {post.taken_at.strftime('%d %b %H:%M')} UTC")
    lines += ["", f"<blockquote>{_esc(preview(post.caption))}</blockquote>", "", f'<a href="{_esc(post.url)}">Open post</a>']
    return "\n".join(lines)


def build_draft_message(lead: "Lead") -> str | None:
    """The draft as a tap-to-copy block. Never sent anywhere by the bot."""
    if not lead.draft:
        return None
    return "Draft (review before sending):\n" f"<pre>{_esc(lead.draft)}</pre>"


def lead_keyboard(pk: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Mark applied", callback_data=f"{CB_APPLIED}:{pk}"),
            InlineKeyboardButton("Not relevant", callback_data=f"{CB_DISMISS}:{pk}"),
        ]]
    )


class Notifier:
    """Sends, and remembers what it could not send."""

    def __init__(self, config: Config, store: Store, bot: Bot | None = None) -> None:
        self.config = config
        self.store = store
        secrets = config.secrets
        self.chat_id = secrets.telegram_chat_id
        self.operator_chat_id = secrets.operator_chat_id
        self.bot = bot or (Bot(secrets.telegram_bot_token) if secrets.telegram_bot_token else None)

    # ------------------------------------------------------------- sending
    async def _send(self, chat_id: str, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        if self.bot is None:
            raise TelegramError("no telegram bot token configured")
        if not chat_id:
            raise TelegramError("no telegram chat id configured")
        await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    async def send_lead(self, lead: "Lead") -> bool:
        """Deliver one lead. Queues it for the next cycle if delivery fails."""
        text = build_lead_message(lead)
        draft = build_draft_message(lead)
        try:
            await self._send(self.chat_id, text, lead_keyboard(lead.post.pk))
            if draft:
                await self._send(self.chat_id, draft)
        except TelegramError as exc:
            log.warning("telegram send failed for %s, queued: %s", lead.post.pk, exc)
            self.store.enqueue_notification(
                "lead",
                {"text": text, "draft": draft, "pk": lead.post.pk},
                lead_pk=lead.post.pk,
            )
            return False
        self.store.mark_lead_notified(lead.post.pk)
        return True

    async def send_operator(self, text: str) -> bool:
        """A message for Yus: source alerts and account suggestions."""
        try:
            await self._send(self.operator_chat_id, text)
        except TelegramError as exc:
            log.warning("telegram operator send failed, queued: %s", exc)
            self.store.enqueue_notification("operator", {"text": text})
            return False
        return True

    async def send_suggestion(self, username: str, hits: int, post_urls: list[str]) -> bool:
        lines = [
            "<b>Watch-list suggestion</b>",
            f"@{_esc(username)} produced {hits} vacancy posts via search but is not on the watch list.",
            "",
            "Posts that triggered this:",
        ]
        lines += [f"· {_esc(url)}" for url in post_urls]
        lines += [
            "",
            f"To promote: add <code>{_esc(username)}</code> to "
            f"<code>source_a.watched_accounts</code> in config.yaml and restart.",
        ]
        return await self.send_operator("\n".join(lines))

    async def send_source_alert(self, source: str, detail: str) -> bool:
        return await self.send_operator(
            f"<b>Source problem: {_esc(source)}</b>\n{_esc(detail)}"
        )

    # --------------------------------------------------------------- queue
    async def flush_queue(self) -> int:
        """Retry queued messages. Called at the top of every cycle."""
        sent = 0
        for item in self.store.pending_notifications():
            payload = item["payload"]
            chat_id = self.chat_id if item["kind"] == "lead" else self.operator_chat_id
            keyboard = lead_keyboard(payload["pk"]) if item["kind"] == "lead" and payload.get("pk") else None
            try:
                await self._send(chat_id, payload["text"], keyboard)
                if payload.get("draft"):
                    await self._send(chat_id, payload["draft"])
            except TelegramError as exc:
                attempts = self.store.bump_notification_attempt(item["id"], str(exc))
                if attempts >= self.config.reliability.notify_max_attempts:
                    log.error("dropping queued notification %s after %d attempts", item["id"], attempts)
                    self.store.drop_notification(item["id"])
                # Telegram is down for everyone, so the rest of the queue will
                # fail identically. Stop and try again next cycle.
                break
            else:
                self.store.drop_notification(item["id"])
                if item["lead_pk"]:
                    self.store.mark_lead_notified(item["lead_pk"])
                sent += 1
        return sent


# ------------------------------------------------------------- callbacks
def make_callback_handler(store: Store) -> CallbackQueryHandler:
    """Wires *Mark applied* and *Not relevant* to the lead's status."""

    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        action, _, pk = query.data.partition(":")
        status = {CB_APPLIED: "applied", CB_DISMISS: "not_relevant"}.get(action)
        if status is None or not pk:
            await query.answer()
            return

        known = store.set_lead_status(pk, status)
        label = "Marked applied" if status == "applied" else "Dismissed"
        await query.answer(label if known else "Lead not found")
        if known:
            try:
                # Replace the buttons with the outcome so the chat history
                # shows what she already handled.
                await query.edit_message_reply_markup(
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"✓ {label}", callback_data="noop:0")]])
                )
            except TelegramError as exc:
                log.debug("could not update markup for %s: %s", pk, exc)

    return CallbackQueryHandler(handle)


def build_application(config: Config, store: Store) -> Application | None:
    """The polling Application that receives button presses."""
    token = config.secrets.telegram_bot_token
    if not token:
        log.warning("no TELEGRAM_BOT_TOKEN: buttons will not respond")
        return None
    application = Application.builder().token(token).build()
    application.add_handler(make_callback_handler(store))
    return application
