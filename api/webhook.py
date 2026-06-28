import json
import re
import base64
import hashlib
import requests
from http.server import BaseHTTPRequestHandler

# Telegram configuration
BOT_TOKEN = "8654457189:AAHxcYpWTR0y1Exbaq30-Gqve0kHzJYdTcE"
GROUP_ID = "-1002252657532"
OWNER_ID = 1745313119
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Web configuration fallback scanning definitions
BASE_URL = 'https://dl.tata.freefiremobile.com/live/ABHotUpdates/android/'
FALLBACK_BASE_URL = 'https://dl-tata.freefireind.in/live/ABHotUpdates/android/'
DOMAINS = [BASE_URL, FALLBACK_BASE_URL]
EXTRACTOR_API_URL = "https://ff-asset-extractor.vercel.app/api/extract"

# Fast Connection pooling session
http_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=15, pool_maxsize=30)
http_session.mount('http://', adapter)
http_session.mount('https://', adapter)

# Persistent file and metadata extraction caching to avoid database spams
BOT_TEMP_MAPPINGS = {}
FILE_INFO_CACHE = {}
EXTRACTED_FILES_CACHE = {}

def get_previous_version(version_str):
    try:
        parts = list(map(int, version_str.split('.')))
        parts[2] -= 1
        if parts[2] < 0:
            parts[1] -= 1
            parts[2] = 99
            if parts[1] < 0:
                parts[0] -= 1
                parts[1] = 99
        return ".".join(map(str, parts))
    except Exception:
        return None

def get_next_version(version_str, step=1):
    try:
        parts = list(map(int, version_str.split('.')))
        parts[2] += step
        return ".".join(map(str, parts))
    except Exception:
        return None

def encode_suffix(suffix):
    if suffix.endswith('='):
        suffix = suffix[:-1]
    return suffix.replace('+', '~2B').replace('/', '~2F').replace('=', '~3D')

def get_file_info_data_exact(version):
    if version in FILE_INFO_CACHE:
        return FILE_INFO_CACHE[version]
        
    for base in DOMAINS:
        url = f"{base}{version}/fileinfo"
        try:
            res = http_session.get(url, timeout=4)
            if res.status_code == 200:
                val = (res.text, version, base)
                FILE_INFO_CACHE[version] = val
                return val
        except Exception:
            pass
    return None, None, None

def get_multiple_file_info_data(versions):
    results = {}
    versions_to_fetch = [v for v in versions if v not in FILE_INFO_CACHE]
    
    def fetch(v):
        for base in DOMAINS:
            url = f"{base}{v}/fileinfo"
            try:
                res = http_session.get(url, timeout=3)
                if res.status_code == 200:
                    return v, (res.text, v, base)
            except Exception:
                pass
        return v, None

    if versions_to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(versions_to_fetch))) as executor:
            fetched_results = executor.map(fetch, versions_to_fetch)
            for v, val in fetched_results:
                if val:
                    FILE_INFO_CACHE[v] = val
                    results[v] = val
    
    for v in versions:
        if v in FILE_INFO_CACHE:
            results[v] = FILE_INFO_CACHE[v]
    return results

def get_extracted_files(file_id, new_text, active_version, base_url):
    cache_key = f"{active_version}:{file_id}"
    if cache_key in EXTRACTED_FILES_CACHE:
        return EXTRACTED_FILES_CACHE[cache_key]

    suffix = None
    for line in new_text.splitlines():
        if not line.strip(): continue
        parts = line.split(',')
        if len(parts) >= 5 and parts[0].strip() == file_id:
            suffix = parts[4].strip()
            break
    if not suffix:
        return []
    encoded = encode_suffix(suffix)
    download_url = f"{base_url}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
    try:
        encoded_url = base64.urlsafe_b64encode(download_url.encode('utf-8')).decode('utf-8')
        extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}"
        res = http_session.get(extractor_url, timeout=30)
        if res.status_code == 200:
            data = res.json()
            files_list = data.get("files", []) or data.get("assets", [])
            EXTRACTED_FILES_CACHE[cache_key] = files_list
            return files_list
    except Exception as e:
        print(f"Failed to extract bundle {file_id}: {e}")
    return []

def find_previous_itemhotfix_content(active_version):
    parts = active_version.split('.')
    if len(parts) != 3:
        return ""
    major, middle, minor = parts[0], parts[1], int(parts[2])
    prev_versions = [f"{major}.{middle}.{m_val}" for m_val in range(minor - 1, max(0, minor - 9), -1)]
    if not prev_versions:
        return ""
    fetched_maps = get_multiple_file_info_data(prev_versions)
    for prev_ver in prev_versions:
        map_val = fetched_maps.get(prev_ver)
        if not map_val:
            continue
        prev_text, _, base_url = map_val
        prev_itemhotfix_id = None
        for line in prev_text.splitlines():
            if not line.strip(): continue
            p_parts = line.split(',')
            if len(p_parts) >= 5:
                fid = p_parts[0].strip()
                if "config/itemhotfix" in fid.lower():
                    prev_itemhotfix_id = fid
                    break
        if prev_itemhotfix_id:
            prev_files = get_extracted_files(prev_itemhotfix_id, prev_text, prev_ver, base_url)
            for f in prev_files:
                fname = f.get("name", "") or f.get("fileName", "")
                if "itemhotfix.txt" in fname.lower():
                    b64_data = f.get("data", "")
                    if b64_data:
                        return base64.b64decode(b64_data).decode('utf-8', errors='ignore')
            break
    return ""

def extract_new_game_items(active_version, new_text):
    itemhotfix_id = None
    lochotfix_id = None
    for line in new_text.splitlines():
        if not line.strip(): continue
        parts = line.split(',')
        if len(parts) >= 5:
            fid = parts[0].strip()
            if "config/itemhotfix" in fid.lower(): itemhotfix_id = fid
            elif "localization/lochotfix" in fid.lower(): lochotfix_id = fid
    if not itemhotfix_id:
        return []
        
    text_data, active_ver, base_url = get_file_info_data_exact(active_version)
    if not base_url:
        return []

    itemhotfix_files = get_extracted_files(itemhotfix_id, new_text, active_version, base_url)
    itemhotfix_content = ""
    for f in itemhotfix_files:
        fname = f.get("name", "") or f.get("fileName", "")
        if "itemhotfix.txt" in fname.lower():
            b64_data = f.get("data", "")
            if b64_data:
                itemhotfix_content = base64.b64decode(b64_data).decode('utf-8', errors='ignore')
                break
    if not itemhotfix_content:
        return []
    previous_itemhotfix_content = find_previous_itemhotfix_content(active_version)
    previous_item_ids = set()
    if previous_itemhotfix_content:
        p_lines = previous_itemhotfix_content.splitlines()
        if len(p_lines) > 2:
            for pl in p_lines[2:]:
                if not pl.strip(): continue
                p_parts = pl.split(',')
                if len(p_parts) > 0:
                    pid = p_parts[0].strip()
                    if pid.isdigit(): previous_item_ids.add(pid)
    lochotfix_content = ""
    if lochotfix_id:
        lochotfix_files = get_extracted_files(lochotfix_id, new_text, active_version, base_url)
        for f in lochotfix_files:
            fname = f.get("name", "") or f.get("fileName", "")
            if "lochotfix.txt" in fname.lower():
                b64_data = f.get("data", "")
                if b64_data:
                    lochotfix_content = base64.b64decode(b64_data).decode('utf-8', errors='ignore')
                    break
    translations = {}
    if lochotfix_content:
        matches = re.findall(r'<l\s+[^>]*id="([^"]+)"[^>]*\ben="([^"]+)"', lochotfix_content)
        for name_id, en_val in matches:
            translations[name_id] = en_val
    raw_parsed_items = []
    lines = itemhotfix_content.splitlines()
    if len(lines) > 2:
        for line in lines[2:]:
            if not line.strip(): continue
            parts = line.split(',')
            if len(parts) >= 8:
                item_id = parts[0].strip()
                if not item_id or not item_id.isdigit(): continue
                if item_id in previous_item_ids: continue
                text_id = parts[7].strip() if parts[7].strip() else ""
                if not text_id:
                    for p in parts:
                        if p.strip().startswith("T_"):
                            text_id = p.strip()
                            break
                raw_parsed_items.append({
                    "id": item_id,
                    "text_id": text_id,
                    "row_parts": parts
                })
    for item in raw_parsed_items:
        text_id = item["text_id"]
        resolved_name = translations.get(text_id, text_id)
        if not resolved_name: resolved_name = f"Item {item['id']}"
        item["name"] = resolved_name
        item["icon_url"] = f"https://dl.cdn.freefiremobile.com/live/ABHotUpdates/IconCDN/other/{item['id']}.png"
    bundles = []
    non_bundles = []
    for item in raw_parsed_items:
        if "bundle" in item["name"].lower(): bundles.append(item)
        else: non_bundles.append(item)
    grouped_items = []
    used_non_bundles = set()
    for bundle in bundles:
        base_name = re.sub(r'\s+bundle\b', '', bundle['name'], flags=re.IGNORECASE).strip()
        parts = []
        for nb in non_bundles:
            if base_name.lower() in nb['name'].lower() or nb['name'].lower().startswith(base_name.lower()):
                parts.append({
                    "id": nb["id"],
                    "name": nb["name"],
                    "text_id": nb["text_id"],
                    "icon_url": nb["icon_url"]
                })
                used_non_bundles.add(nb['id'])
        grouped_items.append({
            "id": bundle["id"],
            "name": bundle["name"],
            "text_id": bundle["text_id"],
            "icon_url": bundle["icon_url"],
            "parts": parts,
            "is_bundle": True
        })
    for nb in non_bundles:
        if nb['id'] not in used_non_bundles:
            grouped_items.append({
                "id": nb["id"],
                "name": nb["name"],
                "text_id": nb["text_id"],
                "icon_url": nb["icon_url"],
                "parts": [],
                "is_bundle": False
            })
    return grouped_items

def run_miniupdate_extraction_details(version):
    """Processes version changes and formats details for Telegram transmission using HTML parse mode."""
    # Strict exact check for manual search queries (as requested)
    new_text, active_version, base_url = get_file_info_data_exact(version)
    if not new_text:
        return f"❌ Version <b>{version}</b> not found.", None, {}

    prev_versions = []
    current_temp = active_version
    for _ in range(6):
        prev_ver = get_previous_version(current_temp)
        if not prev_ver: break
        prev_versions.append(prev_ver)
        current_temp = prev_ver

    fetched_maps = get_multiple_file_info_data(prev_versions)
    old_texts = [fetched_maps[v][0] for v in prev_versions if v in fetched_maps]

    old_files_map = {}
    for old_text in reversed(old_texts):
        for line in old_text.splitlines():
            if not line.strip(): continue
            parts = line.split(',')
            if len(parts) >= 3:
                file_id = parts[0].strip()
                try: sz = int(parts[2])
                except Exception: sz = 0
                old_files_map[file_id] = sz

    file_entries = []
    total_size = 0 # Sum of only updated (Modified + New) files as clarified by the user
    new_files_count = 0
    changed_files_count = 0

    for line in new_text.splitlines():
        if not line.strip(): continue
        parts = line.split(',')
        if len(parts) < 5: continue
        file_id, size_str = parts[0].strip(), parts[2].strip()
        try: size = int(size_str)
        except Exception: size = 0
        
        if file_id in old_files_map:
            if old_files_map[file_id] != size: 
                status = "Modified"
                changed_files_count += 1
                total_size += size
                file_entries.append((file_id, status, size))
        else: 
            status = "New"
            new_files_count += 1
            total_size += size
            file_entries.append((file_id, status, size))

    size_mb = round(total_size / (1024 * 1024), 2)
    
    msg = [
        "<b>╭─📊 MINIUPDATE ANALYSIS ────</b>",
        "│",
        f"├ 🟢 Version Extracted: <b>{version}</b>",
        f"├ MiniUpdate Size: <b>{size_mb} MB</b>",
        f"├ Changed Files Count: <b>{changed_files_count}</b>",
        f"├ New Files Count: <b>{new_files_count}</b>",
        "│",
        "├ 📂 DISCOVERED FILES:"
    ]
    
    max_visible_files = 15
    # Encapsulate listings in native Telegram blockquotes for premium visualization
    msg.append("<blockquote>")
    for idx, (fid, stat, sz) in enumerate(file_entries[:max_visible_files], 1):
        sz_kb = round(sz / 1024, 1)
        if sz_kb >= 1024:
            sz_display = f"{round(sz_kb / 1024, 2)} MB"
        else:
            sz_display = f"{sz_kb} KB"
            
        clean_fid = fid.split('/')[-1]
        clean_fid_escaped = clean_fid.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        # Removed vertical guides inside blockquote block to prevent double border lines
        msg.append(f"▫️ {idx:02d}. <b>{clean_fid_escaped}</b>")
        msg.append(f"      <i>({stat}, {sz_display})</i>")
        
    if len(file_entries) > max_visible_files:
        msg.append(f"... and {len(file_entries) - max_visible_files} more files.")
    msg.append("</blockquote>")

    new_items = []
    try:
        new_items = extract_new_game_items(version, new_text)
    except Exception: pass

    if new_items:
        msg.append("│")
        msg.append("├ ✨ NEW GAME ITEMS:")
        msg.append("<blockquote>")
        for idx, item in enumerate(new_items[:12], 1):
            clean_name = item['name'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            # Emphasize linked name and italicized mono ID layout (removed brackets)
            msg.append(f"▫️ <a href=\"{item['icon_url']}\"><b>{clean_name}</b></a>")
            msg.append(f"      ID: <code><i>{item['id']}</i></code>")
        if len(new_items) > 12:
            msg.append(f"... and {len(new_items) - 12} more items.")
        msg.append("</blockquote>")

    msg.append("╰──────────────────────")
    
    # Store dynamic mapping in local memory store to prevent database repinning spam
    file_keyboard_mappings = {}
    for file_id, stat, sz in file_entries[:5]:
        mapping_id = hashlib.md5(f"{version}:{file_id}".encode('utf-8')).hexdigest()[:8]
        suffix = None
        for line in new_text.splitlines():
            if not line.strip(): continue
            parts = line.split(',')
            if len(parts) >= 5 and parts[0].strip() == file_id:
                suffix = parts[4].strip()
                break
        if suffix:
            encoded = encode_suffix(suffix)
            download_url = f"{base_url}{version}/gameassetbundles/{file_id}.{encoded}~3D"
            BOT_TEMP_MAPPINGS[mapping_id] = {
                "url": download_url,
                "filename": file_id.split('/')[-1],
                "version": version
            }
            file_keyboard_mappings[file_id] = mapping_id
            
    return "\n".join(msg), file_entries, file_keyboard_mappings

def parse_bundle_subfiles(file_path_or_url, is_local_upload=False, upload_bytes=None, filename="bundle.unity3d"):
    try:
        if is_local_upload and upload_bytes:
            files = {'asset_bundle': (filename, upload_bytes, 'application/octet-stream')}
            res = http_session.post(EXTRACTOR_API_URL, files=files, timeout=45)
        else:
            encoded_url = base64.urlsafe_b64encode(file_path_or_url.encode('utf-8')).decode('utf-8')
            res = http_session.get(f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}", timeout=45)
        
        if res.status_code == 200:
            data = res.json()
            return data.get("files", []) or data.get("assets", [])
    except Exception as e:
        print(f"Parse bundle processing failed: {e}")
    return []

def send_extracted_document_to_chat(chat_id, file_bytes, filename):
    url = f"{TELEGRAM_API}/sendDocument"
    files = {"document": (filename, file_bytes, 'application/octet-stream')}
    payload = {"chat_id": chat_id}
    try:
        http_session.post(url, data=payload, files=files, timeout=45)
    except Exception as e:
         print(f"Error sending document: {e}")

def clear_reply_markup(chat_id, message_id):
    url = f"{TELEGRAM_API}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": []}
    }
    try:
        http_session.post(url, json=payload, timeout=5)
    except Exception: pass

def delete_message(chat_id, message_id):
    url = f"{TELEGRAM_API}/deleteMessage"
    try:
        http_session.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
    except Exception: pass

def send_message(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        res = http_session.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        return res.json()
    except Exception as e:
        print(f"Telegram send error: {e}")
    return None

def send_message_with_keyboard(chat_id, text, reply_markup, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        http_session.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
         print(f"Telegram menu send error: {e}")

def read_db_from_chat(chat_id):
    try:
        res = http_session.post(f"{TELEGRAM_API}/getChat", json={"chat_id": chat_id}, timeout=10).json()
        if res.get("ok") and "pinned_message" in res["result"]:
            text = res["result"]["pinned_message"].get("text", "")
            if "📦 DATABASE STORAGE" in text:
                lines = text.split("\n")
                for line in lines:
                    line_strip = line.strip()
                    if line_strip.startswith("{") and line_strip.endswith("}"):
                        return json.loads(line_strip)
    except Exception as e:
        print(f"DB read warning for {chat_id}: {e}")
    return None

def write_db_to_chat(chat_id, db_dict):
    db_text = f"📦 DATABASE STORAGE (DO NOT DELETE/UNPIN)\n{json.dumps(db_dict)}"
    try:
        res = http_session.post(f"{TELEGRAM_API}/getChat", json={"chat_id": chat_id}, timeout=10).json()
        if res.get("ok") and "pinned_message" in res["result"]:
            msg = res["result"]["pinned_message"]
            if "📦 DATABASE STORAGE" in msg.get("text", ""):
                http_session.post(f"{TELEGRAM_API}/editMessageText", json={
                    "chat_id": chat_id,
                    "message_id": msg["message_id"],
                    "text": db_text
                }, timeout=10)
                return
        new_msg = send_message(chat_id, db_text)
        if new_msg and new_msg.get("ok"):
            msg_id = new_msg["result"]["message_id"]
            http_session.post(f"{TELEGRAM_API}/pinChatMessage", json={
                "chat_id": chat_id,
                "message_id": msg_id,
                "disable_notification": True
            }, timeout=10)
    except Exception as e:
         print(f"DB write warning: {e}")

def get_db():
    """Reads persistent DB only from Owner personal chat to avoid group pins."""
    owner_db = read_db_from_chat(OWNER_ID)
    if not owner_db:
        return {
            "allowed_users": [OWNER_ID], 
            "pending_requests": [],
            "rejection_sticker": None,
            "owner_state": None
        }
    
    # Clean up legacy link checker values to keep space low
    needs_write = False
    legacy_keys = ["links", "chats", "temp_mappings", "owner_state"]
    for k in legacy_keys:
        if k in owner_db:
            owner_db.pop(k, None)
            needs_write = True
            
    if "allowed_users" not in owner_db: 
        owner_db["allowed_users"] = [OWNER_ID]
        needs_write = True
    if "pending_requests" not in owner_db: 
        owner_db["pending_requests"] = []
        needs_write = True
        
    if needs_write:
        write_db_to_chat(OWNER_ID, owner_db)
        
    return owner_db

def save_db(db):
    write_db_to_chat(OWNER_ID, db)

def is_user_allowed(user_id):
    if user_id == OWNER_ID: return True
    db = get_db()
    for u in db.get("allowed_users", []):
        if isinstance(u, int) and u == user_id: return True
        elif isinstance(u, dict) and u.get("user_id") == user_id: return True
    return False

def add_allowed_user(user_id, first_name="Unknown", username="None", chat_title="Private Chat"):
    db = get_db()
    allowed = db.get("allowed_users", [])
    exists = False
    for i, u in enumerate(allowed):
        if isinstance(u, int) and u == user_id:
            allowed[i] = {"user_id": user_id, "first_name": first_name, "username": username, "chat_title": chat_title}
            exists = True
            break
        elif isinstance(u, dict) and u.get("user_id") == user_id:
            u["first_name"] = first_name
            u["username"] = username
            u["chat_title"] = chat_title
            exists = True
            break
    if not exists:
        allowed.append({"user_id": user_id, "first_name": first_name, "username": username, "chat_title": chat_title})
    db["allowed_users"] = allowed
    save_db(db)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Respond 200 OK immediately to Telegram to prevent duplicate retry spams
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()

        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)

        try:
            update = json.loads(post_data.decode('utf-8'))
            
            # Callback Queries
            if "callback_query" in update:
                cb = update["callback_query"]
                cb_id = cb["id"]
                chat_id = cb["message"]["chat"]["id"]
                msg_id = cb["message"]["message_id"]
                user_id = cb["from"]["id"]
                data = cb.get("data", "")

                # Secure action bypass for allowed group chat
                if not is_action_permitted(chat_id, user_id):
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Access denied."}, timeout=5)
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return

                # Close / Delete Message Button
                if data == "cb_close":
                    delete_message(chat_id, msg_id)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return

                elif data.startswith("req_access_"):
                    req_uid = int(data.split("_")[-1])
                    cb_user = cb["from"]
                    first_name = cb_user.get("first_name", "Unknown")
                    username = cb_user.get("username", "None")
                    
                    # Manual request authorization prompting Owner's Personal Chat
                    http_session.post(f"{TELEGRAM_API}/sendMessage", json={
                        "chat_id": OWNER_ID,
                        "text": (
                            "╭─👤 ACCESS REQUEST DETECTED\n"
                            f"├ User: {first_name}\n"
                            f"├ ID: {req_uid}\n"
                            f"├ Username: @{username}\n"
                            "╰──────────────────────"
                        ),
                        "reply_markup": {
                            "inline_keyboard": [[
                                {"text": "✅ Approve", "callback_data": f"auth_approve_{req_uid}"},
                                {"text": "❌ Reject", "callback_data": f"auth_reject_{req_uid}"}
                            ]]
                        }
                    }, timeout=10)
                    
                    # Edit requester's button feedback state
                    http_session.post(f"{TELEGRAM_API}/editMessageText", json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": "Please Give me Access 🙏\n\n*(Access request sent to the owner, waiting for approval...)*",
                        "parse_mode": "Markdown"
                    }, timeout=10)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Request submitted!"}, timeout=5)
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return

                elif data.startswith("auth_approve_"):
                    if user_id == OWNER_ID:
                        target_id = int(data.split("_")[-1])
                        add_allowed_user(target_id)
                        http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": msg_id, "text": f"✅ User {target_id} APPROVED."}, timeout=10)
                        http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "User approved!"}, timeout=5)
                        send_message(target_id, "You have been granted access to the bot! 🎉\nUse /start to begin.")
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return

                elif data.startswith("auth_reject_"):
                    if user_id == OWNER_ID:
                        target_id = int(data.split("_")[-1])
                        http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": msg_id, "text": f"❌ User {target_id} REJECTED."}, timeout=10)
                        http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "User rejected."}, timeout=5)
                        send_message(target_id, "Your request was declined by the administrator.")
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return

                # Extraction actions
                elif data.startswith("check_ver_") or data.startswith("back_ver_"):
                    ver = data.replace("check_ver_", "").replace("back_ver_", "")
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                    
                    # Show dynamic in-progress tracking state in-place
                    url = f"{TELEGRAM_API}/editMessageText"
                    http_session.post(url, json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": f"⏳ Checking miniupdate version <b>{ver}</b>...",
                        "parse_mode": "HTML"
                    }, timeout=10)
                    
                    try:
                        detail_msg, file_entries, file_keyboard_mappings = run_miniupdate_extraction_details(ver)
                        keyboard = []
                        if file_entries:
                            # Modified version summary layout contains only the Extract All as ZIP button
                            keyboard.append([{"text": "📦 Extract All as ZIP", "callback_data": f"zip_ver_{ver}"}])
                        
                        url = f"{TELEGRAM_API}/editMessageText"
                        http_session.post(url, json={
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "text": detail_msg,
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": keyboard}
                        }, timeout=10)
                    except Exception:
                        url = f"{TELEGRAM_API}/editMessageText"
                        http_session.post(url, json={
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "text": f"❌ Version <b>{ver}</b> not found.",
                            "parse_mode": "HTML"
                        }, timeout=10)

                elif data.startswith("zip_ver_"):
                    ver = data.replace("zip_ver_", "")
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Preparing ZIP archive..."}, timeout=5)
                    clear_reply_markup(chat_id, msg_id)
                    send_message(chat_id, "⚙️ Generating patch extraction package. Please stand by...")
                    
                    # Call direct python bulk_zip_logic directly
                    try:
                        zip_io = io.BytesIO()
                        new_text, active_version, base_url = get_file_info_data_exact(ver)
                        if new_text:
                            prev_versions = []
                            current_temp = active_version
                            for _ in range(6):
                                prev_ver = get_previous_version(current_temp)
                                if not prev_ver: break
                                prev_versions.append(prev_ver)
                                current_temp = prev_ver
                            fetched_maps = get_multiple_file_info_data(prev_versions)
                            old_texts = [fetched_maps[v][0] for v in prev_versions if v in fetched_maps]
                            old_files_map = {}
                            for old_text in reversed(old_texts):
                                for line in old_text.splitlines():
                                    if not line.strip(): continue
                                    parts = line.split(',')
                                    if len(parts) >= 3:
                                        file_id = parts[0].strip()
                                        try: old_size = int(parts[2])
                                        except Exception: old_size = 0
                                        old_files_map[file_id] = old_size
                            targets = []
                            for line in new_text.splitlines():
                                if not line.strip(): continue
                                parts = line.split(',')
                                if len(parts) < 5: continue
                                file_id, size_str, suffix = parts[0].strip(), parts[2].strip(), parts[4].strip()
                                if not file_id or not suffix: continue
                                try: size = int(size_str)
                                except Exception: size = 0
                                if file_id in old_files_map:
                                    if old_files_map[file_id] != size: pass
                                    else: continue
                                else: pass
                                encoded = encode_suffix(suffix)
                                download_url = f"{base_url}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
                                targets.append((file_id, download_url))
                            
                            files_added = 0
                            def concurrent_fetch_and_extract(target):
                                f_id, d_url = target
                                try:
                                    encoded_url = base64.urlsafe_b64encode(d_url.encode('utf-8')).decode('utf-8')
                                    extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}"
                                    res = http_session.get(extractor_url, timeout=25)
                                    if res.status_code == 200:
                                        data = res.json()
                                        return f_id, data.get("files", []) or data.get("assets", [])
                                except Exception: pass
                                return f_id, []

                            with zipfile.ZipFile(zip_io, "w", zipfile.ZIP_DEFLATED) as zf:
                                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                                    futures_map = executor.map(concurrent_fetch_and_extract, targets)
                                    for file_id, extracted_files in futures_map:
                                        for ext_file in extracted_files:
                                            fname = ext_file.get("name")
                                            zpath = ext_file.get("path") or fname
                                            b64_content = ext_file.get("data", "")
                                            if b64_content and fname:
                                                try:
                                                    file_bytes = base64.b64decode(b64_content)
                                                    zf.writestr(f"{file_id}_extracted/{zpath}", file_bytes)
                                                    files_added += 1
                                                except Exception: pass
                            
                            if files_added > 0:
                                zip_io.seek(0)
                                send_extracted_document_to_chat(chat_id, zip_io.getvalue(), f"FF_MiniUpdate_{ver}_Extracted.zip")
                            else:
                                send_message(chat_id, "❌ Archive generation returned empty subfile lists.")
                        else:
                            send_message(chat_id, "❌ Target version logs missing from server.")
                    except Exception as e:
                        send_message(chat_id, f"❌ Failed building zip document: {str(e)}")

            elif data.startswith("ext_all_"):
                mapping_id = data.replace("ext_all_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Bundling archives..."}, timeout=5)
                clear_reply_markup(chat_id, msg_id)
                
                meta = BOT_TEMP_MAPPINGS.get(mapping_id)
                if meta:
                    target_url = meta.get("url")
                    sub_files = parse_bundle_subfiles(target_url)
                    if sub_files:
                        zip_io = io.BytesIO()
                        with zipfile.ZipFile(zip_io, "w", zipfile.ZIP_DEFLATED) as zf:
                            for sf in sub_files:
                                fname = sf.get("name") or sf.get("fileName")
                                zpath = sf.get("path") or fname
                                b64 = sf.get("data", "")
                                if b64 and fname:
                                    zf.writestr(zpath, base64.b64decode(b64))
                        zip_io.seek(0)
                        send_extracted_document_to_chat(chat_id, zip_io.getvalue(), f"extracted_{mapping_id}.zip")
                    else:
                        send_message(chat_id, "❌ Unable to extract asset bundle.")
                else:
                    send_message(chat_id, "❌ Session expired.")

            elif data.startswith("ext_spec_"):
                mapping_id = data.replace("ext_spec_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Retrieving specific files list..."}, timeout=5)
                clear_reply_markup(chat_id, msg_id)
                
                meta = BOT_TEMP_MAPPINGS.get(mapping_id)
                if meta:
                    target_url = meta.get("url")
                    sub_files = parse_bundle_subfiles(target_url)
                    if sub_files:
                        keyboard = []
                        for idx, sf in enumerate(sub_files[:15], 1):
                            fname = sf.get("name") or sf.get("fileName")
                            keyboard.append([{"text": f"📥 {idx}. {fname}", "callback_data": f"dlf_{mapping_id}_{idx-1}"}])
                        keyboard.append([
                            {"text": "⬅️ Back", "callback_data": f"back_ext_{mapping_id}"}
                        ])
                        send_message_with_keyboard(chat_id, "👇 Click on any file to download it directly:", {"inline_keyboard": keyboard})
                    else:
                        send_message(chat_id, "❌ Extraction returned empty subfile lists.")
                else:
                    send_message(chat_id, "❌ Session expired.")

            elif data.startswith("dlf_"):
                parts = data.split("_")
                mapping_id = parts[1]
                idx = int(parts[2])
                
                meta = BOT_TEMP_MAPPINGS.get(mapping_id)
                if meta:
                    target_url = meta.get("url")
                    sub_files = parse_bundle_subfiles(target_url)
                    if 0 <= idx < len(sub_files):
                        sf = sub_files[idx]
                        fname = sf.get("name") or sf.get("fileName")
                        
                        # Edit current inline menu to show "Extracting" status
                        url = f"{TELEGRAM_API}/editMessageText"
                        http_session.post(url, json={
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "text": f"⏳ Extracting asset file: <b>{fname}</b>...",
                            "parse_mode": "HTML"
                        }, timeout=10)
                        
                        b64 = sf.get("data", "")
                        if b64:
                            file_bytes = base64.b64decode(b64)
                            # Deliver extracted raw document to Telegram
                            send_extracted_document_to_chat(chat_id, file_bytes, fname)
                            
                            # Auto-clean active markup buttons (as requested)
                            keyboard = [[{"text": "⬅️ Back to Files List", "callback_data": f"ext_spec_{mapping_id}"}]]
                            url = f"{TELEGRAM_API}/editMessageText"
                            http_session.post(url, json={
                                "chat_id": chat_id,
                                "message_id": msg_id,
                                "text": f"✅ Successfully extracted and sent: <b>{fname}</b>",
                                "parse_mode": "HTML",
                                "reply_markup": {"inline_keyboard": keyboard}
                            }, timeout=10)
                        else:
                            send_message(chat_id, "❌ Target subfile does not contain data.")
                    else:
                        http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Out of bounds index."}, timeout=5)
                else:
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Session expired."}, timeout=5)

            elif data.startswith("back_ext_"):
                mapping_id = data.replace("back_ext_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Returning..."}, timeout=5)
                
                meta = BOT_TEMP_MAPPINGS.get(mapping_id)
                if meta:
                    file_name = meta.get("filename", "bundle.unity3d")
                    target_url = meta.get("url")
                    ver = meta.get("version", "1.126.6")
                    sub_files = parse_bundle_subfiles(target_url)
                    
                    sub_names = "\n".join([f"│ ▫️ {idx:02d}. {sf.get('name') or sf.get('fileName')}" for idx, sf in enumerate(sub_files[:15], 1)])
                    if len(sub_files) > 15:
                        sub_names += f"\n│ ▫️ ... and {len(sub_files) - 15} more files."

                    keyboard = [[
                        {"text": "📦 Download All as ZIP", "callback_data": f"ext_all_{mapping_id}"},
                        {"text": "🔍 Download Specific File", "callback_data": f"ext_spec_{mapping_id}"}
                    ], [
                        {"text": "⬅️ Back to Version Details", "callback_data": f"back_ver_{ver}"}
                    ]]
                    
                    url = f"{TELEGRAM_API}/editMessageText"
                    http_session.post(url, json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": (
                            "<b>╭─📦 BUNDLE EXTRACTION ────</b>\n"
                            f"├ Source Bundle: <b>{file_name}</b>\n"
                            "│\n"
                            "├ Extracted Components:\n"
                            f"{sub_names}\n"
                            "╰─────────────────────────"
                        ),
                        "reply_markup": {"inline_keyboard": keyboard}
                    }, timeout=10)
                else:
                    send_message(chat_id, "❌ Session expired.")

            self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
            return

        # Chat Text Parser
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            user_info = msg.get("from", {})
            user_id = user_info.get("id")
            first_name = user_info.get("first_name", "Unknown")
            username = user_info.get("username", "None")
            raw_text = msg.get("text", "").strip()

            # Live fileinfo URL hook matching (strict format checking for the link tracker bot's output message)
            if raw_text and "LIVE LINK DETECTED" in raw_text and ("dl.tata.freefiremobile.com" in raw_text or "dl-tata.freefireind.in" in raw_text):
                version_match = re.search(r'/android/(\d+\.\d+\.\d+)/', raw_text)
                if version_match:
                    ver = version_match.group(1)
                    keyboard = [[{"text": f"🔍 Check Miniupdate {ver}", "callback_data": f"check_ver_{ver}"}]]
                    send_message_with_keyboard(chat_id, (
                        "╭──────────────────────\n"
                        "├ New MINIUPDATE\n"
                        f"├ Version: <b>{ver}</b>\n"
                        "╰──────────────────────"
                    ), {"inline_keyboard": keyboard}, parse_mode="HTML")
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return

            # Access controls for private interaction commands
            if raw_text.startswith("/"):
                if not is_action_permitted(chat_id, user_id):
                    if raw_text.startswith("/start") or raw_text.startswith("/check") or raw_text.startswith("/extract"):
                        send_access_request_menu(chat_id, user_id)
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return

            if raw_text.startswith("/start"):
                send_message(chat_id, (
                    "╭─⚡ MINIUPDATE CHECKER BOT ────\n│\n"
                    "├ Welcome! You are an approved user. 🎉\n│\n"
                    "├ ⚙️ COMMANDS:\n"
                    "├ ▫️ /check [version]\n"
                    "├ ▫️ /extract [replying to any asset file]\n"
                    "╰──────────────────────"
                ))

            elif raw_text.startswith("/check"):
                parts = [p.strip() for p in raw_text.split(" ") if p.strip()]
                if len(parts) < 2:
                    send_message(chat_id, "╭─❌ USAGE\n╰─/check [version]\n\nExample: /check 1.126.6")
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return
                ver = parts[1].strip()
                
                # Send live in-progress state first to feel active and responsive
                sent_msg = send_message(chat_id, f"⏳ Checking miniupdate version <b>{ver}</b>...", parse_mode="HTML")
                if not sent_msg or not sent_msg.get("ok"):
                    self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                    return
                
                sent_msg_id = sent_msg["result"]["message_id"]
                
                try:
                    detail_msg, file_entries, file_keyboard_mappings = run_miniupdate_extraction_details(ver)
                    keyboard = []
                    if file_entries:
                        # Modified version summary layout contains only the Extract All as ZIP button
                        keyboard.append([{"text": "📦 Extract All as ZIP", "callback_data": f"zip_ver_{ver}"}])
                    
                    url = f"{TELEGRAM_API}/editMessageText"
                    payload = {
                        "chat_id": chat_id,
                        "message_id": sent_msg_id,
                        "text": detail_msg,
                        "parse_mode": "HTML"
                    }
                    if keyboard:
                        payload["reply_markup"] = {"inline_keyboard": keyboard}
                        
                    http_session.post(url, json=payload, timeout=10)
                except Exception:
                    url = f"{TELEGRAM_API}/editMessageText"
                    http_session.post(url, json={
                        "chat_id": chat_id,
                        "message_id": sent_msg_id,
                        "text": f"❌ Version <b>{ver}</b> not found.",
                        "parse_mode": "HTML"
                    }, timeout=10)

            elif raw_text.startswith("/extract"):
                if "reply_to_message" in msg:
                    reply = msg["reply_to_message"]
                    media = reply.get("document") or reply.get("video") or reply.get("audio") or reply.get("voice")
                    if media:
                        file_id = media["file_id"]
                        file_name = media.get("file_name", "bundle.unity3d")
                        send_message(chat_id, f"📥 Downloading '{file_name}' from Telegram...")
                        
                        path_res = http_session.get(f"{TELEGRAM_API}/getFile?file_id={file_id}", timeout=10).json()
                        if path_res.get("ok"):
                            file_path = path_res["result"]["file_path"]
                            dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                            
                            send_message(chat_id, "⚙️ Processing bundle extraction pipeline...")
                            try:
                                res = http_session.get(dl_url, timeout=45)
                                if res.status_code == 200:
                                    sub_files = parse_bundle_subfiles(None, is_local_upload=True, upload_bytes=res.content, filename=file_name)
                                    if sub_files:
                                        mapping_id = str(uuid.uuid4())[:8]
                                        
                                        # Save dynamic mapping in local memory store to prevent database repinning spam
                                        BOT_TEMP_MAPPINGS[mapping_id] = {
                                            "url": dl_url,
                                            "filename": file_name
                                        }
                                        
                                        sub_names = "\n".join([f"│ ▫️ {idx}. {sf.get('name') or sf.get('fileName')}" for idx, sf in enumerate(sub_files[:15], 1)])
                                        if len(sub_files) > 15:
                                            sub_names += f"\n│ ▫️ ... and {len(sub_files) - 15} more files."
                                            
                                        keyboard = [[
                                            {"text": "📦 Extract All as ZIP", "callback_data": f"ext_all_{mapping_id}"},
                                            {"text": "🔍 Extract Specific File", "callback_data": f"ext_spec_{mapping_id}"}
                                        ]]
                                        send_message_with_keyboard(chat_id, (
                                            "<b>╭─📦 BUNDLE EXTRACTION ────</b>\n"
                                            f"├ Source Bundle: <b>{file_name}</b>\n"
                                            "│\n"
                                            "├ Extracted Components:\n"
                                            f"{sub_names}\n"
                                            "╰─────────────────────────"
                                        ), {"inline_keyboard": keyboard}, parse_mode="HTML")
                                    else:
                                        send_message(chat_id, "❌ Unable to parse subfiles inside the asset bundle.")
                                else:
                                    send_message(chat_id, "❌ Failed to retrieve document bytes from Telegram.")
                            except Exception as e:
                                send_message(chat_id, f"❌ Extraction error: {str(e)}")
                        else:
                            send_message(chat_id, "❌ File path generation rejected by Telegram.")
                    else:
                        send_message(chat_id, "❌ Replying message does not contain a file document attachment.")
                else:
                    send_message(chat_id, "╭─❌ USAGE\n╰─Reply to any asset file document in the group with '/extract'.")

            self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
            return

    except Exception as e:
        print(f"Error handling Telegram message: {e}")
        try:
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
        except Exception: pass