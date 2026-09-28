"""Ephemeral per-attempt Messages proxy; records metadata, never auth or content."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import threading
from urllib.parse import urlsplit

from claude_goal import save
from preflight_binding import expected_tool_names


def upstream_target(base_url, route):
    base, path = urlsplit(base_url), urlsplit(route)
    if (base.scheme != 'https' or not base.hostname or base.username or base.password or base.query or base.fragment
            or path.scheme or path.netloc or path.path not in ('/v1/messages', '/v1/messages/count_tokens')):
        raise ValueError('Invalid Messages provider URL or route')
    prefix = base.path.rstrip('/')
    target = prefix + (path.path[3:] if prefix.endswith('/v1') else path.path)
    if path.query:
        target += '?' + path.query
    return base.hostname, base.port or 443, target


def check_payload(payload, config, route, *, row, condition):
    tools = [x.get('name') for x in payload.get('tools', [])]
    names_ok = len(tools) == len(set(tools)) and set(tools) == expected_tool_names(condition, row)
    count = urlsplit(route).path.endswith('/count_tokens')
    return {'path': route, 'row': row, 'condition': condition, 'model': payload.get('model'), 'output_config': payload.get('output_config'),
            'thinking': payload.get('thinking'), 'max_tokens': payload.get('max_tokens'),
            'tools': tools, 'stream': payload.get('stream'),
            'rejected_host_tool_exposure': not names_ok,
            'contract_valid': bool(names_ok and payload.get('model') == config['model']
                                   and (count or (payload.get('output_config') or {}).get('effort') == config['effort']))}


class Proxy:
    def __init__(self, config, output, *, row, condition):
        expected_tool_names(condition, row)
        self.condition = condition
        self.config, self.output, self.row, self.records, self.lock = config, output, row, [], threading.Lock()
        proxy = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                record = {'path': self.path}
                conn = None
                headers_sent = False
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 64000000:
                        raise ValueError('Invalid request size')
                    body = self.rfile.read(length)
                    payload = json.loads(body)
                    record.update(check_payload(payload, proxy.config, self.path, row=proxy.row, condition=proxy.condition))
                    if not record['contract_valid']:
                        raise ValueError('Model, effort or isolated tool registry mismatch')
                    host, port, route = upstream_target(proxy.config['provider_base_url'], self.path)
                    record.update(upstream_host=host, upstream_path=route)
                    conn = http.client.HTTPSConnection(host, port, timeout=300)
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'connection')}
                    conn.request('POST', route, body, headers)
                    response = conn.getresponse()
                    record['http_status'] = response.status
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() not in ('connection', 'content-length', 'transfer-encoding'):
                            self.send_header(name, value)
                    self.send_header('Connection', 'close'); self.end_headers(); headers_sent = True
                    proxy.record(record)
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk); self.wfile.flush()
                except Exception as error:
                    record['proxy_error'] = type(error).__name__
                    if not headers_sent:
                        self.send_response(400 if isinstance(error, (ValueError, TypeError)) else 502)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Connection', 'close'); self.end_headers()
                        self.wfile.write(json.dumps({'type': 'error', 'error': {'type': 'api_error',
                            'message': 'Isolated proxy rejected request' if isinstance(error, (ValueError, TypeError)) else 'Upstream transport failed'}}).encode())
                finally:
                    if conn:
                        conn.close()
                    proxy.record(record)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True

    def record(self, value):
        with self.lock:
            if not any(x is value for x in self.records):
                self.records.append(value)
            save(self.output, self.records)

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return 'http://127.0.0.1:' + str(self.server.server_port)

    def __exit__(self, *args):
        self.server.shutdown(); self.server.server_close()
