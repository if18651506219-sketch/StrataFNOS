#!/usr/bin/env python3
"""Unix-socket -> TCP proxy for the fnOS unified gateway, serving the Strata App host.

The fnOS gateway forwards /app/<prefix>/... without stripping and passes the
browser's Host through. The Strata App front-end hardcodes root-absolute paths
(/api/..., /assets/, url(/fonts/...)) with no base-path support, so the proxy:
  * rewrites the request path of EVERY request on a connection (a real HTTP/1.1
    keep-alive loop — chromium pipelines fonts/icons/APIs over one connection),
  * rewrites paths inside HTML/JS/CSS responses to carry the gateway prefix,
  * streams SSE (/api/events) unbuffered with no read timeouts (the "engine not
    connected" banner appears when the event stream is cut or mis-framed),
  * frames every response exactly (content-length, or verbatim chunked) so the
    browser's connection reuse never crosses responses (the old one-request-per-
    connection design + Connection: close mix produced crossed 404s in chromium).

usage: sock_proxy.py <unix-socket-path> <tcp-port> [prefix]
"""
import os
import re
import signal
import socket
import sys
import threading

SOCK = sys.argv[1]
PORT = int(sys.argv[2])
PREFIX = (sys.argv[3] if len(sys.argv) > 3 else "/app/strata-app").encode()
BACKLOG = 128
CHUNK = 65536
REWRITE_LIMIT = 8 * 1024 * 1024   # max textual body buffered for rewriting
IDLE_TIMEOUT = 120                # browser connection idle between requests
DEBUG = os.environ.get("STRATA_PROXY_DEBUG") == "1"


def dlog(msg):
    if DEBUG:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()


def recv_head(sock, timeout):
    head = b""
    sock.settimeout(timeout)
    while b"\r\n\r\n" not in head:
        try:
            d = sock.recv(4096)
        except socket.timeout:
            return None, None, None
        if not d:
            return None, None, None
        head += d
        if len(head) > 128 * 1024:
            return None, None, None
    header_block, _, rest = head.partition(b"\r\n\r\n")
    lines = header_block.split(b"\r\n")
    return lines[0], lines[1:], rest


def header_map(headers):
    out = {}
    for h in headers:
        k, _, v = h.partition(b":")
        out[k.strip().lower()] = v.strip().lower()
    return out


def rewrite_path(method_line):
    parts = method_line.split(b" ", 2)
    if len(parts) != 3:
        return method_line
    p = parts[1]
    if p == PREFIX or p.startswith(PREFIX + b"/"):
        target = p[len(PREFIX):] or b"/"
    elif p.startswith(b"/app/"):
        tail = p[len(b"/app/"):]
        seg, slash, remainder = tail.partition(b"/")
        target = (b"/" + remainder) if slash else b"/"
    else:
        target = p
    return parts[0] + b" " + target + b" " + parts[2]


JS_PATH_RE = re.compile(rb'(["`])/(api|icons|examples|brand|fonts|assets|site\.webmanifest|apple-touch-icon\.png|favicon[^\s"`]*|v1/chat/completions|v1/models)([/"`?])')
# template-interpolated hosts: `${ju}/api/..` -> literal prefix; `${ju}${c}` -> just ${c}
JS_TPL_REPL = [(b'${ju}/', PREFIX + b'/'), (b'${ju}${', b'${')]
HTML_PATH_RE = re.compile(rb'((?:href|src)=")(/[^"]*)(")')
CSS_URL_RE = re.compile(rb'url\((["\']?)/')


def rewrite_body(ctype, body):
    if ctype.startswith(b"text/html"):
        return HTML_PATH_RE.sub(lambda m: m.group(1) + PREFIX + m.group(2) + m.group(3), body)
    if ctype.startswith((b"application/javascript", b"text/javascript")):
        for a, b2 in JS_TPL_REPL:
            body = body.replace(a, b2)
        return JS_PATH_RE.sub(lambda m: m.group(1) + PREFIX + b"/" + m.group(2) + m.group(3), body)
    if ctype.startswith(b"text/css"):
        return CSS_URL_RE.sub(b"url(" + PREFIX + b"/", body)
    return body


def pump_exact(src, dst, initial=b"", limit=None):
    """copy up to `limit` bytes (None = until EOF)."""
    sent = 0
    try:
        if initial:
            dst.sendall(initial)
            sent += len(initial)
        while limit is None or sent < limit:
            want = CHUNK if limit is None else min(CHUNK, limit - sent)
            d = src.recv(want)
            if not d:
                break
            dst.sendall(d)
            sent += len(d)
    except OSError:
        pass
    return sent


class ChunkReader:
    """incremental chunked-body reader over one socket."""

    def __init__(self, sock, buf=b""):
        self.sock = sock
        self.buf = buf
        self.pos = 0
        self.eof = False

    def _fill(self):
        try:
            d = self.sock.recv(CHUNK)
        except OSError:
            d = b""
        if not d:
            self.eof = True
            return False
        self.buf += d
        return True

    def read_chunk(self):
        """return next chunk payload bytes, or None at end/trailer."""
        while True:
            j = self.buf.find(b"\r\n", self.pos)
            if j < 0:
                if not self._fill():
                    return None
                continue
            try:
                n = int(self.buf[self.pos:j], 16)
            except ValueError:
                return None
            if n == 0:
                self.eof = True
                return None
            need = j + 2 + n
            while len(self.buf) < need + 2:
                if not self._fill():
                    break
            data = self.buf[need - n:need]
            self.pos = need + 2
            if self.pos > 1 << 20:
                self.buf = self.buf[self.pos:]
                self.pos = 0
            return data


def pump_chunked_forward(reader, dst):
    """re-emit chunk framing to dst until source end."""
    try:
        while True:
            data = reader.read_chunk()
            if data is None:
                dst.sendall(b"0\r\n\r\n")
                break
            dst.sendall(b"%x\r\n" % len(data) + data + b"\r\n")
    except OSError:
        pass


def collect_chunked(reader, limit=REWRITE_LIMIT):
    total = b""
    while True:
        data = reader.read_chunk()
        if data is None:
            return total
        total += data
        if len(total) > limit:
            return None


def send_error(conn, code, text):
    body = text.encode()
    try:
        conn.sendall(b"HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     % (code, text.encode(), len(body)) + body)
    except OSError:
        pass


def handle(conn):
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    keep = True
    try:
        while keep:
            line, headers, rest = recv_head(conn, IDLE_TIMEOUT)
            if line is None:
                return  # idle or closed: normal end of the keep-alive loop
            hmap = header_map(headers)
            method = line.split(b" ", 1)[0].upper()
            client_conn = hmap.get(b"connection", b"")
            client_close = client_conn == b"close" or b"http/1.0" in line.lower() and client_conn != b"keep-alive"
            dlog("%s -> %s" % (line[:100], rewrite_path(line).split(b" ")[1:2]))

            out_headers = []
            for h in headers:
                low = h.lower()
                if low.startswith(b"host:"):
                    continue
                # The host rejects non-localhost Origin on /api/ ("Open the local
                # Strata app to use host controls"); the gateway hands over the
                # browser's LAN origin, so it must not reach the host.
                if low.startswith((b"origin:", b"referer:")):
                    continue
                if low.startswith((b"connection:", b"keep-alive:", b"proxy-connection:")):
                    continue
                if low.startswith(b"accept-encoding:"):
                    h = b"Accept-Encoding: identity"
                out_headers.append(h)
            out_headers.append(b"Host: 127.0.0.1:%d" % PORT)
            up_line = rewrite_path(line)

            up = None
            try:
                up = socket.create_connection(("127.0.0.1", PORT), timeout=10)
                up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                head_bytes = b"\r\n".join([up_line] + out_headers) + b"\r\n\r\n"

                te_req = hmap.get(b"transfer-encoding", b"")
                cl_req = hmap.get(b"content-length")
                if te_req == b"chunked":
                    up.sendall(head_bytes)
                    r = ChunkReader(conn, rest)
                    pump_chunked_forward(r, up)
                elif cl_req and method not in (b"GET", b"HEAD", b"OPTIONS"):
                    need = int(cl_req or 0)
                    up.sendall(head_bytes)
                    pump_exact(conn, up, rest, need)
                else:
                    up.sendall(head_bytes + rest)

                # response head (skip 1xx interim responses)
                rline, rheaders, rrest = recv_head(up, 25)
                while rline is not None and len(rline.split(b" ")) >= 2 and rline.split(b" ")[1].startswith(b"1"):
                    rline, rheaders, rrest = recv_head(up, 25)
                if rline is None:
                    send_error(conn, 502, "upstream unavailable")
                    return

                rmap = header_map(rheaders)
                try:
                    status = int(rline.split(b" ", 2)[1])
                except (IndexError, ValueError):
                    status = 0
                ctype = rmap.get(b"content-type", b"").split(b";")[0].strip()
                is_textual = ctype in (b"text/html", b"application/javascript",
                                       b"text/javascript", b"text/css")
                encoded = rmap.get(b"content-encoding", b"") not in (b"", b"identity")
                is_event = ctype == b"text/event-stream"
                te_resp = rmap.get(b"transfer-encoding", b"")
                cl_resp = rmap.get(b"content-length")
                no_body = method == b"HEAD" or status in (204, 304)

                base_hdrs = [h for h in rheaders
                             if not h.lower().startswith(
                                 (b"connection:", b"keep-alive:", b"content-length:",
                                  b"transfer-encoding:", b"date:"))]

                if is_event:
                    # SSE: stream raw until either side dies. Chromium uses a
                    # dedicated connection for EventSource; terminate our loop.
                    up.settimeout(None)
                    conn.settimeout(None)
                    hdrs = base_hdrs + [b"Cache-Control: no-cache", b"X-Accel-Buffering: no",
                                        b"Connection: close"]
                    conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
                    t = threading.Thread(target=pump_exact, args=(up, conn, rrest), daemon=True)
                    t.start()
                    try:  # client disconnect must tear down the stream
                        while True:
                            d = conn.recv(CHUNK)
                            if not d:
                                break
                    except OSError:
                        pass
                    t.join(timeout=2)
                    return

                keep = not client_close
                if no_body:
                    hdrs = base_hdrs + [b"Connection: " + (b"close" if not keep else b"keep-alive")]
                    conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
                elif is_textual and not encoded:
                    # buffered + rewritten; identity guaranteed via our request
                    if te_resp == b"chunked":
                        body = collect_chunked(ChunkReader(up, rrest))
                    else:
                        need = int(cl_resp or 0)
                        body = rrest
                        if len(body) < need:
                            try:
                                while len(body) < need:
                                    d = up.recv(min(CHUNK, need - len(body)))
                                    if not d:
                                        break
                                    body += d
                            except OSError:
                                pass
                    if body is None or len(body) > REWRITE_LIMIT:
                        send_error(conn, 502, "response too large to rewrite")
                        return
                    body2 = rewrite_body(ctype, body)
                    hdrs = base_hdrs + [b"Content-Length: %d" % len(body2),
                                        b"Connection: " + (b"close" if not keep else b"keep-alive")]
                    conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n" + body2)
                elif te_resp == b"chunked":
                    hdrs = base_hdrs + [b"Transfer-Encoding: chunked",
                                        b"Connection: " + (b"close" if not keep else b"keep-alive")]
                    conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
                    up.settimeout(120)
                    conn.settimeout(120)
                    pump_chunked_forward(ChunkReader(up, rrest), conn)
                elif cl_resp is not None:
                    need = int(cl_resp)
                    hdrs = base_hdrs + [b"Content-Length: %d" % need,
                                        b"Connection: " + (b"close" if not keep else b"keep-alive")]
                    conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
                    up.settimeout(120)
                    sent = pump_exact(up, conn, rrest, need)
                    if sent < need:
                        return  # upstream truncated; framing is broken, stop loop
                else:
                    # unframed upstream response: stream to EOF, end the loop
                    up.settimeout(120)
                    conn.settimeout(120)
                    hdrs = base_hdrs + [b"Connection: close"]
                    conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
                    pump_exact(up, conn, rrest)
                    return
            finally:
                if up:
                    try:
                        up.close()
                    except OSError:
                        pass
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    if os.path.exists(SOCK):
        try:
            os.unlink(SOCK)
        except OSError:
            pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o660)
    srv.listen(BACKLOG)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
