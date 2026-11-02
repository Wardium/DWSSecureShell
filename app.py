# THESE TWO LINES MUST BE AT THE VERY TOP
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, render_template_string, request, jsonify, Response, redirect, send_file
from flask_socketio import SocketIO
from urllib.parse import urlparse
import paramiko
import logging
import sys
import requests
import time
import subprocess
import os
import urllib3
import re
import uuid
import json
import shutil
import base64
import signal
import tempfile
import io

# Suppress insecure request warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Safely import Selenium for the new Stream feature
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

logging.basicConfig(
    stream=sys.stdout, 
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- GLOBAL VARIABLES ---
active_sessions = {}
enforced_devices = {}
stream_browsers = {}  # Tracks headless browsers for the Stream feature

# --- MODULE 6: COMPUTE ENGINE GLOBALS ---
COMPUTE_RAMDISK = "/dev/shm/dws_compute" if os.path.exists("/dev/shm") else os.path.join(tempfile.gettempdir(), "dws_compute")
os.makedirs(COMPUTE_RAMDISK, exist_ok=True)

compute_jobs = {}         # job_id -> job metadata & result dict
compute_processes = {}    # job_id -> {"type": "local"|"ssh", "handle": proc_or_ssh_client}
node_active_counts = {"local": 0, "1": 0, "2": 0, "3": 0}
MAX_JOB_HISTORY = 100     # Prevent unbounded memory growth

# ==========================================
# MODULE 1: DWS Server Shell
# ==========================================
SERVERS = {
    "1": {"host": "192.168.2.143", "user": "dylan", "password": "weqr1234"},
    "2": {"host": "192.168.2.91", "user": "dylanwardstudios", "password": "weqr1234"},
    "3": {"host": "192.168.2.134", "user": "dylan", "password": "sIjkew-1qixwe-sogcog"}
}

# Stores {"ssh": ssh_client, "channel": ssh_channel} mapped by request.sid
active_sessions = {}

@app.route('/shell/<server_id>')
def shell(server_id):
    if server_id not in SERVERS:
        logging.warning(f"404: Invalid Server ID accessed: {server_id}")
        return "Server not found", 404
    return render_template('shell.html', server_id=server_id)

@socketio.on('connect_ssh')
def handle_ssh_connection(data):
    server_id = data.get('server_id')
    if server_id not in SERVERS:
        return

    # Grab initial terminal dimensions sent from the browser (fallback to 80x24)
    cols = data.get('cols', 80)
    rows = data.get('rows', 24)

    server = SERVERS[server_id]
    client_sid = request.sid  
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(server['host'], username=server['user'], password=server['password'], timeout=5)
        
        # Invoke shell with xterm-256color for full ANSI color support and exact initial dimensions
        channel = ssh.invoke_shell(term='xterm-256color', width=cols, height=rows)
        
        active_sessions[client_sid] = {
            'ssh': ssh,
            'channel': channel
        }
        logging.info(f"SUCCESS: SSH session established for {server['host']} ({cols}x{rows})")

        def listen_to_ssh(sid, chan):
            while not chan.closed:
                try:
                    data = chan.recv(4096)
                    if not data:
                        break
                    # errors='replace' prevents crashes if a multi-byte UTF-8 character splits across chunks
                    output = data.decode('utf-8', errors='replace')
                    socketio.emit('ssh_output', {'output': output}, to=sid)
                except Exception:
                    break

        socketio.start_background_task(listen_to_ssh, client_sid, channel)

    except Exception as e:
        socketio.emit('ssh_output', {'output': f'\r\n\x1b[1;31m[!] Connection failed: {str(e)}\x1b[0m\r\n'}, to=client_sid)

@socketio.on('ssh_input')
def handle_input(input_data):
    session = active_sessions.get(request.sid)
    if session:
        channel = session['channel']
        if not channel.closed:
            channel.send(input_data.get('input', ''))

@socketio.on('ssh_resize')
def handle_resize(data):
    session = active_sessions.get(request.sid)
    if session:
        channel = session['channel']
        if not channel.closed:
            cols = data.get('cols', 80)
            rows = data.get('rows', 24)
            try:
                # Dynamically resize the Linux PTY so line-wrapping and nano/htop/vim scale properly
                channel.resize_pty(width=cols, height=rows)
            except Exception as e:
                logging.error(f"Failed to resize PTY: {e}")

@socketio.on('disconnect')
def handle_disconnect():
    session_id = request.sid
    if session_id in active_sessions:
        try:
            active_sessions[session_id]['channel'].close()
            active_sessions[session_id]['ssh'].close()
        except Exception:
            pass
        del active_sessions[session_id]
        
    # Cleanup orphaned streams
    if session_id in stream_browsers:
        try:
            stream_browsers[session_id].quit()
        except Exception:
            pass
        del stream_browsers[session_id]

# ==========================================
# MODULE 4: Simple Reverse Proxy (Reverted)
# ==========================================
def perform_proxy(scheme, target_host, target_port, subpath):
    subpath = subpath.lstrip('/')
    target_url = f"{scheme}://{target_host}:{target_port}/{subpath}"
    
    if request.query_string:
        target_url = f"{target_url}?{request.query_string.decode('utf-8')}"
        
    try:
        # Spoof inbound headers so the target app accepts the request
        req_headers = {}
        for key, value in request.headers:
            k_lower = key.lower()
            if k_lower == 'host':
                continue
            elif k_lower == 'origin':
                req_headers[key] = f"{scheme}://{target_host}:{target_port}"
            elif k_lower == 'referer':
                req_headers[key] = f"{scheme}://{target_host}:{target_port}/"
            else:
                req_headers[key] = value
        
        proxied_response = requests.request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=15,
            verify=False 
        )
        
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        resp_headers = []
        
        if target_host == '127.0.0.1' and scheme == 'http':
            base_proxy_url = f"/port/{target_port}"
        elif scheme == 'https':
            base_proxy_url = f"/https/{target_host}:{target_port}"
        else:
            base_proxy_url = f"/address/{target_host}:{target_port}"
            
        # Rewrite Redirects and Cookies
        for key, value in proxied_response.raw.headers.items():
            if key.lower() not in excluded_headers:
                if key.lower() == 'location':
                    if value.startswith('/'):
                        value = base_proxy_url + value
                    else:
                        value = value.replace(f"{scheme}://{target_host}:{target_port}", base_proxy_url)
                        value = value.replace(f"{scheme}://{target_host}", base_proxy_url)
                
                elif key.lower() == 'set-cookie':
                    value = re.sub(r';\s*Domain=[^;]+', '', value, flags=re.IGNORECASE)
                    value = re.sub(r';\s*Path=[^;]+', '; Path=/', value, flags=re.IGNORECASE)

                resp_headers.append((key, value))
        
        # Create the response object
        response = Response(
            proxied_response.iter_content(chunk_size=10*1024), 
            proxied_response.status_code, 
            resp_headers
        )
        
        # STICKY SESSION: Remember the target so SPA client-side routing doesn't break
        target_identifier = f"{scheme}://{target_host}:{target_port}"
        response.set_cookie('dws_active_target', target_identifier, path='/')
        
        return response

    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Connection Refused", "details": f"Target {target_host}:{target_port} inaccessible."}), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Gateway Timeout", "details": f"Target {target_host}:{target_port} timed out."}), 504
    except Exception as e:
        return jsonify({"error": "Internal Proxy Error", "details": str(e)}), 500


@app.route('/port/<int:target_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/port/<int:target_port>/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/port/<int:target_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def local_port_proxy(target_port, subpath=""):
    if not subpath and not request.path.endswith('/'):
        qs = request.query_string.decode('utf-8')
        return redirect(f"{request.path}/" + (f"?{qs}" if qs else ""))
    # FIXED: Use 127.0.0.1 instead of 0.0.0.0 so Docker container loopback bindings accept connections
    return perform_proxy('http', '127.0.0.1', target_port, subpath)


@app.route('/address/<host_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/address/<host_port>/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/address/<host_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def remote_http_proxy(host_port, subpath=""):
    if not subpath and not request.path.endswith('/'):
        qs = request.query_string.decode('utf-8')
        return redirect(f"{request.path}/" + (f"?{qs}" if qs else ""))
        
    try:
        if ':' in host_port:
            host, port_str = host_port.rsplit(':', 1)
            port = int(port_str)
        else:
            host, port = host_port, 80
    except ValueError:
        return jsonify({"error": "Invalid Port"}), 400
    return perform_proxy('http', host, port, subpath)


@app.route('/https/<host_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/https/<host_port>/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/https/<host_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def remote_https_proxy(host_port, subpath=""):
    if not subpath and not request.path.endswith('/'):
        qs = request.query_string.decode('utf-8')
        return redirect(f"{request.path}/" + (f"?{qs}" if qs else ""))
        
    try:
        if ':' in host_port:
            host, port_str = host_port.rsplit(':', 1)
            port = int(port_str)
        else:
            host, port = host_port, 443
    except ValueError:
        return jsonify({"error": "Invalid Port"}), 400
    return perform_proxy('https', host, port, subpath)

# ==========================================
# MODULE 5: Remote Browser Isolation (Stream)
# ==========================================
STREAM_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>DWS Remote Stream</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { margin: 0; background: #000; display: flex; justify-content: center; align-items: center; height: 100vh; width: 100vw; overflow: hidden; font-family: sans-serif; color: white; }
        /* Using contain ensures the aspect ratio stays perfect so the coordinates don't distort */
        img { width: 100vw; height: 100vh; object-fit: contain; cursor: crosshair; }
        #loading { position: absolute; font-size: 20px; text-shadow: 1px 1px 2px black; }
    </style>
</head>
<body>
    <div id="loading">Booting Hardware-Accelerated Stream...</div>
    <img id="stream-display" src="" />

    <script>
        const socket = io();
        const img = document.getElementById('stream-display');
        const loading = document.getElementById('loading');

        socket.on('connect', () => {
            socket.emit('start_stream', { 
                url: "{{ target_url }}",
                width: window.innerWidth,
                height: window.innerHeight
            });
        });

        socket.on('stream_frame', function(data) {
            if (loading) loading.style.display = 'none';
            // We are now receiving highly compressed JPEGs straight from Chrome
            img.src = "data:image/jpeg;base64," + data.image;
        });

        img.addEventListener('click', function(e) {
            const rect = img.getBoundingClientRect();
            // Calculate exact coordinates relative to the actual image size inside the letterbox
            const scaleX = img.naturalWidth / rect.width;
            const scaleY = img.naturalHeight / rect.height;
            const clickX = Math.round((e.clientX - rect.left) * scaleX);
            const clickY = Math.round((e.clientY - rect.top) * scaleY);
            
            socket.emit('stream_click', { x: clickX, y: clickY });
        });

        window.addEventListener('keydown', function(e) {
            if(["Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].indexOf(e.code) > -1) {
                e.preventDefault();
            }
            socket.emit('stream_keypress', { key: e.key });
        });
    </script>
</body>
</html>
"""

@app.route('/stream/<proxy_type>/<path:target>')
def stream_route(proxy_type, target):
    if not SELENIUM_AVAILABLE:
        return "<h3>Selenium is missing!</h3><p>To use the Stream feature, please run <code>pip install selenium</code> on the host machine.</p>", 500
        
    if proxy_type == 'port': target_url = f"http://0.0.0.0:{target}"
    elif proxy_type == 'address': target_url = f"http://{target}"
    elif proxy_type == 'https': target_url = f"https://{target}"
    else: return "Invalid stream type", 400
        
    return render_template_string(STREAM_HTML, target_url=target_url)

@socketio.on('start_stream')
def handle_start_stream(data):
    if not SELENIUM_AVAILABLE: return
    
    url = data['url']
    width = int(data.get('width', 1920))
    height = int(data.get('height', 1080))
    client_sid = request.sid
    
    logging.info(f"Booting optimized {width}x{height} stream for {url}...")
    
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument(f"--window-size={width},{height}")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--ignore-certificate-errors")
    
    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.get(url)
        stream_browsers[client_sid] = driver
    except Exception as e:
        logging.error(f"Failed to start headless browser: {e}")
        return
        
    def stream_loop():
        while client_sid in stream_browsers:
            try:
                # OPTIMIZATION: Shift processing to Chrome. Ask for a 60% quality JPEG instead of a lossless PNG.
                # This drops the file size from ~4MB to ~150KB, massively increasing stream speed!
                res = stream_browsers[client_sid].execute_cdp_cmd('Page.captureScreenshot', {
                    'format': 'jpeg',
                    'quality': 60
                })
                
                socketio.emit('stream_frame', {'image': res['data']}, to=client_sid)
                eventlet.sleep(0.1) # 10 Frames Per Second
            except Exception as e:
                break
    
    socketio.start_background_task(stream_loop)

@socketio.on('stream_click')
def handle_stream_click(data):
    client_sid = request.sid
    if client_sid in stream_browsers:
        driver = stream_browsers[client_sid]
        x, y = int(data['x']), int(data['y'])
        try:
            # FIX: Hardware-level Mouse Clicks via Chrome DevTools Protocol
            # This completely bypasses React/Vue's fake click blockers
            driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1
            })
            driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
                'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1
            })
        except Exception as e:
            logging.error(f"Stream click failed: {e}")

@socketio.on('stream_keypress')
def handle_stream_keypress(data):
    client_sid = request.sid
    if client_sid in stream_browsers:
        driver = stream_browsers[client_sid]
        key = data['key']
        try:
            active = driver.switch_to.active_element
            if key == 'Enter': active.send_keys('\ue007')
            elif key == 'Backspace': active.send_keys('\ue003')
            elif len(key) == 1: active.send_keys(key)
        except:
            pass

# ==========================================
# MODULE 7: LAN & Docker Port Finder
# ==========================================
import socket
from concurrent.futures import ThreadPoolExecutor

COMMON_PORTS = [80, 443, 3000, 5000, 5050, 8000, 8080, 8123, 8989, 9000, 9090, 6881]

def scan_single_target(ip, port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        result = s.connect_ex((ip, port))
        s.close()
        if result == 0:
            # Try to grab HTTP title or banner
            scheme = "https" if port == 443 or port == 8443 else "http"
            url = f"{scheme}://{ip}:{port}"
            title = "Active Service"
            try:
                resp = requests.get(url, timeout=0.8, verify=False)
                if resp.status_code < 400:
                    match = re.search(r'<title>(.*?)</title>', resp.text, re.IGNORECASE)
                    if match:
                        title = match.group(1).strip()[:40]
            except Exception:
                pass
            return {"ip": ip, "port": port, "url": url, "title": title, "status": "Online"}
    except Exception:
        pass
    return None

FINDER_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>DWS LAN Finder</title>
    <style>
        body { margin: 0; padding: 24px; background: #0d1117; color: #c9d1d9; font-family: monospace; }
        h1 { color: #58a6ff; margin-top: 0; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 20px; }
        button { background: #238636; color: white; border: none; padding: 10px 16px; border-radius: 4px; cursor: pointer; font-family: monospace; font-weight: bold; }
        button:hover { background: #2ea043; }
        table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 12px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #21262d; }
        th { color: #8b949e; }
        a { color: #58a6ff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .badge { padding: 3px 8px; border-radius: 4px; background: #238636; color: #fff; font-size: 12px; }
    </style>
</head>
<body>
    <h1>DWS LAN & Host Finder</h1>
    <div class="card">
        <p>Scans local loopback ports, local docker bridges, and active subnet IPs for web services and APIs.</p>
        <button onclick="startScan()" id="scan-btn">Scan Network Now</button>
        <span id="status-msg" style="margin-left: 15px; color: #8b949e;">Ready.</span>
    </div>
    <div class="card">
        <h2>Discovered Endpoints</h2>
        <table>
            <thead>
                <tr><th>IP / Host</th><th>Port</th><th>Detected Title / Service</th><th>Proxy Link</th></tr>
            </thead>
            <tbody id="results-table">
                <tr><td colspan="4" style="color: #8b949e;">Click 'Scan Network Now' to begin.</td></tr>
            </tbody>
        </table>
    </div>
    <script>
        async function startScan() {
            const btn = document.getElementById('scan-btn');
            const msg = document.getElementById('status-msg');
            const tbody = document.getElementById('results-table');
            btn.disabled = true;
            msg.innerText = "Scanning network ports and hosts...";
            tbody.innerHTML = `<tr><td colspan="4" style="color: #d29922;">Scan in progress... Please wait.</td></tr>`;
            
            try {
                const res = await fetch('/finder/scan');
                const data = await res.json();
                msg.innerText = `Scan complete. Found ${data.services.length} active service(s).`;
                if(data.services.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="4">No services discovered.</td></tr>`;
                } else {
                    tbody.innerHTML = data.services.map(s => `
                        <tr>
                            <td><b>${s.ip}</b></td>
                            <td>${s.port}</td>
                            <td>${s.title}</td>
                            <td><a href="/address/${s.ip}:${s.port}" target="_blank">Open Proxy (/address/${s.ip}:${s.port})</a></td>
                        </tr>
                    `).join('');
                }
            } catch(e) {
                msg.innerText = "Scan failed.";
                tbody.innerHTML = `<tr><td colspan="4" style="color: #da3633;">Error running scan.</td></tr>`;
            }
            btn.disabled = false;
        }
    </script>
</body>
</html>
"""

@app.route('/finder', methods=['GET'])
def finder_page():
    return render_template_string(FINDER_DASHBOARD_HTML)

@app.route('/finder/scan', methods=['GET'])
def finder_scan_api():
    discovered = []
    targets = ["127.0.0.1", "localhost", "192.168.2.143", "192.168.2.91", "192.168.2.134"]
    
    # Add gateway or local subnet range scan if desired
    # We use ThreadPoolExecutor for fast concurrent scanning
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = []
        for ip in targets:
            for port in COMMON_PORTS:
                futures.append(executor.submit(scan_single_target, ip, port))
        
        for future in futures:
            res = future.result()
            if res:
                # Deduplicate
                if not any(d['ip'] == res['ip'] and d['port'] == res['port'] for d in discovered):
                    discovered.append(res)
                    
    return jsonify({"services": discovered})

# ==========================================
# MODULE 6: DWS Compute Engine (Unload -> Process -> Return)
# ==========================================

PYTHON_RUNNER_TEMPLATE = """import json, sys, os, traceback

with open("input.json", "r", encoding="utf-8") as f:
    INPUT = json.load(f)

RESULT = None
INPUT_FILE = os.environ.get("DWS_INPUT_FILE", "")
OUTPUT_FILE = os.environ.get("DWS_OUTPUT_FILE", "output.bin")

# --- BEGIN USER PAYLOAD ---
{user_code}
# --- END USER PAYLOAD ---

with open("output.json", "w", encoding="utf-8") as f:
    try:
        json.dump(RESULT, f)
    except TypeError:
        json.dump(str(RESULT), f)
"""

def select_compute_target(requested_target):
    """Selects 'local' or server ID ('1', '2', '3'), or least-loaded node on 'auto'."""
    if requested_target in ("local", "1", "2", "3"):
        return requested_target
    if requested_target == "auto":
        return min(node_active_counts, key=node_active_counts.get)
    return "local"

def prune_job_history():
    """Keeps compute_jobs dictionary bounded in memory."""
    if len(compute_jobs) > MAX_JOB_HISTORY:
        completed = [
            (jid, j["created_at"]) for jid, j in compute_jobs.items()
            if j["status"] in ("completed", "failed", "timeout", "cancelled")
        ]
        completed.sort(key=lambda x: x[1])
        for jid, _ in completed[:len(compute_jobs) - MAX_JOB_HISTORY]:
            compute_jobs.pop(jid, None)

def parse_compute_request(req):
    """Normalizes either application/json or multipart/form-data requests into a job spec."""
    if req.content_type and req.content_type.startswith("multipart/form-data"):
        form = req.form
        raw_input = form.get("input_data", "{}")
        try:
            input_data = json.loads(raw_input)
        except Exception:
            input_data = {"raw": raw_input}

        spec = {
            "type": form.get("type", "file"),
            "target": form.get("target", "local"),
            "code": form.get("code", ""),
            "command": form.get("command", ""),
            "input_data": input_data,
            "timeout": float(form.get("timeout", 60)),
            "callback_url": form.get("callback_url"),
            "output_filename": form.get("output_filename", "output.bin"),
            "return_raw": form.get("return_raw", "false").lower() == "true",
            "env": {}
        }
        if "file" in req.files:
            up_file = req.files["file"]
            spec["input_filename"] = os.path.basename(up_file.filename or "input.bin")
            spec["input_bytes"] = up_file.read()
        return spec

    data = req.get_json(silent=True) or {}
    spec = {
        "type": data.get("type", "python"),
        "target": str(data.get("target", "local")),
        "code": data.get("code", ""),
        "command": data.get("command", ""),
        "input_data": data.get("input_data", {}),
        "timeout": float(data.get("timeout", 60)),
        "callback_url": data.get("callback_url"),
        "output_filename": data.get("output_filename", "output.bin"),
        "return_raw": bool(data.get("return_raw", False)),
        "env": data.get("env", {})
    }
    if data.get("file_b64"):
        spec["input_filename"] = os.path.basename(data.get("filename", "input.bin"))
        spec["input_bytes"] = base64.b64decode(data["file_b64"])
    return spec

def emit_compute_telemetry(job_meta, stream_sid=None):
    """Broadcasts job updates to the live dashboard and optional SocketIO caller."""
    summary = {
        "job_id": job_meta["job_id"],
        "status": job_meta["status"],
        "type": job_meta["type"],
        "target": job_meta["target"],
        "exec_time_ms": job_meta["exec_time_ms"],
        "exit_code": job_meta["exit_code"],
        "error": job_meta["error"]
    }
    socketio.emit("compute_dashboard_update", summary)
    if stream_sid:
        socketio.emit("compute_status", summary, to=stream_sid)

def execute_local_job(job_id, job_dir, cmd, env, timeout, stream_sid=None):
    """Executes a compute job locally in its own OS process group without blocking Eventlet."""
    proc = subprocess.Popen(
        cmd,
        cwd=job_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None
    )
    compute_processes[job_id] = {"type": "local", "handle": proc}
    start_ts = time.time()

    while proc.poll() is None:
        if time.time() - start_ts > timeout:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            return "", "Execution exceeded timeout limit.", -1, True

        if compute_jobs[job_id]["status"] == "cancelled":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            return "", "Job cancelled by user.", -1, False

        eventlet.sleep(0.02)

    stdout_bytes, stderr_bytes = proc.communicate()
    stdout_str = stdout_bytes.decode("utf-8", errors="replace")
    stderr_str = stderr_bytes.decode("utf-8", errors="replace")

    if stream_sid and (stdout_str or stderr_str):
        socketio.emit("compute_progress", {
            "job_id": job_id,
            "stdout": stdout_str,
            "stderr": stderr_str
        }, to=stream_sid)

    return stdout_str, stderr_str, proc.returncode, False

def execute_ssh_job(job_id, server_id, job_dir, remote_cmd, output_filename, timeout, stream_sid=None):
    """Offloads the staged job directory over SFTP/SSH to a cluster node in SERVERS."""
    server = SERVERS[server_id]
    remote_dir = f"/dev/shm/dws_compute_{job_id}"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(server["host"], username=server["user"], password=server["password"], timeout=8)
    sftp = ssh.open_sftp()
    compute_processes[job_id] = {"type": "ssh", "handle": ssh}

    try:
        try:
            sftp.mkdir(remote_dir)
        except IOError:
            pass

        # Upload all staged files from local RAM-disk to remote node RAM-disk
        for fname in os.listdir(job_dir):
            local_fpath = os.path.join(job_dir, fname)
            if os.path.isfile(local_fpath):
                sftp.put(local_fpath, f"{remote_dir}/{fname}")

        full_remote_cmd = f"cd {remote_dir} && {remote_cmd}"
        stdin, stdout, stderr = ssh.exec_command(full_remote_cmd)
        channel = stdout.channel

        start_ts = time.time()
        stdout_chunks, stderr_chunks = [], []

        while not channel.exit_status_ready():
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                stdout_chunks.append(chunk)
                if stream_sid:
                    socketio.emit("compute_progress", {"job_id": job_id, "stdout": chunk}, to=stream_sid)

            if channel.recv_stderr_ready():
                err_chunk = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                stderr_chunks.append(err_chunk)
                if stream_sid:
                    socketio.emit("compute_progress", {"job_id": job_id, "stderr": err_chunk}, to=stream_sid)

            if time.time() - start_ts > timeout:
                channel.close()
                ssh.exec_command(f"rm -rf {remote_dir}")
                return "".join(stdout_chunks), "Remote SSH execution timed out.", -1, True

            if compute_jobs[job_id]["status"] == "cancelled":
                channel.close()
                ssh.exec_command(f"rm -rf {remote_dir}")
                return "".join(stdout_chunks), "Job cancelled by user.", -1, False

            eventlet.sleep(0.03)

        # Drain remaining buffers
        while channel.recv_ready():
            stdout_chunks.append(channel.recv(4096).decode("utf-8", errors="replace"))
        while channel.recv_stderr_ready():
            stderr_chunks.append(channel.recv_stderr(4096).decode("utf-8", errors="replace"))

        exit_code = channel.recv_exit_status()

        # Pull back output.json or transformed output file if generated
        for pull_file in ("output.json", output_filename):
            if pull_file:
                try:
                    sftp.get(f"{remote_dir}/{pull_file}", os.path.join(job_dir, pull_file))
                except IOError:
                    pass

        # Clean up remote RAM-disk
        ssh.exec_command(f"rm -rf {remote_dir}")
        return "".join(stdout_chunks), "".join(stderr_chunks), exit_code, False

    finally:
        try:
            sftp.close()
        except Exception:
            pass
        try:
            ssh.close()
        except Exception:
            pass

def run_compute_pipeline(job_id, spec, stream_sid=None):
    """Core unload -> process -> return lifecycle runner."""
    job_meta = compute_jobs[job_id]
    target = job_meta["target"]
    job_dir = os.path.join(COMPUTE_RAMDISK, job_id)
    os.makedirs(job_dir, exist_ok=True)

    node_active_counts[target] = node_active_counts.get(target, 0) + 1
    job_meta["status"] = "running"
    job_meta["started_at"] = time.time()
    emit_compute_telemetry(job_meta, stream_sid)

    try:
        # 1. Stage input JSON data
        with open(os.path.join(job_dir, "input.json"), "w", encoding="utf-8") as f:
            json.dump(spec.get("input_data", {}), f)

        # 2. Stage binary input file if provided
        input_filename = spec.get("input_filename", "")
        output_filename = spec.get("output_filename", "output.bin")
        if spec.get("input_bytes"):
            with open(os.path.join(job_dir, input_filename), "wb") as f:
                f.write(spec["input_bytes"])

        # 3. Build execution command & environment
        env = os.environ.copy()
        for k, v in (spec.get("env") or {}).items():
            env[str(k)] = str(v)
        env["DWS_JOB_ID"] = job_id
        env["DWS_INPUT_JSON"] = json.dumps(spec.get("input_data", {}))
        env["DWS_INPUT_FILE"] = input_filename
        env["DWS_OUTPUT_FILE"] = output_filename

        job_type = spec["type"]
        if job_type == "python":
            runner_code = PYTHON_RUNNER_TEMPLATE.format(user_code=spec.get("code", ""))
            with open(os.path.join(job_dir, "runner.py"), "w", encoding="utf-8") as f:
                f.write(runner_code)
            local_cmd = [sys.executable, "runner.py"]
            remote_cmd = f"DWS_INPUT_FILE='{input_filename}' DWS_OUTPUT_FILE='{output_filename}' python3 runner.py"
        elif job_type in ("shell", "file"):
            shell_script = spec.get("command") or spec.get("code") or "echo 'No command provided'"
            with open(os.path.join(job_dir, "runner.sh"), "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n" + shell_script + "\n")
            os.chmod(os.path.join(job_dir, "runner.sh"), 0o755)
            local_cmd = ["/bin/sh", "runner.sh"]
            remote_cmd = f"DWS_INPUT_FILE='{input_filename}' DWS_OUTPUT_FILE='{output_filename}' /bin/sh runner.sh"
        else:
            raise ValueError(f"Unsupported compute type: {job_type}")

        # 4. Dispatch to Local OS Process or Remote Cluster SSH Node
        timeout = spec.get("timeout", 60)
        if target == "local":
            stdout_str, stderr_str, exit_code, timed_out = execute_local_job(
                job_id, job_dir, local_cmd, env, timeout, stream_sid
            )
        elif target in SERVERS:
            stdout_str, stderr_str, exit_code, timed_out = execute_ssh_job(
                job_id, target, job_dir, remote_cmd, output_filename, timeout, stream_sid
            )
        else:
            raise ValueError(f"Unknown compute target: {target}")

        # 5. Collect output payload & artifacts
        job_meta["stdout"] = stdout_str
        job_meta["stderr"] = stderr_str
        job_meta["exit_code"] = exit_code

        output_json_path = os.path.join(job_dir, "output.json")
        if os.path.exists(output_json_path):
            try:
                with open(output_json_path, "r", encoding="utf-8") as f:
                    job_meta["result"] = json.load(f)
            except Exception:
                pass
        elif job_type == "shell":
            # If shell command outputs valid JSON to stdout, parse it into result automatically
            stripped = stdout_str.strip()
            if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
                try:
                    job_meta["result"] = json.loads(stripped)
                except Exception:
                    job_meta["result"] = stripped
            else:
                job_meta["result"] = stripped

        output_file_path = os.path.join(job_dir, output_filename)
        if os.path.exists(output_file_path) and os.path.isfile(output_file_path):
            with open(output_file_path, "rb") as f:
                raw_out = f.read()
            job_meta["output_file_b64"] = base64.b64encode(raw_out).decode("ascii")
            job_meta["output_filename"] = output_filename

        if job_meta["status"] == "cancelled":
            pass
        elif timed_out:
            job_meta["status"] = "timeout"
            job_meta["error"] = f"Job timed out after {timeout}s"
        elif exit_code == 0:
            job_meta["status"] = "completed"
        else:
            job_meta["status"] = "failed"
            job_meta["error"] = stderr_str.strip() or f"Process exited with code {exit_code}"

    except Exception as e:
        job_meta["status"] = "failed"
        job_meta["error"] = str(e)
        logging.error(f"Compute job {job_id} error: {e}")

    finally:
        job_meta["completed_at"] = time.time()
        job_meta["exec_time_ms"] = round((job_meta["completed_at"] - job_meta["started_at"]) * 1000, 2)
        node_active_counts[target] = max(0, node_active_counts.get(target, 1) - 1)
        compute_processes.pop(job_id, None)
        shutil.rmtree(job_dir, ignore_errors=True)
        emit_compute_telemetry(job_meta, stream_sid)

        if stream_sid:
            socketio.emit("compute_result", job_meta, to=stream_sid)

        # Optional Webhook Callback for Async Jobs
        callback_url = spec.get("callback_url")
        if callback_url:
            try:
                requests.post(callback_url, json=job_meta, timeout=10, verify=False)
            except Exception as cb_err:
                logging.warning(f"Webhook callback failed for job {job_id}: {cb_err}")

def create_job_record(spec):
    prune_job_history()
    job_id = uuid.uuid4().hex[:12]
    target = select_compute_target(spec.get("target", "local"))
    job_meta = {
        "job_id": job_id,
        "status": "queued",
        "type": spec.get("type", "python"),
        "target": target,
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "exec_time_ms": None,
        "result": None,
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "error": None,
        "output_file_b64": None,
        "output_filename": None
    }
    compute_jobs[job_id] = job_meta
    return job_id, job_meta

@app.route('/compute/run', methods=['POST'])
def compute_run_sync():
    """Synchronous Unload -> Process -> Return endpoint."""
    spec = parse_compute_request(request)
    job_id, job_meta = create_job_record(spec)
    run_compute_pipeline(job_id, spec)

    # Stream raw file directly back if requested and available
    if spec.get("return_raw") and job_meta.get("output_file_b64"):
        raw_bytes = base64.b64decode(job_meta["output_file_b64"])
        return send_file(
            io.BytesIO(raw_bytes),
            as_attachment=True,
            download_name=job_meta["output_filename"] or "output.bin",
            mimetype="application/octet-stream"
        )

    status_code = 200 if job_meta["status"] == "completed" else 500
    return jsonify(job_meta), status_code

@app.route('/compute/enqueue', methods=['POST'])
def compute_enqueue_async():
    """Asynchronous Fire-and-Forget endpoint. Returns job_id immediately."""
    spec = parse_compute_request(request)
    job_id, job_meta = create_job_record(spec)
    socketio.start_background_task(run_compute_pipeline, job_id, spec)
    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "target": job_meta["target"],
        "poll_url": f"/compute/job/{job_id}",
        "cancel_url": f"/compute/cancel/{job_id}"
    }), 202

@app.route('/compute/job/<job_id>', methods=['GET'])
def compute_get_job(job_id):
    """Polls status or downloads output artifact of an enqueued job."""
    job_meta = compute_jobs.get(job_id)
    if not job_meta:
        return jsonify({"error": "Job ID not found"}), 404

    if request.args.get("download") == "1" and job_meta.get("output_file_b64"):
        raw_bytes = base64.b64decode(job_meta["output_file_b64"])
        return send_file(
            io.BytesIO(raw_bytes),
            as_attachment=True,
            download_name=job_meta["output_filename"] or "output.bin",
            mimetype="application/octet-stream"
        )
    return jsonify(job_meta)

@app.route('/compute/cancel/<job_id>', methods=['POST'])
def compute_cancel_job(job_id):
    """Terminates a running local or remote SSH compute process."""
    job_meta = compute_jobs.get(job_id)
    if not job_meta:
        return jsonify({"error": "Job ID not found"}), 404

    if job_meta["status"] in ("queued", "running"):
        job_meta["status"] = "cancelled"
        proc_info = compute_processes.get(job_id)
        if proc_info:
            try:
                if proc_info["type"] == "local":
                    os.killpg(os.getpgid(proc_info["handle"].pid), signal.SIGKILL)
                elif proc_info["type"] == "ssh":
                    proc_info["handle"].close()
            except Exception:
                pass
    return jsonify({"job_id": job_id, "status": job_meta["status"]})

@app.route('/compute/status', methods=['GET'])
def compute_system_status():
    """Returns host hardware telemetry, RAM-disk space, and cluster job distribution."""
    load_avg = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    shm_usage = shutil.disk_usage(COMPUTE_RAMDISK)
    recent_jobs = sorted(compute_jobs.values(), key=lambda x: x["created_at"], reverse=True)[:25]
    summary_list = [{
        "job_id": j["job_id"],
        "status": j["status"],
        "type": j["type"],
        "target": j["target"],
        "exec_time_ms": j["exec_time_ms"],
        "exit_code": j["exit_code"],
        "error": j["error"]
    } for j in recent_jobs]

    return jsonify({
        "ramdisk_path": COMPUTE_RAMDISK,
        "ramdisk_free_mb": round(shm_usage.free / (1024 * 1024), 2),
        "cpu_load_avg_1_5_15": load_avg,
        "active_jobs_per_node": node_active_counts,
        "available_nodes": ["local"] + list(SERVERS.keys()),
        "recent_jobs": summary_list
    })

@socketio.on('compute_dispatch')
def handle_socket_compute(data):
    """WebSocket streaming compute handler."""
    client_sid = request.sid
    spec = {
        "type": data.get("type", "python"),
        "target": str(data.get("target", "local")),
        "code": data.get("code", ""),
        "command": data.get("command", ""),
        "input_data": data.get("input_data", {}),
        "timeout": float(data.get("timeout", 60)),
        "output_filename": data.get("output_filename", "output.bin"),
        "env": data.get("env", {})
    }
    job_id, _ = create_job_record(spec)
    socketio.start_background_task(run_compute_pipeline, job_id, spec, client_sid)

COMPUTE_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>DWS Compute Engine</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        body { margin: 0; padding: 24px; background: #0d1117; color: #c9d1d9; font-family: monospace; }
        h1, h2 { color: #58a6ff; margin-top: 0; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
        textarea, input, select { width: 100%; box-sizing: border-box; background: #0d1117; color: #58a6ff; border: 1px solid #30363d; border-radius: 4px; padding: 8px; font-family: monospace; margin-bottom: 10px; }
        button { background: #238636; color: white; border: none; padding: 10px 16px; border-radius: 4px; cursor: pointer; font-family: monospace; font-weight: bold; }
        button:hover { background: #2ea043; }
        pre { background: #010409; padding: 12px; border-radius: 6px; overflow-x: auto; max-height: 320px; border: 1px solid #21262d; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td { text-align: left; padding: 8px; border-bottom: 1px solid #21262d; }
        .badge { padding: 2px 6px; border-radius: 4px; font-size: 11px; }
        .completed { background: #238636; color: #fff; }
        .running, .queued { background: #9e6a03; color: #fff; }
        .failed, .timeout, .cancelled { background: #da3633; color: #fff; }
    </style>
</head>
<body>
    <h1>DWS Compute Engine // Unload-Process-Return Node</h1>
    <div class="grid">
        <div class="card">
            <h2>Dispatch Test Workload</h2>
            <label>Workload Type & Target Node</label>
            <div style="display:flex; gap:10px;">
                <select id="type">
                    <option value="python">Python (INPUT -> RESULT)</option>
                    <option value="shell">Shell Command</option>
                </select>
                <select id="target">
                    <option value="local">Local Host</option>
                    <option value="auto">Auto (Least Loaded Cluster Node)</option>
                    <option value="1">Server 1 (192.168.2.143)</option>
                    <option value="2">Server 2 (192.168.2.91)</option>
                    <option value="3">Server 3 (192.168.2.134)</option>
                </select>
            </div>
            <label>Input JSON (INPUT)</label>
            <input id="input_data" value='{"iterations": 250000}' />
            <label>Code / Command</label>
            <textarea id="code" rows="7">import math, platform
n = INPUT.get("iterations", 1000)
val = sum(math.sqrt(i) for i in range(n))
RESULT = {"node": platform.node(), "sum_sqrt": round(val, 3)}</textarea>
            <button onclick="dispatchJob()">Unload & Execute</button>
            <h3>Live Return Payload</h3>
            <pre id="output">Ready.</pre>
        </div>
        <div class="card">
            <h2>Cluster Telemetry & Job Stream</h2>
            <div id="telemetry" style="margin-bottom: 12px; color: #8b949e;">Loading status...</div>
            <table>
                <thead>
                    <tr><th>Job ID</th><th>Target</th><th>Type</th><th>Status</th><th>Time (ms)</th></tr>
                </thead>
                <tbody id="job-table"></tbody>
            </table>
        </div>
    </div>
    <script>
        const socket = io();
        async function refreshStatus() {
            const res = await fetch('/compute/status');
            const data = await res.json();
            document.getElementById('telemetry').innerText =
                `RAM-Disk Free: ${data.ramdisk_free_mb} MB | Load Avg: ${data.cpu_load_avg_1_5_15.join(', ')} | Active Jobs: ${JSON.stringify(data.active_jobs_per_node)}`;
            const tbody = document.getElementById('job-table');
            tbody.innerHTML = data.recent_jobs.map(j => `
                <tr>
                    <td>${j.job_id}</td>
                    <td>${j.target}</td>
                    <td>${j.type}</td>
                    <td><span class="badge ${j.status}">${j.status}</span></td>
                    <td>${j.exec_time_ms ?? '...'}</td>
                </tr>
            `).join('');
        }
        function dispatchJob() {
            document.getElementById('output').innerText = "Running workload...";
            let parsedInput = {};
            try { parsedInput = JSON.parse(document.getElementById('input_data').value); } catch(e) {}
            const payload = {
                type: document.getElementById('type').value,
                target: document.getElementById('target').value,
                input_data: parsedInput,
                code: document.getElementById('code').value,
                command: document.getElementById('code').value
            };
            socket.emit('compute_dispatch', payload);
        }
        socket.on('compute_result', (data) => {
            document.getElementById('output').innerText = JSON.stringify(data, null, 2);
            refreshStatus();
        });
        socket.on('compute_dashboard_update', () => refreshStatus());
        refreshStatus();
        setInterval(refreshStatus, 5000);
    </script>
</body>
</html>
"""

@app.route('/compute/dashboard', methods=['GET'])
def compute_dashboard():
    return render_template_string(COMPUTE_DASHBOARD_HTML)

# ==========================================
# CATCH-ALL ASSET & SPA REDIRECTOR
# ==========================================
@app.route('/', defaults={'missing_path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/<path:missing_path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def catch_all_fallback(missing_path):
    """
    Handles cases where the web app's client-side router drops the proxy prefix
    (e.g., changes the URL to /login). It uses the Referer or a sticky cookie to route it.
    """
    # 1. Try Referer first (Most accurate if present)
    referer = request.headers.get('Referer')
    if referer:
        parsed_referer = urlparse(referer)
        match = re.match(r'^/(port|address|https)/([^/]+)', parsed_referer.path)
        if match:
            proxy_type = match.group(1)
            host_port = match.group(2)
            correct_path = f"/{proxy_type}/{host_port}/{missing_path}"
            if request.query_string:
                correct_path += f"?{request.query_string.decode('utf-8')}"
            return redirect(correct_path, code=307)

    # 2. SPA Fallback: Use the sticky cookie we set during the initial load
    active_target = request.cookies.get('dws_active_target')
    if active_target:
        try:
            parsed_target = urlparse(active_target)
            scheme = parsed_target.scheme
            host = parsed_target.hostname
            # Default to standard ports if not specified in the URL
            port = parsed_target.port or (443 if scheme == 'https' else 80)
            
            # Proxy directly to prevent the browser's address bar from flickering
            return perform_proxy(scheme, host, port, missing_path)
        except Exception as e:
            logging.error(f"Fallback proxy error for SPA routing: {e}")

    return jsonify({
        "error": "Not Found", 
        "details": "No active target session found. Try loading the device through the main proxy URL first."
    }), 404


# ==========================================
# APP EXECUTION
# ==========================================

if __name__ == '__main__':


    logging.info("Starting DWS Server Shell backend...")
    
    # --- MODULE 2: Start the Gatekeeper ---
    gatekeeper_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gatekeeper.py')
    if os.path.exists(gatekeeper_script):
        logging.info("Launching Gatekeeper...")
        subprocess.Popen(
            ["gunicorn", "-w", "4", "-k", "gevent", "-b", "0.0.0.0:5050", "gatekeeper:app"],
            stdout=sys.stdout, stderr=sys.stderr, close_fds=True
        )

    # --- MODULE 3: Start Aurora AI ---
    aurora_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aurora.py')
    if os.path.exists(aurora_script):
        logging.info("Launching Aurora AI...")
        subprocess.Popen(
            [sys.executable, "aurora.py"],
            stdout=sys.stdout, stderr=sys.stderr, close_fds=True
        )
    
    socketio.run(app, host='0.0.0.0', port=5000)
