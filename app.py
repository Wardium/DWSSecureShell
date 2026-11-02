# THESE TWO LINES MUST BE AT THE VERY TOP
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, render_template_string, request, jsonify, Response, redirect, send_file, stream_with_context
from flask_socketio import SocketIO
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
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
import socket
import ssl
import traceback

# Suppress insecure request warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Safely import Selenium for the Stream feature
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
DWS_LISTEN_PORT = 5000
active_sessions = {}      # Stores {"ssh": ssh_client, "channel": ssh_channel} mapped by request.sid
enforced_devices = {}
stream_browsers = {}      # Tracks headless browsers for the Stream feature

# --- MODULE 6: COMPUTE ENGINE GLOBALS ---
COMPUTE_RAMDISK = "/dev/shm/dws_compute" if os.path.exists("/dev/shm") else os.path.join(tempfile.gettempdir(), "dws_compute")
os.makedirs(COMPUTE_RAMDISK, exist_ok=True)

compute_jobs = {}         # job_id -> job metadata & result dict
compute_processes = {}    # job_id -> {"type": "local"|"ssh", "handle": proc_or_ssh_client}
node_active_counts = {"local": 0, "1": 0, "2": 0, "3": 0}
MAX_JOB_HISTORY = 100     # Prevent unbounded memory growth

# ==========================================
# GLOBAL CRASH PROTECTION & ERROR HANDLERS
# ==========================================
@app.errorhandler(Exception)
def handle_unexpected_error(error):
    logging.error(f"Unhandled Flask Exception on {request.path}: {error}\n{traceback.format_exc()}")
    if request.path.startswith(('/compute/', '/finder/scan')):
        return jsonify({"error": "Internal Server Error", "details": str(error)}), 500
    return Response(
        f"""<!DOCTYPE html><html><body style="background:#0d1117;color:#c9d1d9;font-family:monospace;padding:24px;">
        <h2 style="color:#ff7b72;">DWS Guard // Request Handled Safely</h2>
        <p>The target service or route encountered an error, but DWS prevented a crash.</p>
        <pre style="background:#161b22;padding:12px;border:1px solid #30363d;border-radius:6px;color:#ffa657;">{str(error)}</pre>
        <a href="/finder" style="color:#58a6ff;">&larr; Return to DWS Finder</a>
        </body></html>""",
        status=502,
        mimetype='text/html'
    )

@socketio.on_error_default
def default_socket_error_handler(e):
    logging.error(f"Unhandled SocketIO Exception: {e}\n{traceback.format_exc()}")

# ==========================================
# MODULE 1: DWS Server Shell
# ==========================================
SERVERS = {
    "1": {"host": "192.168.2.143", "user": "dylan", "password": "weqr1234"},
    "2": {"host": "192.168.2.91", "user": "dylanwardstudios", "password": "weqr1234"},
    "3": {"host": "192.168.2.134", "user": "dylan", "password": "sIjkew-1qixwe-sogcog"}
}

@app.route('/shell/<server_id>')
def shell(server_id):
    if server_id not in SERVERS:
        logging.warning(f"404: Invalid Server ID accessed: {server_id}")
        return "Server not found", 404
    return render_template('shell.html', server_id=server_id)

@socketio.on('connect_ssh')
def handle_ssh_connection(data):
    data = data or {}
    server_id = str(data.get('server_id', ''))
    if server_id not in SERVERS:
        return

    cols = int(data.get('cols', 80))
    rows = int(data.get('rows', 24))

    server = SERVERS[server_id]
    client_sid = request.sid

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(server['host'], username=server['user'], password=server['password'], timeout=5)
        channel = ssh.invoke_shell(term='xterm-256color', width=cols, height=rows)

        active_sessions[client_sid] = {
            'ssh': ssh,
            'channel': channel
        }
        logging.info(f"SUCCESS: SSH session established for {server['host']} ({cols}x{rows})")

        def listen_to_ssh(sid, chan):
            while not chan.closed:
                try:
                    chunk = chan.recv(4096)
                    if not chunk:
                        break
                    output = chunk.decode('utf-8', errors='replace')
                    socketio.emit('ssh_output', {'output': output}, to=sid)
                except Exception:
                    break

        socketio.start_background_task(listen_to_ssh, client_sid, channel)

    except Exception as e:
        socketio.emit('ssh_output', {'output': f'\r\n\x1b[1;31m[!] Connection failed: {str(e)}\x1b[0m\r\n'}, to=client_sid)

@socketio.on('ssh_input')
def handle_input(input_data):
    session = active_sessions.get(request.sid)
    if session and isinstance(input_data, dict):
        channel = session['channel']
        if not channel.closed:
            try:
                channel.send(input_data.get('input', ''))
            except Exception as e:
                logging.error(f"SSH send error: {e}")

@socketio.on('ssh_resize')
def handle_resize(data):
    session = active_sessions.get(request.sid)
    if session and isinstance(data, dict):
        channel = session['channel']
        if not channel.closed:
            cols = int(data.get('cols', 80))
            rows = int(data.get('rows', 24))
            try:
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
        active_sessions.pop(session_id, None)

    if session_id in stream_browsers:
        try:
            stream_browsers[session_id].quit()
        except Exception:
            pass
        stream_browsers.pop(session_id, None)

# ==========================================
# MODULE 4: Hardened Reverse Proxy
# ==========================================
# Complete list of PEP 3333 WSGI hop-by-hop headers that crash Eventlet if passed through
HOP_BY_HOP_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade',
    'content-encoding', 'content-length', 'alt-svc'
}

def get_local_ips():
    """Returns all IP addresses belonging to this host to prevent self-proxy infinite loops."""
    ips = {'127.0.0.1', 'localhost', '0.0.0.0', '::1'}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips

LOCAL_HOST_IPS = get_local_ips()

def parse_host_port(host_port_str, default_port):
    """Safely parses host:port strings including IPv6 brackets."""
    host_port_str = host_port_str.strip()
    if host_port_str.startswith('['):
        if ']:' in host_port_str:
            host, port_s = host_port_str.rsplit(']:', 1)
            return host + ']', int(port_s)
        return host_port_str, default_port
    if ':' in host_port_str:
        host, port_s = host_port_str.rsplit(':', 1)
        return host, int(port_s)
    return host_port_str, default_port

def perform_proxy(scheme, target_host, target_port, subpath):
    # Prevent infinite self-proxy recursion if user clicks port 5000
    if int(target_port) == DWS_LISTEN_PORT and target_host in LOCAL_HOST_IPS:
        return jsonify({
            "error": "Self-Proxy Blocked",
            "details": f"Port {DWS_LISTEN_PORT} is the DWS server itself. Proxying it into itself is blocked to prevent an infinite loop."
        }), 400

    subpath = subpath.lstrip('/')
    qs = f"?{request.query_string.decode('utf-8', errors='replace')}" if request.query_string else ""

    def send_upstream(use_scheme):
        url = f"{use_scheme}://{target_host}:{target_port}/{subpath}{qs}"
        req_headers = {}
        for key, value in request.headers:
            k_lower = key.lower()
            # Strip host, length, and encoding so requests/urllib3 handles decompression cleanly
            if k_lower in ('host', 'content-length', 'transfer-encoding', 'accept-encoding', 'connection'):
                continue
            elif k_lower == 'origin':
                req_headers[key] = f"{use_scheme}://{target_host}:{target_port}"
            elif k_lower == 'referer':
                req_headers[key] = f"{use_scheme}://{target_host}:{target_port}/"
            else:
                req_headers[key] = value

        # CRITICAL FIX for Port 8380 garbage: Force gzip/deflate only (blocks Brotli 'br' & 'zstd' raw binary dumps)
        req_headers['Accept-Encoding'] = 'gzip, deflate'

        body = request.get_data() if request.method not in ('GET', 'HEAD', 'OPTIONS') else None

        return requests.request(
            method=request.method,
            url=url,
            headers=req_headers,
            data=body,
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=(5, 20),
            verify=False
        )

    try:
        try:
            proxied_response = send_upstream(scheme)
            # Auto-fallback: If an HTTPS port was accessed via HTTP (Nginx/Apache returns 400 plain HTTP sent to HTTPS port)
            if scheme == 'http' and proxied_response.status_code == 400:
                peek = proxied_response.content[:256].decode('utf-8', errors='ignore')
                if 'HTTPS' in peek or 'SSL' in peek:
                    proxied_response.close()
                    scheme = 'https'
                    proxied_response = send_upstream('https')
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            # If HTTP connection reset (common when hitting a TLS port with plain HTTP), try HTTPS automatically
            if scheme == 'http':
                scheme = 'https'
                proxied_response = send_upstream('https')
            else:
                raise

        if target_host in ('127.0.0.1', 'localhost') and scheme == 'http':
            base_proxy_url = f"/port/{target_port}"
        elif scheme == 'https':
            base_proxy_url = f"/https/{target_host}:{target_port}"
        else:
            base_proxy_url = f"/address/{target_host}:{target_port}"

        resp_headers = []
        # Identify dynamicConnection-specified hop-by-hop headers too
        conn_header = proxied_response.headers.get('Connection', '')
        dynamic_hop_headers = {h.strip().lower() for h in conn_header.split(',') if h.strip()}

        for key, value in proxied_response.raw.headers.items():
            k_lower = key.lower()
            if k_lower in HOP_BY_HOP_HEADERS or k_lower in dynamic_hop_headers:
                continue

            # Ensure header values do not contain control characters that crash WSGI
            clean_val = str(value).replace('\r', '').replace('\n', ' ')

            if k_lower == 'location':
                if clean_val.startswith('/'):
                    clean_val = base_proxy_url + clean_val
                else:
                    clean_val = clean_val.replace(f"http://{target_host}:{target_port}", base_proxy_url)
                    clean_val = clean_val.replace(f"https://{target_host}:{target_port}", base_proxy_url)
                    clean_val = clean_val.replace(f"{scheme}://{target_host}", base_proxy_url)
            elif k_lower == 'set-cookie':
                clean_val = re.sub(r';\s*Domain=[^;]+', '', clean_val, flags=re.IGNORECASE)
                clean_val = re.sub(r';\s*Path=[^;]+', '; Path=/', clean_val, flags=re.IGNORECASE)
                clean_val = re.sub(r';\s*SameSite=[^;]+', '', clean_val, flags=re.IGNORECASE)

            resp_headers.append((key, clean_val))

        # CRITICAL FIX for Port 81 crash: Wrap generator in try/except/finally so mid-stream drops never crash Eventlet
        @stream_with_context
        def generate_safe_stream():
            try:
                for chunk in proxied_response.iter_content(chunk_size=16 * 1024):
                    if chunk:
                        yield chunk
            except Exception as stream_err:
                logging.warning(f"Stream interrupted from {target_host}:{target_port}: {stream_err}")
            finally:
                try:
                    proxied_response.close()
                except Exception:
                    pass

        response = Response(
            generate_safe_stream(),
            status=proxied_response.status_code,
            headers=resp_headers
        )

        # STICKY SESSION: Remember the target so SPA client-side routing doesn't break
        target_identifier = f"{scheme}://{target_host}:{target_port}"
        response.set_cookie('dws_active_target', target_identifier, path='/')

        return response

    except requests.exceptions.ConnectionError as e:
        return jsonify({
            "error": "Connection Refused",
            "details": f"Target {target_host}:{target_port} is not listening or refused the connection.",
            "raw": str(e)
        }), 503
    except requests.exceptions.Timeout:
        return jsonify({
            "error": "Gateway Timeout",
            "details": f"Target {target_host}:{target_port} timed out."
        }), 504
    except Exception as e:
        logging.error(f"Proxy error for {target_host}:{target_port}: {e}\n{traceback.format_exc()}")
        return jsonify({
            "error": "Internal Proxy Error",
            "details": str(e)
        }), 502


@app.route('/port/<int:target_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/port/<int:target_port>/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/port/<int:target_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def local_port_proxy(target_port, subpath=""):
    if not subpath and not request.path.endswith('/'):
        qs = request.query_string.decode('utf-8', errors='replace')
        return redirect(f"{request.path}/" + (f"?{qs}" if qs else ""))
    return perform_proxy('http', '127.0.0.1', target_port, subpath)


@app.route('/address/<host_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/address/<host_port>/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/address/<host_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def remote_http_proxy(host_port, subpath=""):
    if not subpath and not request.path.endswith('/'):
        qs = request.query_string.decode('utf-8', errors='replace')
        return redirect(f"{request.path}/" + (f"?{qs}" if qs else ""))
    try:
        host, port = parse_host_port(host_port, 80)
    except ValueError:
        return jsonify({"error": "Invalid Port"}), 400
    return perform_proxy('http', host, port, subpath)


@app.route('/https/<host_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/https/<host_port>/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/https/<host_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def remote_https_proxy(host_port, subpath=""):
    if not subpath and not request.path.endswith('/'):
        qs = request.query_string.decode('utf-8', errors='replace')
        return redirect(f"{request.path}/" + (f"?{qs}" if qs else ""))
    try:
        host, port = parse_host_port(host_port, 443)
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
            img.src = "data:image/jpeg;base64," + data.image;
        });

        img.addEventListener('click', function(e) {
            if (!img.naturalWidth || !img.naturalHeight) return;
            const rect = img.getBoundingClientRect();
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

    # FIXED: Use 127.0.0.1 instead of 0.0.0.0 so headless Chrome can reach local ports reliably
    if proxy_type == 'port':
        target_url = f"http://127.0.0.1:{target}"
    elif proxy_type == 'address':
        target_url = f"http://{target}"
    elif proxy_type == 'https':
        target_url = f"https://{target}"
    else:
        return "Invalid stream type", 400

    return render_template_string(STREAM_HTML, target_url=target_url)

@socketio.on('start_stream')
def handle_start_stream(data):
    if not SELENIUM_AVAILABLE or not isinstance(data, dict):
        return

    url = data.get('url', '')
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
                res = stream_browsers[client_sid].execute_cdp_cmd('Page.captureScreenshot', {
                    'format': 'jpeg',
                    'quality': 60
                })
                socketio.emit('stream_frame', {'image': res['data']}, to=client_sid)
                eventlet.sleep(0.1)
            except Exception:
                break

    socketio.start_background_task(stream_loop)

@socketio.on('stream_click')
def handle_stream_click(data):
    client_sid = request.sid
    if client_sid in stream_browsers and isinstance(data, dict):
        driver = stream_browsers[client_sid]
        x, y = int(data.get('x', 0)), int(data.get('y', 0))
        try:
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
    if client_sid in stream_browsers and isinstance(data, dict):
        driver = stream_browsers[client_sid]
        key = data.get('key', '')
        try:
            active = driver.switch_to.active_element
            if key == 'Enter':
                active.send_keys('\ue007')
            elif key == 'Backspace':
                active.send_keys('\ue003')
            elif len(key) == 1:
                active.send_keys(key)
        except Exception:
            pass

# ==========================================
# MODULE 7: Deep LAN, ARP, SSH & Docker Port Finder
# ==========================================
COMMON_LAN_PORTS = [
    80, 81, 443, 1880, 1984, 2283, 3000, 3001, 32400, 4443, 5000, 5001,
    5050, 5055, 6767, 7878, 8000, 8006, 8080, 8081, 8090, 8096, 8123,
    8200, 8380, 8443, 8554, 8686, 8920, 8989, 9000, 9001, 9090, 9091,
    9117, 9443, 9696, 10000, 10444, 11434, 19999
]

def parse_ports_from_command_output(output):
    """Extracts listening TCP ports from ss/netstat/docker output, including *:81 and [::]:8380."""
    ports = set()
    for line in output.splitlines():
        # Match ss/netstat patterns like 0.0.0.0:81, *:8380, [::]:10444, 127.0.0.1:5000
        for m in re.findall(r'(?:0\.0\.0\.0|127\.0\.0\.1|\[::\]|\*|localhost|[\d\.]+):(\d+)\b', line):
            p = int(m)
            if 1 <= p <= 65535:
                ports.add(p)
        # Match docker ps patterns like ->80/tcp or 0.0.0.0:8380->
        for m in re.findall(r':(\d+)->\d+/tcp', line):
            ports.add(int(m))
    return ports

def get_docker_bound_ports():
    """Queries local Docker daemon directly to find all published ports."""
    docker_ports = set()
    try:
        if shutil.which("docker"):
            output = subprocess.check_output(["docker", "ps", "--format", "{{.Ports}}"], universal_newlines=True, timeout=2)
            docker_ports.update(parse_ports_from_command_output(output))
    except Exception:
        pass
    return docker_ports

def get_active_local_ports():
    """Finds all active listening TCP ports on the local machine."""
    active_ports = set()
    try:
        cmd = ["ss", "-tln"] if shutil.which("ss") else ["netstat", "-tln"]
        output = subprocess.check_output(cmd, universal_newlines=True, timeout=2)
        active_ports.update(parse_ports_from_command_output(output))
    except Exception:
        pass
    return active_ports

def get_remote_ssh_listening_ports(server_info):
    """Connects to known SERVERS via SSH and pulls exact listening OS & Docker ports."""
    ports = set(COMMON_LAN_PORTS)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(server_info["host"], username=server_info["user"], password=server_info["password"], timeout=2.5)
        _, stdout, _ = ssh.exec_command("ss -tln 2>/dev/null || netstat -tln 2>/dev/null; docker ps --format '{{.Ports}}' 2>/dev/null")
        out_text = stdout.read().decode('utf-8', errors='ignore')
        ports.update(parse_ports_from_command_output(out_text))
    except Exception:
        pass
    finally:
        try:
            ssh.close()
        except Exception:
            pass
    return sorted(ports)

def discover_lan_hosts(subnet_prefix="192.168.2"):
    """Discovers active LAN IPs on the subnet using ARP cache + fast parallel probe."""
    hosts = {"127.0.0.1"}
    for srv in SERVERS.values():
        hosts.add(srv["host"])

    # 1. Read OS ARP table (/proc/net/arp and ip neigh) for instant known LAN devices
    try:
        if os.path.exists("/proc/net/arp"):
            with open("/proc/net/arp", "r") as f:
                for line in f.read().splitlines()[1:]:
                    parts = line.split()
                    if parts and parts[0].startswith(subnet_prefix + ".") and parts[2] != "0x0":
                        hosts.add(parts[0])
    except Exception:
        pass

    try:
        if shutil.which("ip"):
            arp_out = subprocess.check_output(["ip", "neigh"], universal_newlines=True, timeout=1.5)
            for m in re.findall(r'(\d+\.\d+\.\d+\.\d+)\s+dev\s+\S+\s+lladdr', arp_out):
                if m.startswith(subnet_prefix + "."):
                    hosts.add(m)
    except Exception:
        pass

    # 2. Fast TCP sweep across the entire /24 subnet to wake & detect any un-cached servers
    probe_ports = (80, 81, 443, 22, 5000, 8080, 8380, 9000, 10444)

    def probe_ip(ip_addr):
        if ip_addr in hosts:
            return ip_addr
        for p in probe_ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.12)
            try:
                res = s.connect_ex((ip_addr, p))
                if res == 0:
                    return ip_addr
            except Exception:
                pass
            finally:
                s.close()
        return None

    all_subnet_ips = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as ex:
        for found in ex.map(probe_ip, all_subnet_ips):
            if found:
                hosts.add(found)

    return sorted(hosts, key=lambda ip: [int(x) for x in ip.split('.')])

def detect_port_protocol_and_title(ip, port):
    """Probes an open port to accurately detect HTTPS vs HTTP and extract its page title."""
    if port == DWS_LISTEN_PORT and (ip == "127.0.0.1" or ip in LOCAL_HOST_IPS):
        return {"ip": ip, "port": port, "url": f"http://{ip}:{port}", "title": "DWS Server (This Host)", "status": "Online", "is_https": False}

    # First check if HTTPS is likely or test TLS handshake quickly
    likely_https = port in (443, 4443, 8443, 9443, 10444, 8006)
    schemes_to_try = ["https", "http"] if likely_https else ["http", "https"]

    for scheme in schemes_to_try:
        url = f"{scheme}://{ip}:{port}"
        try:
            resp = requests.get(
                url,
                timeout=0.8,
                verify=False,
                headers={"Accept-Encoding": "gzip, deflate", "User-Agent": "DWS-Finder/2.0"}
            )
            # If plain HTTP was sent to an HTTPS port, Nginx/Apache returns 400 with HTTPS warning
            if scheme == "http" and resp.status_code == 400 and ("HTTPS" in resp.text[:300] or "SSL" in resp.text[:300]):
                continue

            is_https = (scheme == "https")
            title = f"Service on {port}"
            match = re.search(r'<title[^>]*>(.*?)</title>', resp.text[:4096], re.IGNORECASE | re.DOTALL)
            if match:
                clean_title = re.sub(r'\s+', ' ', match.group(1)).strip()
                if clean_title:
                    title = clean_title[:45]
            elif resp.headers.get("Server"):
                title = f"{resp.headers.get('Server')[:35]} ({resp.status_code})"
            else:
                title = f"HTTP {resp.status_code} Service" if not is_https else f"HTTPS {resp.status_code} Service"

            return {"ip": ip, "port": port, "url": url, "title": title, "status": "Online", "is_https": is_https}
        except requests.exceptions.SSLError:
            if scheme == "https":
                return {"ip": ip, "port": port, "url": url, "title": "Secure Service (HTTPS)", "status": "Online", "is_https": True}
        except Exception:
            continue

    return {"ip": ip, "port": port, "url": f"http://{ip}:{port}", "title": f"TCP Service on {port}", "status": "Online", "is_https": likely_https}

def scan_single_target(ip, port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.22)
        result = s.connect_ex((ip, port))
        s.close()
        if result == 0:
            return detect_port_protocol_and_title(ip, port)
    except Exception:
        pass
    return None

FINDER_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>DWS Deep LAN & Port Finder</title>
    <style>
        body { margin: 0; padding: 24px; background: #0d1117; color: #c9d1d9; font-family: monospace; }
        h1 { color: #58a6ff; margin-top: 0; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 20px; }
        button { background: #238636; color: white; border: none; padding: 10px 16px; border-radius: 4px; cursor: pointer; font-family: monospace; font-weight: bold; }
        button:hover { background: #2ea043; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 12px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid #21262d; }
        th { color: #8b949e; }
        a { color: #58a6ff; text-decoration: none; margin-right: 12px; }
        a:hover { text-decoration: underline; }
        .stream-link { color: #a371f7; font-size: 12px; }
    </style>
</head>
<body>
    <h1>DWS Deep Port & LAN Server Finder</h1>
    <div class="card">
        <p>Discovers all active hosts on <b>192.168.2.0/24</b> (ARP + subnet sweep), queries SSH cluster nodes directly for open OS/Docker ports, and auto-detects HTTP vs HTTPS.</p>
        <button onclick="startScan()" id="scan-btn">Run Full LAN & Port Scan</button>
        <span id="status-msg" style="margin-left: 15px; color: #8b949e;">Ready.</span>
    </div>
    <div class="card">
        <h2>Discovered LAN & Local Services</h2>
        <table>
            <thead>
                <tr><th>Host / IP</th><th>Port</th><th>Detected Title / Service</th><th>Actions</th></tr>
            </thead>
            <tbody id="results-table">
                <tr><td colspan="4" style="color: #8b949e;">Click 'Run Full LAN & Port Scan' to begin.</td></tr>
            </tbody>
        </table>
    </div>
    <script>
        async function startScan() {
            const btn = document.getElementById('scan-btn');
            const msg = document.getElementById('status-msg');
            const tbody = document.getElementById('results-table');
            btn.disabled = true;
            msg.innerText = "Sweeping 192.168.2.0/24 subnet, SSH nodes, Docker bindings, and local sockets...";
            tbody.innerHTML = `<tr><td colspan="4" style="color: #d29922;">Deep LAN scan in progress (takes ~3-5 seconds)...</td></tr>`;

            try {
                const res = await fetch('/finder/scan');
                const data = await res.json();
                msg.innerText = `Scan complete across ${data.hosts_scanned || 0} active LAN hosts. Found ${data.services.length} endpoint(s).`;
                if (data.services.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="4">No services discovered.</td></tr>`;
                } else {
                    tbody.innerHTML = data.services.map(s => {
                        let proxyLink = "";
                        let streamLink = "";
                        if (s.ip === '127.0.0.1' || s.ip === 'localhost') {
                            proxyLink = s.is_https
                                ? `<a href="/https/127.0.0.1:${s.port}/" target="_blank">Open HTTPS Proxy</a>`
                                : `<a href="/port/${s.port}/" target="_blank">Open Port Proxy</a>`;
                            streamLink = s.is_https
                                ? `<a class="stream-link" href="/stream/https/127.0.0.1:${s.port}" target="_blank">[Stream Browser]</a>`
                                : `<a class="stream-link" href="/stream/port/${s.port}" target="_blank">[Stream Browser]</a>`;
                        } else {
                            proxyLink = s.is_https
                                ? `<a href="/https/${s.ip}:${s.port}/" target="_blank">Open HTTPS Proxy</a>`
                                : `<a href="/address/${s.ip}:${s.port}/" target="_blank">Open Address Proxy</a>`;
                            streamLink = s.is_https
                                ? `<a class="stream-link" href="/stream/https/${s.ip}:${s.port}" target="_blank">[Stream Browser]</a>`
                                : `<a class="stream-link" href="/stream/address/${s.ip}:${s.port}" target="_blank">[Stream Browser]</a>`;
                        }
                        return `
                            <tr>
                                <td><b>${s.ip}</b></td>
                                <td><b>${s.port} ${s.is_https ? '<span style="color:#3fb950;">(HTTPS)</span>' : '(HTTP)'}</b></td>
                                <td>${s.title}</td>
                                <td>${proxyLink} ${streamLink}</td>
                            </tr>
                        `;
                    }).join('');
                }
            } catch(e) {
                msg.innerText = "Scan failed.";
                tbody.innerHTML = `<tr><td colspan="4" style="color: #da3633;">Error running deep scan: ${e}</td></tr>`;
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

    # 1. Gather all local ports (OS sockets + Docker + common home lab ports)
    local_ports = get_active_local_ports().union(get_docker_bound_ports()).union(set(COMMON_LAN_PORTS))

    # 2. Discover all active hosts on the 192.168.2.0/24 LAN
    lan_hosts = discover_lan_hosts("192.168.2")

    # 3. Map exact listening ports for known SSH servers, and common ports for other discovered LAN devices
    ssh_host_map = {srv["host"]: srv for srv in SERVERS.values()}
    host_port_targets = []

    for ip in lan_hosts:
        if ip == "127.0.0.1":
            for p in sorted(local_ports):
                host_port_targets.append((ip, p))
        elif ip in ssh_host_map:
            remote_ports = get_remote_ssh_listening_ports(ssh_host_map[ip])
            for p in remote_ports:
                host_port_targets.append((ip, p))
        else:
            for p in COMMON_LAN_PORTS:
                host_port_targets.append((ip, p))

    with ThreadPoolExecutor(max_workers=64) as executor:
        futures = [executor.submit(scan_single_target, ip, port) for ip, port in host_port_targets]
        for future in futures:
            res = future.result()
            if res:
                if not any(d['ip'] == res['ip'] and d['port'] == res['port'] for d in discovered):
                    discovered.append(res)

    discovered.sort(key=lambda x: ([int(part) for part in x['ip'].split('.')], x['port']))
    return jsonify({
        "hosts_scanned": len(lan_hosts),
        "services": discovered
    })

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
    if requested_target in ("local", "1", "2", "3"):
        return requested_target
    if requested_target == "auto":
        return min(node_active_counts, key=node_active_counts.get)
    return "local"

def prune_job_history():
    if len(compute_jobs) > MAX_JOB_HISTORY:
        completed = [
            (jid, j["created_at"]) for jid, j in compute_jobs.items()
            if j["status"] in ("completed", "failed", "timeout", "cancelled")
        ]
        completed.sort(key=lambda x: x[1])
        for jid, _ in completed[:len(compute_jobs) - MAX_JOB_HISTORY]:
            compute_jobs.pop(jid, None)

def parse_compute_request(req):
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

        while channel.recv_ready():
            stdout_chunks.append(channel.recv(4096).decode("utf-8", errors="replace"))
        while channel.recv_stderr_ready():
            stderr_chunks.append(channel.recv_stderr(4096).decode("utf-8", errors="replace"))

        exit_code = channel.recv_exit_status()

        for pull_file in ("output.json", output_filename):
            if pull_file:
                try:
                    sftp.get(f"{remote_dir}/{pull_file}", os.path.join(job_dir, pull_file))
                except IOError:
                    pass

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
    job_meta = compute_jobs[job_id]
    target = job_meta["target"]
    job_dir = os.path.join(COMPUTE_RAMDISK, job_id)
    os.makedirs(job_dir, exist_ok=True)

    node_active_counts[target] = node_active_counts.get(target, 0) + 1
    job_meta["status"] = "running"
    job_meta["started_at"] = time.time()
    emit_compute_telemetry(job_meta, stream_sid)

    try:
        with open(os.path.join(job_dir, "input.json"), "w", encoding="utf-8") as f:
            json.dump(spec.get("input_data", {}), f)

        input_filename = spec.get("input_filename", "")
        output_filename = spec.get("output_filename", "output.bin")
        if spec.get("input_bytes"):
            with open(os.path.join(job_dir, input_filename), "wb") as f:
                f.write(spec["input_bytes"])

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
    spec = parse_compute_request(request)
    job_id, job_meta = create_job_record(spec)
    run_compute_pipeline(job_id, spec)

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
    data = data or {}
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
    (e.g., changes the URL to /login or /assets/app.js).
    """
    # 1. Try Referer first
    referer = request.headers.get('Referer')
    if referer:
        parsed_referer = urlparse(referer)
        match = re.match(r'^/(port|address|https)/([^/]+)', parsed_referer.path)
        if match:
            proxy_type = match.group(1)
            host_port = match.group(2)
            correct_path = f"/{proxy_type}/{host_port}/{missing_path}"
            if request.query_string:
                correct_path += f"?{request.query_string.decode('utf-8', errors='replace')}"
            return redirect(correct_path, code=307)

    # 2. SPA Fallback: Use the sticky cookie set during the initial load
    active_target = request.cookies.get('dws_active_target')
    if active_target:
        try:
            parsed_target = urlparse(active_target)
            scheme = parsed_target.scheme or 'http'
            host = parsed_target.hostname
            port = parsed_target.port or (443 if scheme == 'https' else 80)

            if host and not (int(port) == DWS_LISTEN_PORT and host in LOCAL_HOST_IPS):
                return perform_proxy(scheme, host, port, missing_path)
        except Exception as e:
            logging.error(f"Fallback proxy error for SPA routing: {e}")

    return jsonify({
        "error": "Not Found",
        "details": "No active target session found. Open /finder to scan and launch a service."
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

    socketio.run(app, host='0.0.0.0', port=DWS_LISTEN_PORT)
