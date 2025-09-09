import asyncio
import os
import re
import tempfile
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.helpers import escape_markdown

from config import TELEGRAM_BOT_TOKEN, MAX_WORKERS, DEFAULT_API_URL
from auth_processor import generate_uuids, prepare_headers, check_card_across_sites
import proxy  # proxy.py for proxy management
from bininfo import round_robin_bin_lookup
from manual_check import chk  # Manual single card check handler
from mass_check import handle_file  # Revised mass check handler from uploaded files

SITE_STORAGE_FILE = "current_site.txt"
PROXY_ADD_STATE = "proxy_add_state"
PROXY_MSG_IDS_KEY = "proxy_msg_ids"


def save_current_site(urls):
    """
    Save list of site URLs to a local file for persistent usage.
    """
    with open(SITE_STORAGE_FILE, "w", encoding="utf-8") as f:
        for url in urls:
            f.write(url.strip() + "\n")


def load_current_site():
    """
    Load the list of site URLs from file.
    If file doesn't exist or empty, return list with default site URL.
    """
    try:
        with open(SITE_STORAGE_FILE, "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip()]
            return sites if sites else [DEFAULT_API_URL]
    except FileNotFoundError:
        return [DEFAULT_API_URL]


def build_status_keyboard(
    card, total, processed, status, charged, cvv, ccn, low, declined, checking
):
    """
    Build an inline keyboard with current processing stats and controls.
    """
    keyboard = [
        [InlineKeyboardButton(f"• {card} •", callback_data="noop")],
        [InlineKeyboardButton(f"• STATUS → {status} •", callback_data="noop")],
        [InlineKeyboardButton(f"• CVV → [ {cvv} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• CCN → [ {ccn} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• LOW FUNDS → [ {low} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• DECLINED → [ {declined} ] •", callback_data="noop")],
        [InlineKeyboardButton(f"• TOTAL → [ {total} ] •", callback_data="noop")],
    ]

    if checking:
        keyboard.append([InlineKeyboardButton(" [ STOP ] ", callback_data="stop")])
    else:
        keyboard.append([InlineKeyboardButton(" Replace ", callback_data="replace_site")])
        keyboard.append([InlineKeyboardButton(" Done ", callback_data="done_sites")])

    return InlineKeyboardMarkup(keyboard)


def append_proxy_message(context, message):
    # Store message id for later cleanup
    proxy_msg_ids = context.user_data.setdefault(PROXY_MSG_IDS_KEY, [])
    proxy_msg_ids.append(message.message_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start command handler: instructions for using the bot.
    """
    msg = (
        "Send me a .txt file with one card per line in the format:\n"
        "`card|month|year|cvc`\n"
        "Example:\n"
        "`4242424242424242|12|2025|123`"
    )
    await update.message.reply_markdown_v2(escape_markdown(msg, version=2))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "replace_site":
        context.user_data["awaiting_site"] = True
        context.user_data["site_buffer"] = []

        try:
            # Edit message and save the edited message object
            edited_msg = await query.edit_message_text(
                "Please send site URLs (one or more). You can send multiple messages. When done, click Done.",
            )
            context.user_data["site_prompt_msg_id"] = edited_msg.message_id
            context.user_data["site_prompt_chat_id"] = edited_msg.chat_id

        except Exception:
            sent_msg = await query.message.reply_text(
                "Please send site URLs (one or more). You can send multiple messages. When done, click Done.",
            )
            context.user_data["site_prompt_msg_id"] = sent_msg.message_id
            context.user_data["site_prompt_chat_id"] = sent_msg.chat_id

        return

    elif data == "done_sites":
        # Save collected sites
        sites_to_save = context.user_data.get("site_buffer", [])
        if sites_to_save:
            save_current_site(sites_to_save)
            saved_msg = await query.message.reply_text("Site(s) saved successfully.")
        else:
            saved_msg = await query.message.reply_text("No sites to save.")

        # Clear flags and buffer
        context.user_data["awaiting_site"] = False
        context.user_data["site_buffer"] = []

        # Delete prompt message if exists
        try:
            chat_id = context.user_data.get("site_prompt_chat_id")
            msg_id = context.user_data.get("site_prompt_msg_id")
            if chat_id and msg_id:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                # Remove from user_data after deletion
                context.user_data.pop("site_prompt_chat_id", None)
                context.user_data.pop("site_prompt_msg_id", None)
        except Exception:
            pass

        # Optional: wait and delete saved_msg for clean UI
        await asyncio.sleep(1)
        try:
            await saved_msg.delete()
        except Exception:
            pass

        # Also delete the callback query message with buttons if desired
        try:
            await query.message.delete()
        except Exception:
            pass

        return

    elif data == "finish_site":
        # Final finish callback
        await query.answer("Site management finished.")
        try:
            await query.message.delete()
        except Exception:
            pass

    elif data == "proxy_add":
        context.user_data[PROXY_ADD_STATE] = True
        try:
            await query.edit_message_text(
                "Please send a `.txt` file containing proxies in the format: IP:PORT:USERNAME:PASSWORD",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Back", callback_data="proxy_back"),
                            InlineKeyboardButton("Done", callback_data="proxy_done"),
                        ]
                    ]
                ),
            )
        except Exception as e:
            await query.message.reply_text(
                "Please send a `.txt` file containing proxies in the format: IP:PORT:USERNAME:PASSWORD",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Back", callback_data="proxy_back"),
                            InlineKeyboardButton("Done", callback_data="proxy_done"),
                        ]
                    ]
                ),
            )
    elif data == "proxy_back":
        context.user_data[PROXY_ADD_STATE] = False
        try:
            await query.edit_message_text(
                "Choose Proxy option:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Add", callback_data="proxy_add"),
                            InlineKeyboardButton("Del", callback_data="proxy_del"),
                        ]
                    ]
                ),
            )
        except Exception as e:
            await query.message.reply_text(
                "Choose Proxy option:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Add", callback_data="proxy_add"),
                            InlineKeyboardButton("Del", callback_data="proxy_del"),
                        ]
                    ]
                ),
            )
    elif data == "proxy_del":
        proxy.delete_proxies()
        del_msg = await query.message.reply_text("All proxies have been deleted.")
        append_proxy_message(context, del_msg)
        await asyncio.sleep(1)
        msg_ids = context.user_data.get(PROXY_MSG_IDS_KEY, [])
        for msg_id in msg_ids:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except Exception:
                pass
        context.user_data[PROXY_MSG_IDS_KEY] = []
        context.user_data[PROXY_ADD_STATE] = False
    elif data == "proxy_done":
        done_msg = await query.message.reply_text("Proxy add successfully.")
        append_proxy_message(context, done_msg)
        await asyncio.sleep(1)
        msg_ids = context.user_data.get(PROXY_MSG_IDS_KEY, [])
        for msg_id in msg_ids:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except Exception:
                pass
        context.user_data[PROXY_MSG_IDS_KEY] = []
        context.user_data[PROXY_ADD_STATE] = False
    else:
        # For "noop" or any other unhandled callbacks, do nothing
        pass


async def capture_site_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_site"):
        text = update.message.text
        urls = re.findall(r'https?://[^\s]+', text)
        if urls:
            # Append new URLs to site_buffer
            context.user_data.setdefault("site_buffer", []).extend(urls)

            # Build inline keyboard with Add more and Done buttons side by side
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Add more", callback_data="replace_site"),
                        InlineKeyboardButton("Done", callback_data="done_sites"),
                    ]
                ]
            )
            await update.message.reply_text(
                f"Received {len(context.user_data['site_buffer'])} site(s). Send more or click Done when finished.",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "No valid URLs detected. Please try again or click Done if finished."
            )


async def sitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sitelist command handler: lists currently configured sites.
    Auto-deletes the reply after 10 seconds.
    """
    sites = load_current_site()
    if not sites:
        sent_msg = await update.message.reply_text("No sites are currently set.")
    else:
        sites_text = "\n".join([f"{idx + 1}. {site}" for idx, site in enumerate(sites)])
        sent_msg = await update.message.reply_text(f"Current sites:\n{sites_text}")

    # Wait about 10 seconds and delete the message for cleaner UI
    await asyncio.sleep(10)
    try:
        await sent_msg.delete()
    except Exception:
        pass


async def proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /proxy command handler: one-line keyboard for Add and Del options.
    """
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add", callback_data="proxy_add"),
                InlineKeyboardButton("Del", callback_data="proxy_del"),
            ]
        ]
    )
    sent = await update.message.reply_text("Choose Proxy option:", reply_markup=keyboard)
    append_proxy_message(context, sent)


async def proxy_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    This is effectively handled in button_handler for proxy callbacks,
    so to avoid conflicts this handler does minimal.
    """
    pass


async def handle_other_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler for capturing text messages not recognized as commands.
    Used here to capture site URL input when awaiting site replacement.
    """
    if context.user_data.get("awaiting_site"):
        await capture_site_message(update, context)
    else:
        # Could handle other types of plain text messages here if needed
        pass


async def handle_proxy_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles txt upload ONLY if proxy add state active.
    Tracks messages for bulk cleanup.
    """
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        warn_msg = await update.message.reply_text(
            "Please upload a .txt file with proxies in the format: IP:PORT:USERNAME:PASSWORD"
        )
        append_proxy_message(context, warn_msg)
        return

    file = await doc.get_file()
    local_path = os.path.join(tempfile.gettempdir(), doc.file_name)
    await file.download_to_drive(local_path)

    try:
        with open(local_path, "r") as f:
            proxy_lines = [line.strip() for line in f if line.strip()]
    except Exception as e:
        fail_msg = await update.message.reply_text(f"Failed to read the uploaded file: {e}")
        append_proxy_message(context, fail_msg)
        return

    if not proxy_lines:
        empty_msg = await update.message.reply_text("The uploaded file is empty.")
        append_proxy_message(context, empty_msg)
        return

    proxy.add_proxies(proxy_lines)
    succ_msg = await update.message.reply_text(f"Successfully added {len(proxy_lines)} proxies.")
    append_proxy_message(context, succ_msg)

    try:
        os.remove(local_path)
    except Exception:
        pass


async def handle_file_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Wrapper for file uploads: routes to proxy or card depending on user state.
    """
    if context.user_data.get(PROXY_ADD_STATE, False):
        await handle_proxy_file_upload(update, context)
    else:
        await handle_file(update, context)


async def site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /site command handler: show Replace and Done buttons to manage sites.
    """
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Replace", callback_data="replace_site"),
                InlineKeyboardButton("Done", callback_data="done_sites"),
            ]
        ]
    )
    await update.message.reply_text("Choose an option:", reply_markup=keyboard)


def main():
    """
    Main entry point to start the Telegram bot and register handlers.
    """
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("site", site))
    app.add_handler(CommandHandler("sitelist", sitelist))
    app.add_handler(CommandHandler("proxy", proxy_command))
    app.add_handler(CommandHandler("chk", chk))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(button_handler))

    # No separate proxy_button_handler needed since handled in button_handler

    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_other_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file_wrapper))

    print("🤖 Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
