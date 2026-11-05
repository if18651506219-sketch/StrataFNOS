#!/usr/bin/env python3
"""Black-box regression suite for sock_proxy.py against a mock upstream.

Runs the real proxy as a subprocess over an AF_UNIX socket and drives raw
HTTP/1.1 through it. No fnOS, no Node, no GPU needed.

    python3 tools/verify_proxy.py
    STRATA_PROXY_SRC=/path/to/sock_proxy.py python3 tools/verify_proxy.py

Baseline (fnos-v0.2.10, unpatched): 6/8 of the original scenarios passed;
SSE framing, Expect: 100-continue and body-carry/pipelining were broken.
This suite keeps those 8 and adds WebSocket, forged-header, foreign-prefix,
long-idle-stream and HTML-rewrite scenarios (13 total).
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_SRC = os.environ.get('STRATA_PROXY_SRC', os.path.join(ROOT, 'sock_proxy.py'))
SOCK = '/tmp/stp.sock'          # short: AF_UNIX paths cap at ~104 bytes
PREFIX = b'/app/strata-app'
results = []


def check(name, ok, detail=''):
    results.append((name, ok, detail))
    print(('PASS' if ok else 'FAIL') + ' | ' + name + ' | ' + detail)


class Upstream:
    """One connection per request, mirroring the proxy's upstream behaviour."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('127.0.0.1', 0))
        self.srv.listen(32)
        self.port = self.srv.getsockname()[1]
        self.connections = []
        self.lock = threading.Lock()
        threading.Thread(target=self.accept_loop, daemon=True).start()

    def accept_loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def serve(self, c):
        c.settimeout(6)
        data = b''
        try:
            while b'\r\n\r\n' not in data:
                d = c.recv(4096)
                if not d:
                    break
                data += d
            head, _, rest = data.partition(b'\r\n\r\n')
            low = head.lower()
            path = head.split(b' ')[1] if b' ' in head else b'/'

            if path == b'/ws':
                with self.lock:
                    self.connections.append(data)
                c.sendall(b'HTTP/1.1 101 Switching Protocols\r\n'
                          b'Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
                c.settimeout(None)
                while True:
                    d = c.recv(4096)
                    if not d:
                        break
                    c.sendall(b'ECHO:' + d)
                return

            m = re.search(rb'content-length:\s*(\d+)', low)
            if b'expect: 100-continue' in low:
                c.sendall(b'HTTP/1.1 100 Continue\r\n\r\n')
            if m:
                need = int(m.group(1))
                while len(rest) < need:
                    d = c.recv(4096)
                    if not d:
                        break
                    rest += d
                data = head + b'\r\n\r\n' + rest
            elif b'transfer-encoding: chunked' in low:
                while b'\r\n0\r\n\r\n' not in rest:
                    d = c.recv(4096)
                    if not d:
                        break
                    rest += d
                data = head + b'\r\n\r\n' + rest
            with self.lock:
                self.connections.append(data)

            if path == b'/sse':
                c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                          b'Transfer-Encoding: chunked\r\nCache-Control: no-cache\r\n\r\n'
                          b'd\r\ndata: hello\n\n\r\n')
                time.sleep(0.5)
                c.sendall(b'0\r\n\r\n')
            elif path == b'/idle-sse':
                # >10s gap: the old `create_connection(timeout=10)` left a
                # 10s timeout on the socket and truncated the stream here.
                c.settimeout(None)
                c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                          b'Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n')
                time.sleep(11)
                c.sendall(b'3\r\ndef\r\n0\r\n\r\n')
            elif path == b'/chunked':
                c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n'
                          b'Transfer-Encoding: chunked\r\n\r\n'
                          b'3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n')
            elif path == b'/html':
                body = (b'<html><head></head><body><script src="/assets/a.js"></script>'
                        b'<a href="/api/x">x</a></body></html>')
                c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n'
                          b'Content-Length: %d\r\n\r\n' % len(body) + body)
            else:
                c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n'
                          b'Content-Length: 2\r\nConnection: keep-alive\r\n\r\nOK')
            time.sleep(0.4)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def received(self):
        with self.lock:
            return list(self.connections)


def start_proxy(port):
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    log = open('/tmp/strata-proxy.log', 'wb')
    p = subprocess.Popen([sys.executable, PROXY_SRC, SOCK, str(port), PREFIX.decode()],
                         stdout=log, stderr=subprocess.STDOUT)
    for _ in range(40):
        if os.path.exists(SOCK):
            break
        time.sleep(0.05)
    return p


def client(req, timeout=4.0, extra=None, pause=0.0, until=None):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    c.connect(SOCK)
    c.sendall(req)
    if extra:
        time.sleep(pause)
        for chunk in extra:
            c.sendall(chunk)
            time.sleep(0.02)
    out = b''
    try:
        while True:
            d = c.recv(65536)
            if not d:
                break
            out += d
            if until and until(out):
                break
            if until:
                # an explicit termination condition means "wait for ALL of it",
                # not just the first framed response (pipelining scenarios)
                continue
            if b'\r\n\r\n' in out:
                head, _, body = out.partition(b'\r\n\r\n')
                m = re.search(rb'content-length:\s*(\d+)', head.lower())
                if m and len(body) >= int(m.group(1)):
                    break
    except (socket.timeout, OSError):
        pass
    c.close()
    return out


def decode_chunked(body):
    """Minimal chunked decoder used to assert SSE events survive framing."""
    out = b''
    while True:
        line, sep, rest = body.partition(b'\r\n')
        if not sep:
            return out
        try:
            n = int(line.split(b';', 1)[0], 16)
        except ValueError:
            return out
        if n == 0:
            return out
        out += rest[:n]
        body = rest[n + 2:]


def main():
    up = Upstream()
    proxy = start_proxy(up.port)
    time.sleep(0.2)

    # 1. small POST, headers+body in one write
    out = client(b'POST /app/strata-app/small HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello')
    rec = up.received()
    check('small POST body relay', out.endswith(b'OK') and any(b'hello' in r for r in rec),
          'client=%r upstream_bodies=%r' % (out[-40:], [r[-20:] for r in rec]))

    # 2. 10KB POST split over several writes
    body = b'x' * 10240
    out = client(b'POST /app/strata-app/big HTTP/1.1\r\nHost: x\r\nContent-Length: 10240\r\n\r\n',
                 extra=[body[:3000], body[3000:7000], body[7000:]], pause=0.05)
    got = max((len(r.split(b'\r\n\r\n', 1)[1]) for r in up.received() if b'/big' in r), default=0)
    check('10KB POST split writes', got == 10240 and out.endswith(b'OK'), 'upstream_body_len=%d' % got)

    # 3. pipelined second request riding in the same recv as the first body
    before = len(up.received())
    out = client(b'POST /app/strata-app/pipe HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello'
                 b'GET /app/strata-app/second HTTP/1.1\r\nHost: x\r\n\r\n', timeout=3.0,
                 until=lambda b: b.count(b'HTTP/1.1 200') >= 2)
    new = up.received()[before:]
    pipe_req = next((r for r in new if b'/pipe' in r), b'')
    second_req = next((r for r in new if b'/second' in r), b'')
    body_ok = pipe_req.split(b'\r\n\r\n', 1)[1] == b'hello'
    second_ok = second_req.startswith(b'GET /second ')
    check('pipelining: carry clipped AND next request served',
          body_ok and second_ok and out.count(b'HTTP/1.1 200') == 2,
          'pipe_body=%r second_req=%r responses=%d' % (
              pipe_req.split(b'\r\n\r\n', 1)[1][:30], second_req.split(b'\r\n', 1)[0],
              out.count(b'HTTP/1.1 200')))

    # 4. Expect: 100-continue — real client waits for 100 before sending body
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(4)
    c.connect(SOCK)
    c.sendall(b'POST /app/strata-app/expect HTTP/1.1\r\nHost: x\r\n'
              b'Expect: 100-continue\r\nContent-Length: 5\r\n\r\n')
    try:
        interim = c.recv(4096)
    except OSError:
        interim = b''
    if b'100 Continue' in interim:
        c.sendall(b'hello')
    out = b''
    try:
        while True:
            d = c.recv(4096)
            if not d:
                break
            out += d
            if b'OK' in out:
                break
    except OSError:
        pass
    c.close()
    expect_req = next((r for r in up.received() if b'/expect' in r), b'')
    check('Expect: 100-continue answered + body delivered',
          b'100 Continue' in interim and b'hello' in expect_req and b'OK' in out,
          'interim=%r upstream_body=%r' % (interim[:40], expect_req.split(b'\r\n\r\n', 1)[-1][:20]))

    # 5. SSE response with upstream chunked framing: TE header must survive,
    #    and the event must be decodable from the chunk stream.
    out = client(b'GET /app/strata-app/sse HTTP/1.1\r\nHost: x\r\n\r\n', timeout=3.0,
                 until=lambda b: b'0\r\n\r\n' in b)
    head = out.split(b'\r\n\r\n', 1)[0]
    sse_body = out.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in out else b''
    has_te = b'transfer-encoding: chunked' in head.lower()
    decoded = decode_chunked(sse_body)
    check('SSE chunked framing preserved', has_te and b'data: hello' in decoded,
          'resp_has_TE=%s decoded=%r' % (has_te, decoded[:40]))

    # 6. ordinary chunked response
    out = client(b'GET /app/strata-app/chunked HTTP/1.1\r\nHost: x\r\n\r\n', timeout=3.0,
                 until=lambda b: b'0\r\n\r\n' in b)
    ok = b'3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n' in out or b'abcdef' in out
    check('chunked response re-framed', ok, 'client=%r' % out[-80:])

    # 7. chunked request body, including a chunk extension
    out = client(b'POST /app/strata-app/chunkreq HTTP/1.1\r\nHost: x\r\n'
                 b'Transfer-Encoding: chunked\r\n\r\n5;note=1\r\nhello\r\n0\r\n\r\n', timeout=4.0)
    rec = up.received()
    ok = any(b'5;note=1\r\nhello\r\n0\r\n\r\n' in r for r in rec) and out.endswith(b'OK')
    check('chunked request body relay', ok, 'upstream_tail=%r' % [r[-30:] for r in rec][-2:])

    # 8. two sequential keep-alive requests on one client connection
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(4)
    c.connect(SOCK)
    c.sendall(b'GET /app/strata-app/ka1 HTTP/1.1\r\nHost: x\r\n\r\n')
    time.sleep(0.4)
    c.sendall(b'GET /app/strata-app/ka2 HTTP/1.1\r\nHost: x\r\n\r\n')
    buf = b''
    try:
        while True:
            d = c.recv(65536)
            if not d:
                break
            buf += d
            if buf.count(b'HTTP/1.1 200') >= 2:
                break
    except OSError:
        pass
    c.close()
    check('keep-alive two requests', buf.count(b'HTTP/1.1 200') == 2,
          'responses=%d' % buf.count(b'HTTP/1.1 200'))

    # 9. WebSocket upgrade: 101 handshake + full-duplex echo
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(4)
    c.connect(SOCK)
    c.sendall(b'GET /app/strata-app/ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n'
              b'Connection: Upgrade\r\n\r\n')
    buf = b''
    try:
        while b'101 Switching Protocols' not in buf or b'\r\n\r\n' not in buf:
            buf += c.recv(4096)
        c.sendall(b'PING')
        while b'ECHO:PING' not in buf:
            d = c.recv(4096)
            if not d:
                break
            buf += d
    except OSError:
        pass
    c.close()
    ws_req = next((r for r in up.received() if b'/ws' in r), b'')
    check('WebSocket upgrade tunnel',
          b'101 Switching Protocols' in buf and b'ECHO:PING' in buf
          and b'upgrade: websocket' in ws_req.lower(),
          'handshake=%r echo=%r upstream_has_upgrade=%s' % (
              buf[:40], b'ECHO:PING' in buf, b'upgrade: websocket' in ws_req.lower()))

    # 10. forged forwarding headers must not reach the host
    client(b'GET /app/strata-app/forged HTTP/1.1\r\nHost: x\r\n'
           b'X-Forwarded-Prefix: /evil\r\nForwarded: for=1.2.3.4\r\n\r\n')
    forged = next((r for r in up.received() if b'/forged' in r), b'')
    check('forged X-Forwarded-* dropped',
          b'evil' not in forged and b'forwarded' not in forged.lower(),
          'upstream_head=%r' % forged.split(b'\r\n\r\n', 1)[0][-120:])

    # 11. a foreign /app/<segment>/ prefix must NOT be stripped
    client(b'GET /app/other-app/api/x HTTP/1.1\r\nHost: x\r\n\r\n')
    foreign = next((r for r in up.received() if b'other-app' in r), b'')
    check('foreign /app/<seg>/ prefix untouched',
          foreign.startswith(b'GET /app/other-app/api/x '),
          'upstream_line=%r' % foreign.split(b'\r\n', 1)[0])

    # 12. SSE idle >10s must not be truncated by an inherited socket timeout
    out = client(b'GET /app/strata-app/idle-sse HTTP/1.1\r\nHost: x\r\n\r\n', timeout=15.0,
                 until=lambda b: b'0\r\n\r\n' in b)
    idle_body = out.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in out else b''
    decoded = decode_chunked(idle_body)
    check('SSE idle >10s not truncated', b'abc' in decoded and b'def' in decoded,
          'decoded=%r' % decoded[:40])

    # 13. HTML body rewriting + official API-base hook
    out = client(b'GET /app/strata-app/html HTTP/1.1\r\nHost: x\r\n\r\n')
    check('HTML rewrite + __STRATA_HOST__ hook',
          b'/app/strata-app/assets/a.js' in out and b'__STRATA_HOST__' in out,
          'body=%r' % out[-160:])

    proxy.terminate()
    try:
        proxy.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proxy.kill()
    print('\nSUMMARY: %d/%d PASS' % (sum(1 for _, ok, _ in results if ok), len(results)))
    for name, ok, detail in results:
        if not ok:
            print('  FAILED: %s -- %s' % (name, detail))
    sys.exit(0 if all(ok for _, ok, _ in results) else 1)


if __name__ == '__main__':
    main()
