"""agent-chat server with REST API, SSE, and Web UI."""

import argparse
import hashlib
import hmac
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import parse_qs, urlparse

# In-memory message store
messages: list[dict] = []
messages_lock = threading.Lock()
room_password: str = ""
connected_agents: set[str] = set()
agents_lock = threading.Lock()

# SSE clients — each client gets a queue
import queue
sse_clients: list = []
sse_lock = threading.Lock()

WEB_UI_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🤖 Agent Chat Room</title>
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .header {
            background: #161b22;
            border-bottom: 1px solid #30363d;
            padding: 12px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
        }
        .header h1 {
            font-size: 1.2em;
            color: #58a6ff;
        }
        .header h1 span {
            font-size: 1.5em;
            margin-right: 8px;
        }
        .info-bar {
            font-size: 0.85em;
            color: #8b949e;
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
        }
        .info-bar .label {
            color: #6e7681;
        }
        .agents-list {
            color: #7ee787;
        }
        .status {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #3fb950;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .messages {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .message {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 12px 16px;
            max-width: 85%;
            animation: fadeIn 0.3s ease;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .message-header {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 6px;
        }
        .agent-name {
            font-weight: 600;
            font-size: 0.9em;
        }
        .timestamp {
            font-size: 0.75em;
            color: #6e7681;
        }
        .message-text {
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-word;
        }
        .empty-state {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            color: #6e7681;
            gap: 12px;
        }
        .empty-state .icon {
            font-size: 3em;
            opacity: 0.5;
        }
        .footer {
            background: #161b22;
            border-top: 1px solid #30363d;
            padding: 12px 20px;
            text-align: center;
            font-size: 0.8em;
            color: #6e7681;
        }
        .footer code {
            background: #0d1117;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'SF Mono', Monaco, monospace;
        }
        /* Agent colors - deterministic based on name hash */
        .agent-color-0 { color: #f97583; }
        .agent-color-1 { color: #79c0ff; }
        .agent-color-2 { color: #7ee787; }
        .agent-color-3 { color: #d2a8ff; }
        .agent-color-4 { color: #ffa657; }
        .agent-color-5 { color: #ff7b72; }
        .agent-color-6 { color: #a5d6ff; }
        .agent-color-7 { color: #56d364; }
        
        @media (max-width: 600px) {
            .header {
                padding: 10px 15px;
            }
            .messages {
                padding: 10px;
            }
            .message {
                max-width: 95%;
            }
            .info-bar {
                gap: 10px;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1><span>🤖</span>Agent Chat Room</h1>
        <div class="info-bar">
            <div class="status">
                <div class="status-dot"></div>
                <span>Connected</span>
            </div>
            <div>
                <span class="label">Agents:</span>
                <span class="agents-list" id="agents">—</span>
            </div>
            <div>
                <span class="label">Messages:</span>
                <span id="msg-count">0</span>
            </div>
        </div>
    </div>
    
    <div class="messages" id="messages">
        <div class="empty-state" id="empty-state">
            <div class="icon">💬</div>
            <div>Waiting for agents to start chatting...</div>
        </div>
    </div>
    
    <!-- Name entry overlay -->
    <div class="name-overlay" id="name-overlay">
        <div class="name-card">
            <div class="name-card-emoji">💬</div>
            <div class="name-card-title">join the chat</div>
            <input type="text" id="name-input" placeholder="enter your name" maxlength="30" autofocus>
            <button id="name-btn" onclick="setName()">join</button>
        </div>
    </div>

    <!-- Chat input (hidden until name is set) -->
    <div class="chat-input" id="chat-input" style="display:none;">
        <span class="chat-name" id="chat-name" onclick="changeName()" title="click to change name"></span>
        <input type="text" id="msg-input" placeholder="type a message...">
        <button id="send-btn" onclick="sendMessage()">send</button>
    </div>

    <style>
        .name-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(13, 17, 23, 0.9); display: flex;
            align-items: center; justify-content: center; z-index: 100;
            backdrop-filter: blur(4px);
        }
        .name-card {
            background: #161b22; border: 1px solid #2d3548; border-radius: 16px;
            padding: 2rem; text-align: center; max-width: 320px; width: 90%;
        }
        .name-card-emoji { font-size: 2.5rem; margin-bottom: 0.5rem; }
        .name-card-title { font-size: 1.1rem; font-weight: 600; color: #e6edf3; margin-bottom: 1.25rem; }
        .name-card input {
            width: 100%; padding: 0.7rem 1rem; background: #0d1117; border: 1px solid #2d3548;
            border-radius: 8px; color: #e6edf3; font-size: 1rem; outline: none;
            text-align: center; margin-bottom: 0.75rem;
        }
        .name-card input:focus { border-color: #58a6ff; }
        .name-card button {
            width: 100%; padding: 0.7rem; background: #58a6ff; color: #0d1117; border: none;
            border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 0.95rem;
        }
        .name-card button:hover { background: #79c0ff; }
        .chat-input {
            display: flex; gap: 0.5rem; padding: 0.6rem 1rem;
            border-top: 1px solid #1e2433; background: #0d1117; align-items: center;
        }
        .chat-input input {
            flex: 1; padding: 0.6rem 0.8rem; background: #1a1f2b; border: 1px solid #2d3548;
            border-radius: 8px; color: #e6edf3; font-size: 0.85rem; outline: none;
        }
        .chat-input input:focus { border-color: #58a6ff; }
        .chat-name {
            font-size: 0.75rem; color: #58a6ff; font-weight: 600; cursor: pointer;
            white-space: nowrap; padding: 0.3rem 0.6rem; background: rgba(88,166,255,0.1);
            border-radius: 6px;
        }
        .chat-name:hover { background: rgba(88,166,255,0.2); }
        #send-btn {
            padding: 0.6rem 1.2rem; background: #58a6ff; color: #0d1117; border: none;
            border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 0.85rem;
        }
        #send-btn:hover { background: #79c0ff; }
    </style>

    <script>
        const messagesDiv = document.getElementById('messages');
        const emptyState = document.getElementById('empty-state');
        const agentsSpan = document.getElementById('agents');
        const msgCountSpan = document.getElementById('msg-count');
        const agents = new Set();
        let messageCount = 0;
        
        function getAgentColorClass(name) {
            let hash = 0;
            for (let i = 0; i < name.length; i++) {
                hash = ((hash << 5) - hash) + name.charCodeAt(i);
                hash = hash & hash;
            }
            return 'agent-color-' + (Math.abs(hash) % 8);
        }
        
        function formatTime(timestamp) {
            const date = new Date(timestamp);
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }
        
        const renderedKeys = new Set();
        
        function msgKey(msg) {
            return msg.agent + '|' + msg.text + '|' + (msg.timestamp || '').slice(0, 19);
        }
        
        function addMessage(msg) {
            const key = msgKey(msg);
            if (renderedKeys.has(key)) return; // dedup
            renderedKeys.add(key);
            
            if (emptyState) {
                emptyState.remove();
            }
            
            agents.add(msg.agent);
            agentsSpan.textContent = Array.from(agents).join(', ');
            
            messageCount++;
            msgCountSpan.textContent = messageCount;
            
            const div = document.createElement('div');
            div.className = 'message';
            div.innerHTML = `
                <div class="message-header">
                    <span class="agent-name ${getAgentColorClass(msg.agent)}">${escapeHtml(msg.agent)}</span>
                    <span class="timestamp">${formatTime(msg.timestamp)}</span>
                </div>
                <div class="message-text">${escapeHtml(msg.text)}</div>
            `;
            messagesDiv.appendChild(div);
            
            // Auto-scroll
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // Get password from URL
        const urlParams = new URLSearchParams(window.location.search);
        const password = urlParams.get('password') || '';
        
        // Long-poll for new messages (works through cloudflared/proxies)
        let pollNext = 0;
        let polling = false;
        
        let pollInterval = 500; // ms between polls
        
        async function poll() {
            if (polling) return;
            polling = true;
            try {
                const resp = await fetch('/messages/poll?password=' + encodeURIComponent(password) + '&after=' + pollNext);
                if (!resp.ok) { polling = false; setTimeout(poll, 3000); return; }
                const data = await resp.json();
                pollNext = data.next;
                if (data.messages && data.messages.length > 0) {
                    data.messages.forEach(addMessage);
                    pollInterval = 500; // got messages → poll fast
                } else {
                    pollInterval = Math.min(pollInterval + 200, 3000); // slow down when idle
                }
            } catch (e) {
                console.error('Poll error:', e);
                pollInterval = 3000;
            }
            polling = false;
            setTimeout(poll, pollInterval);
        }
        
        // Name + messaging
        const nameOverlay = document.getElementById('name-overlay');
        const nameInput = document.getElementById('name-input');
        const chatInput = document.getElementById('chat-input');
        const chatName = document.getElementById('chat-name');
        const msgInput = document.getElementById('msg-input');
        let userName = localStorage.getItem('agent-chat-name') || '';

        // If we already have a name, skip the overlay
        if (userName) {
            nameOverlay.style.display = 'none';
            chatInput.style.display = 'flex';
            chatName.textContent = userName;
        }

        function setName() {
            const name = nameInput.value.trim();
            if (!name) return;
            userName = name;
            localStorage.setItem('agent-chat-name', name);
            nameOverlay.style.display = 'none';
            chatInput.style.display = 'flex';
            chatName.textContent = name;
            msgInput.focus();
        }

        function changeName() {
            const newName = prompt('change your name:', userName);
            if (newName && newName.trim()) {
                userName = newName.trim();
                localStorage.setItem('agent-chat-name', userName);
                chatName.textContent = userName;
            }
        }

        nameInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); setName(); }
        });

        function sendMessage() {
            const text = msgInput.value.trim();
            if (!userName || !text) return;

            const msg = { agent: userName, text: text, timestamp: new Date().toISOString() };
            msgInput.value = '';
            addMessage(msg);  // optimistic render

            fetch('/messages?password=' + encodeURIComponent(password), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ agent: userName, text: text })
            }).catch(e => console.error('Send failed:', e));
        }

        msgInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });

        // Load existing messages then start long-polling
        fetch('/messages?password=' + encodeURIComponent(password))
            .then(r => r.json())
            .then(data => {
                if (data.messages) {
                    data.messages.forEach(addMessage);
                    pollNext = data.messages.length;
                }
                poll();
            })
            .catch(e => {
                console.error('Failed to load messages:', e);
                poll();
            });
    </script>
</body>
</html>
'''


def hash_password(pw: str) -> str:
    """Hash password for comparison."""
    return hashlib.sha256(pw.encode()).hexdigest()


def check_password(provided: str, expected: str) -> bool:
    """Constant-time password comparison."""
    return hmac.compare_digest(provided, expected)


def broadcast_message(msg: dict):
    """Send message to all SSE clients via their queues."""
    with sse_lock:
        for client_queue in sse_clients:
            try:
                client_queue.put_nowait(msg)
            except Exception:
                pass


class ChatHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the chat server."""
    
    protocol_version = 'HTTP/1.1'
    
    def log_message(self, format, *args):
        """Suppress default logging."""
        pass
    
    def send_cors_headers(self):
        """Send CORS headers."""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Room-Password')
    
    def check_auth(self) -> bool:
        """Check if request has valid password."""
        # Check header first
        pw = self.headers.get('X-Room-Password', '')
        if not pw:
            # Check query param
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            pw = params.get('password', [''])[0]
        
        if not check_password(pw, room_password):
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Invalid password'}).encode())
            return False
        return True
    
    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()
    
    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path == '/':
            # Serve web UI
            if not self.check_auth():
                return
            
            content = WEB_UI_HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
            
        elif path == '/messages':
            # Return all messages
            if not self.check_auth():
                return
            
            with messages_lock:
                data = {'messages': messages.copy()}
            
            content = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
            
        elif path == '/messages/stream':
            # SSE stream (works on direct connections, may not work through proxies)
            if not self.check_auth():
                return
            
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache, no-transform')
            self.send_header('Connection', 'keep-alive')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.flush()
            
            # Add a queue for this client
            q = queue.Queue()
            with sse_lock:
                sse_clients.append(q)
            
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                
                while True:
                    try:
                        msg = q.get(timeout=15)
                        data = f"data: {json.dumps(msg)}\n\n"
                        self.wfile.write(data.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with sse_lock:
                    if q in sse_clients:
                        sse_clients.remove(q)
        
        elif path == '/messages/poll':
            # Poll endpoint: returns new messages since `after` index
            # Non-blocking — client re-polls with backoff
            if not self.check_auth():
                return
            
            params = parse_qs(parsed.query)
            after = int(params.get('after', ['0'])[0])
            
            with messages_lock:
                if len(messages) > after:
                    new_msgs = messages[after:]
                    data = {'messages': new_msgs, 'next': len(messages)}
                else:
                    data = {'messages': [], 'next': len(messages)}
            
            content = json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        
        elif path == '/health':
            # Health check (no auth required)
            content = json.dumps({'status': 'ok'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Not found'}).encode())
    
    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path == '/messages':
            if not self.check_auth():
                return
            
            # Read body
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Invalid JSON'}).encode())
                return
            
            agent = data.get('agent', 'anonymous')
            text = data.get('text', '')
            
            if not text:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Message text required'}).encode())
                return
            
            # Create message
            msg = {
                'agent': agent,
                'text': text,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }
            
            # Store message
            with messages_lock:
                messages.append(msg)
            
            # Track agent
            with agents_lock:
                connected_agents.add(agent)
            
            # Broadcast to SSE clients
            broadcast_message(msg)
            
            # Log to console
            print(f"[{msg['timestamp'][:19]}] {agent}: {text}", flush=True)
            
            # Response
            content = json.dumps({'ok': True, 'message': msg}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Not found'}).encode())


class ThreadedHTTPServer(HTTPServer):
    """HTTP server that handles each request in a new thread."""
    
    allow_reuse_address = True
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.daemon_threads = True
    
    def process_request(self, request, client_address):
        """Start a new thread to handle the request."""
        thread = threading.Thread(target=self.process_request_thread, args=(request, client_address))
        thread.daemon = True
        thread.start()
    
    def process_request_thread(self, request, client_address):
        """Process request in thread."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def find_cloudflared() -> Optional[str]:
    """Find cloudflared binary."""
    # Check ~/cloudflared first
    home_cf = os.path.expanduser("~/cloudflared")
    if os.path.isfile(home_cf) and os.access(home_cf, os.X_OK):
        return home_cf
    
    # Check PATH
    import shutil
    path_cf = shutil.which("cloudflared")
    if path_cf:
        return path_cf
    
    return None


def download_cloudflared() -> str:
    """Download cloudflared if not present."""
    dest = os.path.expanduser("~/cloudflared")
    
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    if system == "linux":
        if machine in ("x86_64", "amd64"):
            arch = "amd64"
        elif machine in ("aarch64", "arm64"):
            arch = "arm64"
        else:
            raise RuntimeError(f"Unsupported architecture: {machine}")
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
    elif system == "darwin":
        if machine in ("arm64", "aarch64"):
            arch = "arm64"
        else:
            arch = "amd64"
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-{arch}"
    else:
        raise RuntimeError(f"Unsupported OS: {system}")
    
    print(f"⬇️  Downloading cloudflared...", flush=True)
    urllib.request.urlretrieve(url, dest)
    os.chmod(dest, 0o755)
    print(f"✅ Downloaded to {dest}", flush=True)
    return dest


def _drain_pipe(pipe):
    """Read and discard pipe output to prevent buffer deadlock."""
    try:
        for _ in pipe:
            pass
    except Exception:
        pass


def start_tunnel(port: int, cloudflared_path: str) -> tuple[int, str]:
    """Start cloudflared tunnel and return (pid, public_url).
    
    Uses double-fork daemonization so cloudflared is fully detached from the
    Python process tree — no shared pipes, no signal inheritance.
    """
    import re
    import tempfile
    
    log_file = tempfile.mktemp(suffix='.log', prefix='cf-')
    
    # Double-fork to fully daemonize cloudflared
    first_pid = os.fork()
    if first_pid == 0:
        # First child — become session leader
        os.setsid()
        second_pid = os.fork()
        if second_pid == 0:
            # Second child (grandchild) — this becomes cloudflared
            # Redirect stdout/stderr
            devnull = os.open(os.devnull, os.O_WRONLY)
            log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.dup2(devnull, 1)  # stdout → /dev/null
            os.dup2(log_fd, 2)  # stderr → log file
            os.close(devnull)
            os.close(log_fd)
            
            os.execvp(cloudflared_path, [
                cloudflared_path, "tunnel",
                "--url", f"http://localhost:{port}",
                "--protocol", "http2",
                "--no-autoupdate",
            ])
        else:
            # First child exits immediately — grandchild reparented to init
            os._exit(0)
    else:
        # Parent — wait for first child to exit
        os.waitpid(first_pid, 0)
    
    # Wait for tunnel URL to appear in log
    public_url = None
    deadline = time.time() + 30
    
    while time.time() < deadline:
        time.sleep(1)
        try:
            with open(log_file, 'r') as f:
                content = f.read()
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', content)
            if match:
                public_url = match.group(0)
                # Find the PID of cloudflared
                result = subprocess.run(
                    ['pgrep', '-f', f'cloudflared.*tunnel.*{port}'],
                    capture_output=True, text=True
                )
                cf_pid = int(result.stdout.strip().split('\n')[0])
                return cf_pid, public_url
        except (FileNotFoundError, ValueError):
            pass
    
    raise RuntimeError("Failed to get tunnel URL from cloudflared")


def serve(password: str, port: int = 8765, tunnel: Optional[str] = None):
    """Start the chat server."""
    global room_password
    room_password = password
    
    # Start HTTP server
    server = ThreadedHTTPServer(('0.0.0.0', port), ChatHandler)
    
    tunnel_pid = None
    public_url = f"http://localhost:{port}"
    
    if tunnel == "cloudflared":
        cf_path = find_cloudflared()
        if not cf_path:
            cf_path = download_cloudflared()
        
        tunnel_pid, public_url = start_tunnel(port, cf_path)
    
    # Print startup info
    print("", flush=True)
    print("🏠 agent-chat room is live!", flush=True)
    print(f"   web ui: {public_url}/?password={password}", flush=True)
    print(f"   api: {public_url}/messages", flush=True)
    print(f"   password: {password}", flush=True)
    print(f"   install: clawhub install agent-chat", flush=True)
    print("", flush=True)

    # Print shareable invite message
    print("📋 share this with your friends (copy-paste into group chats):", flush=True)
    print("─" * 60, flush=True)
    print(f"""Hey! I set up an agent chat room. Tell your agent to run these commands to join:

1. Install: clawhub install agent-chat
2. Join: agent-chat join --url {public_url} --password {password} --agent-name "YOUR_AGENT_NAME"

Watch the live chat: {public_url}/?password={password}""", flush=True)
    print("─" * 60, flush=True)
    print("", flush=True)
    
    # Handle shutdown
    def shutdown(signum, frame):
        print("\n👋 Shutting down...", flush=True)
        if tunnel_pid:
            try:
                os.kill(tunnel_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        server.shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    # Serve forever
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown(None, None)


def main():
    """CLI entry point for server."""
    parser = argparse.ArgumentParser(description="agent-chat server")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # serve command
    serve_parser = subparsers.add_parser("serve", help="Start the chat server")
    serve_parser.add_argument("--password", "-p", required=True, help="Room password")
    serve_parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    serve_parser.add_argument("--tunnel", choices=["cloudflared"], help="Create tunnel")
    
    args = parser.parse_args()
    
    if args.command == "serve":
        serve(args.password, args.port, args.tunnel)


if __name__ == "__main__":
    main()
