import re
import asyncio
import aiohttp
from bininfo import round_robin_bin_lookup
from auth_processor import prepare_headers, generate_uuids, check_card_across_sites
from config import TELEGRAM_BOT_TOKEN

async def check_ip_via_proxy_async(proxy_url):
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get('https://api.ipify.org?format=json', proxy=proxy_url) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get('ip', 'N/A')
    except Exception:
        return "N/A"

async def get_own_ip_async():
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get('https://api.ipify.org?format=json') as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get('ip', 'N/A')
    except Exception:
        return "N/A"

async def chk(update, context):
    text = update.message.text
    processing_msg = await update.message.reply_text(
        "Processing ⚙️...",
        reply_to_message_id=update.message.message_id
    )
    match = re.match(r'^\.?\/?chk\s+(.+)', text, re.IGNORECASE)
    if not match:
        await processing_msg.delete()
        await update.message.reply_text(
            "Usage: /chk card|month|year|cvc\n"
            "Example: /chk 4242424242424242|12|25|123\n"
            "Expiration must be in MM|YY or MM|YYYY format."
        )
        return
    rest = match.group(1).strip()
    fields = re.split(r'\s*\|\s*', rest)
    if len(fields) != 4:
        await processing_msg.delete()
        await update.message.reply_text(
            "Usage: /chk card|month|year|cvc\n"
            "Example: /chk 4242424242424242|12|25|123\n"
            "Expiration must be in MM|YY or MM|YYYY format."
        )
        return
    card_number, exp_month, exp_year, cvc = fields
    card_data = f"{card_number}|{exp_month}|{exp_year}|{cvc}"
    from main import load_current_site
    sites = load_current_site()
    headers = prepare_headers()
    uuids = generate_uuids()
    chat_id = update.message.chat_id
    bot_token = TELEGRAM_BOT_TOKEN
    await asyncio.sleep(1)  # Delay to reduce rapid repeated checks
    import proxy
    proxy_for_card = proxy.get_next_proxy()
    print("Proxy for card:", proxy_for_card)  # Debug: print proxy used
    proxy_ip = "N/A"
    if proxy_for_card:
        proxy_url = proxy_for_card.get("http") or proxy_for_card.get("https")
        if proxy_url:
            proxy_ip = await check_ip_via_proxy_async(proxy_url)
    else:
        own_ip = await get_own_ip_async()
        proxy_ip = f"{own_ip} (Own)"

    try:
        status, msg, raw = await asyncio.get_running_loop().run_in_executor(
            None,
            check_card_across_sites,
            card_data,
            headers,
            uuids,
            chat_id,
            bot_token,
            sites,
            proxy_for_card,
        )
    except Exception as e:
        print(f"Proxy failed: {e}, retrying without proxy")
        status, msg, raw = await asyncio.get_running_loop().run_in_executor(
            None,
            check_card_across_sites,
            card_data,
            headers,
            uuids,
            chat_id,
            bot_token,
            sites,
            None,
        )
        # Set proxy_ip to your actual server IP after fallback
        own_ip = await get_own_ip_async()
        proxy_ip = f"{own_ip} (Own)"
    # Detect proxy failure messages and retry without proxy
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

    if any(indicator in msg.lower() for indicator in proxy_failure_indicators):
        print("Detected proxy failure in message, retrying without proxy")
        status, msg, raw = await asyncio.get_running_loop().run_in_executor(
            None,
            check_card_across_sites,
            card_data,
            headers,
            uuids,
            chat_id,
            bot_token,
            sites,
            None,
        )

    await processing_msg.delete()
    site_num = ""
    site_search = re.search(r"Site: (\d+)", msg)
    if site_search:
        site_num = site_search.group(1)
        msg = re.sub(r"\nSite: \d+", "", msg)

    try:
        bin_info, bank, country = round_robin_bin_lookup(raw.split('|')[0])
    except Exception:
        bin_info, bank, country = "N/A", "N/A", "N/A"

    status_mapping = {
        "CVV": "CVV",
        "CCN": "CCN Live",
        "LOW_FUNDS": "Insufficient Funds",
        "DECLINED": "Declined",
        "INVALID_FORMAT": "Invalid Format",
    }

    if status in ["APPROVED", "CVV", "CCN", "LOW_FUNDS"]:
        status_emoji = "✅"
    else:
        status_emoji = "❌"

    status_text = status_mapping.get(status, status)

    final_msg = (
        f"<b>CARD:</b> <code>{raw}</code>\n"
        f"<b>Gateway:</b> Stripe Auth\n"
        f"<b>Response:</b> {status_text} {status_emoji}\n"
        f"<b>Site:</b> {site_num}     <b>Ip:</b> {proxy_ip}\n"
        f"<b>Bin Info:</b> {bin_info}\n"
        f"<b>Bank:</b> {bank}\n"
        f"<b>Country:</b> {country}"
    )

    await update.message.reply_text(
        final_msg,
        parse_mode="HTML",
        reply_to_message_id=update.message.message_id
    )
