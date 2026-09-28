"""One short probe records outbound model/effort/tool names, without credentials."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'preflight'
records = []


def upstream_target(base_url, local_path):
    """Map the probe's /v1 route to the configured provider without redirects."""
    base, route = urlsplit(base_url), urlsplit(local_path)
    if (base.scheme != 'https' or not base.hostname or base.username or base.password
            or base.query or base.fragment or route.scheme or route.netloc
            or not route.path.startswith('/v1/')):
        raise ValueError('Invalid provider or probe route')
    path = base.path.rstrip('/') + route.path[len('/v1'):]
    if route.query:
        path += '?' + route.query
    return base.hostname, base.port or 443, path


def names(tools):
    result = []
    for tool in tools:
        if tool.get('tools'):
            result.extend(names(tool['tools']))
        else:
            result.append(tool.get('name') or tool.get('type'))
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        payload = json.loads(body)
        tools = names(payload.get('tools', []))
        record = {'path': self.path, 'model': payload.get('model'), 'reasoning': payload.get('reasoning'),
                  'tools': tools, 'stream': payload.get('stream')}
        records.append(record)
        if set(tools) & {'exec_command', 'shell', 'shell_command', 'write_stdin', 'apply_patch', 'view_image'}:
            record['rejected_host_tool_exposure'] = True
            self.send_response(400); self.end_headers()
            self.wfile.write(b'{"error":{"message":"Unexpected host filesystem tool in isolated probe"}}')
            return
        cfg = json.loads((ROOT / 'CONFIG.json').read_text())
        hostname, port, path = upstream_target(cfg['provider_base_url'], self.path)
        record.update(upstream_host=hostname, upstream_path=path)
        conn = http.client.HTTPSConnection(hostname, port=port, timeout=100)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'connection')}
        try:
            conn.request('POST', path, body=body, headers=headers)
            response = conn.getresponse()
            record['http_status'] = response.status
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                    self.send_header(key, value)
            self.send_header('Connection', 'close'); self.end_headers()
            while chunk := response.read1(65536):
                self.wfile.write(chunk); self.wfile.flush()
        finally:
            conn.close()
