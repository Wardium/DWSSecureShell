from flask import Flask, render_template
from flask_socketio import SocketIO
import paramiko

app = Flask(__name__)
# eventlet enables high-performance WebSockets
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- CONFIGURATION ---
# Define your 3 server options here. 
SERVERS = {
    "1": {"host": "192.168.2.111", "user": "dylan", "password": "weqr1234"},
    "2": {"host": "192.168.1.11", "user": "root", "password": "yourpassword2"},
    "3": {"host": "192.168.1.12", "user": "admin", "password": "yourpassword3"}
}

# ==========================================
# MODULE 1: DWS Server Shell
# ==========================================
@app.route('/shell/<server_id>')
def shell(server_id):
    if server_id not in SERVERS:
        return "Server not found", 404
    # Serves the HTML file that acts as the iframe content
    return render_template('shell.html', server_id=server_id)

@socketio.on('connect_ssh')
def handle_ssh_connection(data):
    server_id = data.get('server_id')
    if server_id not in SERVERS:
        return

    server = SERVERS[server_id]
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        # Establish the server-side SSH connection
        ssh.connect(server['host'], username=server['user'], password=server['password'], timeout=5)
        channel = ssh.invoke_shell()

        # Background task to constantly read SSH output and send it to the browser
        def listen_to_ssh():
            while not channel.closed:
                try:
                    output = channel.recv(1024).decode('utf-8')
                    socketio.emit('ssh_output', {'output': output})
                except Exception:
                    break

        socketio.start_background_task(listen_to_ssh)

        # Listen for keystrokes from the web frontend and send them to the SSH session
        @socketio.on('ssh_input')
        def handle_input(input_data):
            if not channel.closed:
                channel.send(input_data['input'])

    except Exception as e:
        socketio.emit('ssh_output', {'output': f'\r\n[!] Connection failed: {str(e)}\r\n'})

# ==========================================
# MODULE 2: [Your Next Module Here]
# ==========================================
# @app.route('/next-module')
# def next_module():
#     return "Independent module ready."


if __name__ == '__main__':
    # Running with socketio handles the WebSocket upgrade automatically
    socketio.run(app, host='0.0.0.0', port=5000)
