import os
import subprocess
import json
import re
import time
import uuid
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_file
from flask_sock import Sock
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)
sock = Sock(app)

# Rate limiting - prevents abuse
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["100 per hour", "10 per minute"],
    storage_uri="memory://"
)

# Ensure downloads directory exists
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloads')
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Store for connected clients and current state
clients = set()
current_state = {
    "playing": False,
    "paused": False,
    "title": None,
    "url": None,
    "audio_url": None,
    "video_url": None,
    "duration": 0,
    "current_time": 0,
    "format": "video",
    "quality": "best"
}

# Download jobs storage
download_jobs = {}
jobs_lock = threading.Lock()

# Supported platforms with their specific handling
SUPPORTED_PLATFORMS = {
    'youtube.com': {'name': 'YouTube', 'extractor': 'youtube'},
    'youtu.be': {'name': 'YouTube', 'extractor': 'youtube'},
    'instagram.com': {'name': 'Instagram', 'extractor': 'instagram'},
    'tiktok.com': {'name': 'TikTok', 'extractor': 'tiktok'},
    'twitter.com': {'name': 'Twitter/X', 'extractor': 'twitter'},
    'x.com': {'name': 'Twitter/X', 'extractor': 'twitter'},
    'facebook.com': {'name': 'Facebook', 'extractor': 'facebook'},
    'fb.watch': {'name': 'Facebook', 'extractor': 'facebook'}
}

# Error message mapping for user-friendly errors
ERROR_MESSAGES = {
    'private': 'This video is private or requires authentication',
    'unavailable': 'This video is unavailable or has been removed',
    'region': 'This video is region-restricted in your area',
    'age': 'This video is age-restricted',
    'copyright': 'This video has been removed due to copyright claims',
    'login': 'This content requires login credentials',
    'network': 'Network error while fetching video. Please try again',
    'parse': 'Could not parse video information. Platform may have changed',
    'timeout': 'Request timed out. The video may be too large or the platform slow',
}

def is_valid_url(url: str) -> bool:
    """Check if URL is from a supported platform"""
    url_lower = url.lower()
    return any(platform in url_lower for platform in SUPPORTED_PLATFORMS.keys())

def get_platform_info(url: str) -> dict:
    """Get platform name and extractor from URL"""
    url_lower = url.lower()
    for platform, info in SUPPORTED_PLATFORMS.items():
        if platform in url_lower:
            return info
    return {'name': 'Unknown', 'extractor': 'generic'}

def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename"""
    return re.sub(r'[<>"/\\|?*]', '', filename)[:150]

def parse_error_message(error_text: str) -> str:
    """Parse yt-dlp error and return user-friendly message"""
    error_lower = error_text.lower()

    if any(x in error_lower for x in ['private', 'sign in', 'login', 'auth']):
        return ERROR_MESSAGES['private']
    elif any(x in error_lower for x in ['unavailable', 'not exist', 'removed']):
        return ERROR_MESSAGES['unavailable']
    elif any(x in error_lower for x in ['region', 'country', 'blocked', 'geoblock']):
        return ERROR_MESSAGES['region']
    elif any(x in error_lower for x in ['age', 'age-restricted', 'adults']):
        return ERROR_MESSAGES['age']
    elif any(x in error_lower for x in ['copyright', 'dmca', 'violation']):
        return ERROR_MESSAGES['copyright']
    elif any(x in error_lower for x in ['timeout', 'time out']):
        return ERROR_MESSAGES['timeout']
    elif any(x in error_lower for x in ['network', 'connection', 'unreachable']):
        return ERROR_MESSAGES['network']
    elif any(x in error_lower for x in ['unable to extract', 'parse']):
        return ERROR_MESSAGES['parse']
    else:
        return 'An error occurred while processing the video'

def get_video_info(video_url: str) -> dict:
    """Extract video info and available formats from video URL using yt-dlp"""
    try:
        # Get video info with format listing
        info_cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-download",
            video_url
        ]
        result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            error_msg = parse_error_message(result.stderr)
            return {'error': error_msg, 'details': result.stderr}

        info = json.loads(result.stdout)

        # Extract available formats
        formats = []
        seen_qualities = set()

        for fmt in info.get('formats', []):
            # Skip audio-only for video selection
            if fmt.get('vcodec') != 'none' and fmt.get('height'):
                height = fmt.get('height')
                if height not in seen_qualities:
                    seen_qualities.add(height)
                    formats.append({
                        'format_id': fmt.get('format_id'),
                        'height': height,
                        'ext': fmt.get('ext', 'mp4'),
                        'quality_label': f"{height}p",
                        'filesize_approx': fmt.get('filesize_approx', 0)
                    })

        # Sort by height descending
        formats.sort(key=lambda x: x['height'], reverse=True)

        # Audio formats
        audio_formats = []
        for fmt in info.get('formats', []):
            if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                audio_formats.append({
                    'format_id': fmt.get('format_id'),
                    'ext': fmt.get('ext', 'm4a'),
                    'abr': fmt.get('abr', 0),
                    'filesize_approx': fmt.get('filesize_approx', 0)
                })

        # Sort by bitrate
        audio_formats.sort(key=lambda x: x['abr'] if x['abr'] else 0, reverse=True)

        return {
            "title": info.get('title', 'Unknown'),
            "duration": info.get('duration', 0),
            "thumbnail": info.get('thumbnail', ''),
            "uploader": info.get('uploader', 'Unknown'),
            "upload_date": info.get('upload_date', ''),
            "view_count": info.get('view_count', 0),
            "formats": formats[:10],  # Limit to top 10
            "audio_formats": audio_formats[:5],  # Top 5 audio formats
            "original_url": video_url,
            "platform": get_platform_info(video_url)['name'],
            "extractor": get_platform_info(video_url)['extractor']
        }
    except subprocess.TimeoutExpired:
        return {'error': ERROR_MESSAGES['timeout']}
    except Exception as e:
        print(f"[video] Error: {e}")
        return {'error': ERROR_MESSAGES['parse']}

def get_stream_urls(video_url: str, video_format: str = None, audio_format: str = None) -> dict:
    """Get streaming URLs for video and audio"""
    try:
        # Get best audio stream
        audio_cmd = [
            "yt-dlp",
            "-f", audio_format if audio_format else "bestaudio/best",
            "-g",
            video_url
        ]
        audio_result = subprocess.run(audio_cmd, capture_output=True, text=True, timeout=30)
        audio_url = audio_result.stdout.strip().split('\n')[0] if audio_result.returncode == 0 else None

        # Get video stream URL
        if video_format:
            video_cmd = [
                "yt-dlp",
                "-f", f"{video_format}+bestaudio/best[height<={video_format}p]/best",
                "-g",
                video_url
            ]
        else:
            video_cmd = [
                "yt-dlp",
                "-f", "best[height<=1080]/best",
                "-g",
                video_url
            ]
        video_result = subprocess.run(video_cmd, capture_output=True, text=True, timeout=30)
        video_stream_url = video_result.stdout.strip().split('\n')[0] if video_result.returncode == 0 else None

        return {
            "audio_url": audio_url,
            "video_url": video_stream_url
        }
    except Exception as e:
        print(f"[stream] Error: {e}")
        return {"audio_url": None, "video_url": None}

def run_download_job(job_id: str, url: str, format_type: str, quality: str = None):
    """Background job for downloading media"""
    try:
        with jobs_lock:
            download_jobs[job_id]['status'] = 'downloading'
            download_jobs[job_id]['progress'] = 10

        # Get video info first
        info = get_video_info(url)
        if 'error' in info:
            with jobs_lock:
                download_jobs[job_id]['status'] = 'failed'
                download_jobs[job_id]['error'] = info['error']
            return

        with jobs_lock:
            download_jobs[job_id]['progress'] = 30
            download_jobs[job_id]['title'] = info['title']

        safe_title = sanitize_filename(info['title'])
        timestamp = int(time.time())

        if format_type == 'audio':
            filename = f"{safe_title}_{timestamp}.mp3"
            filepath = os.path.join(DOWNLOADS_DIR, filename)

            # Format selection for audio
            format_spec = "bestaudio/best"
            if quality == 'best':
                format_spec = "bestaudio/best"
            elif quality == 'medium':
                format_spec = "bestaudio[abr<=128]/bestaudio"
            elif quality == 'low':
                format_spec = "worstaudio"

            cmd = [
                "yt-dlp",
                "-f", format_spec,
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "-o", filepath,
                "--newline",
                "--progress",
                url
            ]
        else:  # video
            filename = f"{safe_title}_{timestamp}.mp4"
            filepath = os.path.join(DOWNLOADS_DIR, filename)

            # Format selection for video
            if quality and quality != 'best':
                height = quality.replace('p', '')
                format_spec = f"best[height<={height}][ext=mp4]/best[height<={height}]/best"
            else:
                format_spec = "best[height<=1080][ext=mp4]/best[ext=mp4]/best"

            cmd = [
                "yt-dlp",
                "-f", format_spec,
                "--merge-output-format", "mp4",
                "-o", filepath,
                "--newline",
                "--progress",
                url
            ]

        with jobs_lock:
            download_jobs[job_id]['progress'] = 50

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        # Check for actual file
        actual_file = filepath
        if not os.path.exists(actual_file):
            ext = '.mp3' if format_type == 'audio' else '.mp4'
            if os.path.exists(filepath + ext):
                actual_file = filepath + ext

        if result.returncode != 0 or not os.path.exists(actual_file):
            error_msg = parse_error_message(result.stderr)
            with jobs_lock:
                download_jobs[job_id]['status'] = 'failed'
                download_jobs[job_id]['error'] = error_msg
            return

        with jobs_lock:
            download_jobs[job_id]['status'] = 'completed'
            download_jobs[job_id]['progress'] = 100
            download_jobs[job_id]['filepath'] = actual_file
            download_jobs[job_id]['filename'] = os.path.basename(actual_file)
            download_jobs[job_id]['completed_at'] = datetime.now().isoformat()

    except subprocess.TimeoutExpired:
        with jobs_lock:
            download_jobs[job_id]['status'] = 'failed'
            download_jobs[job_id]['error'] = ERROR_MESSAGES['timeout']
    except Exception as e:
        print(f"[download_job] Error: {e}")
        with jobs_lock:
            download_jobs[job_id]['status'] = 'failed'
            download_jobs[job_id]['error'] = str(e)

def broadcast_state():
    """Broadcast current state to all connected clients"""
    message = json.dumps(current_state)
    disconnected = set()
    for client in clients:
        try:
            client.send(message)
        except:
            disconnected.add(client)
    clients -= disconnected

def cleanup_old_jobs():
    """Remove jobs older than 24 hours"""
    cutoff = time.time() - (24 * 3600)
    with jobs_lock:
        expired = [k for k, v in download_jobs.items() if v.get('created_at', 0) < cutoff]
        for k in expired:
            del download_jobs[k]

@app.route('/')
def index():
    return render_template('player.html')

@app.route('/api/formats', methods=['POST'])
@limiter.limit("10 per minute")
def get_formats():
    """Get available formats for a URL"""
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not is_valid_url(url):
        return jsonify({"error": "Unsupported platform. Try YouTube, Instagram, TikTok, Twitter/X, or Facebook"}), 400

    info = get_video_info(url)
    if 'error' in info:
        return jsonify({"error": info['error']}), 400

    return jsonify({
        "success": True,
        "title": info['title'],
        "duration": info['duration'],
        "thumbnail": info['thumbnail'],
        "platform": info['platform'],
        "video_formats": info['formats'],
        "audio_formats": info['audio_formats']
    })

@app.route('/api/play', methods=['POST'])
@limiter.limit("20 per minute")
def play():
    data = request.get_json()
    url = data.get('url')
    format_type = data.get('format', 'video')
    quality = data.get('quality', 'best')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not is_valid_url(url):
        return jsonify({"error": "Unsupported platform. Try YouTube, Instagram, TikTok, or Twitter/X"}), 400

    # Get video info
    info = get_video_info(url)
    if 'error' in info:
        return jsonify({"error": info['error']}), 400

    # Get stream URLs
    video_format_id = None
    audio_format_id = None

    if format_type == 'video' and quality != 'best':
        for fmt in info.get('formats', []):
            if fmt.get('quality_label') == quality:
                video_format_id = fmt.get('format_id')
                break

    streams = get_stream_urls(url, video_format_id, audio_format_id)

    if not streams.get('video_url') and format_type == 'video':
        return jsonify({"error": "Could not get stream URL. Video may be restricted."}), 400

    if not streams.get('audio_url') and format_type == 'audio':
        return jsonify({"error": "Could not get audio stream URL."}), 400

    # Update state
    current_state.update({
        "playing": True,
        "paused": False,
        "title": info["title"],
        "url": info["original_url"],
        "audio_url": streams.get("audio_url"),
        "video_url": streams.get("video_url"),
        "duration": info["duration"],
        "thumbnail": info.get("thumbnail", ""),
        "uploader": info.get("uploader", "Unknown"),
        "platform": info.get("platform", "Unknown"),
        "current_time": 0,
        "format": format_type,
        "quality": quality
    })

    broadcast_state()
    return jsonify({"success": True, **current_state})

@app.route('/api/download', methods=['POST'])
@limiter.limit("5 per minute")
def download():
    """Start a download job"""
    data = request.get_json()
    url = data.get('url')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not is_valid_url(url):
        return jsonify({"error": "Unsupported platform. Try YouTube, Instagram, TikTok, or Twitter/X"}), 400

    format_type = data.get('format', 'audio')
    quality = data.get('quality', 'best')

    # Create job
    job_id = str(uuid.uuid4())[:8]

    with jobs_lock:
        download_jobs[job_id] = {
            'id': job_id,
            'url': url,
            'format': format_type,
            'quality': quality,
            'status': 'pending',
            'progress': 0,
            'created_at': time.time()
        }

    # Start background thread
    thread = threading.Thread(
        target=run_download_job,
        args=(job_id, url, format_type, quality)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "success": True,
        "job_id": job_id,
        "status": "started"
    })

@app.route('/api/download/<job_id>/status')
@limiter.limit("60 per minute")
def download_status(job_id):
    """Get download job status"""
    with jobs_lock:
        job = download_jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "success": True,
        "job": job
    })

@app.route('/api/download/<job_id>/file')
def download_file(job_id):
    """Download the completed file"""
    with jobs_lock:
        job = download_jobs.get(job_id)

    if not job or job.get('status') != 'completed':
        return jsonify({"error": "File not ready"}), 400

    filepath = job.get('filepath')
    filename = job.get('filename')

    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename
    )

@app.route('/api/jobs')
@limiter.limit("30 per minute")
def list_jobs():
    """List recent download jobs"""
    with jobs_lock:
        jobs = list(download_jobs.values())
    return jsonify({"jobs": jobs})

@app.route('/api/pause', methods=['POST'])
def pause():
    current_state["paused"] = True
    broadcast_state()
    return jsonify({"success": True, **current_state})

@app.route('/api/resume', methods=['POST'])
def resume():
    current_state["paused"] = False
    broadcast_state()
    return jsonify({"success": True, **current_state})

@app.route('/api/stop', methods=['POST'])
def stop():
    current_state.update({
        "playing": False,
        "paused": False,
        "title": None,
        "url": None,
        "audio_url": None,
        "video_url": None,
        "duration": 0,
        "current_time": 0,
        "thumbnail": "",
        "uploader": "",
        "platform": "",
        "format": "video",
        "quality": "best"
    })
    broadcast_state()
    return jsonify({"success": True, **current_state})

@app.route('/api/seek', methods=['POST'])
def seek():
    data = request.get_json()
    time_pos = data.get('time', 0)
    current_state["current_time"] = time_pos
    broadcast_state()
    return jsonify({"success": True, **current_state})

@app.route('/api/state')
def get_state():
    return jsonify(current_state)

@sock.route('/ws')
def websocket(ws):
    """WebSocket for real-time updates"""
    clients.add(ws)
    try:
        ws.send(json.dumps(current_state))

        while True:
            data = ws.receive()
            if data:
                try:
                    msg = json.loads(data)
                    if msg.get('type') == 'time_update':
                        current_state["current_time"] = msg.get('time', 0)
                except json.JSONDecodeError:
                    pass
    except:
        pass
    finally:
        clients.discard(ws)

if __name__ == '__main__':
    print("\n" + "="*60)
    print("🎵 MediaPull - Universal Media Extraction Platform")
    print("="*60)
    print("Open: http://localhost:5000")
    print("="*60 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=True)
