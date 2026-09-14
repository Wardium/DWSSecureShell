from flask import Flask, request, jsonify, render_template, make_response
import requests
import sqlite3
import datetime
import json
import base64

app = Flask(__name__)
OLLAMA_URL = "https://ai-super.teamexist.com"

def init_db():
    conn = sqlite3.connect('aurora.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id TEXT,
                  role TEXT,
                  content TEXT,
                  timestamp DATETIME,
                  starred BOOLEAN)''')
    conn.commit()
    conn.close()

def cleanup_history():
    """Deletes unstarred messages older than 7 days."""
    conn = sqlite3.connect('aurora.db')
    c = conn.cursor()
    seven_days_ago = datetime.datetime.now() - datetime.timedelta(days=7)
    c.execute("DELETE FROM history WHERE timestamp < ? AND starred = 0", (seven_days_ago,))
    conn.commit()
    conn.close()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user_id = data.get('id')
    response = make_response(jsonify({"status": "success"}))
    response.set_cookie('aurora_user_id', user_id, max_age=60*60*24*365) # 1 year
    return response

@app.route('/api/history', methods=['GET'])
def get_history():
    user_id = request.cookies.get('aurora_user_id')
    if not user_id:
        return jsonify([])
        
    cleanup_history()
    conn = sqlite3.connect('aurora.db')
    c = conn.cursor()
    c.execute("SELECT id, role, content, starred FROM history WHERE user_id = ? ORDER BY timestamp ASC", (user_id,))
    rows = c.fetchall()
    conn.close()
    
    history = [{"id": r[0], "role": r[1], "content": r[2], "starred": bool(r[3])} for r in rows]
    return jsonify(history)

@app.route('/api/star', methods=['POST'])
def star_message():
    data = request.json
    msg_id = data.get('id')
    starred = data.get('starred', True)
    
    conn = sqlite3.connect('aurora.db')
    c = conn.cursor()
    c.execute("UPDATE history SET starred = ? WHERE id = ?", (starred, msg_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    model = data.get('model', 'DWS:Aurora')
    image = data.get('image', None) # Base64 encoded if present
    user_id = request.cookies.get('aurora_user_id')
    
    timestamp = datetime.datetime.now()
    
    # Save user message to DB
    if user_id:
        conn = sqlite3.connect('aurora.db')
        c = conn.cursor()
        c.execute("INSERT INTO history (user_id, role, content, timestamp, starred) VALUES (?, ?, ?, ?, ?)",
                  (user_id, 'user', user_message, timestamp, False))
        conn.commit()
    
    # Prepare payload for Ollama
    payload = {
        "model": model,
        "prompt": user_message,
        "stream": False
    }
    if image:
        payload["images"] = [image]

    # Talk to the AI Server
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload)
        ai_response = r.json().get('response', '')
    except Exception as e:
        ai_response = "Error connecting to AI Server."

    # Save AI message to DB
    if user_id:
        c.execute("INSERT INTO history (user_id, role, content, timestamp, starred) VALUES (?, ?, ?, ?, ?)",
                  (user_id, 'ai', ai_response, datetime.datetime.now(), False))
        conn.commit()
        conn.close()

    return jsonify({"response": ai_response})

if __name__ == '__main__':
    init_db()
    # Host on 0.0.0.0 so it can be exposed to the outside world
    app.run(host='0.0.0.0', port=5000, debug=True)
