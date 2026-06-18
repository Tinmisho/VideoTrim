import os
import uuid
import subprocess
import threading
import re
import time
from flask import Flask, request, jsonify, send_file, render_template, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB

ALLOWED_EXTENSIONS = {'mp4', 'mkv', 'mov', 'avi', 'webm', 'ts', 'flv', 'm4v'}

# In-memory job tracker: job_id -> {status, percent, output_id, output_ext, error}
JOBS = {}
JOBS_LOCK = threading.Lock()

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

def set_job(job_id, **kwargs):
    with JOBS_LOCK:
        JOBS[job_id].update(kwargs)

def run_ffmpeg_with_progress(cmd, job_id, total_duration):
    """Run ffmpeg with -progress pipe:1 and update JOBS[job_id]['percent'] as it goes."""
    cmd = cmd + ['-progress', 'pipe:1', '-nostats']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    stderr_lines = []

    def read_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)

    err_thread = threading.Thread(target=read_stderr, daemon=True)
    err_thread.start()

    out_time = 0.0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith('out_time_ms='):
            try:
                out_time = int(line.split('=')[1]) / 1_000_000
            except (ValueError, IndexError):
                pass
        elif line.startswith('out_time='):
            try:
                h, m, s = line.split('=')[1].split(':')
                out_time = int(h) * 3600 + int(m) * 60 + float(s)
            except (ValueError, IndexError):
                pass
        if total_duration and total_duration > 0:
            pct = min(99, int((out_time / total_duration) * 100))
            set_job(job_id, percent=pct)

    proc.wait()
    err_thread.join(timeout=2)
    return proc.returncode, ''.join(stderr_lines)

def trim_worker(job_id, input_path, output_path, start_sec, end_sec, fast_mode, total_duration):
    set_job(job_id, status='processing', percent=0)
    cmd = ['ffmpeg', '-y', '-ss', seconds_to_ts(start_sec), '-i', input_path]
    if end_sec is not None:
        cmd += ['-t', str(end_sec - start_sec)]
    if fast_mode:
        cmd += ['-c', 'copy']
    else:
        cmd += ['-c:v', 'libx264', '-c:a', 'aac', '-crf', '18', '-preset', 'fast']
    cmd.append(output_path)

    job_duration = (end_sec - start_sec) if end_sec is not None else (total_duration - start_sec if total_duration else None)

    try:
        rc, stderr = run_ffmpeg_with_progress(cmd, job_id, job_duration)
        if rc != 0:
            set_job(job_id, status='error', error=stderr[-1000:])
        else:
            set_job(job_id, status='done', percent=100)
    except Exception as e:
        set_job(job_id, status='error', error=str(e))

def merge_worker(job_id, output_ids, ext, output_folder):
    set_job(job_id, status='processing', percent=0)
    concat_id = str(uuid.uuid4())
    concat_list = os.path.join(output_folder, f"{concat_id}.txt")
    merged_path_id = job_id
    merged_path = os.path.join(output_folder, f"{merged_path_id}.{ext}")

    lines = []
    total_duration = 0
    for oid in output_ids:
        path = os.path.join(output_folder, f"{oid}.{ext}")
        lines.append(f"file '{os.path.abspath(path)}'")
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', path],
                capture_output=True, text=True, timeout=30
            )
            total_duration += float(result.stdout.strip())
        except Exception:
            pass

    with open(concat_list, 'w') as f:
        f.write('\n'.join(lines))

    cmd = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_list, '-c', 'copy', merged_path]

    try:
        rc, stderr = run_ffmpeg_with_progress(cmd, job_id, total_duration if total_duration else None)
        if rc != 0:
            set_job(job_id, status='error', error=stderr[-1000:])
        else:
            set_job(job_id, status='done', percent=100, output_id=merged_path_id, output_ext=ext)
    except Exception as e:
        set_job(job_id, status='error', error=str(e))
    finally:
        try: os.remove(concat_list)
        except: pass

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
    """Start an async trim job. Returns a job_id immediately; poll /job/<job_id> for progress."""
    data = request.json
    file_id = data.get('file_id')
    ext = data.get('ext', 'mp4')
    start_raw = data.get('start', '0')
    end_raw = data.get('end', '')
    output_ext = data.get('output_ext', ext)
    fast_mode = bool(data.get('fast_mode'))

    if not file_id or not re.match(r'^[a-f0-9\-]{36}$', file_id):
        return jsonify({'error': 'Invalid file ID'}), 400

    input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.{ext}")
    if not os.path.exists(input_path):
        return jsonify({'error': 'File not found'}), 404

    try:
        start_sec = parse_time(start_raw)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    end_sec = None
    if end_raw.strip():
        try:
            end_sec = parse_time(end_raw)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if end_sec <= start_sec:
            return jsonify({'error': 'End time must be after start time'}), 400

    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
            capture_output=True, text=True, timeout=30
        )
        total_duration = float(result.stdout.strip())
    except Exception:
        total_duration = None

    job_id = str(uuid.uuid4())
    out_id = str(uuid.uuid4())
    output_path = os.path.join(app.config['OUTPUT_FOLDER'], f"{out_id}.{output_ext}")

    with JOBS_LOCK:
        JOBS[job_id] = {'status': 'queued', 'percent': 0, 'output_id': out_id, 'output_ext': output_ext, 'error': None}

    t = threading.Thread(
        target=trim_worker,
        args=(job_id, input_path, output_path, start_sec, end_sec, fast_mode, total_duration),
        daemon=True
    )
    t.start()

    return jsonify({'job_id': job_id})

@app.route('/merge', methods=['POST'])
def merge():
    """Start an async merge job. Returns a job_id immediately; poll /job/<job_id> for progress."""
    data = request.json
    output_ids = data.get('output_ids', [])
    ext = data.get('ext', 'mp4')

    if not output_ids or len(output_ids) < 2:
        return jsonify({'error': 'Need at least 2 segments to merge'}), 400

    for oid in output_ids:
        if not re.match(r'^[a-f0-9\-]{36}$', oid):
            return jsonify({'error': f'Invalid output ID: {oid}'}), 400
        path = os.path.join(app.config['OUTPUT_FOLDER'], f"{oid}.{ext}")
        if not os.path.exists(path):
            return jsonify({'error': f'Segment not found: {oid}'}), 404

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {'status': 'queued', 'percent': 0, 'output_id': None, 'output_ext': ext, 'error': None}

    t = threading.Thread(
        target=merge_worker,
        args=(job_id, output_ids, ext, app.config['OUTPUT_FOLDER']),
        daemon=True
    )
    t.start()

    return jsonify({'job_id': job_id})

@app.route('/job/<job_id>')
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)

@app.route('/preview/<file_id>/<ext>')
def preview(file_id, ext):
    """Stream the uploaded source video for in-browser preview, with HTTP Range support
    so the <video> element can seek without downloading the whole file."""
    if not re.match(r'^[a-f0-9\-]{36}$', file_id):
        return jsonify({'error': 'Invalid ID'}), 400
    path = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.{ext}")
    if not os.path.exists(path):
        return jsonify({'error': 'File not found'}), 404

    file_size = os.path.getsize(path)
    range_header = request.headers.get('Range', None)

    mime_map = {
        'mp4': 'video/mp4', 'webm': 'video/webm', 'mov': 'video/quicktime',
        'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo', 'ts': 'video/mp2t',
        'flv': 'video/x-flv', 'm4v': 'video/x-m4v',
    }
    mimetype = mime_map.get(ext, 'application/octet-stream')

    if not range_header:
        return send_file(path, mimetype=mimetype)

    m = re.match(r'bytes=(\d+)-(\d*)', range_header)
    if not m:
        return send_file(path, mimetype=mimetype)

    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)
    length = end - start + 1

    def generate():
        with open(path, 'rb') as f:
            f.seek(start)
            remaining = length
            chunk_size = 1024 * 1024
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(generate(), 206, mimetype=mimetype, direct_passthrough=True)
    resp.headers.add('Content-Range', f'bytes {start}-{end}/{file_size}')
    resp.headers.add('Accept-Ranges', 'bytes')
    resp.headers.add('Content-Length', str(length))
    return resp

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
