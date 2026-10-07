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
import urllib3

# Suppress insecure request warnings for self-signed certificates on local HTTPS endpoints
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def perform_proxy(scheme, target_host, target_port, subpath):
    # Route traffic to the requested target
    target_url = f"{scheme}://{target_host}:{target_port}/{subpath}"
    
    # Forward query parameters if they exist
    if request.query_string:
        target_url = f"{target_url}?{request.query_string.decode('utf-8')}"
        
    try:
        # Strip the original Host header so the request appears native to the target app
        req_headers = {key: value for key, value in request.headers if key.lower() != 'host'}
        
        # Forward the request (verify=False allows self-signed local certs for https)
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
        
        # Exclude hop-by-hop headers that shouldn't be forwarded to the client
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        resp_headers = []
        
        for key, value in proxied_response.raw.headers.items():
            if key.lower() not in excluded_headers:
                # Rewrite redirects so you aren't forced out of the proxy path
                if key.lower() == 'location':
                    # Determine our base proxy URL
                    if target_host == '127.0.0.1' and scheme == 'http':
                        base_proxy_url = f"{request.scheme}://{request.host}/port/{target_port}"
                    elif scheme == 'https':
                        base_proxy_url = f"{request.scheme}://{request.host}/https/{target_host}:{target_port}"
                    else:
                        base_proxy_url = f"{request.scheme}://{request.host}/address/{target_host}:{target_port}"

                    # Fix relative redirects (e.g., redirecting to "/login")
                    if value.startswith('/'):
                        value = base_proxy_url + value
                    # Fix absolute redirects
                    else:
                        value = value.replace(f"{scheme}://{target_host}:{target_port}", base_proxy_url)
                        value = value.replace(f"{scheme}://{target_host}", base_proxy_url)
                        # Catch localhost edge cases
                        value = value.replace(f"{scheme}://127.0.0.1:{target_port}", base_proxy_url)
                        value = value.replace(f"{scheme}://localhost:{target_port}", base_proxy_url)

                resp_headers.append((key, value))
        
        # Stream the response back to the client
        return Response(
            proxied_response.iter_content(chunk_size=10*1024), 
            proxied_response.status_code, 
            resp_headers
        )

    except requests.exceptions.ConnectionError:
        logging.error(f"Proxy module: Connection Refused to {target_host}:{target_port}")
        return jsonify({
            "error": "Connection Refused", 
            "details": f"The target {target_host}:{target_port} is either not running or inaccessible."
        }), 503
        
    except requests.exceptions.Timeout:
        logging.error(f"Proxy module: Timeout connecting to {target_host}:{target_port}")
        return jsonify({"error": "Gateway Timeout", "details": f"Target {target_host}:{target_port} took too long to respond."}), 504
        
    except Exception as e:
        logging.error(f"Proxy module error connecting to {target_host}:{target_port}: {str(e)}")
        return jsonify({"error": "Internal Proxy Error", "details": str(e)}), 500


# Original backwards-compatible loopback proxy (RESTORED)
@app.route('/port/<int:target_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/port/<int:target_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def local_port_proxy(target_port, subpath=""):
    return perform_proxy('http', '127.0.0.1', target_port, subpath)

# New HTTP address proxy
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
        return jsonify({"error": "Invalid Port", "details": "The port must be a valid number."}), 400
        
    return perform_proxy('http', host, port, subpath)

# New HTTPS address proxy
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
        return jsonify({"error": "Invalid Port", "details": "The port must be a valid number."}), 400
        
    return perform_proxy('https', host, port, subpath)

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
