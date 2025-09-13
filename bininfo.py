import requests
import re
import threading

BIN_CACHE = {}

BIN_LOOKUP_SERVICES = [
    {
        "name": "binlist",
        "url": "https://lookup.binlist.net/",
        "headers": {"Accept-Version": "3", "User-Agent": "Mozilla/5.0"},
        "params": {},
        "api_key": False,
        "parse": lambda data: (
            data.get("scheme", "N/A").upper(),
            data.get("type", "N/A").upper(),
            data.get("brand", "STANDARD").upper(),
            data.get("bank", {}).get("name", "Unknown Bank"),
            data.get("country", {}).get("name", "Unknown Country"),
        ),
    },
    
]

_service_index_lock = threading.Lock()
_service_index = 0

def round_robin_bin_lookup(card_number: str, proxy=None):
    global _service_index
    bin_number = card_number[:6]

    if bin_number in BIN_CACHE:
        return BIN_CACHE[bin_number]

    num_services = len(BIN_LOOKUP_SERVICES)
    attempts = 0

    while attempts < num_services:
        with _service_index_lock:
            service = BIN_LOOKUP_SERVICES[_service_index]
            _service_index = (_service_index + 1) % num_services
        try:
            headers = service.get("headers", {}).copy()
            params = {}
            url = service["url"]
            auth = service.get("auth")

            if service.get("post", False):
                # POST request with auth data for some services
                params = service.get("auth", {}).copy()
                params["bin"] = bin_number
                resp = requests.post(url, headers=headers, data=params, proxies=proxy, timeout=15)
            else:
                if not url.endswith("/"):
                    url += "/"
                url += bin_number
                if auth:
                    headers.update(auth)
                resp = requests.get(url, headers=headers, params=params, proxies=proxy, timeout=15)

            if resp.status_code == 200:
                data = resp.json()
                scheme, card_type, level, bank, country = service["parse"](data)
                country_clean = re.sub(r"\s*\(.*?\)", "", country).strip()
                result = (f"{bin_number} - {level} - {card_type} - {scheme}", bank, country_clean)
                BIN_CACHE[bin_number] = result
                return result
        except Exception:
            attempts += 1
            continue

    result = (f"{bin_number} - ERROR", "Unknown Bank", "Unknown Country")
    BIN_CACHE[bin_number] = result
    return result


