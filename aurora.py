from flask import Flask, request, jsonify, render_template, make_response
import requests
import sqlite3
import datetime
import uuid

app = Flask(__name__)

# Configuration
OLLAMA_URL = "http://192.168.2.134:11434/api/chat"
DB_NAME = "aurora.db"

# Map your custom names to the actual Ollama model names you have installed
MODEL_MAP = {
    "DWS:Aurora": "llama3",       # Base model
    "DWS:Swift": "phi3",          # Fast model
    "DWS:Avani": "llama3:70b",    # Smart model
    "DWS:Optic": "llava"          # Vision model
}

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chats
                 (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at DATETIME, starred INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, role TEXT, content TEXT, timestamp DATETIME)''')
    conn.commit()
    conn.close()

def cleanup_old_history():
    """Deletes chats older than 7 days unless they are starred"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    seven_days_ago = datetime.datetime.now() - datetime.timedelta(days=7)
    
    # Get old unstarred chats to delete their messages
    c.execute("SELECT id FROM chats WHERE created_at < ? AND starred = 0", (seven_days_ago,))
    old_chats = [row[0] for row in c.fetchall()]
    
    for chat_id in old_chats:
        c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    
    c.execute("DELETE FROM chats WHERE created_at < ? AND starred = 0", (seven_days_ago,))
    conn.commit()
    conn.close()

@app.route('/')
def index():
    return render_template('aurora.html')

@app.route('/api/login', methods=['POST'])
def login():
    user_id = request.json.get('user_id', '').strip()
    if not user_id:
        return jsonify({"error": "Invalid ID"}), 400
    
    resp = make_response(jsonify({"success": True, "user_id": user_id}))
    # Set cookie to remember user (expires in 1 year)
    resp.set_cookie('aurora_user_id', user_id, max_age=60*60*24*365)
    return resp

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({"success": True}))
    resp.set_cookie('aurora_user_id', '', expires=0)
    return resp

@app.route('/api/history', methods=['GET'])
def get_history():
    cleanup_old_history()
    user_id = request.cookies.get('aurora_user_id')
    if not user_id:
        return jsonify([])

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, title, created_at, starred FROM chats WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    chats = [{"id": row[0], "title": row[1], "created_at": row[2], "starred": bool(row[3])} for row in c.fetchall()]
    conn.close()
    return jsonify(chats)

@app.route('/api/chat/<chat_id>', methods=['GET'])
def get_chat_messages(chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC", (chat_id,))
    messages = [{"role": row[0], "content": row[1]} for row in c.fetchall()]
    conn.close()
    return jsonify(messages)

@app.route('/api/star/<chat_id>', methods=['POST'])
def toggle_star(chat_id):
    starred = request.json.get('starred', True)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE chats SET starred = ? WHERE id = ?", (1 if starred else 0, chat_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/generate', methods=['POST'])
def generate():
    user_id = request.cookies.get('aurora_user_id', 'anonymous')
    data = request.json
    chat_id = data.get('chat_id')
    model_choice = data.get('model', 'DWS:Aurora')
    message = data.get('message')
    images = data.get('images', []) # Base64 images
    
    actual_model = MODEL_MAP.get(model_choice, "llama3")
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    if not chat_id:
        chat_id = str(uuid.uuid4())
        title = message[:30] + "..." if len(message) > 30 else message
        c.execute("INSERT INTO chats (id, user_id, title, created_at, starred) VALUES (?, ?, ?, ?, 0)",
                  (chat_id, user_id, title, datetime.datetime.now()))
    
    c.execute("INSERT INTO messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
              (chat_id, "user", message, datetime.datetime.now()))
    conn.commit()

    # Get chat history for context
    c.execute("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC", (chat_id,))
    history = [{"role": row[0], "content": row[1]} for row in c.fetchall()]
    
    # Attach image to the latest user prompt if provided
    if images:
        history[-1]['images'] = images

    # Send to Ollama
    payload = {
        "model": actual_model,
        "messages": history,
        "stream": False
    }
    
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        response_data = response.json()
        ai_message = response_data.get('message', {}).get('content', '')
        
        c.execute("INSERT INTO messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                  (chat_id, "assistant", ai_message, datetime.datetime.now()))
        conn.commit()
        
        conn.close()
        return jsonify({"chat_id": chat_id, "response": ai_message})
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    init_db()
    # Host on 0.0.0.0 so it is accessible on your local network / outside world via port forwarding
    app.run(host='0.0.0.0', port=5000, debug=True)
