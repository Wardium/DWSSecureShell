# THESE TWO LINES MUST BE AT THE VERY TOP
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import paramiko
import logging
import sys
import requests
import time
import subprocess
import os

logging.basicConfig(
    stream=sys.stdout, 
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- GLOBAL VARIABLES ---
# Dictionary to track active SSH sessions
active_sessions = {}
# Dictionary to track what state we are forcing devices into
enforced_devices = {}

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
    
    logging.info(f"Attempting SSH connection to {server['host']}...")
    
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
                except Exception as e:
                    logging.error(f"SSH listener error: {e}")
                    break
            logging.info(f"Stopped listening to SSH on {server['host']}")

        socketio.start_background_task(listen_to_ssh)

        @socketio.on('ssh_input')
        def handle_input(input_data):
            if not channel.closed:
                channel.send(input_data['input'])

    except Exception as e:
        logging.error(f"FAILED: SSH connection failed. Reason: {str(e)}")
        socketio.emit('ssh_output', {'output': f'\r\n[!] Connection failed: {str(e)}\r\n'}, to=client_sid)

@socketio.on('disconnect')
def handle_disconnect():
    session_id = request.sid
    if session_id in active_sessions:
        logging.info(f"Tab closed. Terminating SSH session for {session_id}")
        active_sessions[session_id].close()
        del active_sessions[session_id]

# ==========================================
# MODULE 4: Local Port Forwarding Proxy
# ==========================================
from flask import Response
from urllib.parse import urlparse
import urllib3

# Suppress insecure request warnings for self-signed certificates on local HTTPS endpoints
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def perform_proxy(scheme, target_host, target_port, subpath):
    target_url = f"{scheme}://{target_host}:{target_port}/{subpath}"
    
    if request.query_string:
        target_url = f"{target_url}?{request.query_string.decode('utf-8')}"
        
    try:
        # Strip the original Host header
        req_headers = {key: value for key, value in request.headers if key.lower() != 'host'}
        
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
        
        # Determine the base proxy path to use for rewrites
        if target_host == '127.0.0.1' and scheme == 'http':
            base_proxy_url = f"/port/{target_port}"
        elif scheme == 'https':
            base_proxy_url = f"/https/{target_host}:{target_port}"
        else:
            base_proxy_url = f"/address/{target_host}:{target_port}"
            
        for key, value in proxied_response.raw.headers.items():
            if key.lower() not in excluded_headers:
                if key.lower() == 'location':
                    if value.startswith('/'):
                        value = base_proxy_url + value
                    else:
                        value = value.replace(f"{scheme}://{target_host}:{target_port}", base_proxy_url)
                        value = value.replace(f"{scheme}://{target_host}", base_proxy_url)
                resp_headers.append((key, value))
        
        # Set a cookie so our Catch-All knows where to send orphaned API requests
        cookie_val = f"{scheme}|{target_host}:{target_port}"
        resp_headers.append(('Set-Cookie', f"dws_proxy_target={cookie_val}; Path=/; SameSite=Lax"))

        # --- THE FIX: HTML REWRITING ---
        content_type = proxied_response.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            try:
                # Read the HTML and rewrite absolute paths to point at our proxy!
                html_content = proxied_response.content.decode('utf-8', errors='ignore')
                html_content = html_content.replace('href="/', f'href="{base_proxy_url}/')
                html_content = html_content.replace('src="/', f'src="{base_proxy_url}/')
                html_content = html_content.replace('action="/', f'action="{base_proxy_url}/')
                
                return Response(html_content, proxied_response.status_code, resp_headers)
            except Exception as e:
                logging.error(f"HTML Rewrite failed: {str(e)}")

        # If it's not HTML, stream the response normally
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


# Original backwards-compatible loopback proxy
@app.route('/port/<int:target_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/port/<int:target_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def local_port_proxy(target_port, subpath=""):
    return perform_proxy('http', '127.0.0.1', target_port, subpath)

# HTTP address proxy
@app.route('/address/<host_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/address/<host_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def remote_http_proxy(host_port, subpath=""):
    try:
        if ':' in host_port:
            host, port_str = host_port.rsplit(':', 1)
            port = int(port_str)
        else:
            host, port = host_port, 80
    except ValueError:
        return jsonify({"error": "Invalid Port"}), 400
    return perform_proxy('http', host, port, subpath)

# HTTPS address proxy
@app.route('/https/<host_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/https/<host_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def remote_https_proxy(host_port, subpath=""):
    try:
        if ':' in host_port:
            host, port_str = host_port.rsplit(':', 1)
            port = int(port_str)
        else:
            host, port = host_port, 443
    except ValueError:
        return jsonify({"error": "Invalid Port"}), 400
    return perform_proxy('https', host, port, subpath)

# THE FIX: TRUE CATCH-ALL ROUTE FOR ORPHANED ASSETS
@app.route('/<path:orphan_path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def catch_all(orphan_path):
    proxy_target = None
    
    # Check if the browser told us where this request came from
    referrer = request.headers.get("Referer")
    if referrer:
        ref_path = urlparse(referrer).path.strip('/').split('/')
        if len(ref_path) >= 2 and ref_path[0] in ['port', 'address', 'https']:
            proxy_type = ref_path[0]
            host_port = ref_path[1]
            if proxy_type == 'port':
                proxy_target = f"http|127.0.0.1:{host_port}"
            else:
                scheme = 'https' if proxy_type == 'https' else 'http'
                proxy_target = f"{scheme}|{host_port}"
                
    # Fallback to the cookie we injected earlier
    if not proxy_target:
        proxy_target = request.cookies.get('dws_proxy_target')

    # Intercept and proxy!
    if proxy_target:
        try:
            scheme, host_port = proxy_target.split('|', 1)
            if ':' in host_port:
                host, port_str = host_port.rsplit(':', 1)
                port = int(port_str)
            else:
                host = host_port
                port = 443 if scheme == 'https' else 80
                
            logging.info(f"Auto-routing orphaned request '/{orphan_path}' to {host}:{port}")
            return perform_proxy(scheme, host, port, orphan_path)
        except Exception as e:
            logging.error(f"Auto-proxy failed for {orphan_path}: {str(e)}")
            
    return "Not Found", 404

# ==========================================
# APP EXECUTION
# ==========================================

if __name__ == '__main__':
    logging.info("Starting DWS Server Shell backend...")
    
    # --- MODULE 2: Start the Gatekeeper ---
    gatekeeper_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gatekeeper.py')
    if os.path.exists(gatekeeper_script):
        logging.info("Launching high-performance concurrent Gatekeeper process via Gunicorn...")
        subprocess.Popen(
            ["gunicorn", "-w", "4", "-k", "gevent", "-b", "0.0.0.0:5050", "gatekeeper:app"],
            stdout=sys.stdout,
            stderr=sys.stderr,
            close_fds=True
        )
    else:
        logging.error(f"gatekeeper.py not found at {gatekeeper_script}. Skipping Gatekeeper launch.")

    # --- MODULE 3: Start Aurora AI ---
    aurora_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aurora.py')
    if os.path.exists(aurora_script):
        logging.info("Launching Aurora AI module on port 5101...")
        subprocess.Popen(
            [sys.executable, "aurora.py"],
            stdout=sys.stdout,
            stderr=sys.stderr,
            close_fds=True
        )
    else:
        logging.error(f"aurora.py not found at {aurora_script}. Skipping Aurora launch.")
    
    # Start the main SocketIO app
    socketio.run(app, host='0.0.0.0', port=5000)
