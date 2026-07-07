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
BOT_TOKEN = "8654457189:AAGIh11dHZWkGeh4d7xzF6MtwvcJ9UxmA_U"
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

# Stateless short-lived in-memory mappings to stop database spam
BOT_TEMP_MAPPINGS = {}
PURGED_LEGACY_DB = False

# List of allowed user IDs permitted in private chats
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
    if content_bytes:
        # If there's no real web URL, embed the content bytes regardless of size
        if len(content_bytes) < 2048 or "local-placeholder" in remote_url:
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

def check_name_similarity(name1, name2):
    """Normalized token alignment index mapping mesh names with texture assets safely."""
    n1 = re.sub(r'[\W_]+', ' ', name1).lower().strip()
    n2 = re.sub(r'[\W_]+', ' ', name2).lower().strip()
    
    tokens1 = set(t for t in n1.split() if len(t) > 2)
    tokens2 = set(t for t in n2.split() if len(t) > 2)
    
    ignore_words = {"mesh", "tex", "texture", "preview", "object", "model", "vfx", "part", "material", "mat"}
    t1_clean = tokens1 - ignore_words
    t2_clean = tokens2 - ignore_words
    
    overlap = t1_clean.intersection(t2_clean)
    if overlap:
        return True
        
    clean_n1 = "".join(list(t1_clean))
    clean_n2 = "".join(list(t2_clean))
    if clean_n1 and clean_n2:
        if clean_n1 in clean_n2 or clean_n2 in clean_n1:
            return True
            
    return False

def find_references_in_json(obj, asset_names):
    """Recursively scans parsed JSON object values for references to other files inside the bundle."""
    referenced = []
    strings = []
    
    def extract_strings(v):
        if isinstance(v, str):
            strings.append(v.lower())
        elif isinstance(v, dict):
            for val in v.values():
                extract_strings(val)
        elif isinstance(v, list):
            for item in v:
                extract_strings(item)
                
    extract_strings(obj)
    
    for name in asset_names:
        base_name = os.path.splitext(name)[0].lower()
        if len(base_name) > 4:
            for s in strings:
                if base_name in s:
                    referenced.append(name)
                    break
    return referenced

def extract_version_from_input(input_str):
    """Robust regex matching system extracting clean target versions or fileinfo URLs."""
    if not input_str:
        return None
    match = re.search(r'(?:/android/|android/|^)(\d+\.\d+\.\d+)', input_str.strip())
    if match:
        return match.group(1)
    match_fallback = re.search(r'(\d+\.\d+\.\d+)', input_str)
    if match_fallback:
        return match_fallback.group(1)
    return None

def analyze_update_purpose(files, new_items):
    categories = set()
    highlights = []
    
    obj_count = sum(1 for f in files if f["fileName"].lower().endswith(".obj"))
    png_count = sum(1 for f in files if any(f["fileName"].lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]))
    config_count = sum(1 for f in files if "config/" in f["fileName"].lower())
    loc_count = sum(1 for f in files if "localization/" in f["fileName"].lower())
    sound_count = sum(1 for f in files if any(f["fileName"].lower().endswith(ext) for ext in [".ogg", ".wav", ".mp3"]))
    
    if obj_count > 0:
        categories.add("3D Visual Models")
        highlights.append(f"Injected {obj_count} high-fidelity 3D meshes")
    if png_count > 0:
        categories.add("High-Res Textures")
        highlights.append(f"Added {png_count} render mapping sprites/textures")
    if config_count > 0:
        categories.add("Gameplay Config")
        highlights.append("Modified gameplay attribute settings or balance rules")
    if loc_count > 0:
        categories.add("Localization / Texts")
        highlights.append("Updated localized text strings")
    if sound_count > 0:
        categories.add("Audio Effects")
        highlights.append(f"Updated {sound_count} acoustic assets/effects")
        
    weapon_skins = set()
    emotes = set()
    for f in files:
        fname = f["fileName"].lower()
        for g in ["aug", "m249", "ak47", "scar", "m1887", "mp5", "groza", "awm", "desert_eagle"]:
            if g in fname:
                weapon_skins.add(g.upper())
        if "emote" in fname or "motion" in fname:
            emotes.add(fname.split('/')[-1])
            
    if weapon_skins:
        categories.add("Weapon Customization")
        highlights.append(f"New cosmetics or parameters for: {', '.join(weapon_skins)}")
    if emotes:
        categories.add("Expressive Emotes")
        highlights.append(f"New animation sequences: {', '.join(list(emotes)[:2])}")
        
    if new_items:
        categories.add("Content Update")
        bundle_count = sum(1 for i in new_items if i.get("is_bundle"))
        if bundle_count > 0:
            categories.add("Exclusive Costumes")
            highlights.append(f"Discovered {bundle_count} item bundles")
        item_names = [i["name"] for i in new_items[:3]]
        highlights.append(f"Added new inventory assets: {', '.join(item_names)}")

    if "Gameplay Config" in categories and len(categories) == 1:
        summary = "This mini-update acts as a tactical hotfix modifying underlying server gameplay rules or parameters."
    elif "3D Visual Models" in categories or "High-Res Textures" in categories:
        summary = "This update introduces visual content enhancements, skin updates, and textures for localized events."
        if weapon_skins:
            summary += f" Specifically targeting visual attributes for weapon models ({', '.join(weapon_skins)})."
    else:
        summary = "This is a standard client synchronization maintenance patch coordinating game asset metadata rules."
        
    return {
        "summary": summary,
        "categories": list(categories) if categories else ["Maintenance Patch"],
        "highlights": highlights,
        "severity": "Visual Content" if "3D Visual Models" in categories else "System Synced"
    }

def detect_dynamic_themes(file_entries):
    """Scans file lists dynamically to identify thematic codes/tags without reliance on hardcoded keyword lists."""
    from collections import Counter
    token_counts = Counter()
    
    generic_words = {
        "gameassetbundles", "sound", "voice", "en", "audio", "vfx", "weapon", "assembly", "config", "patch",
        "extracted", "mesh", "tex", "texture", "material", "animator", "controller", "prefab", "other",
        "containers", "geometry", "skinnedmeshrenderer", "meshrenderer", "transform", "resconf",
        "resconfhotupdate", "matchreviewparam", "patchoptionalresmanager", "uidrawshop", "unlimited", "cardpool"
    }
    
    for fid, _, _ in file_entries:
        basename = fid.split('/')[-1]
        parts = re.split(r'[\W_]+', basename)
        for part in parts:
            part_lower = part.lower()
            if len(part_lower) >= 4 and part_lower not in generic_words:
                token_counts[part_lower] += 1
                
    detected = []
    for tag, count in token_counts.most_common(5):
        if count >= 2 or any(char.isdigit() for char in tag):
            detected.append((tag, count))
    return detected

def latest_version_logic():
    try:
        res = http_session.get(VERSION_API, timeout=5)
        data = res.json()
        if data.get("success") and "remote_version" in data:
            base_ver = data["remote_version"]
            
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
    version = extract_version_from_input(input_val)
    if not version:
        return jsonify({"success": False, "message": "Incorrect version format or link entered"}), 400

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
        
    analysis = analyze_update_purpose(file_infos, new_items)
    
    return jsonify({
        "success": True,
        "version": active_version,
        "totalSize": total_size,
        "files": file_infos,
        "new_items": new_items,
        "analysis": analysis
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

    assets_list = (
        extractor_data.get("assets") or 
        extractor_data.get("files") or 
        extractor_data.get("extracted") or 
        extractor_data.get("data")
    )
    if not isinstance(assets_list, list):
        assets_list = []
    
    processed_assets = []
    for asset in assets_list:
        filename_str = asset.get("name") or asset.get("fileName") or "extracted_file"
        detected_content_type = get_mime_type(filename_str)
        
        structured_data = None
        for key in ["data", "properties", "fields", "json", "jsonData", "content"]:
            val = asset.get(key)
            if val is not None:
                if isinstance(val, (dict, list)):
                    structured_data = val
                    break
                elif isinstance(val, str) and (val.strip().startswith("{") or val.strip().startswith("[")):
                    try:
                        structured_data = json.loads(val)
                        break
                    except Exception:
                        pass

        file_bytes = b""
        if structured_data is not None:
            try:
                json_str = json.dumps(structured_data, indent=2, ensure_ascii=False)
                file_bytes = json_str.encode('utf-8')
                detected_content_type = "application/json"
            except Exception:
                file_bytes = b""
        else:
            raw_val = asset.get("data", "")
            if isinstance(raw_val, str) and raw_val:
                try:
                    file_bytes = base64.b64decode(raw_val)
                except Exception:
                    file_bytes = raw_val.encode('utf-8')

        remote_preview_url = asset.get("preview_url") or asset.get("url") or ""
        if not remote_preview_url:
            if not file_bytes:
                file_bytes = b"{}"
                detected_content_type = "application/json"
            dummy_hash = hashlib.md5(filename_str.encode('utf-8')).hexdigest()
            remote_preview_url = f"https://local-placeholder/{dummy_hash}"

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
        asset["preview_url"] = local_proxy_route
        asset["url"] = local_proxy_route
        asset["fileName"] = filename_str
        asset["label"] = detected_content_type
        processed_assets.append(asset)

    # Separate assets by visual groups to scan data reference connections
    meshes = [a for a in processed_assets if a["fileName"].lower().endswith(".obj")]
    textures = [a for a in processed_assets if any(a["fileName"].lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"])]
    materials = [a for a in processed_assets if "material" in a["fileName"].lower() or "material" in (a.get("type") or "").lower()]
    renderers = [a for a in processed_assets if "renderer" in a["fileName"].lower() or "renderer" in (a.get("type") or "").lower()]

    # 1. Parse Materials to find referenced textures
    material_to_textures = {}
    for mat in materials:
        mat_name = os.path.splitext(os.path.basename(mat["fileName"]))[0].lower()
        mat_cache_key = mat["url"].split("token=")[-1]
        mat_item = MEMORY_ASSET_CACHE.get(mat_cache_key)
        if mat_item:
            try:
                mat_json = json.loads(mat_item["content"].decode('utf-8'))
                referenced = find_references_in_json(mat_json, [t["fileName"] for t in textures])
                if referenced:
                    material_to_textures[mat_name] = referenced
            except Exception: pass

    # 2. Parse Renderers to map meshes to materials
    mesh_to_materials = {}
    for rend in renderers:
        rend_cache_key = rend["url"].split("token=")[-1]
        rend_item = MEMORY_ASSET_CACHE.get(rend_cache_key)
        if rend_item:
            try:
                rend_json = json.loads(rend_item["content"].decode('utf-8'))
                ref_meshes = find_references_in_json(rend_json, [m["fileName"] for m in meshes])
                ref_mats = find_references_in_json(rend_json, [mt["fileName"] for mt in materials])
                for rm in ref_meshes:
                    rm_name = os.path.splitext(os.path.basename(rm))[0].lower()
                    for rmat in ref_mats:
                        rmat_name = os.path.splitext(os.path.basename(rmat))[0].lower()
                        mesh_to_materials[rm_name] = rmat_name
            except Exception: pass

    # 3. Associate Textures with Meshes using Compiled reference pathways
    for m in meshes:
        m_name = os.path.splitext(os.path.basename(m["fileName"]))[0].lower()
        matched_textures = []
        
        target_mat = mesh_to_materials.get(m_name)
        if target_mat and target_mat in material_to_textures:
            for tex_file in material_to_textures[target_mat]:
                for t in textures:
                    if t["fileName"].lower() == tex_file.lower():
                        matched_textures.append(t["url"])
                        
        if not matched_textures:
            for t in textures:
                t_name = os.path.splitext(os.path.basename(t["fileName"]))[0].lower()
                if check_name_similarity(m_name, t_name):
                    matched_textures.append(t["url"])
                    
        m["associated_textures"] = matched_textures

    # Group sibling parts for concurrent rendering using semantic checks
    composites = []
    used_meshes_in_composites = set()
    
    for i, m1 in enumerate(meshes):
        if m1["fileName"] in used_meshes_in_composites:
            continue
        m1_basename = os.path.basename(m1["fileName"])
        m1_name = os.path.splitext(m1_basename)[0].lower()
        
        siblings = []
        for j, m2 in enumerate(meshes):
            if i == j: continue
            m2_name = os.path.splitext(os.path.basename(m2["fileName"]))[0].lower()
            if check_name_similarity(m1_name, m2_name):
                siblings.append(m2)
                
        if siblings:
            all_composite_meshes = [m1] + siblings
            comp_textures = set()
            comp_parts = []
            
            for mesh_obj in all_composite_meshes:
                used_meshes_in_composites.add(mesh_obj["fileName"])
                for tex_url in mesh_obj["associated_textures"]:
                    comp_textures.add(tex_url)
                    
            for mesh_obj in siblings:
                comp_parts.append({
                    "name": os.path.basename(mesh_obj["fileName"]),
                    "url": mesh_obj["url"],
                    "associated_textures": mesh_obj["associated_textures"]
                })
                
            composites.append({
                "id": f"comp_{i}",
                "name": f"{m1_basename.split('.')[0]} (Assembly Scene)",
                "type": "Composite 3D Assembly",
                "url": m1["url"],
                "associated_textures": list(comp_textures),
                "composite_parts": comp_parts,
                "preview_summary": f"Unified assembly of {len(all_composite_meshes)} structural mesh layers and {len(comp_textures)} mapped shader textures."
            })

    # Group categorized explorer categories cleanly
    categories = {
        "3d_models": [m for m in meshes if m["fileName"] not in used_meshes_in_composites],
        "textures": textures,
        "materials": materials,
        "animations": [a for a in processed_assets if "animation" in a["fileName"].lower() or "animator" in a["fileName"].lower()],
        "configs": [a for a in processed_assets if any(ext in a["fileName"].lower() for ext in [".json", ".txt", ".dat", ".xml"]) and not any(a["fileName"] == m["fileName"] for m in meshes) and not any(a["fileName"] == t["fileName"] for t in textures)],
        "others": []
    }
    
    for asset in processed_assets:
        is_categorized = (
            any(asset["fileName"] == m["fileName"] for m in meshes) or
            any(asset["fileName"] == t["fileName"] for t in textures) or
            any(asset["fileName"] == mat["fileName"] for mat in materials) or
            any(asset["fileName"] == anim["fileName"] for anim in categories["animations"]) or
            any(asset["fileName"] == conf["fileName"] for conf in categories["configs"])
        )
        if not is_categorized:
            categories["others"].append(asset)

    response_data = {
        "success": True,
        "composites": composites,
        "categories": categories,
        "assets": processed_assets
    }
    return jsonify(response_data), 200

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
            "owner_state": None
        }
        
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
        save_db(owner_db)
        
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

def is_action_permitted(chat_id, user_id):
    if user_id == OWNER_ID: return True
    if str(chat_id) == GROUP_ID: return True
    if is_user_allowed(user_id): return True
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

def add_pending_request(user_id, first_name, username, chat_title):
    db = get_db()
    pending = db.get("pending_requests", [])
    if not any(p["user_id"] == user_id for p in pending):
        pending.append({
            "user_id": user_id,
            "first_name": first_name,
            "username": username,
            "chat_title": chat_title
        })
        db["pending_requests"] = pending
        write_db_to_chat(OWNER_ID, db)

def remove_pending_request(user_id):
    db = get_db()
    pending = db.get("pending_requests", [])
    pending = [p for p in pending if p["user_id"] != user_id]
    db["pending_requests"] = pending
    write_db_to_chat(OWNER_ID, db)

def send_item_icons_to_chat(chat_id, new_items):
    """Downloads all newly parsed item icons and delivers them directly as cleanly formatted Telegram photo messages."""
    for item in new_items:
        item_id = item.get("id")
        item_name = item.get("name", "Unknown Item")
        icon_url = item.get("icon_url")
        if not item_id or not icon_url:
            continue
        try:
            res = http_session.get(icon_url, timeout=12)
            if res.status_code == 200:
                url = f"{TELEGRAM_API}/sendPhoto"
                files = {"photo": (f"{item_id}.png", res.content, "image/png")}
                payload = {
                    "chat_id": chat_id,
                    "caption": f"✨ New Item: <b>{item_name}</b>\nID: <code>{item_id}</code>",
                    "parse_mode": "HTML"
                }
                http_session.post(url, data=payload, files=files, timeout=15)
        except Exception as e:
            print(f"Error sending item icon for {item_id}: {e}")

def run_miniupdate_extraction_details(version):
    """Processes version changes and formats details for Telegram transmission using HTML parse mode."""
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
        
        status = None
        if file_id in old_files_map:
            if old_files_map[file_id] != size: 
                status = "Modified"
                changed_files_count += 1
                total_size += size
        else: 
            status = "New"
            new_files_count += 1
            total_size += size
            
        if status:
            file_entries.append((file_id, status, size))

    size_mb = round(total_size / (1024 * 1024), 2)
    
    simple_file_list = [{"fileName": entry[0]} for entry in file_entries]
    new_items = []
    try:
        new_items = extract_new_game_items(version, new_text)
    except Exception: pass
    
    analysis = analyze_update_purpose(simple_file_list, new_items)

    # Dynamic thematic scanning logic
    themes = detect_dynamic_themes(file_entries)

    # Category classifications for the bot
    categories = {"obj": 0, "png": 0, "json": 0, "other": 0}
    for fid, stat, sz in file_entries:
        l_fid = fid.lower()
        if l_fid.endswith(".obj"): categories["obj"] += 1
        elif any(l_fid.endswith(ext) for ext in [".png", ".jpg", ".jpeg"]): categories["png"] += 1
        elif l_fid.endswith(".json"): categories["json"] += 1
        else: categories["other"] += 1

    msg = [
        "<b>╭─📊 MINIUPDATE ANALYSIS ────</b>",
        "│",
        f"├ 🟢 Version Extracted: <b>{version}</b>",
        f"├ MiniUpdate Size: <b>{size_mb} MB</b>",
        f"├ Changed Files Count: <b>{changed_files_count}</b>",
        f"├ New Files Count: <b>{new_files_count}</b>",
        "│",
        "├ 🧠 INTEL REPORT:",
        f"│ <code>{analysis['summary']}</code>",
        "│"
    ]

    if themes:
        msg.append("├ 🏷️ THEMATIC THEMES DETECTED:")
        for tag, count in themes:
            msg.append(f"│ • Tag <b>#{tag.upper()}</b>: {count} matches")
        msg.append("│")

    # Grouped files category overview for Telegram
    msg.append("├ 📁 CATEGORIES:")
    msg.append("<blockquote>")
    if categories["obj"] > 0: msg.append(f"📦 <b>Geometry & 3D Meshes:</b> {categories['obj']} meshes")
    if categories["png"] > 0: msg.append(f"🎨 <b>Textures & Sprite Assets:</b> {categories['png']} sprites")
    if categories["json"] > 0: msg.append(f"⚙️ <b>MonoBehaviour & GameData:</b> {categories['json']} configs")
    if categories["other"] > 0: msg.append(f"📝 <b>Other Resources:</b> {categories['other']} files")
    msg.append("</blockquote>")

    msg.append("├ 📂 DISCOVERED FILES:")
    msg.append("<blockquote>")
    max_visible_files = 15
    for idx, (fid, stat, sz) in enumerate(file_entries[:max_visible_files], 1):
        sz_kb = round(sz / 1024, 1)
        if sz_kb >= 1024:
            sz_display = f"{round(sz_kb / 1024, 2)} MB"
        else:
            sz_display = f"{sz_kb} KB"
            
        clean_fid = fid.split('/')[-1]
        clean_fid_escaped = clean_fid.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        msg.append(f"▫️ {idx:02d}. <b>{clean_fid_escaped}</b>")
        msg.append(f"            <i>({stat}, {sz_display})</i>")
        
    if len(file_entries) > max_visible_files:
        msg.append(f"▫️ ... and {len(file_entries) - max_visible_files} more files.")
    msg.append("</blockquote>")

    if new_items:
        msg.append("│")
        msg.append("├ ✨ NEW GAME ITEMS:")
        msg.append("<blockquote>")
        for idx, item in enumerate(new_items[:12], 1):
            clean_name = item['name'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            msg.append(f"▫️ <a href=\"{item['icon_url']}\"><b>{clean_name}</b></a>")
            msg.append(f"      ID: <code><i>{item['id']}</i></code>")
        if len(new_items) > 12:
            msg.append(f"... and {len(new_items) - 12} more items.")
        msg.append("</blockquote>")

    msg.append("╰──────────────────────")
    
    # Compile composite mesh-texture connections on the fly for zip buttons
    file_keyboard_mappings = {}
    meshes = [f[0] for f in file_entries if f[0].lower().endswith(".obj")]
    textures = [f[0] for f in file_entries if any(f[0].lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg"])]
    
    for m in meshes[:3]:
        m_basename = m.split('/')[-1]
        m_clean_name = os.path.splitext(m_basename)[0]
        
        comp_meshes = []
        for other_m in meshes:
            other_m_basename = other_m.split('/')[-1]
            if check_name_similarity(m_clean_name, os.path.splitext(other_m_basename)[0]):
                comp_meshes.append({
                    "name": other_m_basename,
                    "url": f"{base_url}{version}/gameassetbundles/{other_m}"
                })
                
        comp_textures = []
        for t in textures:
            t_basename = t.split('/')[-1]
            if check_name_similarity(m_clean_name, os.path.splitext(t_basename)[0]):
                comp_textures.append({
                    "name": t_basename,
                    "url": f"{base_url}{version}/gameassetbundles/{t}"
                })
                
        if comp_textures and len(comp_meshes) > 0:
            comp_id = hashlib.md5(f"{version}:{m_clean_name}".encode('utf-8')).hexdigest()[:8]
            BOT_TEMP_MAPPINGS[comp_id] = {
                "type": "composite_zip",
                "version": version,
                "name": m_clean_name,
                "mesh_assets": comp_meshes,
                "texture_assets": comp_textures
            }
            file_keyboard_mappings[m_basename] = f"zip_comp_{comp_id}"
            
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


# ==========================================
# Flask Controller & Explicit Webhook Routes
# ==========================================

@app.route('/api/webhook', methods=['GET', 'POST'])
@app.route('/webhook', methods=['GET', 'POST'])
def telegram_webhook():
    if request.method == 'GET':
        try:
            host = request.headers.get('Host')
            protocol = "https" if "localhost" not in host else "http"
            current_url = f"{protocol}://{host}/api/webhook"
            
            webhook_setup_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
            setup_res = http_session.post(webhook_setup_url, json={"url": current_url}, timeout=10).json()
            
            diagnostic_text = (
                "<b>🔔 MINIUPDATE BOT ONLINE</b>\n\n"
                "🟢 Webhook successfully compiled and is active!\n"
                f"🔗 Webhook Registered: <code>{current_url}</code>\n"
                f"📦 Telegram Response: <code>{json.dumps(setup_res)}</code>"
            )
            send_message(OWNER_ID, diagnostic_text, parse_mode="HTML")
            
            return jsonify({
                "status": "active",
                "message": "Dynamic Webhook auto-configured! Diagnostic message sent to Owner.",
                "bot_token": BOT_TOKEN[:10] + "...",
                "registered_url": current_url,
                "telegram_webhook_response": setup_res
            }), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
            
    return handle_bot_webhook()

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
def handle_all_requests(path):
    full_path = "/" + path.strip("/")
    action = request.args.get('action')
    
    if full_path == '/api/process' or full_path == '/process' or action == 'process':
        return process_file_logic()
    elif full_path == '/api/latest-version' or full_path == '/latest-version' or action == 'latest-version':
        return latest_version_logic()
    elif full_path == '/api/update' or full_path == '/update' or action == 'update':
        return check_update_logic()
    elif full_path == '/api/bulk-zip' or full_path == '/bulk-zip' or action == 'bulk-zip':
        version = request.args.get('version', '').strip()
        return bulk_zip_logic(version)
    elif full_path == '/api/extract-zip' or full_path == '/extract-zip' or action == 'extract-zip':
        token = request.args.get('token')
        return extract_zip_logic(token)
    elif full_path.startswith('/api/stream-cache/') or full_path.startswith('/stream-cache/'):
        token = full_path.split('/')[-1]
        return stream_cache_logic(token)
    elif action == 'stream':
        return stream_cache_logic(request.args.get('token'))

    if request.method == 'GET' and (full_path == '/' or full_path == '/api' or full_path == '/api/'):
        try:
            for filename in ['api/index.html', 'public/index.html', 'index.html']:
                if os.path.exists(filename):
                    with open(filename, 'r', encoding='utf-8') as f:
                        return f.read()
            return jsonify({"success": False, "message": "index.html template missing"}), 404
        except Exception as e:
            return f"Error: {str(e)}", 500

    return jsonify({"success": False, "message": f"Endpoint '{full_path}' is unmapped."}), 404


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

            if not is_action_permitted(chat_id, user_id):
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Access denied."}, timeout=5)
                return jsonify({"status": "ok"}), 200

            if data == "cb_close":
                delete_message(chat_id, msg_id)
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                return jsonify({"status": "ok"}), 200

            elif data.startswith("req_access_"):
                req_uid = int(data.split("_")[-1])
                cb_user = cb["from"]
                first_name = cb_user.get("first_name", "Unknown")
                username = cb_user.get("username", "None")
                
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
                
                http_session.post(f"{TELEGRAM_API}/editMessageText", json={
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": "Please Give me Access 🙏\n\n*(Access request sent to the owner, waiting for approval...)*",
                    "parse_mode": "Markdown"
                }, timeout=10)
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Request submitted!"}, timeout=5)
                return jsonify({"status": "ok"}), 200

            elif data.startswith("auth_approve_"):
                if user_id == OWNER_ID:
                    target_id = int(data.split("_")[-1])
                    add_allowed_user(target_id)
                    http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": msg_id, "text": f"✅ User {target_id} APPROVED."}, timeout=10)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "User approved!"}, timeout=5)
                    send_message(target_id, "You have been granted access to the bot! 🎉\nUse /start to begin.")
                return jsonify({"status": "ok"}), 200

            elif data.startswith("auth_reject_"):
                if user_id == OWNER_ID:
                    target_id = int(data.split("_")[-1])
                    http_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": msg_id, "text": f"❌ User {target_id} REJECTED."}, timeout=10)
                    http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "User rejected."}, timeout=5)
                    send_message(target_id, "Your request was declined by the administrator.")
                return jsonify({"status": "ok"}), 200

            # Extraction actions
            elif data.startswith("check_ver_") or data.startswith("back_ver_"):
                ver = data.replace("check_ver_", "").replace("back_ver_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                
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
                    
                    for fname, action_data in file_keyboard_mappings.items():
                        if action_data.startswith("zip_comp_"):
                            comp_id = action_data.replace("zip_comp_", "")
                            meta = BOT_TEMP_MAPPINGS.get(comp_id)
                            if meta:
                                keyboard.append([{"text": f"📦 Combined {meta['name']} Scene ZIP", "callback_data": f"zip_comp_{comp_id}"}])
                                
                    if file_entries:
                        keyboard.append([{"text": "📦 Extract All as ZIP", "callback_data": f"zip_ver_{ver}"}])
                    
                    url = f"{TELEGRAM_API}/editMessageText"
                    http_session.post(url, json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": detail_msg,
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": keyboard}
                    }, timeout=10)

                    new_items = extract_new_game_items(ver, get_file_info_data_exact(ver)[0])
                    if new_items:
                        send_item_icons_to_chat(chat_id, new_items)
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
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                clear_reply_markup(chat_id, msg_id)
                send_message(chat_id, "⚙️ Generating patch extraction package. Please stand by...")
                
                zip_data = bulk_zip_logic(ver)
                if isinstance(zip_data, Response):
                    send_extracted_document_to_chat(chat_id, zip_data.get_data(), f"FF_MiniUpdate_{ver}_Extracted.zip")
                else:
                    send_message(chat_id, "❌ Archive generation failed.")

            elif data.startswith("zip_comp_"):
                comp_id = data.replace("zip_comp_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Generating combined zip..."}, timeout=5)
                
                meta = BOT_TEMP_MAPPINGS.get(comp_id)
                if meta:
                    version = meta.get("version")
                    comp_name = meta.get("name", "Composite_Asset")
                    
                    send_message(chat_id, f"⚙️ Compiling combined zip for <b>{comp_name}</b>. Downloading meshes and textures...", parse_mode="HTML")
                    
                    zip_io = io.BytesIO()
                    files_added = 0
                    
                    def fetch_asset(item):
                        name, url = item
                        try:
                            res_bytes = None
                            if "token=" in url:
                                token_match = re.search(r'token=([^&]+)', url)
                                if token_match:
                                    decoded = decode_token(token_match.group(1))
                                    if decoded:
                                        cache_key = decoded.get("cache_key")
                                        if cache_key and cache_key in MEMORY_ASSET_CACHE:
                                            res_bytes = MEMORY_ASSET_CACHE[cache_key]["content"]
                                        else:
                                            raw_res = http_session.get(decoded.get("url"), timeout=15)
                                            if raw_res.status_code == 200:
                                                res_bytes = raw_res.content
                            if not res_bytes and url.startswith("http"):
                                raw_res = http_session.get(url, timeout=15)
                                if raw_res.status_code == 200:
                                    res_bytes = raw_res.content
                            return name, res_bytes
                        except Exception:
                            return name, None

                    assets_to_fetch = []
                    for mesh_item in meta.get("mesh_assets", []):
                        assets_to_fetch.append((mesh_item["name"], mesh_item["url"]))
                    for tex_item in meta.get("texture_assets", []):
                        assets_to_fetch.append((tex_item["name"], tex_item["url"]))
                        
                    with zipfile.ZipFile(zip_io, "w", zipfile.ZIP_DEFLATED) as zf:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                            results = executor.map(fetch_asset, assets_to_fetch)
                            for name, content in results:
                                if content:
                                    zf.writestr(name, content)
                                    files_added += 1
                                    
                    if files_added > 0:
                        send_extracted_document_to_chat(chat_id, zip_io.getvalue(), f"{comp_name}_Combined_Asset.zip")
                    else:
                        send_message(chat_id, "❌ Composite zip generation failed.")
                else:
                    send_message(chat_id, "❌ Session expired.")

            elif data.startswith("ext_bnd_"):
                mapping_id = data.replace("ext_bnd_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                
                meta = BOT_TEMP_MAPPINGS.get(mapping_id)
                if meta:
                    file_name = meta.get("filename", "bundle.unity3d")
                    target_url = meta.get("url")
                    ver = meta.get("version", "1.126.6")
                    
                    url = f"{TELEGRAM_API}/editMessageText"
                    http_session.post(url, json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": f"⏳ Extracting bundle: <b>{file_name}</b>...",
                        "parse_mode": "HTML"
                    }, timeout=10)
                    
                    sub_files = parse_bundle_subfiles(target_url)
                    if sub_files:
                        sub_names = "\n".join([f"│ ▫️ {idx:02d}. {sf.get('name') or sf.get('fileName')}" for idx, sf in enumerate(sub_files[:15], 1)])
                        if len(sub_files) > 15:
                            sub_names += f"\n│ ▫️ ... and {len(sub_files) - 15} more files."

                        keyboard = [
                            [
                                {"text": "📦 Download All as ZIP", "callback_data": f"ext_all_{mapping_id}"},
                                {"text": "🔍 Download Specific File", "callback_data": f"ext_spec_{mapping_id}"}
                            ],
                            [{"text": "⬅️ Back to Version Details", "callback_data": f"back_ver_{ver}"}]
                        ]
                        
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
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": keyboard}
                        }, timeout=10)
                    else:
                        keyboard = [[{"text": "⬅️ Back to Version Details", "callback_data": f"back_ver_{ver}"}]]
                        url = f"{TELEGRAM_API}/editMessageText"
                        http_session.post(url, json={
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "text": f"❌ Extraction failed or returned empty files for: <b>{file_name}</b>",
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": keyboard}
                        }, timeout=10)
                else:
                    send_message(chat_id, "❌ Session expired.")

            elif data.startswith("ext_all_"):
                mapping_id = data.replace("ext_all_", "")
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
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
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
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
                            send_extracted_document_to_chat(chat_id, file_bytes, fname)
                            
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
                http_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
                
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

            # Dynamic link check containing Android version or fileinfo link formats
            version = extract_version_from_input(raw_text)
            if version and ("dl.tata.freefiremobile.com" in raw_text or "dl-tata.freefireind.in" in raw_text or "fileinfo" in raw_text.lower() or "android/" in raw_text.lower() or "🟢 live link detected" in raw_text.lower()):
                keyboard = [[{"text": f"🔍 Check Miniupdate {version}", "callback_data": f"check_ver_{version}"}]]
                send_message_with_keyboard(chat_id, (
                    "╭──────────────────────\n"
                    "├ New MINIUPDATE Detected\n"
                    f"├ Version: <b>{version}</b>\n"
                    "╰──────────────────────"
                ), {"inline_keyboard": keyboard}, parse_mode="HTML")
                return jsonify({"status": "ok"}), 200

            # Access controls for private interaction commands
            if raw_text.startswith("/"):
                if not is_action_permitted(chat_id, user_id):
                    if raw_text.startswith("/start") or raw_text.startswith("/check") or raw_text.startswith("/extract"):
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

            elif raw_text.startswith("/check"):
                parts = [p.strip() for p in raw_text.split(" ") if p.strip()]
                if len(parts) < 2:
                    send_message(chat_id, "╭─❌ USAGE\n╰─/check [version / fileinfo URL]\n\nExample: /check 1.128.9")
                    return jsonify({"status": "ok"}), 200
                ver_input = parts[1].strip()
                ver = extract_version_from_input(ver_input)
                if not ver:
                    send_message(chat_id, "❌ Unable to extract a valid patch version string from the input.")
                    return jsonify({"status": "ok"}), 200
                
                # Send live in-progress state first to feel active and responsive
                sent_msg = send_message(chat_id, f"⏳ Checking miniupdate version <b>{ver}</b>...", parse_mode="HTML")
                if not sent_msg or not sent_msg.get("ok"):
                    return jsonify({"status": "ok"}), 200
                
                sent_msg_id = sent_msg["result"]["message_id"]
                
                try:
                    detail_msg, file_entries, file_keyboard_mappings = run_miniupdate_extraction_details(ver)
                    keyboard = []
                    
                    # Group model-textures Zip action button directly
                    for fname, action_data in file_keyboard_mappings.items():
                        if action_data.startswith("zip_comp_"):
                            comp_id = action_data.replace("zip_comp_", "")
                            meta = BOT_TEMP_MAPPINGS.get(comp_id)
                            if meta:
                                keyboard.append([{"text": f"📦 Combined {meta['name']} Scene ZIP", "callback_data": f"zip_comp_{comp_id}"}])
                                
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

                    # Trigger dynamic delivery of discovered item icons if item details present
                    new_items = extract_new_game_items(ver, get_file_info_data_exact(ver)[0])
                    if new_items:
                        send_item_icons_to_chat(chat_id, new_items)
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

    except Exception as e:
        print(f"Error handling Telegram message: {e}")
    return jsonify({"status": "ok"}), 200