import os
import re
import asyncio
import tempfile
import random

from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup

from bininfo import round_robin_bin_lookup
from auth_processor import generate_uuids, prepare_headers, check_card_across_sites
from config import TELEGRAM_BOT_TOKEN, DEFAULT_API_URL, MAX_WORKERS

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
        [InlineKeyboardButton(f"• {card} •", callback_data="noop")],
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

async def process_card(card, headers, sites, chat_id, bot_token, semaphore, proxy_instance):
    async with semaphore:
        uuids = generate_uuids()
        status, message, raw_card = await asyncio.get_running_loop().run_in_executor(
            None,
            check_card_across_sites,
            card,
            headers,
            uuids,
            chat_id,
            bot_token,
            sites,
            proxy_instance,
        )

        status_map = {
            "CVV": "CVV",
            "CCN": "CCN",
            "LOW_FUNDS": "Insufficient Funds",
            "DECLINED": "Declined",
            "APPROVED": "Approved",
            "INVALID_FORMAT": "Invalid Format",
        }

        status_text = status_map.get(status, status)

        if status == "INVALID_FORMAT":
            return {
                "raw_card": raw_card,
                "status": status,
                "status_text": status_text,
                "bin_info": "N/A",
                "bank": "N/A",
                "country": "N/A",
                "site_num": "N/A",
                "skip_detail": True
            }

        if status in ["APPROVED", "CVV", "CCN", "LOW_FUNDS"]:
            try:
                bin_info, bank, country = round_robin_bin_lookup(raw_card.split("|")[0])
            except Exception:
                bin_info, bank, country = "N/A", "N/A", "N/A"
            site_num = ""
            site_search = re.search(r"Site: (\d+)", message)
            if site_search:
                site_num = site_search.group(1)
            return {
                "raw_card": raw_card,
                "status": status,
                "status_text": status_text,
                "bin_info": bin_info,
                "bank": bank,
                "country": country,
                "site_num": site_num,
                "skip_detail": False
            }
        else:
            site_num = ""
            site_search = re.search(r"Site: (\d+)", message)
            if site_search:
                site_num = site_search.group(1)
            return {
                "raw_card": raw_card,
                "status": status,
                "status_text": status_text,
                "bin_info": "N/A",
                "bank": "N/A",
                "country": "N/A",
                "site_num": site_num,
                "skip_detail": True
            }

async def handle_file(update, context):
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please upload a valid .txt file with cards in format: card|month|year|cvc")
        return

    temp_path = os.path.join(tempfile.gettempdir(), doc.file_name)
    file = await update.message.document.get_file()
    local_path = os.path.join(tempfile.gettempdir(), doc.file_name)
    await file.download_to_drive(local_path)

    valid_cards = []
    with open(temp_path, "r") as f:
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
    )

    import proxy
    from manual_check import check_ip_via_proxy_async, get_own_ip_async

    output_file = os.path.join(tempfile.gettempdir(), f"results_{doc.file_name}")
    semaphore = asyncio.Semaphore(MAX_WORKERS)

    async def process_card_with_proxy_ip(card, proxy_instance, proxy_ip_info):
        print(f"Proxy for card: {proxy_instance}")  # print proxy for debug as manual check

        result = await process_card(card, headers, sites, chat_id, bot_token, semaphore, proxy_instance)
        result['proxy_ip'] = proxy_ip_info
        return result

    try:
        with open(output_file, "w") as outfile:
            batch_size = MAX_WORKERS

            proxies = proxy.load_proxies()
            if not proxies:
                proxies = [None]

            results = []

            for batch_start in range(0, total, batch_size):
                batch = valid_cards[batch_start:batch_start + batch_size]

                proxy_instance = random.choice(proxies)  # Random proxy per batch
                print(f"[BATCH {batch_start // batch_size + 1}] Proxy for batch: {proxy_instance}")

                proxy_ip_info = "N/A"
                if proxy_instance:
                    proxy_url = proxy_instance.get('http') or proxy_instance.get('https')
                    if proxy_url:
                        proxy_ip_info = await check_ip_via_proxy_async(proxy_url)
                else:
                    ip = await get_own_ip_async()
                    proxy_ip_info = f"{ip} (Own)" if ip else "N/A"

                tasks = [
                    process_card_with_proxy_ip(card, proxy_instance, proxy_ip_info)
                    for card in batch
                ]
                batch_results = await asyncio.gather(*tasks)

                for result in batch_results:
                    status = result["status"]
                    status_text = result["status_text"]
                    proxy_ip = result.get('proxy_ip', "N/A")

                    if status == "CVV":
                        cvv += 1
                    elif status == "CCN":
                        ccn += 1
                    elif status == "LOW_FUNDS":
                        low += 1
                    elif status == "DECLINED":
                        declined += 1
                    elif status == "APPROVED":
                        charged += 1

                    if status != "INVALID_FORMAT":
                        outfile.write(f"{result['raw_card']}|{status_text}\n")
                        outfile.flush()

                    if not result["skip_detail"]:
                        emoji = "✅" if status in ["APPROVED", "CVV", "CCN", "LOW_FUNDS"] else "❌"
                        detail_msg = (
                            f"CARD: {result['raw_card']}\n"
                            f"Gateway: Stripe Auth\n"
                            f"Response: {status_text} {emoji}\n"
                            f"Site: {result['site_num']} Ip: {proxy_ip}\n"
                            f"Bin Info: {result['bin_info']}\n"
                            f"Bank: {result['bank']}\n"
                            f"Country: {result['country']}"
                        )
                        await update.message.reply_text(
                            detail_msg,
                            parse_mode="HTML",
                            reply_to_message_id=update.message.message_id,
                        )

                    results.append(result)

                try:
                    await reply_msg.edit_text(
                        f"Processing {len(results)}/{total} cards...",
                        reply_markup=build_status_keyboard(
                            card=batch_results[-1]["raw_card"] if batch_results else "N/A",
                            total=total,
                            processed=len(results),
                            status=batch_results[-1]["status_text"] if batch_results else "Idle",
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

        if os.path.exists(output_file):
            try:
                await update.message.reply_document(
                    InputFile(output_file),
                    caption=f"Results: {cvv + ccn + low + charged} cards found"
                )
            except Exception:
                pass
            try:
                os.remove(output_file)
            except Exception:
                pass
        try:
            os.remove(temp_path)
        except Exception:
            pass
