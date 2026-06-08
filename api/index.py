from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import re

app = Flask(__name__)
CORS(app)

BASE_URL = 'https://dl.tata.freefiremobile.com/live/ABHotUpdates/android/'

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
    res = requests.get(url, timeout=7)
    if res.status_code == 200:
        return res.text, version
    return None, None

@app.route('/api/update', methods=['GET'])
def check_update():
    input_val = request.args.get('version', '').strip()
    if not input_val:
        return jsonify({"success": False, "message": "Missing version input"}), 400

    # Extract version format
    if "http" in input_val:
        match = re.search(r'/(\d+\.\d+\.\d+)/', input_val)
        version = match.group(1) if match else None
    else:
        version = input_val

    if not version or not re.match(r'^\d+\.\d+\.\d+$', version):
        return jsonify({"success": False, "message": "Invalid version format"}), 400

    # 1. Fetch current targeted fileinfo
    new_text, active_version = get_file_info_data(version)
    
    # Smart Fallback Logic: If target fails, attempt immediate rollback jump verification
    if not new_text:
        prev_fallback = get_previous_version(version)
        new_text, active_version = get_file_info_data(prev_fallback)
        if not new_text:
            return jsonify({"success": False, "message": "Version updates not found on CDN storage"}), 404

    # 2. Get past relative reference version for comparison differences
    prev_version = get_previous_version(active_version)
    old_text, _ = get_file_info_data(prev_version)

    # Generate old ID sets for mapping differences
    old_ids = set()
    if old_text:
        for line in old_text.splitlines():
            parts = line.split(',')
            if parts: old_ids.add(parts[0])

    file_infos = []
    total_size = 0

    for line in new_text.splitlines():
        if not line.strip(): continue
        parts = line.split(',')
        if len(parts) < 5: continue
        
        file_id, _, size_str, _, suffix = parts[0], parts[1], parts[2], parts[3], parts[4]
        if file_id in old_ids or not file_id or not suffix:
            continue # Skip matched records (No change detected)

        try:
            size = int(size_str)
        except:
            size = 0

        encoded = encode_suffix(suffix)
        download_url = f"{BASE_URL}{active_version}/gameassetbundles/{file_id}.{encoded}~3D"
        file_name = file_id.split('/')[-1]

        total_size += size
        file_infos.append({
            "fileName": file_name,
            "size": size,
            "url": download_url
        })

    return jsonify({
        "success": True,
        "version": active_version,
        "totalSize": total_size,
        "files": file_infos
    })

# Local testing container wrapper setup
if __name__ == '__main__':
    app.run(debug=True)
