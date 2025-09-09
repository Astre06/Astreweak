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
    {
        "name": "api_ninjas",
        "url": "https://api.api-ninjas.com/v1/bin?bin=",
        "headers": {"X-Api-Key": "YOUR_API_NINJAS_KEY"},
        "params": {},
        "api_key": True,
        "parse": lambda data: (
            data.get("scheme", "N/A").upper(),
            data.get("type", "N/A").upper(),
            data.get("brand", "STANDARD").upper(),
            data.get("bank", "Unknown Bank"),
            data.get("country", "Unknown Country"),
        ),
    },
    {
        "name": "neutrino_api",
        "url": "https://neutrinoapi.net/bin-lookup",
        "headers": {},
        "params": {},
        "api_key": True,
        "post": True,
        "parse": lambda data: (
            data.get("scheme", "N/A").upper(),
            data.get("type", "N/A").upper(),
            data.get("brand", "STANDARD").upper(),
            data.get("bank", "Unknown Bank"),
            data.get("country", "Unknown Country"),
        ),
        # Add your user-id and api-key below for the Neutrino API:
        "auth": {"user-id": "YOUR_USER_ID", "api-key": "YOUR_API_KEY"},
    },
    {
        "name": "mastercard",
        "url": "https://sandbox.api.mastercard.com/bin/v1/bins/",  # Example sandbox endpoint
        "headers": {"Accept": "application/json"},
        "api_key": True,
        "parse": lambda data: (
            data.get("binAttributes", {}).get("cardBrand", "N/A").upper(),
            data.get("binAttributes", {}).get("cardType", "N/A").upper(),
            data.get("binAttributes", {}).get("cardCategory", "STANDARD").upper(),
            data.get("issuer", {}).get("name", "Unknown Bank"),
            data.get("country", {}).get("name", "Unknown Country"),
        ),
        # Add your Mastercard API key/credentials here as needed
        "auth": None,
    },
    {
        "name": "bincodes",
        "url": "https://api.bincodes.com/bin/check/",
        "headers": {},
        "api_key": True,
        "parse": lambda data: (
            data.get("scheme", "N/A").upper(),
            data.get("type", "N/A").upper(),
            data.get("brand", "STANDARD").upper(),
            data.get("bank_name", "Unknown Bank"),
            data.get("country_name", "Unknown Country"),
        ),
        "auth": {"apikey": "YOUR_BINCODES_API_KEY"},
    },
    {
        "name": "chargebackgurus",
        "url": "https://bin-api.chargebackgurus.com/",
        "headers": {},
        "api_key": True,
        "parse": lambda data: (
            data.get("scheme", "N/A").upper(),
            data.get("type", "N/A").upper(),
            data.get("brand", "STANDARD").upper(),
            data.get("bank", "Unknown Bank"),
            data.get("country", "Unknown Country"),
        ),
        "auth": {"x-api-key": "YOUR_CHARGEBACKGURUS_KEY"},
    },
    {
        "name": "bincheck_io",
        "url": "https://api.bincheck.io/v1/bin/",
        "headers": {},
        "api_key": True,
        "parse": lambda data: (
            data.get("payload", {}).get("scheme", "N/A").upper(),
            data.get("payload", {}).get("type", "N/A").upper(),
            data.get("payload", {}).get("brand", "STANDARD").upper(),
            data.get("payload", {}).get("bank", "Unknown Bank"),
            data.get("payload", {}).get("country", "Unknown Country"),
        ),
        "auth": {"apikey": "YOUR_BINCHECK_API_KEY"},
    },
    {
        "name": "pulse",
        "url": "https://pulse.pst.net/bin-lookup/api/bin/",
        "headers": {},
        "api_key": True,
        "parse": lambda data: (
            data.get("scheme", "N/A").upper(),
            data.get("type", "N/A").upper(),
            data.get("brand", "STANDARD").upper(),
            data.get("issuer", {}).get("name", "Unknown Bank"),
            data.get("country", {}).get("name", "Unknown Country"),
        ),
        "auth": {"Authorization": "Bearer YOUR_PULSE_API_KEY"},
    },
    {
        "name": "fraudlabspro",
        "url": "https://api.fraudlabspro.com/v1/bin/check",
        "headers": {},
        "api_key": True,
        "parse": lambda data: (
            data.get("bin", {}).get("scheme", "N/A").upper(),
            data.get("bin", {}).get("type", "N/A").upper(),
            data.get("bin", {}).get("brand", "STANDARD").upper(),
            data.get("bin", {}).get("bank", "Unknown Bank"),
            data.get("bin", {}).get("country_code", "Unknown Country"),
        ),
        "auth": {"key": "YOUR_FRAUDLABSPRO_KEY"},
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
