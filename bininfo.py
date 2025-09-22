import requests
import re

_cache = {}

def bin_lookup(card_number: str, proxy=None, timeout_seconds=10):
    bin_number = card_number[:6]
    if bin_number in _cache:
        print(f"Cache hit for BIN {bin_number}: {_cache[bin_number]}")
        return _cache[bin_number]

    service = {
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
    }

    try:
        headers = service.get("headers", {}).copy()
        params = {}
        url = service["url"]
        auth = service.get("auth")

        if service.get("post", False):
            params = service.get("auth", {}).copy()
            params["bin"] = bin_number
            resp = requests.post(url, headers=headers, data=params, proxies=proxy, timeout=timeout_seconds)
        else:
            if not url.endswith("/"):
                url += "/"
            url += bin_number
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

    default_result = (f"{bin_number} - Unknown", "Unknown Bank", "Unknown Country")
    print(f"BIN lookup failed for {bin_number}. Returning unknown result.")
    return default_result
