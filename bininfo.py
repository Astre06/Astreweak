import requests
import re
import threading

BIN_LOOKUP_SERVICES = [
    {
        "name": "binlist_net",
        "url_template": "https://lookup.binlist.net/{}",  # Using your requested URL pattern
        "headers": {},
        "params": {},
        "api_key": False,
        "post": False,
        # For this service, parsing JSON response directly
        "parse": lambda data: (
            data.get("scheme", "N/A").upper(),
            data.get("type", "N/A").upper(),
            data.get("brand", "STANDARD").upper(),
            data.get("bank", {}).get("name", "Unknown Bank"),
            data.get("country", {}).get("name", "Unknown Country"),
        ),
    },
    {
        "name": "antipublic_bins",
        "url": "https://bins.antipublic.cc/bins/",
        "headers": {},
        "params": {},
        "api_key": False,
        "post": False,
        "parse": lambda data: (
            data.get("brand", "Unknown").upper(),
            data.get("type", "Unknown").upper(),
            "STANDARD",
            data.get("bank", "Unknown Bank"),
            data.get("country_name", "Unknown Country"),
        ),
    },
]

_service_index_lock = threading.Lock()
_service_index = 0
_cache = {}
_active_services = BIN_LOOKUP_SERVICES.copy()

def _get_next_service():
    global _service_index
    with _service_index_lock:
        if not _active_services:
            return None
        service = _active_services[_service_index]
        _service_index = (_service_index + 1) % len(_active_services)
        return service

def _mark_service_failed(failed_service):
    global _service_index
    with _service_index_lock:
        _active_services[:] = [s for s in _active_services if s != failed_service]
        if _service_index >= len(_active_services):
            _service_index = 0

def _lookup_single_service(bin_number, service, proxy=None, timeout_seconds=10):
    try:
        headers = service.get("headers", {}).copy()
        params = {}
        auth = service.get("auth")

        if "url_template" in service:
            url = service["url_template"].format(bin_number)
        else:
            url = service["url"]
            if not url.endswith("/"):
                url += "/"
            url += bin_number

        if service.get("post", False):
            params = service.get("auth", {}).copy()
            params["bin"] = bin_number
            resp = requests.post(url, headers=headers, data=params, proxies=proxy, timeout=timeout_seconds)
        else:
            if auth:
                headers.update(auth)
            resp = requests.get(url, headers=headers, params=params, proxies=proxy, timeout=timeout_seconds)

        if resp.status_code == 200:
            data = resp.json()
            scheme, card_type, level, bank, country = service["parse"](data)
            country_clean = re.sub(r"\s*\(.*?\)", "", country).strip()
            result = (f"{bin_number} - {level} - {card_type} - {scheme}", bank, country_clean)

            # Cache only if info is not Unknown
            if "Unknown" not in result:
                _cache[bin_number] = result

            return result
        else:
            print(f"Error response from {service['name']}: HTTP {resp.status_code}")

    except Exception as e:
        print(f"Exception during request to {service['name']}: {e}")

    return None

def round_robin_bin_lookup(card_number: str, proxy=None, timeout_seconds=10):
    bin_number = card_number[:6]
    if bin_number in _cache:
        print(f"Cache hit for BIN {bin_number}: {_cache[bin_number]}")
        return _cache[bin_number]

    default_result = (f"{bin_number} - Unknown", "Unknown Bank", "Unknown Country")
    tried_services = set()

    while _active_services and len(tried_services) < len(BIN_LOOKUP_SERVICES):
        service = _get_next_service()
        if service in tried_services:
            continue
        tried_services.add(service)

        print(f"Trying BIN lookup using site: {service['name']} for BIN {bin_number}")
        result = _lookup_single_service(bin_number, service, proxy, timeout_seconds)
        if result:
            print(f"Success from {service['name']}: {result}")
            return result
        else:
            print(f"Failed to get BIN info from {service['name']}")
            _mark_service_failed(service)

    print(f"All BIN lookup services failed for BIN {bin_number}. Returning Unknown.")
    return default_result
