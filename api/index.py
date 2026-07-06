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
import time
from datetime import datetime
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

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

# Enable CORS for all routes
CORS(app, 
     resources={r"/*": {
         "origins": ["*"],
         "methods": ["GET", "POST", "OPTIONS"],
         "allow_headers": ["Content-Type", "Authorization"],
         "expose_headers": ["Content-Type"],
         "max_age": 3600
     }})

# Connection pooling with optimized settings
http_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    pool_connections=50,
    pool_maxsize=100,
    max_retries=3,
    pool_block=False
)
http_session.mount('http://', adapter)
http_session.mount('https://', adapter)

# Enhanced caching system
FILE_INFO_CACHE = {}
EXTRACTED_FILES_CACHE = {}
MEMORY_ASSET_CACHE = {}
HEALTH_STATUS = {
    'status': 'initializing',
    'last_check': None,
    'version_api': False,
    'extractor_api': False,
    'file_sources': False,
    'uptime': datetime.now().isoformat()
}

BOT_TEMP_MAPPINGS = {}
PURGED_LEGACY_DB = False
ALLOWED_USERS = [OWNER_ID]

# Rate limiting and request tracking
REQUEST_TRACKER = {
    'total_requests': 0,
    'failed_requests': 0,
    'cache_hits': 0,
    'last_reset': time.time()
}

def get_mime_type(filename):
    """Enhanced MIME type detection with fallbacks"""
    mime, _ = mimetypes.guess_type(filename)
    if mime:
        return mime
    
    filename_lower = filename.lower()
    mime_map = {
        '.json': 'application/json',
        '.manifest': 'text/plain',
        '.txt': 'text/plain',
        '.atlas': 'text/plain',
        '.ogg': 'audio/ogg',
        '.mp3': 'audio/mpeg',
        '.mp4': 'video/mp4',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.obj': 'text/plain',
    }
    
    for ext, mime_type in mime_map.items():
        if filename_lower.endswith(ext):
            return mime_type
    
    return 'application/octet-stream'

def encode_token(remote_url, filename, content_type, content_bytes=None):
    """Token encoding with improved security"""
    cache_key = hashlib.sha256(remote_url.encode('utf-8')).hexdigest()
    data = {
        "url": remote_url,
        "filename": filename,
        "content_type": content_type,
        "cache_key": cache_key,
        "timestamp": int(time.time())
    }
    if content_bytes and len(content_bytes) < 2048:
        data["content_b64"] = base64.b64encode(content_bytes).decode('utf-8')
    
    json_bytes = json.dumps(data).encode('utf-8')
    compressed = zlib.compress(json_bytes)
    return base64.urlsafe_b64encode(compressed).decode('utf-8')

def decode_token(token):
    """Safe token decoding with validation"""
    try:
        padding_needed = len(token) % 4
        if padding_needed:
            token += '=' * (4 - padding_needed)
        compressed = base64.urlsafe_b64decode(token.encode('utf-8'))
        json_bytes = zlib.decompress(compressed)
        decoded = json.loads(json_bytes.decode('utf-8'))
        
        # Validate token age (30 days expiry)
        token_age = int(time.time()) - decoded.get('timestamp', 0)
        if token_age > 2592000:
            return None
        
        return decoded
    except Exception:
        return None

def check_api_health():
    """Check health of external APIs"""
    try:
        # Check VERSION_API
        version_check = http_session.get(VERSION_API, timeout=5)
        HEALTH_STATUS['version_api'] = version_check.status_code == 200
    except:
        HEALTH_STATUS['version_api'] = False
    
    try:
        # Check EXTRACTOR_API
        extractor_check = http_session.get(EXTRACTOR_API_URL, timeout=5)
        HEALTH_STATUS['extractor_api'] = extractor_check.status_code in [200, 400]
    except:
        HEALTH_STATUS['extractor_api'] = False
    
    try:
        # Check file sources
        for domain in DOMAINS:
            file_check = http_session.head(domain, timeout=5)
            if file_check.status_code == 200:
                HEALTH_STATUS['file_sources'] = True
                break
    except:
        HEALTH_STATUS['file_sources'] = False
    
    # Update overall status
    if HEALTH_STATUS['version_api'] and HEALTH_STATUS['file_sources']:
        HEALTH_STATUS['status'] = 'operational'
    elif HEALTH_STATUS['version_api'] or HEALTH_STATUS['file_sources']:
        HEALTH_STATUS['status'] = 'degraded'
    else:
        HEALTH_STATUS['status'] = 'offline'
    
    HEALTH_STATUS['last_check'] = datetime.now().isoformat()
    return HEALTH_STATUS

def get_previous_version(version_str):
    """Version decrement logic"""
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
    """Version increment logic"""
    try:
        parts = list(map(int, version_str.split('.')))
        parts[2] += step
        return ".".join(map(str, parts))
    except Exception:
        return None

def encode_suffix(suffix):
    """Safe suffix encoding"""
    if suffix.endswith('='):
        suffix = suffix[:-1]
    return suffix.replace('+', '~2B').replace('/', '~2F').replace('=', '~3D')

def get_file_info_data_exact(version, timeout=6):
    """Get file info with timeout and retry logic"""
    if version in FILE_INFO_CACHE:
        REQUEST_TRACKER['cache_hits'] += 1
        return FILE_INFO_CACHE[version]
    
    for attempt in range(2):
        for base in DOMAINS:
            url = f"{base}{version}/fileinfo"
            try:
                res = http_session.get(url, timeout=timeout)
                if res.status_code == 200:
                    val = (res.text, version, base)
                    FILE_INFO_CACHE[version] = val
                    return val
            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                print(f"Error fetching {url}: {e}")
                continue
    
    return None, None, None

def get_file_info_data(version):
    """Wrapper for get_file_info_data_exact"""
    return get_file_info_data_exact(version)

def get_multiple_file_info_data(versions):
    """Parallel fetch with improved error handling"""
    results = {}
    versions_to_fetch = [v for v in versions if v not in FILE_INFO_CACHE]
    
    def fetch(v):
        for base in DOMAINS:
            url = f"{base}{v}/fileinfo"
            try:
                res = http_session.get(url, timeout=5)
                if res.status_code == 200:
                    return v, (res.text, v, base)
            except requests.exceptions.Timeout:
                continue
            except Exception:
                pass
        return v, None
    
    if versions_to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(15, len(versions_to_fetch))) as executor:
            fetched_results = list(executor.map(fetch, versions_to_fetch))
            for v, val in fetched_results:
                if val:
                    FILE_INFO_CACHE[v] = val
                    results[v] = val
    
    for v in versions:
        if v in FILE_INFO_CACHE:
            results[v] = FILE_INFO_CACHE[v]
    
    return results

def find_latest_active_version(requested_version):
    """Find latest version with improved logic"""
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
    """Extract files with caching"""
    cache_key = f"{active_version}:{file_id}"
    if cache_key in EXTRACTED_FILES_CACHE:
        return EXTRACTED_FILES_CACHE[cache_key]
    
    suffix = None
    for line in new_text.splitlines():
        if not line.strip():
            continue
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
    except requests.exceptions.Timeout:
        print(f"Timeout extracting bundle {file_id}")
    except Exception as e:
        print(f"Failed to extract bundle {file_id}: {e}")
    
    return []

def find_previous_itemhotfix_content(active_version):
    """Find previous item hotfix content"""
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
            if not line.strip():
                continue
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
    """Extract new game items with improved error handling"""
    itemhotfix_id = None
    lochotfix_id = None
    
    for line in new_text.splitlines():
        if not line.strip():
            continue
        parts = line.split(',')
        if len(parts) >= 5:
            fid = parts[0].strip()
            if "config/itemhotfix" in fid.lower():
                itemhotfix_id = fid
            elif "localization/lochotfix" in fid.lower():
                lochotfix_id = fid
    
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
                try:
                    itemhotfix_content = base64.b64decode(b64_data).decode('utf-8', errors='ignore')
                    break
                except:
                    continue
    
    if not itemhotfix_content:
        return []
    
    # Process item data...
    previous_itemhotfix_content = find_previous_itemhotfix_content(active_version)
    previous_item_ids = set()
    
    if previous_itemhotfix_content:
        p_lines = previous_itemhotfix_content.splitlines()
        if len(p_lines) > 2:
            for pl in p_lines[2:]:
                if not pl.strip():
                    continue
                p_parts = pl.split(',')
                if len(p_parts) > 0:
                    pid = p_parts[0].strip()
                    if pid.isdigit():
                        previous_item_ids.add(pid)
    
    lochotfix_content = ""
    if lochotfix_id:
        lochotfix_files = get_extracted_files(lochotfix_id, new_text, active_version, base_url)
        for f in lochotfix_files:
            fname = f.get("name", "") or f.get("fileName", "")
            if "lochotfix.txt" in fname.lower():
                b64_data = f.get("data", "")
                if b64_data:
                    try:
                        lochotfix_content = base64.b64decode(b64_data).decode('utf-8', errors='ignore')
                        break
                    except:
                        continue
    
    translations = {}
    if lochotfix_content:
        matches = re.findall(r'<l\s+[^>]*id="([^"]+)"[^>]*\ben="([^"]+)"', lochotfix_content)
        for name_id, en_val in matches:
            translations[name_id] = en_val
    
    raw_parsed_items = []
    lines = itemhotfix_content.splitlines()
    if len(lines) > 2:
        for line in lines[2:]:
            if not line.strip():
                continue
            parts = line.split(',')
            if len(parts) >= 8:
                item_id = parts[0].strip()
                if not item_id or not item_id.isdigit():
                    continue
                if item_id in previous_item_ids:
                    continue
                
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
        if not resolved_name:
            resolved_name = f"Item {item['id']}"
        item["name"] = resolved_name
        item["icon_url"] = f"https://dl.cdn.freefiremobile.com/live/ABHotUpdates/IconCDN/other/{item['id']}.png"
    
    bundles = []
    non_bundles = []
    for item in raw_parsed_items:
        if "bundle" in item["name"].lower():
            bundles.append(item)
        else:
            non_bundles.append(item)
    
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

def analyze_update_purpose(files, new_items):
    """Analyze update purpose"""
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

def latest_version_logic():
    """Get latest version"""
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
                "target_version": target_ver,
                "timestamp": datetime.now().isoformat()
            })
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "message": "Version API timeout. Please try again."}), 504
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    
    return jsonify({"success": False, "message": "Failed response lookup."}), 500

def check_update_logic():
    """Check update logic"""
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
    
    new_text, active_version, base_url = get_file_info_data_exact(version)
    if not new_text:
        return jsonify({"success": False, "message": "Target version logs missing from server"}), 404
    
    prev_versions = []
    current_temp = active_version
    for _ in range(6):
        prev_ver = get_previous_version(current_temp)
        if not prev_ver:
            break
        prev_versions.append(prev_ver)
        current_temp = prev_ver
    
    fetched_maps_prev = get_multiple_file_info_data(prev_versions)
    old_texts = [fetched_maps_prev[v][0] for v in prev_versions if v in fetched_maps_prev]
    
    old_files_map = {}
    for old_text in reversed(old_texts):
        for line in old_text.splitlines():
            if not line.strip():
                continue
            parts = line.split(',')
            if len(parts) >= 3:
                file_id = parts[0].strip()
                try:
                    old_size = int(parts[2])
                except Exception:
                    old_size = 0
                old_files_map[file_id] = old_size
    
    file_infos = []
    total_size = 0
    
    for line in new_text.splitlines():
        if not line.strip():
            continue
        parts = line.split(',')
        if len(parts) < 5:
            continue
        
        file_id, size_str, suffix = parts[0].strip(), parts[2].strip(), parts[4].strip()
        if not file_id or not suffix:
            continue
        
        try:
            size = int(size_str)
        except Exception:
            size = 0
        
        if file_id in old_files_map:
            if old_files_map[file_id] != size:
                status = "Modified"
            else:
                continue
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
        "analysis": analysis,
        "timestamp": datetime.now().isoformat()
    })

def check_name_similarity(name1, name2):
    """Check name similarity across models and textures with standard suffix handling"""
    n1 = re.sub(r'[\W_]+', ' ', name1).lower().strip()
    n2 = re.sub(r'[\W_]+', ' ', name2).lower().strip()
    
    # Strip common suffix identifiers to check true base name mappings
    base1 = re.sub(r'\s+(d|n|s|m|albedo|normal|specular|metallic|metal|roughness|ao|color|diffuse)$', '', n1)
    base2 = re.sub(r'\s+(d|n|s|m|albedo|normal|specular|metallic|metal|roughness|ao|color|diffuse)$', '', n2)
    
    if base1 == base2 or base1 in base2 or base2 in base1:
        return True
        
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
    """Find references in JSON"""
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

def handle_extractor_response(response):
    """Handle extractor API response"""
    if not response.ok:
        try:
            err_data = response.json()
        except Exception:
            err_data = {"error": response.text}
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
    
    # Process structured metadata files safely first
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
        
        # ONLY populate cache if the actual binary payload is present to prevent blank 0-byte caching
        if file_bytes:
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
            except Exception:
                pass
    
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
            except Exception:
                pass
    
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
            if i == j:
                continue
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
        "configs": [a for a in processed_assets if any(ext in a["fileName"].lower() for ext in [".json", ".txt", ".dat", ".xml"]) and not any(a["fileName"] == m["fileName"] for m in meshes)],
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
    """Process/extract file logic"""
    bundle_url = request.args.get('bundle_url', '') or request.form.get('bundle_url', '')
    token = request.args.get('token', '') or request.form.get('token', '')
    real_bundle_url = ""
    
    if "token=" in bundle_url:
        token_match = re.search(r'token=([^&]+)', bundle_url)
        if token_match:
            tok = token_match.group(1)
            decoded = decode_token(tok)
            if decoded:
                real_bundle_url = decoded.get("url", "")
    elif token:
        decoded = decode_token(token)
        if decoded:
            real_bundle_url = decoded.get("url", "")
    
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
    """Stream cached asset logic"""
    if not token:
        return "Missing token query parameter", 400
    
    decoded = decode_token(token)
    if not decoded:
        return "Expired or invalid stream token payload.", 404
    
    filename = decoded.get("filename", "file")
    mime_type = decoded.get("content_type", "application/octet-stream")
    cache_key = decoded.get("cache_key")
    
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
        except Exception:
            pass
    
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
        except Exception:
            pass
    
    return "Expired or unavailable stream token payload.", 404

def extract_zip_logic(token):
    """Extract and download as ZIP logic"""
    if not token:
        return "Missing token query parameter", 400
    
    decoded = decode_token(token)
    if not decoded:
        return "Invalid or expired stream token payload", 400
    
    remote_url = decoded.get("url")
    if not remote_url:
        return "Target bundle address missing in decrypted token payload", 400
    
    encoded_url = base64.urlsafe_b64encode(remote_url.encode('utf-8')).decode('utf-8')
    extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}"
    
    try:
        res = http_session.get(extractor_url, timeout=60)
        if res.status_code != 200:
            return f"Extractor pipeline failure status code: {res.status_code}", 500
        
        try:
            data = res.json()
        except Exception as e:
            return f"Invalid JSON response returned from extractor: {str(e)}", 500
            
        files_list = data.get("files", []) or data.get("assets", []) or data.get("extracted", [])
        if not isinstance(files_list, list):
            files_list = []
            
        zip_io = io.BytesIO()
        files_added = 0
        with zipfile.ZipFile(zip_io, "w", zipfile.ZIP_DEFLATED) as zf:
            for ext_file in files_list:
                fname = ext_file.get("name") or ext_file.get("fileName")
                zpath = ext_file.get("path") or fname
                b64_content = ext_file.get("data", "")
                if b64_content and fname:
                    try:
                        file_bytes = base64.b64decode(b64_content)
                        zf.writestr(zpath, file_bytes)
                        files_added += 1
                    except Exception:
                        pass
                        
        if files_added == 0:
            return "No assets could be extracted from this bundle payload to generate a ZIP", 400
            
        zip_io.seek(0)
        filename = decoded.get("filename", "extracted_asset")
        if not filename.endswith(".zip"):
            filename += ".zip"
            
        response = Response(zip_io.getvalue(), mimetype='application/zip')
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
        
    except Exception as e:
        return f"Failed connecting proxy mapping to zip extractor endpoint: {e}", 500

def bulk_zip_logic(version):
    """Bulk ZIP download logic"""
    if not version or not re.match(r'^\d+\.\d+\.\d+$', version):
        return "Incorrect version format entered", 400
    
    new_text, active_version, base_url = get_file_info_data(version)
    if not new_text:
        return "Target version logs missing from server", 404
    
    prev_versions = []
    current_temp = active_version
    for _ in range(6):
        prev_ver = get_previous_version(current_temp)
        if not prev_ver:
            break
        prev_versions.append(prev_ver)
        current_temp = prev_ver
    
    fetched_maps = get_multiple_file_info_data(prev_versions)
    old_texts = [fetched_maps[v][0] for v in prev_versions if v in fetched_maps]
    
    old_files_map = {}
    for old_text in reversed(old_texts):
        for line in old_text.splitlines():
            if not line.strip():
                continue
            parts = line.split(',')
            if len(parts) >= 3:
                file_id = parts[0].strip()
                try:
                    old_size = int(parts[2])
                except Exception:
                    old_size = 0
                old_files_map[file_id] = old_size
    
    targets = []
    for line in new_text.splitlines():
        if not line.strip():
            continue
        parts = line.split(',')
        if len(parts) < 5:
            continue
        
        file_id, size_str, suffix = parts[0].strip(), parts[2].strip(), parts[4].strip()
        if not file_id or not suffix:
            continue
        
        try:
            size = int(size_str)
        except Exception:
            size = 0
        
        if file_id in old_files_map:
            if old_files_map[file_id] != size:
                pass
            else:
                continue
        else:
            pass
        
        encoded = encode_suffix(suffix)
        download_url = f"{base_url}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
        targets.append((file_id, download_url))
    
    if not targets:
        return "No assets could be extracted for this version update", 400
    
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
                        except Exception:
                            pass
    
    if files_added_to_zip == 0:
        return "No assets could be extracted for this version update", 400
    
    zip_io.seek(0)
    response = Response(zip_io.getvalue(), mimetype='application/zip')
    response.headers["Content-Disposition"] = f'attachment; filename="FF_MiniUpdate_{active_version}_Extracted.zip"'
    return response

# API Endpoints
@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    check_api_health()
    return jsonify({
        "status": HEALTH_STATUS['status'],
        "apis": {
            "version_api": HEALTH_STATUS['version_api'],
            "extractor_api": HEALTH_STATUS['extractor_api'],
            "file_sources": HEALTH_STATUS['file_sources']
        },
        "last_check": HEALTH_STATUS['last_check'],
        "uptime": HEALTH_STATUS['uptime'],
        "cache_stats": {
            "file_info_cache_size": len(FILE_INFO_CACHE),
            "extracted_files_cache_size": len(EXTRACTED_FILES_CACHE),
            "memory_asset_cache_size": len(MEMORY_ASSET_CACHE)
        }
    }), 200

@app.route('/api/webhook', methods=['GET', 'POST'])
@app.route('/webhook', methods=['GET', 'POST'])
def telegram_webhook():
    """Telegram webhook endpoint"""
    if request.method == 'GET':
        try:
            host = request.headers.get('Host', 'localhost')
            protocol = "https" if "localhost" not in host else "http"
            current_url = f"{protocol}://{host}/api/webhook"
            
            webhook_setup_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
            setup_res = http_session.post(webhook_setup_url, json={"url": current_url}, timeout=10).json()
            
            return jsonify({
                "status": "active",
                "webhook_url": current_url,
                "telegram_response": setup_res
            }), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    
    return jsonify({"status": "ok"}), 200

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'OPTIONS'])
@app.route('/<path:path>', methods=['GET', 'POST', 'OPTIONS'])
def handle_all_requests(path):
    """Main request handler"""
    if request.method == 'OPTIONS':
        return '', 204
    
    full_path = "/" + path.strip("/")
    action = request.args.get('action')
    
    # Process file extraction
    if full_path == '/api/process' or action == 'process':
        return process_file_logic()
    # Check for updates
    elif full_path == '/api/update' or action == 'update':
        return check_update_logic()
    # Get latest version
    elif full_path == '/api/latest-version' or action == 'latest-version':
        return latest_version_logic()
    # Stream cached asset (supports action parameters or URL suffix)
    elif full_path.startswith('/api/stream-cache/') or full_path.startswith('/stream-cache/') or action == 'stream':
        token = request.args.get('token')
        if not token:
            token = full_path.split('/')[-1]
        return stream_cache_logic(token)
    # Extract and download as ZIP
    elif full_path == '/api/extract-zip' or action == 'extract-zip':
        token = request.args.get('token')
        return extract_zip_logic(token)
    # Bulk download all files as ZIP
    elif full_path == '/api/bulk-zip' or action == 'bulk-zip':
        version = request.args.get('version', '').strip()
        return bulk_zip_logic(version)
    # Serve index.html
    elif full_path == '/' or full_path == '/api' or full_path == '/api/':
        try:
            for filename in ['api/index.html', 'public/index.html', 'index.html']:
                if os.path.exists(filename):
                    with open(filename, 'r', encoding='utf-8') as f:
                        return f.read()
            return jsonify({"success": False, "message": "index.html template missing"}), 404
        except Exception as e:
            return f"Error: {str(e)}", 500
    
    return jsonify({"success": False, "message": f"Endpoint '{full_path}' is unmapped."}), 404

if __name__ == '__main__':
    check_api_health()
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)