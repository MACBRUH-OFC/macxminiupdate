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
from flask import Flask, jsonify, request, Response, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_URL = 'https://dl.tata.freefiremobile.com/live/ABHotUpdates/android/'
VERSION_API = 'https://ff-version.vercel.app/update'
EXTRACTOR_API_URL = "https://ff-asset-extractor.vercel.app/api/extract"

# Local Persistent Session Cache Map fallback
MEMORY_ASSET_CACHE = {}

def encode_token(remote_url, filename, content_type, content_bytes=None):
    data = {
        "url": remote_url,
        "filename": filename,
        "content_type": content_type
    }
    # Compress and embed content if it is small (under 100KB) to ensure statelessness on Vercel
    if content_bytes and len(content_bytes) < 100000:
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
    except Exception as e:
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
    except:
        return None

def encode_suffix(suffix):
    if suffix.endswith('='):
        suffix = suffix[:-1]
    return suffix.replace('+', '~2B').replace('/', '~2F').replace('=', '~3D')

def get_file_info_data(version):
    url = f"{BASE_URL}{version}/fileinfo"
    try:
        res = requests.get(url, timeout=6)
        if res.status_code == 200:
            return res.text, version
    except:
        pass
    return None, None

def get_extracted_files(file_id, new_text, active_version):
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
    download_url = f"{BASE_URL}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
    try:
        encoded_url = base64.urlsafe_b64encode(download_url.encode('utf-8')).decode('utf-8')
        extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}"
        res = requests.get(extractor_url, timeout=30)
        if res.status_code == 200:
            data = res.json()
            return data.get("files", []) or data.get("assets", [])
    except Exception as e:
        print(f"Failed to extract bundle {file_id}: {e}")
    return []

def find_previous_itemhotfix_content(active_version):
    parts = active_version.split('.')
    if len(parts) != 3:
        return ""
    
    major, middle, minor = parts[0], parts[1], int(parts[2])
    
    # Trace back sequentially to version index 1 to locate the preceding build with itemhotfix [2]
    for m_val in range(minor - 1, 0, -1):
        prev_ver = f"{major}.{middle}.{m_val}"
        prev_text, _ = get_file_info_data(prev_ver)
        if not prev_text:
            continue
            
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
            prev_files = get_extracted_files(prev_itemhotfix_id, prev_text, prev_ver)
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
            if "config/itemhotfix" in fid.lower():
                itemhotfix_id = fid
            elif "localization/lochotfix" in fid.lower():
                lochotfix_id = fid

    if not itemhotfix_id:
        return []

    itemhotfix_files = get_extracted_files(itemhotfix_id, new_text, active_version)
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

    # Get the older itemhotfix data to isolate only the current unique additions [2]
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
                    if pid.isdigit():
                        previous_item_ids.add(pid)

    lochotfix_content = ""
    if lochotfix_id:
        lochotfix_files = get_extracted_files(lochotfix_id, new_text, active_version)
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
                if not item_id or not item_id.isdigit():
                    continue
                
                # Filter out historical item IDs
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

def latest_version_logic():
    try:
        res = requests.get(VERSION_API, timeout=5)
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

    new_text, active_version = get_file_info_data(version)

    if not new_text:
        fallback_ver = get_previous_version(version)
        new_text, active_version = get_file_info_data(fallback_ver)
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

    old_texts = []
    for pv in prev_versions:
        txt, _ = get_file_info_data(pv)
        if txt:
            old_texts.append(txt)

    old_files_map = {}
    for old_text in reversed(old_texts):
        for line in old_text.splitlines():
            if not line.strip(): continue
            parts = line.split(',')
            if len(parts) >= 3:
                file_id = parts[0].strip()
                try:
                    old_size = int(parts[2])
                except:
                    old_size = 0
                old_files_map[file_id] = old_size

    file_infos = []
    total_size = 0

    for line in new_text.splitlines():
        if not line.strip(): continue
        parts = line.split(',')
        if len(parts) < 5: continue

        file_id, size_str, suffix = parts[0].strip(), parts[2].strip(), parts[4].strip()
        if not file_id or not suffix:
            continue

        try: 
            size = int(size_str)
        except: 
            size = 0

        if file_id in old_files_map:
            if old_files_map[file_id] != size:
                status = "Modified"
            else:
                continue
        else:
            status = "New"

        encoded = encode_suffix(suffix)
        download_url = f"{BASE_URL}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
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
        try:
            err_data = response.json()
        except:
            err_data = {"error": response.text}
        return jsonify(err_data), response.status_code

    extractor_data = response.json()
    assets_list = extractor_data.get("assets", []) or extractor_data.get("files", [])

    for asset in assets_list:
        remote_preview_url = asset.get("preview_url") or asset.get("url")
        if not remote_preview_url:
            continue

        if remote_preview_url.startswith('/'):
            remote_preview_url = "https://ff-asset-extractor.vercel.app" + remote_preview_url

        filename_str = asset.get("name") or asset.get("fileName") or "extracted_file"
        
        detected_content_type = 'application/octet-stream'
        if filename_str.endswith('.json'):
            detected_content_type = 'application/json'
        elif filename_str.endswith('.manifest') or filename_str.endswith('.txt') or filename_str.endswith('.atlas'):
            detected_content_type = 'text/plain'
        elif filename_str.endswith('.ogg'):
            detected_content_type = 'audio/ogg'
        elif filename_str.endswith('.mp3'):
            detected_content_type = 'audio/mpeg'
        elif filename_str.endswith('.mp4'):
            detected_content_type = 'video/mp4'

        b64_content = asset.get("data", "")
        file_bytes = base64.b64decode(b64_content) if b64_content else b""

        local_cache_token = encode_token(remote_preview_url, filename_str, detected_content_type, file_bytes)

        global MEMORY_ASSET_CACHE
        MEMORY_ASSET_CACHE[local_cache_token] = {
            "content": file_bytes,
            "filename": filename_str,
            "content_type": detected_content_type
        }

        local_proxy_route = f"/api?action=stream&token={local_cache_token}"
        if "preview_url" in asset: asset["preview_url"] = local_proxy_route
        if "url" in asset: asset["url"] = local_proxy_route

    # Populate matching keys to shield extraction layouts against missing elements [1]
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
                response = requests.post(EXTRACTOR_API_URL, files=files, data=data, timeout=45)
                return handle_extractor_response(response)
            except Exception as e:
                return jsonify({"success": False, "message": f"Failed to connect to extractor: {str(e)}"}), 500

    if real_bundle_url:
        try:
            encoded_url = base64.urlsafe_b64encode(real_bundle_url.encode('utf-8')).decode('utf-8')
            extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}"
            response = requests.get(extractor_url, timeout=45)
            return handle_extractor_response(response)
        except Exception as e:
            return jsonify({"success": False, "message": f"Failed to fetch from extractor: {str(e)}"}), 500

    return jsonify({"success": False, "message": "No file uploaded or bundle URL provided"}), 400

def stream_cache_logic(token):
    if not token:
        return "Missing token query parameter", 400

    decoded = decode_token(token)
    if decoded:
        filename = decoded.get("filename", "file")
        mime_type = decoded.get("content_type", "application/octet-stream")
        
        if "content_b64" in decoded:
            try:
                content = base64.b64decode(decoded["content_b64"])
                response = Response(content, mimetype=mime_type)
                response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
                response.headers["Accept-Ranges"] = "bytes"
                response.headers["Access-Control-Allow-Origin"] = "*"
                return response
            except:
                pass

        remote_url = decoded.get("url")
        if remote_url:
            try:
                res = requests.get(remote_url, timeout=15)
                if res.status_code == 200:
                    response = Response(res.content, mimetype=mime_type)
                    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
                    response.headers["Accept-Ranges"] = "bytes"
                    response.headers["Access-Control-Allow-Origin"] = "*"
                    return response
            except Exception as e:
                pass

    global MEMORY_ASSET_CACHE
    cached_item = MEMORY_ASSET_CACHE.get(token)

    if not cached_item:
        return "Expired or invalid stream token payload.", 404

    mime_type = cached_item["content_type"]
    response = Response(cached_item["content"], mimetype=mime_type)
    response.headers["Content-Disposition"] = f'inline; filename="{cached_item["filename"]}"'
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

def extract_zip_logic(token):
    if not token:
        return "Missing token query parameter", 400
    
    decoded = decode_token(token)
    if not decoded:
        return "Invalid or expired stream token payload", 400
    
    remote_url = decoded.get("url")
    if not remote_url:
        return "Target bundle address missing in decrypted token payload", 400
        
    encoded_url = base64.urlsafe_b64encode(remote_url.encode('utf-8')).decode('utf-8')
    extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}&download_type=zip"
    
    try:
        res = requests.get(extractor_url, timeout=60, stream=True)
        if res.status_code == 200:
            filename = decoded.get("filename", "extracted_asset") + ".zip"
            def generate():
                for chunk in res.iter_content(chunk_size=8192):
                    yield chunk
            response = Response(generate(), mimetype='application/zip')
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response
        else:
            return f"Extractor pipeline failure status code: {res.status_code}", 500
    except Exception as e:
        return f"Failed connecting proxy mapping to zip extractor endpoint: {e}", 500

def bulk_zip_logic(version):
    if not version or not re.match(r'^\d+\.\d+\.\d+$', version):
        return "Incorrect version format entered", 400

    new_text, active_version = get_file_info_data(version)
    if not new_text:
        fallback_ver = get_previous_version(version)
        new_text, active_version = get_file_info_data(fallback_ver)
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

    old_texts = []
    for pv in prev_versions:
        txt, _ = get_file_info_data(pv)
        if txt:
            old_texts.append(txt)

    old_files_map = {}
    for old_text in reversed(old_texts):
        for line in old_text.splitlines():
            if not line.strip(): continue
            parts = line.split(',')
            if len(parts) >= 3:
                file_id = parts[0].strip()
                try: old_size = int(parts[2])
                except: old_size = 0
                old_files_map[file_id] = old_size

    zip_io = io.BytesIO()
    files_added_to_zip = 0
    
    with zipfile.ZipFile(zip_io, "w", zipfile.ZIP_DEFLATED) as zf:
        for line in new_text.splitlines():
            if not line.strip(): continue
            parts = line.split(',')
            if len(parts) < 5: continue

            file_id, size_str, suffix = parts[0].strip(), parts[2].strip(), parts[4].strip()
            if not file_id or not suffix: continue

            try: size = int(size_str)
            except: size = 0

            # Filter modified/new files
            if file_id in old_files_map:
                if old_files_map[file_id] != size:
                    pass
                else:
                    continue
            else:
                pass

            encoded = encode_suffix(suffix)
            download_url = f"{BASE_URL}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
            
            try:
                # Call extractor statelessly using the remote URL
                encoded_url = base64.urlsafe_b64encode(download_url.encode('utf-8')).decode('utf-8')
                extractor_url = f"{EXTRACTOR_API_URL}?bundle_url={encoded_url}"
                res = requests.get(extractor_url, timeout=45)
                if res.status_code == 200:
                    data = res.json()
                    extracted_files = data.get("files", [])
                    for ext_file in extracted_files:
                        fname = ext_file.get("name")
                        zpath = ext_file.get("path")
                        b64_content = ext_file.get("data", "")
                        if b64_content and fname:
                            file_bytes = base64.b64decode(b64_content)
                            zf.writestr(f"{file_id}_extracted/{zpath}", file_bytes)
                            files_added_to_zip += 1
            except Exception as e:
                print(f"Failed to extract bundle {file_id}: {e}")
                
    if files_added_to_zip == 0:
        return "No assets could be extracted for this version update", 400
        
    zip_io.seek(0)
    response = Response(zip_io.getvalue(), mimetype='application/zip')
    response.headers["Content-Disposition"] = f'attachment; filename="FF_MiniUpdate_{active_version}_Extracted.zip"'
    return response

@app.route('/', methods=['GET', 'POST'])
@app.route('/api', methods=['GET', 'POST'])
def handle_all_requests():
    action = request.args.get('action')
    
    if not action:
        path = request.path.rstrip('/')
        if path == '/api/process' or path == '/process':
            return process_file_logic()
        elif path == '/api/latest-version' or path == '/latest-version':
            return latest_version_logic()
        elif path == '/api/update' or path == '/update':
            return check_update_logic()
        elif path == '/api/bulk-zip' or path == '/bulk-zip':
            version = request.args.get('version', '').strip()
            return bulk_zip_logic(version)
        elif path.startswith('/api/stream-cache/') or path.startswith('/stream-cache/'):
            token = path.split('/')[-1]
            return stream_cache_logic(token)
        elif path == '/api/extract-zip' or path == '/extract-zip':
            token = request.args.get('token')
            return extract_zip_logic(token)
        
        # Serves the static landing page
        if request.method == 'GET' and (path == '' or path == '/api'):
            try:
                for filename in ['public/index.html', 'index.html']:
                    if os.path.exists(filename):
                        with open(filename, 'r', encoding='utf-8') as f:
                            return f.read()
                return jsonify({"success": False, "message": "index.html template missing"}), 404
            except Exception as e:
                return f"Error: {str(e)}", 500

    if action == 'latest-version':
        return latest_version_logic()
    elif action == 'update':
        return check_update_logic()
    elif action == 'process':
        return process_file_logic()
    elif action == 'stream':
        return stream_cache_logic(request.args.get('token'))
    elif action == 'bulk-zip':
        return bulk_zip_logic(request.args.get('version'))
    elif action == 'extract-zip':
        return extract_zip_logic(request.args.get('token'))

    return jsonify({"success": False, "message": f"Action parameter '{action}' is unmapped."}), 404

if __name__ == '__main__':
    app.run(port=5000, debug=True)