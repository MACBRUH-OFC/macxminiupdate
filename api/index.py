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
BOT_TOKEN = "8654457189:AAHxcYpWTR0y1Exbaq30-Gqve0kHzJYdTcE"
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

def encode_suffix(suffix):
    if suffix.endswith('='):
        suffix = suffix[:-1]
    return suffix.replace('+', '~2B').replace('/', '~2F').replace('=', '~3D')

def get_file_info_data(version):
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
            
    # Try fallback previous version
    fallback_ver = get_previous_version(version)
    if fallback_ver:
        for base in DOMAINS:
            url = f"{base}{fallback_ver}/fileinfo"
            try:
                res = http_session.get(url, timeout=4)
                if res.status_code == 200:
                    val = (res.text, fallback_ver, base)
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
            base_ver = data["remote_version"]
            parts = list(map(int, base_ver.split('.')))
            parts[2] += 1
            calculated_target = ".".join(map(str, parts))
            return jsonify({"success": True, "target_version": calculated_target})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    return jsonify({"success": False, "message": "Failed response lookup."}), 500

def check_update_logic():
    input_val = request.args.get('version', '').strip()
    if not input_val:
        return jsonify({"success": False, "message": "Missing criteria parameters"}), 400
    if "http" in input_val:
        match = re.search(r'/(\d+\.\d+\.\d+)/', input_val)
        version = match.group(1) if match else None
    else:
        version = input_val
    if not version or not re.match(r'^\d+\.\d+\.\d+$', version):
        return jsonify({"success": False, "message": "Incorrect version format entered"}), 400
    new_text, active_version, base_url = get_file_info_data(version)
    if not new_text:
        return jsonify({"success": False, "message": "Target version logs missing from server"}), 404
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
    extractor_data = response.json()
    assets_list = extractor_data.get("assets", []) or extractor_data.get("files", [])
    for asset in assets_list:
        remote_preview_url = asset.get("preview_url") or asset.get("url")
        if not remote_preview_url: continue
        if remote_preview_url.startswith('/'):
            remote_preview_url = "https://ff-asset-extractor.vercel.app" + remote_preview_url
        filename_str = asset.get("name") or asset.get("fileName") or "extracted_file"
        detected_content_type = get_mime_type(filename_str)
        b64_content = asset.get("data", "")
        file_bytes = base64.b64decode(b64_content) if b64_content else b""
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
    extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}&download_type=zip"
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
    owner_db = read_db_from_chat(OWNER_ID)
    if not owner_db:
        owner_db = read_db_from_chat(GROUP_ID)
        if owner_db:
            write_db_to_chat(OWNER_ID, owner_db)
            try:
                res = http_session.post(f"{TELEGRAM_API}/getChat", json={"chat_id": GROUP_ID}, timeout=10).json()
                if res.get("ok") and "pinned_message" in res["result"]:
                    msg = res["result"]["pinned_message"]
                    if "📦 DATABASE STORAGE" in msg.get("text", ""):
                        http_session.post(f"{TELEGRAM_API}/unpinChatMessage", json={"chat_id": GROUP_ID, "message_id": msg["message_id"]}, timeout=10)
                        http_session.post(f"{TELEGRAM_API}/deleteMessage", json={"chat_id": GROUP_ID, "message_id": msg["message_id"]}, timeout=10)
            except Exception: pass
    if not owner_db:
        return {
            "allowed_users": [OWNER_ID], 
            "pending_requests": [],
            "rejection_sticker": None,
            "owner_state": None,
            "temp_mappings": {}
        }
    if "allowed_users" not in owner_db: owner_db["allowed_users"] = [OWNER_ID]
    if "pending_requests" not in owner_db: owner_db["pending_requests"] = []
    if "temp_mappings" not in owner_db: owner_db["temp_mappings"] = {}
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

def send_access_request_menu(chat_id, user_id):
    keyboard = [[{"text": "Request Access 🔑", "callback_data": f"req_access_{user_id}"}]]
    send_message_with_keyboard(chat_id, "╭─❌ ACCESS RESTRICTED\n╰─You don't have access to this bot. Click below to request.", {"inline_keyboard": keyboard})

def run_miniupdate_extraction_details(version):
    """Processes version changes and formats details for Telegram transmission using HTML parse mode."""
    new_text, active_version, base_url = get_file_info_data(version)
    if not new_text:
        return f"❌ Version <b>{version}</b> not found.", None, []

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
    total_size = 0
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
            else: 
                continue
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
    for idx, (fid, stat, sz) in enumerate(file_entries[:max_visible_files], 1):
        sz_kb = round(sz / 1024, 1)
        # Escape HTML entities so filename symbols do not crash the Telegram HTML parser
        clean_fid = fid.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        msg.append(f"│ ▫️ {idx:02d}. {clean_fid} ({stat}, {sz_kb} KB)")
        
    if len(file_entries) > max_visible_files:
        msg.append(f"│ ▫️ ... and {len(file_entries) - max_visible_files} more files.")

    new_items = []
    try:
        new_items = extract_new_game_items(version, new_text)
    except Exception: pass

    if new_items:
        msg.append("│")
        msg.append("├ ✨ NEW GAME ITEMS:")
        for idx, item in enumerate(new_items[:12], 1):
            clean_name = item['name'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            msg.append(f"│ ▫️ <a href=\"{item['icon_url']}\">{clean_name} ({item['id']})</a>")
        if len(new_items) > 12:
            msg.append(f"│ ▫️ ... and {len(new_items) - 12} more items.")

    msg.append("╰──────────────────────")
    return "\n".join(msg), file_entries, new_items

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

def send_admin_main_menu(chat_id, message_id=None):
    keyboard = [
        [{"text": "👥 Permitted Users List", "callback_data": "admin_view_users"}],
        [{"text": "⏳ Pending Access Requests", "callback_data": "admin_view_pending"}],
        [{"text": "❌ Close Menu", "callback_data": "cb_close"}]
    ]
    reply_markup = {"inline_keyboard": keyboard}
    text = (
        "╭─🛠 OWNER ADMIN PANEL\n"
        "│\n"
        "├ Manage user permissions and approve or revoke\n"
        "├ access requests silently.\n"
        "╰──────────────────────"
    )
    if message_id:
        http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}, timeout=10)
    else:
        http_session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup}, timeout=10)

def send_admin_pending_view(chat_id, message_id=None):
    db = get_db()
    pending = db.get("pending_requests", [])
    keyboard = []
    if not pending:
        text = "╭─⏳ PENDING REQUESTS\n│\n├ 📭 No pending access requests found.\n╰──────────────────────"
    else:
        text = "╭─⏳ PENDING REQUESTS\n│\n├ Click a request below to authorize:\n╰──────────────────────"
        for p in pending:
            uid = p["user_id"]
            name = p.get("first_name", "Unknown")
            username = p.get("username", "None")
            keyboard.append([{"text": f"👤 {name} (@{username})", "callback_data": f"admin_manage_pending_{uid}"}])
    keyboard.append([{"text": "⬅️ Back to Admin Panel", "callback_data": "admin_main"}])
    reply_markup = {"inline_keyboard": keyboard}
    if message_id:
        http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}, timeout=10)
    else:
        http_session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup}, timeout=10)

def send_admin_pending_manage(chat_id, target_uid, message_id=None):
    db = get_db()
    pending = db.get("pending_requests", [])
    p_user = None
    for p in pending:
        if p["user_id"] == target_uid:
            p_user = p
            break
    if not p_user:
        text = "❌ Pending request not found."
        keyboard = [[{"text": "⬅️ Back", "callback_data": "admin_view_pending"}]]
    else:
        name = p_user.get("first_name", "Unknown")
        username = p_user.get("username", "None")
        text = (
            f"╭─⏳ AUTHORIZE USER\n"
            f"├ Name: {name}\n"
            f"├ ID: {target_uid}\n"
            f"├ Username: @{username}\n"
            "╰──────────────────────"
        )
        keyboard = [
            [
                {"text": "✅ Approve", "callback_data": f"auth_approve_{target_uid}"},
                {"text": "❌ Reject", "callback_data": f"auth_reject_{target_uid}"}
            ],
            [{"text": "⬅️ Back", "callback_data": "admin_view_pending"}]
        ]
    reply_markup = {"inline_keyboard": keyboard}
    if message_id:
        http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}, timeout=10)
    else:
        http_session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup}, timeout=10)

def send_admin_users_view(chat_id, message_id=None):
    db = get_db()
    users = db.get("allowed_users", [])
    users = [u for u in users if (isinstance(u, dict) and u.get("user_id") != OWNER_ID) or (isinstance(u, int) and u != OWNER_ID)]
    keyboard = []
    if not users:
        text = "╭─👥 PERMITTED USERS\n│\n├ No other users have permission.\n╰──────────────────────"
    else:
        text = "╭─👥 PERMITTED USERS\n│\n├ Click a user below to revoke their access:\n╰──────────────────────"
        for u in users:
            if isinstance(u, dict):
                uid = u["user_id"]
                name = u.get("first_name", "Unknown")
                username = u.get("username", "None")
                label = f"👤 {name} (@{username})"
            else:
                uid = u
                label = f"👤 User [ID: {uid}]"
            keyboard.append([{"text": label, "callback_data": f"admin_manage_user_{uid}"}])
    keyboard.append([{"text": "⬅️ Back to Admin Panel", "callback_data": "admin_main"}])
    reply_markup = {"inline_keyboard": keyboard}
    if message_id:
        http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}, timeout=10)
    else:
        http_session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup}, timeout=10)

def send_user_management_view(chat_id, target_id, message_id=None):
    db = get_db()
    target_user = None
    for u in db.get("allowed_users", []):
        if isinstance(u, dict) and u.get("user_id") == target_id:
            target_user = u
            break
    name = target_user.get("first_name", "Unknown") if target_user else f"ID: {target_id}"
    username = target_user.get("username", "None") if target_user else "None"
    text = (
        f"╭─🛠 MANAGE USER: {name}\n"
        f"├ ID: {target_id}\n"
        f"├ Username: @{username}\n"
        "╰──────────────────────"
    )
    keyboard = [
        [{"text": "❌ Revoke Access", "callback_data": f"admin_revoke_{target_id}"}],
        [{"text": "⬅️ Back to Users List", "callback_data": "admin_view_users"}]
    ]
    reply_markup = {"inline_keyboard": keyboard}
    if message_id:
        http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}, timeout=10)
    else:
        http_session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "reply_markup": reply_markup}, timeout=10)

@app.route('/api/webhook', methods=['POST'])
def handle_bot_webhook():
    try:
        update = request.get_json()
        if not update: return jsonify({"status": "ok"}), 200

        # Callback Queries
        if "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb["id"]
            chat_id = cb["message"]["chat"]["id"]
            msg_id = cb["message"]["message_id"]
            user_id = cb["from"]["id"]
            data = cb.get("data", "")

            # Message deletion / Close button
            if data == "cb_close":
                delete_message(chat_id, msg_id)
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                return jsonify({"status": "ok"}), 200

            if data.startswith("req_access_"):
                req_uid = int(data.split("_")[-1])
                cb_user = cb["from"]
                first_name = cb_user.get("first_name", "Unknown")
                username = cb_user.get("username", "None")
                
                db = get_db()
                pending = db.setdefault("pending_requests", [])
                if not any(p["user_id"] == req_uid for p in pending):
                    pending.append({"user_id": req_uid, "first_name": first_name, "username": username})
                    save_db(db)
                    
                    send_message(OWNER_ID, (
                        "╭─👤 ACCESS REQUEST DETECTED\n"
                        f"├ User: {first_name}\n"
                        f"├ ID: {req_uid}\n"
                        f"├ Username: @{username}\n"
                        "╰──────────────────────"
                    ), parse_mode="Markdown")
                
                # Edit requester's button feedback state
                http_session.post(f"{TELEGRAM_API}/editMessageText", json={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": "Please Give me Access 🙏\n\n*(Access request sent to the owner, waiting for approval...)*",
                    "parse_mode": "Markdown"
                }, timeout=10)
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Request submitted!"}, timeout=5)
                return jsonify({"status": "ok"}), 200

            if not is_user_allowed(user_id):
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Access denied."}, timeout=5)
                return jsonify({"status": "ok"}), 200

            # Admin Navigation callbacks
            if data == "admin_main":
                if user_id == OWNER_ID:
                    send_admin_main_menu(chat_id, msg_id)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                return jsonify({"status": "ok"}), 200
            elif data == "admin_view_users":
                if user_id == OWNER_ID:
                    send_admin_users_view(chat_id, msg_id)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                return jsonify({"status": "ok"}), 200
            elif data == "admin_view_pending":
                if user_id == OWNER_ID:
                    send_admin_pending_view(chat_id, msg_id)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                return jsonify({"status": "ok"}), 200
            elif data.startswith("admin_manage_pending_"):
                if user_id == OWNER_ID:
                    target_uid = int(data.split("_")[-1])
                    send_admin_pending_manage(chat_id, target_uid, msg_id)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                return jsonify({"status": "ok"}), 200
            elif data.startswith("admin_manage_user_"):
                if user_id == OWNER_ID:
                    target_uid = int(data.split("_")[-1])
                    send_user_management_view(chat_id, target_uid, msg_id)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                return jsonify({"status": "ok"}), 200

            elif data.startswith("auth_approve_"):
                if user_id == OWNER_ID:
                    target_id = int(data.split("_")[-1])
                    db = get_db()
                    pending = db.get("pending_requests", [])
                    first_name = "Unknown"
                    username = "None"
                    for p in pending:
                        if p["user_id"] == target_id:
                            first_name = p.get("first_name", "Unknown")
                            username = p.get("username", "None")
                            break
                    add_allowed_user(target_id, first_name, username)
                    db = get_db() # Reload DB
                    db["pending_requests"] = [p for p in db.get("pending_requests", []) if p["user_id"] != target_id]
                    save_db(db)
                    
                    http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": msg_id, "text": f"✅ User {target_id} APPROVED."}, timeout=10)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "User approved!"}, timeout=5)
                    send_message(target_id, "You have been granted access to the bot! 🎉\nUse /start to begin.")
                return jsonify({"status": "ok"}), 200

            elif data.startswith("auth_reject_"):
                if user_id == OWNER_ID:
                    target_id = int(data.split("_")[-1])
                    db = get_db()
                    db["pending_requests"] = [p for p in db.get("pending_requests", []) if p["user_id"] != target_id]
                    save_db(db)
                    http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": msg_id, "text": f"❌ User {target_id} REJECTED."}, timeout=10)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "User rejected."}, timeout=5)
                    send_message(target_id, "Your request was declined by the administrator.")
                return jsonify({"status": "ok"}), 200

            elif data.startswith("admin_revoke_"):
                if user_id == OWNER_ID:
                    target_id = int(data.split("_")[-1])
                    db = get_db()
                    db["allowed_users"] = [u for u in db.get("allowed_users", []) if (isinstance(u, int) and u != target_id) or (isinstance(u, dict) and u.get("user_id") != target_id)]
                    save_db(db)
                    http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": msg_id, "text": f"🚫 User {target_id} REVOKED."}, timeout=10)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Access revoked!"}, timeout=5)
                    send_message(target_id, "Your bot access has been revoked by the administrator.")
                return jsonify({"status": "ok"}), 200

            # Extraction actions
            elif data.startswith("check_ver_"):
                ver = data.replace("check_ver_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Comparing patch logs..."}, timeout=5)
                
                detail_msg, file_entries, _ = run_miniupdate_extraction_details(ver)
                keyboard = []
                if file_entries:
                    keyboard.append([{"text": "📦 Extract All as ZIP", "callback_data": f"zip_ver_{ver}"}])
                keyboard.append([{"text": "❌ Close Viewport", "callback_data": "cb_close"}])
                
                # Update current message in-place using HTML parse mode
                url = f"{TELEGRAM_API}/editMessageText"
                http_session.post(url, json={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": detail_msg,
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": keyboard}
                }, timeout=10)
                
            elif data.startswith("zip_ver_"):
                ver = data.replace("zip_ver_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Preparing ZIP archive..."}, timeout=5)
                clear_reply_markup(chat_id, msg_id)
                send_message(chat_id, "⚙️ Generating patch extraction package. Please stand by...")
                
                zip_data = bulk_zip_logic(ver)
                if isinstance(zip_data, Response):
                    send_extracted_document_to_chat(chat_id, zip_data.get_data(), f"FF_MiniUpdate_{ver}_Extracted.zip")
                else:
                    send_message(chat_id, "❌ Archive generation failed.")

            elif data.startswith("ext_all_"):
                mapping_id = data.replace("ext_all_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Bundling archives..."}, timeout=5)
                clear_reply_markup(chat_id, msg_id)
                
                db = get_db()
                meta = db.get("temp_mappings", {}).get(mapping_id)
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
                
                db = get_db()
                meta = db.get("temp_mappings", {}).get(mapping_id)
                if meta:
                    target_url = meta.get("url")
                    sub_files = parse_bundle_subfiles(target_url)
                    if sub_files:
                        keyboard = []
                        # Limit specific downloads to top 15 components to avoid huge inline keyboards
                        for idx, sf in enumerate(sub_files[:15], 1):
                            fname = sf.get("name") or sf.get("fileName")
                            keyboard.append([{"text": f"📥 {idx}. {fname}", "callback_data": f"dlf_{mapping_id}_{idx-1}"}])
                        keyboard.append([
                            {"text": "⬅️ Back", "callback_data": f"back_ext_{mapping_id}"},
                            {"text": "❌ Close", "callback_data": "cb_close"}
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
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Downloading specific asset..."}, timeout=5)
                
                db = get_db()
                meta = db.get("temp_mappings", {}).get(mapping_id)
                if meta:
                    target_url = meta.get("url")
                    sub_files = parse_bundle_subfiles(target_url)
                    if 0 <= idx < len(sub_files):
                        sf = sub_files[idx]
                        fname = sf.get("name") or sf.get("fileName")
                        b64 = sf.get("data", "")
                        if b64:
                            send_extracted_document_to_chat(chat_id, base64.b64decode(b64), fname)
                        else:
                            send_message(chat_id, "❌ Target subfile does not contain data.")
                    else:
                        send_message(chat_id, "❌ Out of bounds index.")
                else:
                    send_message(chat_id, "❌ Session expired.")

            elif data.startswith("back_ext_"):
                mapping_id = data.replace("back_ext_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Returning..."}, timeout=5)
                
                db = get_db()
                meta = db.get("temp_mappings", {}).get(mapping_id)
                if meta:
                    file_name = meta.get("filename", "bundle.unity3d")
                    target_url = meta.get("url")
                    sub_files = parse_bundle_subfiles(target_url)
                    
                    sub_names = "\n".join([f"│ ▫️ {idx}. {sf.get('name') or sf.get('fileName')}" for idx, sf in enumerate(sub_files[:15], 1)])
                    if len(sub_files) > 15:
                        sub_names += f"\n│ ▫️ ... and {len(sub_files) - 15} more files."

                    keyboard = [[
                        {"text": "📦 Extract All as ZIP", "callback_data": f"ext_all_{mapping_id}"},
                        {"text": "🔍 Extract Specific File", "callback_data": f"ext_spec_{mapping_id}"}
                    ], [
                        {"text": "❌ Close Viewport", "callback_data": "cb_close"}
                    ]]
                    
                    url = f"{TELEGRAM_API}/editMessageText"
                    http_session.post(url, json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": (
                            "╭─📦 BUNDLE EXTRACTION ────\n"
                            f"├ Source Bundle: {file_name}\n"
                            "│\n"
                            "├ Extracted Components:\n"
                            f"{sub_names}\n"
                            "╰─────────────────────────"
                        ),
                        "reply_markup": {"inline_keyboard": keyboard}
                    }, timeout=10)
                else:
                    send_message(chat_id, "❌ Session expired.")

            return jsonify({"status": "ok"}), 200

        # Chat Text Parser
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            user_info = msg.get("from", {})
            user_id = user_info.get("id")
            first_name = user_info.get("first_name", "Unknown")
            username = user_info.get("username", "None")
            raw_text = msg.get("text", "").strip()

            # Live fileinfo URL hook matching (strict format checking for the alert message)
            if raw_text and "LIVE LINK DETECTED" in raw_text and ("dl.tata.freefiremobile.com" in raw_text or "dl-tata.freefireind.in" in raw_text):
                version_match = re.search(r'/android/(\d+\.\d+\.\d+)/', raw_text)
                if version_match:
                    ver = version_match.group(1)
                    keyboard = [[{"text": f"🔍 Check Miniupdate {ver}", "callback_data": f"check_ver_{ver}"}]]
                    send_message_with_keyboard(chat_id, (
                        "╭──────────────────────\n"
                        "├ New MINIUPDATE\n"
                        f"├ Version: {ver}\n"
                        "╰──────────────────────"
                    ), {"inline_keyboard": keyboard})
                    return jsonify({"status": "ok"}), 200

            # Access controls for private interaction commands
            if raw_text.startswith("/"):
                if not is_user_allowed(user_id):
                    if raw_text.startswith("/start") or raw_text.startswith("/check") or raw_text.startswith("/extract") or raw_text.startswith("/admin"):
                        send_access_request_menu(chat_id, user_id)
                    return jsonify({"status": "ok"}), 200

            if raw_text.startswith("/start"):
                send_message(chat_id, (
                    "╭─⚡ MINIUPDATE CHECKER BOT ────\n│\n"
                    "├ Welcome! You are an approved user. 🎉\n│\n"
                    "├ ⚙️ COMMANDS:\n"
                    "├ ▫️ /check [version]\n"
                    "├ ▫️ /extract [replying to any asset file]\n"
                    "╰──────────────────────"
                ))

            elif raw_text.startswith("/admin"):
                if user_id == OWNER_ID:
                    send_admin_main_menu(chat_id)
                else:
                    send_message(chat_id, "❌ This command is restricted to the administrator.")

            elif raw_text.startswith("/check"):
                parts = raw_text.split(" ")
                if len(parts) < 2:
                    send_message(chat_id, "╭─❌ USAGE\n╰─/check [version]\n\nExample: /check 1.126.6")
                    return jsonify({"status": "ok"}), 200
                ver = parts[1].strip()
                try:
                    detail_msg, file_entries, _ = run_miniupdate_extraction_details(ver)
                    keyboard = []
                    if file_entries:
                        keyboard.append([{"text": "📦 Extract All as ZIP", "callback_data": f"zip_ver_{ver}"}])
                    keyboard.append([{"text": "❌ Close Viewport", "callback_data": "cb_close"}])
                    
                    if keyboard and any(keyboard):
                        send_message_with_keyboard(chat_id, detail_msg, {"inline_keyboard": keyboard}, parse_mode="HTML")
                    else:
                        send_message(chat_id, detail_msg, parse_mode="HTML")
                except Exception as e:
                    send_message(chat_id, f"❌ Version <b>{ver}</b> not found.")

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
                                        db = get_db()
                                        db["temp_mappings"][mapping_id] = {
                                            "url": dl_url,
                                            "filename": file_name
                                        }
                                        save_db(db)
                                        
                                        sub_names = "\n".join([f"│ ▫️ {idx}. {sf.get('name') or sf.get('fileName')}" for idx, sf in enumerate(sub_files[:15], 1)])
                                        if len(sub_files) > 15:
                                            sub_names += f"\n│ ▫️ ... and {len(sub_files) - 15} more files."
                                            
                                        keyboard = [[
                                            {"text": "📦 Extract All as ZIP", "callback_data": f"ext_all_{mapping_id}"},
                                            {"text": "🔍 Extract Specific File", "callback_data": f"ext_spec_{mapping_id}"}
                                        ], [
                                            {"text": "❌ Close Viewport", "callback_data": "cb_close"}
                                        ]]
                                        send_message_with_keyboard(chat_id, (
                                            "╭─📦 BUNDLE EXTRACTION ────\n"
                                            f"├ Source Bundle: {file_name}\n"
                                            "│\n"
                                            "├ Extracted Components:\n"
                                            f"{sub_names}\n"
                                            "╰─────────────────────────"
                                        ), {"inline_keyboard": keyboard})
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

    except Exception as e:
        print(f"Error handling Telegram message: {e}")
    return jsonify({"status": "ok"}), 200

# ==========================================
# Web Dashboard Routes Configuration
# ==========================================

@app.route('/', methods=['GET', 'POST'])
@app.route('/api', methods=['GET', 'POST'])
def handle_all_requests():
    action = request.args.get('action')
    path = request.path.rstrip('/')
    
    if path == '/api/process' or path == '/process' or action == 'process':
        return process_file_logic()
    elif path == '/api/latest-version' or path == '/latest-version' or action == 'latest-version':
        return latest_version_logic()
    elif path == '/api/update' or path == '/update' or action == 'update':
        return check_update_logic()
    elif path == '/api/bulk-zip' or path == '/bulk-zip' or action == 'bulk-zip':
        version = request.args.get('version', '').strip()
        return bulk_zip_logic(version)
    elif path.startswith('/api/stream-cache/') or path.startswith('/stream-cache/'):
        token = path.split('/')[-1]
        return stream_cache_logic(token)
    elif action == 'stream':
        return stream_cache_logic(request.args.get('token'))
    elif path == '/api/extract-zip' or path == '/extract-zip' or action == 'extract-zip':
        token = request.args.get('token')
        return extract_zip_logic(token)
    
    if request.method == 'GET' and (path == '' or path == '/api'):
        try:
            for filename in ['public/index.html', 'index.html']:
                if os.path.exists(filename):
                    with open(filename, 'r', encoding='utf-8') as f:
                        return f.read()
            return jsonify({"success": False, "message": "index.html template missing"}), 404
        except Exception as e:
            return f"Error: {str(e)}", 500

    return jsonify({"success": False, "message": f"Endpoint or Action parameter is unmapped."}), 404

if __name__ == '__main__':
    app.run(port=5000, debug=True)