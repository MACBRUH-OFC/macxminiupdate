import os
import re
import uuid
import mimetypes
import base64
import json
import zlib
import io
import zipfile
import requests
import hashlib
import concurrent.futures
from flask import Flask, jsonify, request, Response

# Web configuration
BASE_URL = 'https://dl.tata.freefiremobile.com/live/ABHotUpdates/android/'
FALLBACK_BASE_URL = 'https://dl-tata.freefireind.in/live/ABHotUpdates/android/'
DOMAINS = [BASE_URL, FALLBACK_BASE_URL]

VERSION_API = 'https://ff-version.vercel.app/update'
EXTRACTOR_API_URL = "https://ff-asset-extractor.vercel.app/api/extract"

# Telegram configuration
BOT_TOKEN = "8654457189:AAHIdGT0cWFn41Z2mw2ClymmarSWW_DjZls"
GROUP_ID = "-1002252657532"
OWNER_ID = 1745313119
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

# Connection pooling with high limits for faster processing
http_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=25, pool_maxsize=50)
http_session.mount('http://', adapter)
http_session.mount('https://', adapter)

# Fast Caching dictionaries to prevent hanging
FILE_INFO_CACHE = {}
EXTRACTED_FILES_CACHE = {}
MEMORY_ASSET_CACHE = {}

# Stateless short-lived in-memory mappings to stop pinned database spam
BOT_TEMP_MAPPINGS = {}
PURGED_LEGACY_DB = False

# List of allowed user IDs permitted in private chats (Owner is natively allowed)
ALLOWED_USERS = [OWNER_ID]

def get_mime_type(filename):
    mime, _ = mimetypes.guess_type(filename)
    if mime:
        return mime
    filename_lower = filename.lower()
    if filename_lower.endswith('.json'): return 'application/json'
    elif filename_lower.endswith('.manifest') or filename_lower.endswith('.txt') or filename_lower.endswith('.atlas'): return 'text/plain'
    elif filename_lower.endswith('.ogg'): return 'audio/ogg'
    elif filename_lower.endswith('.mp3'): return 'audio/mpeg'
    elif filename_lower.endswith('.mp4'): return 'video/mp4'
    elif filename_lower.endswith('.png'): return 'image/png'
    elif filename_lower.endswith('.jpg') or filename_lower.endswith('.jpeg'): return 'image/jpeg'
    elif filename_lower.endswith('.gif'): return 'image/gif'
    elif filename_lower.endswith('.webp'): return 'image/webp'
    elif filename_lower.endswith('.obj'): return 'text/plain'
    return 'application/octet-stream'

def encode_token(remote_url, filename, content_type, content_bytes=None):
    cache_key = hashlib.sha256(remote_url.encode('utf-8')).hexdigest()
    data = {
        "url": remote_url,
        "filename": filename,
        "content_type": content_type,
        "cache_key": cache_key
    }
    if content_bytes and len(content_bytes) < 2048:
        data["content_b64"] = base64.b64encode(content_bytes).decode('utf-8')
    json_bytes = json.dumps(data).encode('utf-8')
    compressed = zlib.compress(json_bytes)
    return base64.urlsafe_b64encode(compressed).decode('utf-8')

def decode_token(token):
    try:
        padding_needed = len(token) % 4
        if padding_needed:
            token += '=' * (4 - padding_needed)
        compressed = base64.urlsafe_b64decode(token.encode('utf-8'))
        json_bytes = zlib.decompress(compressed)
        return json.loads(json_bytes.decode('utf-8'))
    except Exception:
        return None

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
    """Fetches exact version without fallback."""
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

def get_file_info_data(version):
    """Primary fetch routine."""
    return get_file_info_data_exact(version)

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

def find_latest_active_version(requested_version):
    """Scans the requested version as well as the next 3 versions in parallel on the web, with downward tracing fallback."""
    candidates = [requested_version]
    for step in [1, 2, 3]:
        nxt = get_next_version(requested_version, step)
        if nxt:
            candidates.append(nxt)
            
    fetched = get_multiple_file_info_data(candidates)
    
    for cand in reversed(candidates):
        if cand in fetched:
            return fetched[cand]
            
    current_temp = requested_version
    for _ in range(5):
        prev = get_previous_version(current_temp)
        if not prev:
            break
        exact_fetched = get_multiple_file_info_data([prev])
        if prev in exact_fetched:
            return exact_fetched[prev]
        current_temp = prev
        
    return None, None, None

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
        
    text_data, active_ver, base_url = get_file_info_data(active_version)
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

def latest_version_logic():
    try:
        res = http_session.get(VERSION_API, timeout=5)
        data = res.json()
        if data.get("success") and "remote_version" in data:
            base_ver = data["remote_version"]  # e.g., 1.111.6
            
            # Concurrently scan base version + next 3 versions in parallel (Auto Radar only)
            candidates = [base_ver]
            for step in [1, 2, 3]:
                nxt = get_next_version(base_ver, step)
                if nxt:
                    candidates.append(nxt)
            
            fetched_maps = get_multiple_file_info_data(candidates)
            
            active_ver = base_ver
            for cand in reversed(candidates):
                if cand in fetched_maps:
                    active_ver = cand
                    break
                    
            target_ver = get_next_version(active_ver, 1)
            
            return jsonify({
                "success": True, 
                "current_version": active_ver, 
                "target_version": target_ver
            })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": False, "message": "Failed response lookup."}), 500

def check_update_logic():
    input_val = request.args.get('version', '').strip()
    if not input_val:
        return jsonify({"success": False, "message": "Missing criteria parameters"}), 400
    if "http" in input_val:
        match = re.search(r'/android/(\d+\.\d+\.\d+)/', input_val)
        version = match.group(1) if match else None
    else:
        version = input_val
    if not version or not re.match(r'^\d+\.\d+\.\d+$', version):
        return jsonify({"success": False, "message": "Incorrect version format entered"}), 400

    # Strict exact check for manual search queries (as requested)
    new_text, active_version, base_url = get_file_info_data_exact(version)
    if not new_text:
        return jsonify({"success": False, "message": "Target version logs missing from server"}), 404
        
    prev_versions = []
    current_temp = active_version
    for _ in range(6):
        prev_ver = get_previous_version(current_temp)
        if not prev_ver: break
        prev_versions.append(prev_ver)
        current_temp = prev_ver
        
    fetched_maps_prev = get_multiple_file_info_data(prev_versions)
    old_texts = [fetched_maps_prev[v][0] for v in prev_versions if v in fetched_maps_prev]
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
    file_infos = []
    total_size = 0
    for line in new_text.splitlines():
        if not line.strip(): continue
        parts = line.split(',')
        if len(parts) < 5: continue
        file_id, size_str, suffix = parts[0].strip(), parts[2].strip(), parts[4].strip()
        if not file_id or not suffix: continue
        try: size = int(size_str)
        except Exception: size = 0
        if file_id in old_files_map:
            if old_files_map[file_id] != size: status = "Modified"
            else: continue
        else:
            status = "New"
        encoded = encode_suffix(suffix)
        download_url = f"{base_url}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
        file_name = file_id.split('/')[-1]
        token = encode_token(download_url, file_name, 'application/octet-stream')
        proxy_url = f"/api?action=stream&token={token}"
        total_size += size
        file_infos.append({
            "fileName": file_id,
            "size": size,
            "url": proxy_url,
            "status": status
        })
    new_items = []
    try:
        new_items = extract_new_game_items(active_version, new_text)
    except Exception as e:
        print(f"Error parsing game items hotfix: {e}")
    return jsonify({
        "success": True,
        "version": active_version,
        "totalSize": total_size,
        "files": file_infos,
        "new_items": new_items
    })

def handle_extractor_response(response):
    if not response.ok:
        try: err_data = response.json()
        except Exception: err_data = {"error": response.text}
        return jsonify(err_data), response.status_code
        
    try:
        extractor_data = response.json()
    except Exception as e:
        return jsonify({"success": False, "message": f"Invalid JSON returned from extractor: {str(e)}"}), 500

    assets_list = extractor_data.get("assets", []) or extractor_data.get("files", [])
    for asset in assets_list:
        remote_preview_url = asset.get("preview_url") or asset.get("url")
        if not remote_preview_url: continue
        if remote_preview_url.startswith('/'):
            remote_preview_url = "https://ff-asset-extractor.vercel.app" + remote_preview_url
        filename_str = asset.get("name") or asset.get("fileName") or "extracted_file"
        detected_content_type = get_mime_type(filename_str)
        
        b64_content = asset.get("data", "")
        try:
            file_bytes = base64.b64decode(b64_content) if b64_content else b""
        except Exception:
            file_bytes = b""

        local_cache_token = encode_token(remote_preview_url, filename_str, detected_content_type, file_bytes)
        decoded = decode_token(local_cache_token)
        cache_key = decoded.get("cache_key") if decoded else local_cache_token
        global MEMORY_ASSET_CACHE
        MEMORY_ASSET_CACHE[cache_key] = {
            "content": file_bytes,
            "filename": filename_str,
            "content_type": detected_content_type
        }
        local_proxy_route = f"/api?action=stream&token={local_cache_token}"
        if "preview_url" in asset: asset["preview_url"] = local_proxy_route
        if "url" in asset: asset["url"] = local_proxy_route
    if "files" not in extractor_data and "assets" in extractor_data:
        extractor_data["files"] = extractor_data["assets"]
    elif "assets" not in extractor_data and "files" in extractor_data:
        extractor_data["assets"] = extractor_data["files"]
    return jsonify(extractor_data), 200

def process_file_logic():
    bundle_url = request.args.get('bundle_url', '') or request.form.get('bundle_url', '')
    token = request.args.get('token', '') or request.form.get('token', '')
    real_bundle_url = ""
    if "token=" in bundle_url:
        token_match = re.search(r'token=([^&]+)', bundle_url)
        if token_match:
            tok = token_match.group(1)
            decoded = decode_token(tok)
            if decoded: real_bundle_url = decoded.get("url", "")
    elif token:
        decoded = decode_token(token)
        if decoded: real_bundle_url = decoded.get("url", "")
    if not real_bundle_url and bundle_url.startswith("http"):
        real_bundle_url = bundle_url
    if 'file' in request.files:
        uploaded_file = request.files['file']
        if uploaded_file.filename != '':
            files = {
                'asset_bundle': (uploaded_file.filename, uploaded_file.read(), uploaded_file.content_type)
            }
            data = {
                'bundle_url': real_bundle_url if real_bundle_url else uploaded_file.filename
            }
            try:
                response = http_session.post(EXTRACTOR_API_URL, files=files, data=data, timeout=45)
                return handle_extractor_response(response)
            except Exception as e:
                return jsonify({"success": False, "message": f"Failed to connect to extractor: {str(e)}"}), 500
    if real_bundle_url:
        try:
            encoded_url = base64.urlsafe_b64encode(real_bundle_url.encode('utf-8')).decode('utf-8')
            extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}"
            response = http_session.get(extractor_url, timeout=45)
            return handle_extractor_response(response)
        except Exception as e:
            return jsonify({"success": False, "message": f"Failed to fetch from extractor: {str(e)}"}), 500
    return jsonify({"success": False, "message": "No file uploaded or bundle URL provided"}), 400

def stream_cache_logic(token):
    if not token: return "Missing token query parameter", 400
    decoded = decode_token(token)
    if not decoded: return "Expired or invalid stream token payload.", 404
    filename = decoded.get("filename", "file")
    mime_type = decoded.get("content_type", "application/octet-stream")
    cache_key = decoded.get("cache_key")
    global MEMORY_ASSET_CACHE
    if cache_key and cache_key in MEMORY_ASSET_CACHE:
        cached_item = MEMORY_ASSET_CACHE[cache_key]
        response = Response(cached_item["content"], mimetype=mime_type)
        response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    if "content_b64" in decoded:
        try:
            content = base64.b64decode(decoded["content_b64"])
            if cache_key:
                MEMORY_ASSET_CACHE[cache_key] = {
                    "content": content,
                    "filename": filename,
                    "content_type": mime_type
                }
            response = Response(content, mimetype=mime_type)
            response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response
        except Exception: pass
    remote_url = decoded.get("url")
    if remote_url:
        try:
            res = http_session.get(remote_url, timeout=15)
            if res.status_code == 200:
                if cache_key:
                    MEMORY_ASSET_CACHE[cache_key] = {
                        "content": res.content,
                        "filename": filename,
                        "content_type": mime_type
                    }
                response = Response(res.content, mimetype=mime_type)
                response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
                response.headers["Accept-Ranges"] = "bytes"
                response.headers["Access-Control-Allow-Origin"] = "*"
                return response
        except Exception: pass
    return "Expired or unavailable stream token payload.", 404

def extract_zip_logic(token):
    if not token: return "Missing token query parameter", 400
    decoded = decode_token(token)
    if not decoded: return "Invalid or expired stream token payload", 400
    remote_url = decoded.get("url")
    if not remote_url: return "Target bundle address missing in decrypted token payload", 400
    encoded_url = base64.urlsafe_b64encode(remote_url.encode('utf-8')).decode('utf-8')
    extractor_url = f"{EXTRACTOR_API_URL}&download_type=zip"
    try:
        res = http_session.get(extractor_url, timeout=60, stream=True)
        if res.status_code == 200:
            filename = decoded.get("filename", "extracted_asset") + ".zip"
            def generate():
                for chunk in res.iter_content(chunk_size=8192): yield chunk
            response = Response(generate(), mimetype='application/zip')
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response
        else:
            return f"Extractor pipeline failure status code: {res.status_code}", 500
    except Exception as e:
        return f"Failed connecting proxy mapping to zip extractor endpoint: {e}", 500

def bulk_zip_logic(version):
    if not version or not re.match(r'^\d+\.\d+\.\d+$', version): return "Incorrect version format entered", 400
    new_text, active_version, base_url = get_file_info_data(version)
    if not new_text:
        return "Target version logs missing from server", 404
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
    if not targets: return "No assets could be extracted for this version update", 400
    zip_io = io.BytesIO()
    files_added_to_zip = 0
    def concurrent_fetch_and_extract(target):
        file_id, d_url = target
        try:
            encoded_url = base64.urlsafe_b64encode(d_url.encode('utf-8')).decode('utf-8')
            extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}"
            res = http_session.get(extractor_url, timeout=25)
            if res.status_code == 200:
                data = res.json()
                return file_id, data.get("files", []) or data.get("assets", [])
        except Exception as e:
            print(f"Error parallel extracting {file_id}: {e}")
        return file_id, []
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
                            files_added_to_zip += 1
                        except Exception: pass
    if files_added_to_zip == 0: return "No assets could be extracted for this version update", 400
    zip_io.seek(0)
    response = Response(zip_io.getvalue(), mimetype='application/zip')
    response.headers["Content-Disposition"] = f'attachment; filename="FF_MiniUpdate_{active_version}_Extracted.zip"'
    return response


# ==========================================
# Telegram Bot Database & Webhook System
# ==========================================

def is_action_permitted(chat_id, user_id):
    # Owner is always allowed
    if user_id == OWNER_ID:
         return True
    # Anyone inside the designated allowed Group Chat is allowed
    if str(chat_id) == GROUP_ID:
        return True
    if is_user_allowed(user_id):
        return True
    return False

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

def send_access_request_menu(chat_id, user_id):
    keyboard = [[{"text": "Request Access 🔑", "callback_data": f"req_access_{user_id}"}]]
    send_message_with_keyboard(chat_id, "╭─❌ ACCESS RESTRICTED\n╰─You don't have access to this bot. Click below to request.", {"inline_keyboard": keyboard})

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