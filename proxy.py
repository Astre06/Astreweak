import os
import threading

PROXY_FILE = "notepad.txt"
_proxy_lock = threading.Lock()
_proxy_list = []
_proxy_index = 0

def load_proxies():
    global _proxy_list
    if not os.path.exists(PROXY_FILE):
        _proxy_list = []
        return _proxy_list
    with _proxy_lock:
        with open(PROXY_FILE, "r") as f:
            lines = f.readlines()
        proxies = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 4:
                ip, port, user, pwd = parts
                proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
                proxies.append({
                    "http": proxy_url,
                    "https": proxy_url,
                })
        _proxy_list = proxies
    return _proxy_list

def save_proxies(proxy_lines):
    # proxy_lines: list of strings like "IP:PORT:USER:PASS"
    with _proxy_lock:
        with open(PROXY_FILE, "w") as f:
            for line in proxy_lines:
                f.write(line.strip() + "\n")
    load_proxies()  # refresh proxy list after saving

def add_proxies(proxy_lines):
    # Add proxies (strings) to existing file without duplication
    current = set()
    with _proxy_lock:
        if os.path.exists(PROXY_FILE):
            with open(PROXY_FILE, "r") as f:
                current = set(line.strip() for line in f.readlines() if line.strip())
        with open(PROXY_FILE, "a") as f:
            for line in proxy_lines:
                line = line.strip()
                if line and line not in current:
                    f.write(line + "\n")
                    current.add(line)
    load_proxies()

def delete_proxies():
    with _proxy_lock:
        if os.path.exists(PROXY_FILE):
            os.remove(PROXY_FILE)
        global _proxy_list, _proxy_index
        _proxy_list = []
        _proxy_index = 0

def get_next_proxy():
    global _proxy_index
    with _proxy_lock:
        if not _proxy_list:
            return None
        proxy = _proxy_list[_proxy_index]
        _proxy_index = (_proxy_index + 1) % len(_proxy_list)
        return proxy