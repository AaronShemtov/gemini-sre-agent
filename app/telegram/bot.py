"""
Telegram bot — the agent's front door.

Design choices:
- Long polling (not webhooks): no public endpoint needed, simplest to run
  inside the cluster behind no ingress.
- User whitelist: the bot only obeys user IDs in TELEGRAM_ALLOWED_USERS.
  This is critical — the agent can mutate the cluster, so it must not take
  orders from strangers who find the bot.
- Approval gate: when the graph proposes a mutating action, we send an
  inline keyboard (Approve / Reject). The pending action is held in memory
  keyed by chat; the callback resumes the RESUME_GRAPH.

Commands:
  /start, /help            — usage
  /investigate <question>  — run an investigation
  plain text               — treated as an investigation question too

Example:
  /investigate why is cv-frontend unhealthy in namespace cv
  why does urlshortener-reader keep restarting
"""

from __future__ import annotations

import logging
import os
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.graph.agent import GRAPH, RESUME_GRAPH
from app.telegram import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ai-sre-agent")

_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
_ALLOWED = {
    int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").replace(" ", "").split(",") if x
}

# In-memory store of pending mutating actions, keyed by chat_id.
# value: full AgentState dict awaiting approval.
_PENDING: dict[int, dict[str, Any]] = {}


def _authorised(update: Update) -> bool:
    user = update.effective_user
    if not _ALLOWED:
        # Fail closed: if no whitelist is configured, obey no one.
        log.warning("No TELEGRAM_ALLOWED_USERS configured — refusing all.")
        return False
    return bool(user and user.id in _ALLOWED)


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await update.message.reply_text("Not authorised.")
        return
    await update.message.reply_text(
        "AI SRE Agent for OKE.\n\n"
        "Ask me to investigate a Kubernetes issue, e.g.:\n"
        "• why is cv-frontend unhealthy in namespace cv\n"
        "• /investigate urlshortener-reader keeps restarting\n\n"
        "I gather pods, events, logs, deployment & service state, then give "
        "you an RCA. If a safe fix applies (rollout restart / service "
        "selector), I'll ask before doing anything."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


async def _run_investigation(update: Update, question: str) -> None:
    chat_id = update.effective_chat.id
    await update.effective_chat.send_action("typing")

    try:
        # LangGraph compiled graphs are sync; run in a thread so we don't
        # block the asyncio event loop.
        import asyncio
        state = await asyncio.to_thread(GRAPH.invoke, {"question": question})
    except Exception as e:  # noqa: BLE001
        log.exception("investigation failed")
        await update.message.reply_text(f"Investigation failed: {type(e).__name__}: {e}")
        return

    analysis = state.get("analysis", {})
    namespace = state.get("namespace", "?")
    await update.message.reply_text(
        render.render_analysis(analysis, namespace), parse_mode=ParseMode.HTML
    )

    proposed = state.get("proposed_action")
    if proposed:
        _PENDING[chat_id] = state
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Approve", callback_data="approve"),
                InlineKeyboardButton("❌ Reject", callback_data="reject"),
            ]]
        )
        await update.message.reply_text(
            render.render_action_prompt(proposed),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


async def cmd_investigate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await update.message.reply_text("Not authorised.")
        return
    question = " ".join(ctx.args) if ctx.args else ""
    if not question:
        await update.message.reply_text("Usage: /investigate <your question>")
        return
    await _run_investigation(update, question)


async def on_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await update.message.reply_text("Not authorised.")
        return
    await _run_investigation(update, update.message.text)


async def on_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # Re-check authorisation on the callback too — buttons can be pressed
    # by anyone who can see the message.
    user = query.from_user
    if not _ALLOWED or user.id not in _ALLOWED:
        await query.edit_message_text("Not authorised.")
        return

    chat_id = query.message.chat.id
    state = _PENDING.pop(chat_id, None)
    if state is None:
        await query.edit_message_text("This approval has expired.")
        return

    if query.data == "reject":
        await query.edit_message_text("❌ Action rejected. No changes made.")
        return

    # Approved → resume the graph to execute + verify.
    await query.edit_message_text("✅ Approved — executing…")
    import asyncio
    state["approved"] = True
    try:
        result_state = await asyncio.to_thread(RESUME_GRAPH.invoke, state)
    except Exception as e:  # noqa: BLE001
        log.exception("action execution failed")
        await query.message.reply_text(f"Execution failed: {type(e).__name__}: {e}")
        return

    await query.message.reply_text(
        render.render_action_result(
            state["proposed_action"],
            result_state.get("action_result", {}),
            result_state.get("verification", ""),
        ),
        parse_mode=ParseMode.HTML,
    )


def main() -> None:
    if not _ALLOWED:
        log.warning("TELEGRAM_ALLOWED_USERS is empty — the bot will refuse every request.")
    app = Application.builder().token(_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("investigate", cmd_investigate))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("AI SRE Agent starting (long polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
