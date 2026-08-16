from flask import Flask, render_template
from flask_socketio import SocketIO
import paramiko
import logging
import sys

# --- CONSOLE LOGGING SETUP ---
# This forces logs to stream immediately to Docker's standard output
logging.basicConfig(
    stream=sys.stdout, 
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- CONFIGURATION ---
SERVERS = {
    "1": {"host": "192.168.2.111", "user": "dylan", "password": "weqr1234"},
    "2": {"host": "192.168.2.91", "user": "dylanwardstudios", "password": "weqr1234"},
    "3": {"host": "192.168.1.12", "user": "admin", "password": "yourpassword3"}
}

# ==========================================
# MODULE 1: DWS Server Shell
# ==========================================
@app.route('/shell/<server_id>')
def shell(server_id):
    if server_id not in SERVERS:
        logging.warning(f"404: Someone tried to access invalid Server ID: {server_id}")
        return "Server not found", 404
    
    logging.info(f"Web interface loaded for Server ID: {server_id}")
    return render_template('shell.html', server_id=server_id)

@socketio.on('connect_ssh')
def handle_ssh_connection(data):
    server_id = data.get('server_id')
    if server_id not in SERVERS:
        return

    server = SERVERS[server_id]
    logging.info(f"Attempting SSH connection to {server['host']} as user '{server['user']}'...")
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(server['host'], username=server['user'], password=server['password'], timeout=5)
        channel = ssh.invoke_shell()
        logging.info(f"SUCCESS: SSH session established for {server['host']}")

        def listen_to_ssh():
            while not channel.closed:
                try:
                    output = channel.recv(1024).decode('utf-8')
                    socketio.emit('ssh_output', {'output': output})
                except Exception:
                    logging.info(f"SSH session closed for {server['host']}")
                    break

        socketio.start_background_task(listen_to_ssh)

        @socketio.on('ssh_input')
        def handle_input(input_data):
            if not channel.closed:
                channel.send(input_data['input'])

    except Exception as e:
        error_msg = str(e)
        logging.error(f"FAILED: SSH connection to {server['host']} failed. Reason: {error_msg}")
        socketio.emit('ssh_output', {'output': f'\r\n[!] Connection failed: {error_msg}\r\n'})

if __name__ == '__main__':
    logging.info("Starting DWS Server Shell backend...")
    socketio.run(app, host='0.0.0.0', port=5000)
