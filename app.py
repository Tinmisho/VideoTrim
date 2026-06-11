import os
import uuid
import subprocess
import re
from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB

ALLOWED_EXTENSIONS = {'mp4', 'mkv', 'mov', 'avi', 'webm', 'ts', 'flv', 'm4v'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_time(t):
    t = t.strip()
    if re.match(r'^\d+(\.\d+)?$', t):
        return float(t)
    parts = t.split(':')
    parts = [float(p) for p in parts]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Invalid time format: {t}")

def seconds_to_ts(s):
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not allowed_file(f.filename):
        return jsonify({'error': 'File type not allowed'}), 400

    uid = str(uuid.uuid4())
    ext = f.filename.rsplit('.', 1)[1].lower()
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{uid}.{ext}")
    f.save(save_path)

    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', save_path],
            capture_output=True, text=True, timeout=30
        )
        duration = float(result.stdout.strip())
    except Exception:
        duration = None

    return jsonify({'file_id': uid, 'ext': ext, 'original_name': f.filename, 'duration': duration})

@app.route('/trim', methods=['POST'])
def trim():
    data = request.json
    file_id = data.get('file_id')
    ext = data.get('ext', 'mp4')
    start_raw = data.get('start', '0')
    end_raw = data.get('end', '')
    output_ext = data.get('output_ext', ext)

    if not file_id or not re.match(r'^[a-f0-9\-]{36}$', file_id):
        return jsonify({'error': 'Invalid file ID'}), 400

    input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.{ext}")
    if not os.path.exists(input_path):
        return jsonify({'error': 'File not found'}), 404

    try:
        start_sec = parse_time(start_raw)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    out_id = str(uuid.uuid4())
    output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{out_id}.{output_ext}")

    cmd = ['ffmpeg', '-y', '-ss', seconds_to_ts(start_sec), '-i', input_path]

    if end_raw.strip():
        try:
            end_sec = parse_time(end_raw)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if end_sec <= start_sec:
            return jsonify({'error': 'End time must be after start time'}), 400
        cmd += ['-t', str(end_sec - start_sec)]

    if data.get('fast_mode'):
        cmd += ['-c', 'copy']
    else:
        cmd += ['-c:v', 'libx264', '-c:a', 'aac', '-crf', '18', '-preset', 'fast']

    cmd.append(output_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return jsonify({'error': 'ffmpeg failed', 'details': result.stderr[-1000:]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Trim timed out'}), 500

    return jsonify({'output_id': out_id, 'output_ext': output_ext})

@app.route('/merge', methods=['POST'])
def merge():
    data = request.json
    output_ids = data.get('output_ids', [])
    ext = data.get('ext', 'mp4')

    if not output_ids or len(output_ids) < 2:
        return jsonify({'error': 'Need at least 2 segments to merge'}), 400

    # Validate all IDs
    for oid in output_ids:
        if not re.match(r'^[a-f0-9\-]{36}$', oid):
            return jsonify({'error': f'Invalid output ID: {oid}'}), 400

    # Build concat list file
    concat_id = str(uuid.uuid4())
    concat_list = os.path.join(app.config['OUTPUT_FOLDER'], f"{concat_id}.txt")
    merged_id = str(uuid.uuid4())
    merged_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{merged_id}.{ext}")

    lines = []
    for oid in output_ids:
        path = os.path.join(app.config['OUTPUT_FOLDER'], f"{oid}.{ext}")
        if not os.path.exists(path):
            return jsonify({'error': f'Segment not found: {oid}'}), 404
        lines.append(f"file '{os.path.abspath(path)}'")

    with open(concat_list, 'w') as f:
        f.write('\n'.join(lines))

    cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
           '-i', concat_list, '-c', 'copy', merged_path]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            return jsonify({'error': 'ffmpeg merge failed', 'details': result.stderr[-1000:]}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Merge timed out'}), 500
    finally:
        try: os.remove(concat_list)
        except: pass

    return jsonify({'output_id': merged_id, 'output_ext': ext})

@app.route('/download/<out_id>/<ext>')
def download(out_id, ext):
    if not re.match(r'^[a-f0-9\-]{36}$', out_id):
        return jsonify({'error': 'Invalid ID'}), 400
    path = os.path.join(app.config['OUTPUT_FOLDER'], f"{out_id}.{ext}")
    if not os.path.exists(path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(path, as_attachment=True, download_name=f"trimmed.{ext}")

if __name__ == '__main__':
    os.makedirs('uploads', exist_ok=True)
    os.makedirs('outputs', exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
