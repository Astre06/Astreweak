import os
import re
import asyncio
import tempfile
import random
import proxy

from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from bininfo import round_robin_bin_lookup
from auth_processor import generate_uuids, prepare_headers, check_card_across_sites
from config import TELEGRAM_BOT_TOKEN, DEFAULT_API_URL, MAX_WORKERS
from manual_check import check_ip_via_proxy_async, get_own_ip_async

SITE_STORAGE_FILE = "current_site.txt"

def load_current_site():
    try:
        with open(SITE_STORAGE_FILE, "r", encoding="utf-8") as f:
            sites = [line.strip() for line in f if line.strip()]
            return sites if sites else [DEFAULT_API_URL]
    except FileNotFoundError:
        return [DEFAULT_API_URL]

def build_status_keyboard(card, total, processed, status, charged, cvv, ccn, low, declined, checking):
    keyboard = [
        [InlineKeyboardButton(f"•{card}•", callback_data="noop")],
        [InlineKeyboardButton(f" STATUS → {status} ", callback_data="noop")],
        [InlineKeyboardButton(f" CVV → [ {cvv} ] ", callback_data="noop")],
        [InlineKeyboardButton(f" CCN → [ {ccn} ] ", callback_data="noop")],
        [InlineKeyboardButton(f" LOW FUNDS → [ {low} ] ", callback_data="noop")],
        [InlineKeyboardButton(f" DECLINED → [ {declined} ] ", callback_data="noop")],
        [InlineKeyboardButton(f" TOTAL → [ {total} ] ", callback_data="noop")],
    ]

    if checking:
        keyboard.append([InlineKeyboardButton(" STOP ", callback_data="stop")])
    return InlineKeyboardMarkup(keyboard)

async def process_card(card, headers, sites, chat_id, bot_token, semaphore):
    import proxy
    async with semaphore:
        uuids = generate_uuids()
        proxy_for_card = proxy.get_next_proxy()
        proxy_ip = "N/A"

        if proxy_for_card:
            proxy_url = proxy_for_card.get("http") or proxy_for_card.get("https")
            if proxy_url:
                proxy_ip = await check_ip_via_proxy_async(proxy_url)
        else:
            own_ip = await get_own_ip_async()
            proxy_ip = f"{own_ip} (Own)"

        try:
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
        except Exception as e:
            print(f"Proxy failed: {e}, retrying without proxy")
            status, message, raw_card = await asyncio.get_running_loop().run_in_executor(
                None,
                check_card_across_sites,
                card,
                headers,
                uuids,
                chat_id,
                bot_token,
                sites,
                None,
            )
            own_ip = await get_own_ip_async()
            proxy_ip = f"{own_ip} (Own)"

        proxy_failure_indicators = [
            "unable to connect",
            "proxy error",
            "proxyconnectionfailed",
            "proxyconnect",
            "connection refused",
            "connection timed out",
            "proxy refused",
            "proxy failed",
        ]
        if any(indicator in message.lower() for indicator in proxy_failure_indicators):
            print("Detected proxy failure in message, retrying without proxy")
            status, message, raw_card = await asyncio.get_running_loop().run_in_executor(
                None,
                check_card_across_sites,
                card,
                headers,
                uuids,
                chat_id,
                bot_token,
                sites,
                None,
            )
            own_ip = await get_own_ip_async()
            proxy_ip = f"{own_ip} (Own)"

        return {
            "raw_card": raw_card,
            "status": status,
            "status_text": status,
            "proxy_ip": proxy_ip,
        }

async def handle_file(update, context):
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please upload a valid .txt file with cards in format: card|month|year|cvc")
        return

    temp_path = os.path.join(os.getcwd(), doc.file_name)
    file = await update.message.document.get_file()
    await file.download_to_drive(temp_path)

    valid_cards = []
    with open(temp_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            normalized = re.sub(r"\s*\|\s*", "|", line)
            if len(normalized.split("|")) == 4:
                valid_cards.append(normalized)

    if not valid_cards:
        await update.message.reply_text("No valid entries found in the file.")
        return

    total = len(valid_cards)
    cvv = ccn = low = declined = charged = 0
    sites = load_current_site()
    headers = prepare_headers()
    chat_id = update.effective_chat.id
    bot_token = context.bot.token

    reply_msg = await update.message.reply_text(
        f"Processing 0/{total} cards...",
        reply_markup=build_status_keyboard(
            card="Waiting",
            total=total,
            processed=0,
            status="Idle",
            charged=charged,
            cvv=cvv,
            ccn=ccn,
            low=low,
            declined=declined,
            checking=True,
        ),
        reply_to_message_id=update.message.message_id,
    )

    base_name, _ = os.path.splitext(doc.file_name)
    output_filename = f"{base_name}_results.txt"
    output_file = os.path.join(os.getcwd(), output_filename)
    semaphore = asyncio.Semaphore(MAX_WORKERS)

    results = []
    status_lock = asyncio.Lock()  # ✅ FIX: define lock for live updates

    try:
        with open(output_file, "w", encoding="utf-8") as outfile:
            batch_size = MAX_WORKERS
            proxies = proxy.load_proxies()
            if not proxies:
                proxies = [None]

            for batch_start in range(0, total, batch_size):
                batch = valid_cards[batch_start:batch_start + batch_size]
                proxy_instance = random.choice(proxies)  # Not used here, but kept for original design

                proxy_ip_info = "N/A"
                if proxy_instance:
                    proxy_url = proxy_instance.get('http') or proxy_instance.get('https')
                    if proxy_url:
                        proxy_ip_info = await check_ip_via_proxy_async(proxy_url)
                else:
                    ip = await get_own_ip_async()
                    proxy_ip_info = f"{ip} (Own)" if ip else "N/A"

                tasks = [
                    process_card(card, headers, sites, chat_id, bot_token, semaphore)
                    for card in batch
                ]

                batch_results = await asyncio.gather(*tasks)

                for result in batch_results:
                    results.append(result)
                    
                    status = result["status"]
                    status_text = result["status_text"]
                    proxy_ip = result.get('proxy_ip', "N/A")
                    raw_card = result["raw_card"]

                    # ✅ FIX: proper classification
                    status_lower = (status or "").lower()

                    if "declined" in status_lower:
                        declined += 1
                    elif "security code incorrect" in status_lower or "incorrect_cvc" in status_lower or status == "CCN":
                        ccn += 1
                    elif "succeeded" in status_lower or "approved" in status_lower or status == "CVV":
                        cvv += 1
                    elif "low_funds" in status_lower or "insufficient funds" in status_lower or status == "LOW_FUNDS":
                        low += 1
                    else:
                        declined += 1

                    if status in ["APPROVED", "CVV", "CCN", "LOW_FUNDS"]:
                        try:
                            bin_info, bank, country = round_robin_bin_lookup(raw_card.split("|")[0])
                        except Exception:
                            bin_info, bank, country = "N/A", "N/A", "N/A"
                        
                        emoji = "✅"
                        
                        detail_msg = (
                            f"<b>CARD:</b> <code>{raw_card}</code>\n"
                            f"<b>Gateway:</b> Stripe Auth\n"
                            f"<b>Response:</b> {status_text} {emoji}\n"
                            f"<b>Site:</b>      <b>Ip:</b> {proxy_ip}\n"
                            f"<b>Bin Info:</b> {bin_info}\n"
                            f"<b>Bank:</b> {bank}\n"
                            f"<b>Country:</b> {country}"
                        )

                        await update.message.reply_text(
                            detail_msg.replace(" | ", "\n"),  # pretty format for chat
                            parse_mode="HTML"
                        )
                        # ✅ Save valid card into results file
                        outfile.write(detail_msg + "\n")
                        outfile.flush()

                    try:
                        async with status_lock:
                            await reply_msg.edit_text(
                                f"Processing {len(results)}/{total} cards...",
                                reply_markup=build_status_keyboard(
                                    card=raw_card,
                                    total=total,
                                    processed=len(results),
                                    status=status_text,
                                    charged=charged,
                                    cvv=cvv,
                                    ccn=ccn,
                                    low=low,
                                    declined=declined,
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

        try:
            os.remove(output_file)
        except Exception as e:
            print(f"Error deleting results file: {e}")



