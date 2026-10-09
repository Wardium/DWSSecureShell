# THESE TWO LINES MUST BE AT THE VERY TOP
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, render_template_string, request, jsonify, Response, redirect
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

# ==========================================
# MODULE 1: DWS Server Shell
# ==========================================
SERVERS = {
    "1": {"host": "192.168.2.111", "user": "dylan", "password": "weqr1234"},
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
    server_id = data.get('server_id')
    if server_id not in SERVERS:
        return

    server = SERVERS[server_id]
    client_sid = request.sid  
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(server['host'], username=server['user'], password=server['password'], timeout=5)
        channel = ssh.invoke_shell()
        active_sessions[client_sid] = ssh
        logging.info(f"SUCCESS: SSH session established for {server['host']}")

        def listen_to_ssh():
            while not channel.closed:
                try:
                    output = channel.recv(1024).decode('utf-8')
                    if output:
                        socketio.emit('ssh_output', {'output': output}, to=client_sid)
                except:
                    break

        socketio.start_background_task(listen_to_ssh)

        @socketio.on('ssh_input')
        def handle_input(input_data):
            if not channel.closed:
                channel.send(input_data['input'])

    except Exception as e:
        socketio.emit('ssh_output', {'output': f'\r\n[!] Connection failed: {str(e)}\r\n'}, to=client_sid)

@socketio.on('disconnect')
def handle_disconnect():
    session_id = request.sid
    if session_id in active_sessions:
        active_sessions[session_id].close()
        del active_sessions[session_id]
        
    # Cleanup orphaned streams
    if session_id in stream_browsers:
        try:
            stream_browsers[session_id].quit()
        except:
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
        
        return Response(
            proxied_response.iter_content(chunk_size=10*1024), 
            proxied_response.status_code, 
            resp_headers
        )

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
        
    if proxy_type == 'port': target_url = f"http://127.0.0.1:{target}"
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
