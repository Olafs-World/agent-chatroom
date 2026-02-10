"""agent-chat CLI client for joining, listening, and sending messages."""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from typing import Optional


def format_message(msg: dict) -> str:
    """Format a message for display."""
    ts = msg.get('timestamp', '')
    if ts:
        # Parse ISO timestamp and format nicely
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            ts = dt.strftime('%H:%M:%S')
        except Exception:
            ts = ts[:19]
    
    agent = msg.get('agent', 'anonymous')
    text = msg.get('text', '')
    
    return f"[{ts}] {agent}: {text}"


def send_message(url: str, password: str, agent: str, message: str) -> bool:
    """Send a single message to the chat room."""
    # Ensure URL ends correctly
    api_url = url.rstrip('/') + '/messages'
    
    data = json.dumps({'agent': agent, 'text': message}).encode()
    
    req = urllib.request.Request(
        api_url,
        data=data,
        headers={
            'Content-Type': 'application/json',
            'X-Room-Password': password
        },
        method='POST'
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get('ok', False)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("❌ Invalid password", file=sys.stderr, flush=True)
        else:
            print(f"❌ HTTP error {e.code}: {e.reason}", file=sys.stderr, flush=True)
        return False
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr, flush=True)
        return False


def get_messages(url: str, password: str) -> list:
    """Get all messages from the chat room."""
    api_url = url.rstrip('/') + '/messages'
    
    req = urllib.request.Request(
        api_url,
        headers={'X-Room-Password': password},
        method='GET'
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get('messages', [])
    except Exception as e:
        print(f"❌ Error fetching messages: {e}", file=sys.stderr, flush=True)
        return []


def listen_sse(url: str, password: str, callback):
    """Listen for messages via SSE stream."""
    api_url = url.rstrip('/') + '/messages/stream?password=' + urllib.parse.quote(password)
    
    req = urllib.request.Request(
        api_url,
        headers={'Accept': 'text/event-stream'},
        method='GET'
    )
    
    try:
        with urllib.request.urlopen(req, timeout=None) as resp:
            buffer = ""
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                
                buffer += chunk.decode('utf-8', errors='replace')
                
                # Process complete lines
                while '\n\n' in buffer:
                    event, buffer = buffer.split('\n\n', 1)
                    
                    # Parse SSE event
                    for line in event.split('\n'):
                        if line.startswith('data: '):
                            data = line[6:]
                            if data == ':keepalive':
                                continue
                            try:
                                msg = json.loads(data)
                                callback(msg)
                            except json.JSONDecodeError:
                                pass
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"❌ SSE error: {e}", file=sys.stderr, flush=True)


def listen_poll(url: str, password: str, callback):
    """Listen for messages via polling (works through all proxies)."""
    api_url = url.rstrip('/') + '/messages/poll'
    next_idx = 0
    interval = 0.5  # seconds
    
    while True:
        try:
            poll_url = f"{api_url}?password={urllib.parse.quote(password)}&after={next_idx}"
            req = urllib.request.Request(poll_url, method='GET')
            
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                next_idx = result.get('next', next_idx)
                msgs = result.get('messages', [])
                for msg in msgs:
                    callback(msg)
                
                if msgs:
                    interval = 0.5  # got messages, poll fast
                else:
                    interval = min(interval + 0.2, 3.0)  # slow down when idle
            
            time.sleep(interval)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Poll error: {e}", file=sys.stderr, flush=True)
            time.sleep(3)


def cmd_send(args):
    """Handle send command."""
    success = send_message(args.url, args.password, args.agent_name, args.message)
    if success:
        print(f"✅ Sent message as {args.agent_name}", flush=True)
    sys.exit(0 if success else 1)


def cmd_listen(args):
    """Handle listen command."""
    print(f"👂 Listening to {args.url}...", file=sys.stderr, flush=True)
    
    def on_message(msg):
        print(format_message(msg), flush=True)
    
    listen_poll(args.url, args.password, on_message)


def cmd_join(args):
    """Handle join command - announce presence and listen."""
    print(f"🤖 Joining as {args.agent_name}...", file=sys.stderr, flush=True)
    
    # Get existing messages first (so we know the index before join msg)
    existing = get_messages(args.url, args.password)
    for msg in existing:
        print(format_message(msg), flush=True)
    
    if existing:
        print("--- end of history ---", flush=True)
    
    # Send join message
    send_message(args.url, args.password, args.agent_name, f"*joined the chat*")
    
    def on_message(msg):
        print(format_message(msg), flush=True)
    
    # Listen from after existing messages (join msg + future will come through)
    listen_poll(args.url, args.password, on_message)


def main():
    """CLI entry point for client."""
    parser = argparse.ArgumentParser(
        description="agent-chat client",
        prog="agent-chat"
    )
    subparsers = parser.add_subparsers(dest="command")
    
    # serve command (delegates to server)
    serve_parser = subparsers.add_parser("serve", help="Start the chat server")
    serve_parser.add_argument("--password", "-p", required=True, help="Room password")
    serve_parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    serve_parser.add_argument("--tunnel", choices=["cloudflared"], help="Create tunnel")
    
    # send command
    send_parser = subparsers.add_parser("send", help="Send a message")
    send_parser.add_argument("--url", "-u", required=True, help="Server URL")
    send_parser.add_argument("--password", "-p", required=True, help="Room password")
    send_parser.add_argument("--agent-name", "-a", required=True, help="Your agent name")
    send_parser.add_argument("--message", "-m", required=True, help="Message to send")
    
    # listen command
    listen_parser = subparsers.add_parser("listen", help="Listen for messages")
    listen_parser.add_argument("--url", "-u", required=True, help="Server URL")
    listen_parser.add_argument("--password", "-p", required=True, help="Room password")
    
    # join command
    join_parser = subparsers.add_parser("join", help="Join the chat (announce + listen)")
    join_parser.add_argument("--url", "-u", required=True, help="Server URL")
    join_parser.add_argument("--password", "-p", required=True, help="Room password")
    join_parser.add_argument("--agent-name", "-a", required=True, help="Your agent name")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    if args.command == "serve":
        # Delegate to server module
        from agent_chatroom import server
        server.serve(args.password, args.port, args.tunnel)
    elif args.command == "send":
        cmd_send(args)
    elif args.command == "listen":
        cmd_listen(args)
    elif args.command == "join":
        cmd_join(args)


if __name__ == "__main__":
    main()
