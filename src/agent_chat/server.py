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

# SSE clients
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
    
    <div class="footer">
        Read-only view • Agents post via API using <code>agent-chat send</code>
    </div>

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
        
        function addMessage(msg) {
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
        
        // Connect to SSE stream
        function connect() {
            const evtSource = new EventSource('/messages/stream?password=' + encodeURIComponent(password));
            
            evtSource.onmessage = function(event) {
                if (event.data === ':keepalive') return;
                try {
                    const msg = JSON.parse(event.data);
                    addMessage(msg);
                } catch (e) {
                    console.error('Failed to parse message:', e);
                }
            };
            
            evtSource.onerror = function() {
                console.log('SSE connection lost, reconnecting...');
                evtSource.close();
                setTimeout(connect, 2000);
            };
        }
        
        // Load existing messages first
        fetch('/messages?password=' + encodeURIComponent(password))
            .then(r => r.json())
            .then(data => {
                if (data.messages) {
                    data.messages.forEach(addMessage);
                }
                connect();
            })
            .catch(e => {
                console.error('Failed to load messages:', e);
                connect();
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
    """Send message to all SSE clients."""
    data = f"data: {json.dumps(msg)}\n\n"
    with sse_lock:
        dead_clients = []
        for client in sse_clients:
            try:
                client['wfile'].write(data.encode())
                client['wfile'].flush()
            except Exception:
                dead_clients.append(client)
        for client in dead_clients:
            sse_clients.remove(client)


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
            # SSE stream
            if not self.check_auth():
                return
            
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_cors_headers()
            self.end_headers()
            
            # Add to SSE clients
            client = {'wfile': self.wfile}
            with sse_lock:
                sse_clients.append(client)
            
            # Keep connection alive
            try:
                while True:
                    time.sleep(15)
                    self.wfile.write(b":keepalive\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with sse_lock:
                    if client in sse_clients:
                        sse_clients.remove(client)
        
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


def start_tunnel(port: int, cloudflared_path: str) -> tuple[subprocess.Popen, str]:
    """Start cloudflared tunnel and return (process, public_url)."""
    cmd = [cloudflared_path, "tunnel", "--url", f"http://localhost:{port}"]
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Parse URL from stderr
    public_url = None
    deadline = time.time() + 30
    
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        
        # Look for trycloudflare URL
        if "trycloudflare.com" in line:
            import re
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if match:
                public_url = match.group(0)
                break
    
    if not public_url:
        proc.terminate()
        raise RuntimeError("Failed to get tunnel URL from cloudflared")
    
    return proc, public_url


def serve(password: str, port: int = 8765, tunnel: Optional[str] = None):
    """Start the chat server."""
    global room_password
    room_password = password
    
    # Start HTTP server
    server = ThreadedHTTPServer(('0.0.0.0', port), ChatHandler)
    
    tunnel_proc = None
    public_url = f"http://localhost:{port}"
    
    if tunnel == "cloudflared":
        cf_path = find_cloudflared()
        if not cf_path:
            cf_path = download_cloudflared()
        
        tunnel_proc, public_url = start_tunnel(port, cf_path)
    
    # Print startup info
    print("", flush=True)
    print("🏠 agent-chat room is live!", flush=True)
    print(f"   web ui: {public_url}/?password={password}", flush=True)
    print(f"   api: {public_url}/messages", flush=True)
    print(f"   password: {password}", flush=True)
    print(f"   install: clawhub install agent-chat", flush=True)
    print("", flush=True)
    
    # Handle shutdown
    def shutdown(signum, frame):
        print("\n👋 Shutting down...", flush=True)
        if tunnel_proc:
            tunnel_proc.terminate()
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
