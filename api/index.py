from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import requests
import re
import uuid
import mimetypes

app = Flask(__name__)
CORS(app)

BASE_URL = 'https://dl.tata.freefiremobile.com/live/ABHotUpdates/android/'
VERSION_API = 'https://ff-version.vercel.app/update'
EXTRACTOR_API_URL = "https://ff-asset-extractor.vercel.app/api/extract/bundle"

# Local Persistent Session Cache Map (Saves raw data assets in Server RAM)
# Stays perfectly intact throughout the session until the server process resets
MEMORY_ASSET_CACHE = {}

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

@app.route('/')
def home():
    try:
        with open('index.html', 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return jsonify({"success": False, "message": "index.html template missing"}), 404

@app.route('/api/latest-version', methods=['GET'])
def get_latest_calculated_version():
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

@app.route('/api/update', methods=['GET'])
def check_update():
    # REMOVED: MEMORY_ASSET_CACHE.clear() has been eliminated so extraction items 
    # remain persistently available throughout your entire runtime session.
    
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

    prev_version = get_previous_version(active_version)
    old_text, _ = get_file_info_data(prev_version)

    old_files_map = {}
    if old_text:
        for line in old_text.splitlines():
            if not line.strip(): continue
            parts = line.split(',')
            if len(parts) >= 3:
                file_id = parts[0]
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

        file_id, size_str, suffix = parts[0], parts[2], parts[4]
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

        total_size += size
        file_infos.append({
            "fileName": file_name,
            "size": size,
            "url": download_url,
            "status": status
        })

    return jsonify({
        "success": True,
        "version": active_version,
        "totalSize": total_size,
        "files": file_infos
    })

@app.route('/api/process', methods=['POST'])
def process_file():
    global MEMORY_ASSET_CACHE
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "No file uploaded"}), 400

    uploaded_file = request.files['file']
    if uploaded_file.filename == '':
        return jsonify({"success": False, "message": "Empty filename header sent"}), 400

    files = {
        'asset_bundle': (uploaded_file.filename, uploaded_file.read(), uploaded_file.content_type)
    }

    try:
        # 1. Ship bundle data block over to Vercel architecture parser engine
        response = requests.post(EXTRACTOR_API_URL, files=files, timeout=45)
        extractor_data = response.json()

        if not response.ok or not extractor_data.get("success"):
            return jsonify(extractor_data), response.status_code

        assets_list = extractor_data.get("assets", []) or extractor_data.get("files", [])
        
        # 2. Intercept extracted files payload and cache actual content strings into local memory
        for asset in assets_list:
            remote_preview_url = asset.get("preview_url") or asset.get("url")
            if not remote_preview_url:
                continue

            # Generate a secure, persistent tracking hash token for this asset component
            local_cache_token = str(uuid.uuid4())

            try:
                # Instantly download and capture raw structure layers before Vercel drops them
                asset_file_response = requests.get(remote_preview_url, timeout=15)
                if asset_file_response.status_code == 200:
                    
                    # Read the raw bytes safely
                    raw_bytes = asset_file_response.content
                    filename_str = asset.get("name") or asset.get("fileName") or "extracted_file"
                    detected_content_type = asset_file_response.headers.get("Content-Type")

                    # Force correct content type for common text assets if headers show application/octet-stream
                    if filename_str.endswith('.json'):
                        detected_content_type = 'application/json'
                    elif filename_str.endswith('.manifest') or filename_str.endswith('.txt'):
                        detected_content_type = 'text/plain'

                    # Save the raw binary content in server RAM mapped to our clean UUID link
                    MEMORY_ASSET_CACHE[local_cache_token] = {
                        "content": raw_bytes,
                        "filename": filename_str,
                        "content_type": detected_content_type
                    }
                    
                    # Point the frontend to view directly through our persistent local python cache route
                    local_proxy_route = f"/api/stream-cache/{local_cache_token}"
                    if "preview_url" in asset: asset["preview_url"] = local_proxy_route
                    if "url" in asset: asset["url"] = local_proxy_route
            except Exception as inner_err:
                print(f"Error caching asset chunk data stream: {inner_err}")
                continue

        return jsonify(extractor_data), 200
        
    except Exception as e:
        return jsonify({"success": False, "message": f"Failed to connect to extractor pipeline: {str(e)}"}), 500

# Completely independent ultra-stable streaming channel to serve cached objects straight from RAM
@app.route('/api/stream-cache/<token>', methods=['GET'])
def stream_cached_asset_layer(token):
    global MEMORY_ASSET_CACHE
    cached_item = MEMORY_ASSET_CACHE.get(token)
    
    if not cached_item:
        return f"Asset Token Stream Key Expired or Modified out of Server Range Layer.", 404

    # Dynamically determine strict audio/video headers to enable timeline scrubbing
    mime_type = cached_item["content_type"]
    filename_lower = cached_item["filename"].lower()
    
    if not mime_type or mime_type == 'application/octet-stream':
        mime_type, _ = mimetypes.guess_type(cached_item["filename"])
        
    # Absolute strict clean overrides for config assets and basic media links
    if filename_lower.endswith('.json'):
        mime_type = 'application/json'
    elif filename_lower.endswith('.manifest') or filename_lower.endswith('.txt') or filename_lower.endswith('.atlas'):
        mime_type = 'text/plain'
    elif filename_lower.endswith('.ogg'):
        mime_type = 'audio/ogg'
    elif filename_lower.endswith('.mp3'):
        mime_type = 'audio/mpeg'
    elif filename_lower.endswith('.mp4'):
        mime_type = 'video/mp4'
    elif not mime_type:
        mime_type = 'application/octet-stream'

    # Build and emit response straight from local runtime memory structure
    response = Response(cached_item["content"], mimetype=mime_type)
    response.headers["Content-Disposition"] = f'inline; filename="{cached_item["filename"]}"'
    response.headers["Accept-Ranges"] = "bytes"
    
    # Enable global reading access to avoid strict browser CORS interception
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

if __name__ == '__main__':
    app.run(port=5000, debug=True)
