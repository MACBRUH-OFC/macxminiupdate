from flask import Flask, jsonify, request, Response, send_file
from flask_cors import CORS
import requests
import re
import uuid
import mimetypes
import base64
import json
import zlib
import io
import zipfile
import os

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

def find_file_in_info(fileinfo_text, search_id):
    if not fileinfo_text:
        return None, None
    for line in fileinfo_text.splitlines():
        if not line.strip(): 
            continue
        parts = line.split(',')
        if len(parts) >= 5 and parts[0].strip() == search_id:
            return parts[0].strip(), parts[4].strip()
    return None, None

def extract_hotfix_file(version, file_id, suffix):
    encoded = encode_suffix(suffix)
    download_url = f"{BASE_URL}{version}/gameassetbundles/{file_id}.{encoded}~3D"
    try:
        res = requests.get(download_url, timeout=15)
        if res.status_code == 200:
            files = {
                'asset_bundle': (file_id.split('/')[-1], res.content, 'application/octet-stream')
            }
            data = {
                'bundle_url': download_url
            }
            response = requests.post(EXTRACTOR_API_URL, files=files, data=data, timeout=35)
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        print(f"Hotfix extraction failure {file_id}: {e}")
    return None

def get_extracted_file_content(extractor_data, target_name):
    if not extractor_data:
        return None
    assets_list = extractor_data.get("assets", []) or extractor_data.get("files", [])
    for asset in assets_list:
        fname = asset.get("name") or asset.get("fileName") or ""
        if target_name.lower() in fname.lower():
            b64_content = asset.get("data", "")
            if b64_content:
                try:
                    return base64.b64decode(b64_content).decode('utf-8', errors='ignore')
                except Exception as e:
                    print(f"Failed decoding content: {e}")
    return None

def parse_itemhotfix(text):
    items = []
    if not text:
        return items
    for line in text.splitlines():
        line = line.strip()
        if not line or "unread" in line or line.startswith("Id,"):
            continue
        parts = line.split(',')
        if len(parts) >= 10:
            item_id = parts[0].strip()
            text_id = ""
            if len(parts) > 7 and parts[7].strip():
                text_id = parts[7].strip()
            elif len(parts) > 5 and parts[5].strip():
                text_id = parts[5].strip()
            
            icon_id = parts[9].strip() if len(parts) > 9 else ""
            if item_id:
                items.append({
                    "id": item_id,
                    "text_id": text_id,
                    "icon_id": icon_id
                })
    return items

def parse_lochotfix(text):
    translations = {}
    if not text:
        return translations
    pattern = re.compile(r'<l\s+id="([^"]+)"[^>]*\ben="([^"]*)"')
    for match in pattern.finditer(text):
        tid = match.group(1).strip()
        val = match.group(2).strip()
        translations[tid] = val
    return translations

def compile_added_items(items, translations):
    compiled = []
    for item in items:
        text_id = item["text_id"]
        resolved_name = translations.get(text_id)
        if not resolved_name:
            resolved_name = text_id if text_id else f"Item {item['id']}"
        item["name"] = resolved_name

    bundles = []
    individual_parts = []
    
    for item in items:
        if "bundle" in item["name"].lower():
            item["parts"] = []
            bundles.append(item)
        else:
            individual_parts.append(item)

    bundled_item_ids = set()
    for b in bundles:
        base_name = re.sub(r'\s+bundle$', '', b["name"], flags=re.IGNORECASE).strip()
        for p in individual_parts:
            if p["name"].lower().startswith(base_name.lower()):
                b["parts"].append({
                    "id": p["id"],
                    "name": p["name"],
                    "icon_id": p["icon_id"]
                })
                bundled_item_ids.add(p["id"])

    standalone_items = []
    for p in individual_parts:
        if p["id"] not in bundled_item_ids:
            standalone_items.append(p)

    for b in bundles:
        compiled.append({
            "id": b["id"],
            "name": b["name"],
            "icon_id": b["icon_id"],
            "is_bundle": True,
            "parts": b["parts"]
        })
        
    for s in standalone_items:
        compiled.append({
            "id": s["id"],
            "name": s["name"],
            "icon_id": s["icon_id"],
            "is_bundle": False,
            "parts": []
        })

    return compiled

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

    # Fetch and combine up to 6 historical fileinfo databases
    prev_versions = []
    curr = active_version
    for _ in range(6):
        prev = get_previous_version(curr)
        if not prev:
            break
        prev_versions.append(prev)
        curr = prev

    old_files_map = {}
    for prev_ver in prev_versions:
        old_text, _ = get_file_info_data(prev_ver)
        if old_text:
            for line in old_text.splitlines():
                if not line.strip(): 
                    continue
                parts = line.split(',')
                if len(parts) >= 3:
                    file_id = parts[0]
                    try:
                        old_size = int(parts[2])
                    except:
                        old_size = 0
                    if file_id not in old_files_map:
                        old_files_map[file_id] = set()
                    old_files_map[file_id].add(old_size)

    file_infos = []
    total_size = 0

    for line in new_text.splitlines():
        if not line.strip(): 
            continue
        parts = line.split(',')
        if len(parts) < 5: 
            continue

        file_id, size_str, suffix = parts[0], parts[2], parts[4]
        if not file_id or not suffix:
            continue

        try: 
            size = int(size_str)
        except: 
            size = 0

        # Run size differential logic across all loaded past logs
        if file_id in old_files_map:
            if size not in old_files_map[file_id]:
                status = "Modified"
            else:
                continue
        else:
            status = "New"

        encoded = encode_suffix(suffix)
        download_url = f"{BASE_URL}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
        file_name = file_id.split('/')[-1]

        # Obfuscate CDN routes by issuing stateless proxy action tokens
        token_payload = encode_token(download_url, file_name, 'application/octet-stream')
        local_proxy_download_url = f"/api?action=download-bundle&token={token_payload}"

        total_size += size
        file_infos.append({
            "fileName": file_name,
            "size": size,
            "url": local_proxy_download_url,
            "token": token_payload,
            "status": status
        })

    # Added Items scanner execution
    added_items = []
    item_file_id, item_suffix = find_file_in_info(new_text, "config/itemhotfix")
    loc_file_id, loc_suffix = find_file_in_info(new_text, "localization/lochotfix")
    
    if item_file_id and loc_file_id:
        item_data = extract_hotfix_file(active_version, item_file_id, item_suffix)
        loc_data = extract_hotfix_file(active_version, loc_file_id, loc_suffix)
        
        item_text = get_extracted_file_content(item_data, "itemhotfix.txt")
        loc_text = get_extracted_file_content(loc_data, "lochotfix.txt")
        
        if item_text and loc_text:
            parsed_items = parse_itemhotfix(item_text)
            translations = parse_lochotfix(loc_text)
            added_items = compile_added_items(parsed_items, translations)

    return jsonify({
        "success": True,
        "version": active_version,
        "totalSize": total_size,
        "files": file_infos,
        "added_items": added_items
    })

def process_file_logic():
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "No file uploaded"}), 400

    uploaded_file = request.files['file']
    if uploaded_file.filename == '':
        return jsonify({"success": False, "message": "Empty filename header sent"}), 400

    bundle_url = request.form.get('bundle_url', '')

    files = {
        'asset_bundle': (uploaded_file.filename, uploaded_file.read(), uploaded_file.content_type)
    }
    data = {
        'bundle_url': bundle_url
    }

    try:
        response = requests.post(EXTRACTOR_API_URL, files=files, data=data, timeout=45)
        
        if not response.ok:
            try:
                err_data = response.json()
            except:
                err_data = {"error": response.text}
            return jsonify(err_data), response.status_code

        extractor_data = response.json()
        assets_list = extractor_data.get("assets", []) or extractor_data.get("files", [])

        # Process each extracted component to build safe base64-encoded stream routes
        for asset in assets_list:
            remote_preview_url = asset.get("preview_url") or asset.get("url")
            if not remote_preview_url:
                continue

            # Convert relative path schema from extractor to absolute address format
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

            # Decode the base64 content received from extractor
            b64_content = asset.get("data", "")
            file_bytes = base64.b64decode(b64_content) if b64_content else b""

            # Encode URL token (embedded with compressed file data for statelessness)
            local_cache_token = encode_token(remote_preview_url, filename_str, detected_content_type, file_bytes)

            # Keep in memory cache as fallback
            global MEMORY_ASSET_CACHE
            MEMORY_ASSET_CACHE[local_cache_token] = {
                "content": file_bytes,
                "filename": filename_str,
                "content_type": detected_content_type
            }

            local_proxy_route = f"/api?action=stream&token={local_cache_token}"
            if "preview_url" in asset: asset["preview_url"] = local_proxy_route
            if "url" in asset: asset["url"] = local_proxy_route

        return jsonify(extractor_data), 200

    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to connect to extractor pipeline: {str(e)}"}), 500

def stream_cache_logic(token):
    if not token:
        return "Missing token query parameter", 400

    decoded = decode_token(token)
    if decoded:
        filename = decoded.get("filename", "file")
        mime_type = decoded.get("content_type", "application/octet-stream")
        
        # Check if content is embedded directly in the token
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

        # Otherwise download it from the remote URL
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

def download_bundle_logic(token):
    if not token:
        return "Missing token", 400
    decoded = decode_token(token)
    if not decoded:
        return "Invalid token payload.", 400
    
    remote_url = decoded.get("url")
    filename = decoded.get("filename", "bundle")
    try:
        res = requests.get(remote_url, stream=True, timeout=25)
        if res.status_code == 200:
            def generate():
                for chunk in res.iter_content(chunk_size=4096):
                    yield chunk
            response = Response(generate(), mimetype='application/octet-stream')
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response
    except Exception as e:
        return f"Proxy error: {str(e)}", 500
    return "Error finding remote file.", 404

def extract_zip_logic(token):
    if not token:
        return "Missing token", 400
    decoded = decode_token(token)
    if not decoded:
        return "Invalid token payload.", 400
    
    remote_url = decoded.get("url")
    encoded_url = base64.urlsafe_b64encode(remote_url.encode('utf-8')).decode('utf-8')
    extractor_url = f"https://ff-asset-extractor.vercel.app/api/extract?bundle_url={encoded_url}&download_type=zip"
    
    try:
        res = requests.get(extractor_url, stream=True, timeout=60)
        if res.status_code == 200:
            def generate():
                for chunk in res.iter_content(chunk_size=4096):
                    yield chunk
            response = Response(generate(), mimetype='application/zip')
            response.headers["Content-Disposition"] = res.headers.get("Content-Disposition", 'attachment; filename="bundle_extracted.zip"')
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response
    except Exception as e:
        return f"Extraction proxy error: {str(e)}", 500
    return "Failed processing extraction.", 404

def proxy_icon_logic(item_id):
    if not item_id:
        return "Missing item ID", 400
    item_id = "".join([c for c in item_id if c.isalnum() or c in "-_."])
    cdn_url = f"https://dl.cdn.freefiremobile.com/live/ABHotUpdates/IconCDN/other/{item_id}.png"
    try:
        res = requests.get(cdn_url, timeout=10)
        if res.status_code == 200:
            response = Response(res.content, mimetype='image/png')
            response.headers["Cache-Control"] = "public, max-age=86400"
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response
    except:
        pass
    transparent_gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    return Response(transparent_gif, mimetype='image/gif')

def bulk_zip_logic(version):
    if not version or not re.match(r'^\d+\.\d+\.\d+$', version):
        return "Incorrect version format entered", 400

    new_text, active_version = get_file_info_data(version)
    if not new_text:
        fallback_ver = get_previous_version(version)
        new_text, active_version = get_file_info_data(fallback_ver)
        if not new_text:
            return "Target version logs missing from server", 404

    # Historical mapping database consistency
    prev_versions = []
    curr = active_version
    for _ in range(6):
        prev = get_previous_version(curr)
        if not prev:
            break
        prev_versions.append(prev)
        curr = prev

    old_files_map = {}
    for prev_ver in prev_versions:
        old_text, _ = get_file_info_data(prev_ver)
        if old_text:
            for line in old_text.splitlines():
                if not line.strip(): 
                    continue
                parts = line.split(',')
                if len(parts) >= 3:
                    file_id = parts[0]
                    try: 
                        old_size = int(parts[2])
                    except: 
                        old_size = 0
                    if file_id not in old_files_map:
                        old_files_map[file_id] = set()
                    old_files_map[file_id].add(old_size)

    zip_io = io.BytesIO()
    files_added_to_zip = 0
    
    with zipfile.ZipFile(zip_io, "w", zipfile.ZIP_DEFLATED) as zf:
        for line in new_text.splitlines():
            if not line.strip(): 
                continue
            parts = line.split(',')
            if len(parts) < 5: 
                continue

            file_id, size_str, suffix = parts[0], parts[2], parts[4]
            if not file_id or not suffix: 
                continue

            try: 
                size = int(size_str)
            except: 
                size = 0

            # Historical validation checks
            if file_id in old_files_map:
                if size not in old_files_map[file_id]:
                    pass
                else:
                    continue
            else:
                pass

            encoded = encode_suffix(suffix)
            download_url = f"{BASE_URL}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
            
            try:
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
        elif path == '/api/download-bundle' or path == '/download-bundle':
            return download_bundle_logic(request.args.get('token'))
        elif path == '/api/extract-zip' or path == '/extract-zip':
            return extract_zip_logic(request.args.get('token'))
        elif path == '/api/proxy-icon' or path == '/proxy-icon':
            return proxy_icon_logic(request.args.get('id'))
        elif path.startswith('/api/stream-cache/') or path.startswith('/stream-cache/'):
            token = path.split('/')[-1]
            return stream_cache_logic(token)
        
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
    elif action == 'download-bundle':
        return download_bundle_logic(request.args.get('token'))
    elif action == 'extract-zip':
        return extract_zip_logic(request.args.get('token'))
    elif action == 'proxy-icon':
        return proxy_icon_logic(request.args.get('id'))

    return jsonify({"success": False, "message": f"Action parameter '{action}' is unmapped."}), 404

if __name__ == '__main__':
    app.run(port=5000, debug=True)