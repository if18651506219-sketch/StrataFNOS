#!/usr/bin/env python3
"""Unix-socket -> TCP proxy for the fnOS unified gateway, serving the Strata App host.

The fnOS gateway forwards /app/<prefix>/... without stripping and passes the
browser's Host through. The Strata App front-end hardcodes root-absolute paths
(/api/..., /assets/, url(/fonts/...)) with no base-path support, so the proxy:
  * rewrites the request path of EVERY request on a connection (a real HTTP/1.1
    keep-alive loop — chromium pipelines fonts/icons/APIs over one connection),
  * rewrites paths inside HTML/JS/CSS responses to carry the gateway prefix and
    injects upstream's official `window.__STRATA_HOST__` API-base hook,
  * does NOT inject X-Forwarded-Prefix (Strata's host does not read it); instead
    it drops client-supplied Forwarded/X-Forwarded-* so they cannot be spoofed,
  * forwards the COMPLETE request body (content-length or chunked), including
    bodies split across writes larger than one recv(), and keeps bytes belonging
    to a pipelined next request for the next loop iteration,
  * answers `Expect: 100-continue` itself (the old code forwarded Expect, then
    waited for a body the client would not send until it saw a 100 -> deadlock),
  * tunnels WebSocket upgrades verbatim as an unbuffered bidirectional stream,
  * frames every response exactly: content-length, OR verbatim chunked WITH its
    `Transfer-Encoding: chunked` header. SSE is chunked-framed too — the old
    code stripped the header but forwarded raw chunk frames, so the browser saw
    size lines as event data and events crossing a chunk boundary were lost,
  * applies timeouts per phase only: bounded waits for request/response HEADs,
    then BLOCKING (no timeout) for all streaming phases (SSE/WS/downloads).

fnOS-port 0.2.10 regression fix: the P0 findings from the black-box proxy PoC
(SSE framing, body carry, Expect 100) plus prefix stripping / forged headers /
WebSocket / timeout hardening. See tools/verify_proxy.py for the 12-scenario
regression suite.

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
IDLE_TIMEOUT = 300                # browser connection idle between requests
REQ_BODY_TIMEOUT = 120            # client must finish sending a declared body
RESP_HEAD_TIMEOUT = 120           # upstream must produce response headers; body phase is unbounded
DEBUG = os.environ.get("STRATA_PROXY_DEBUG") == "1"


def dlog(msg):
    if DEBUG:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()


def recv_head(sock, timeout, initial=b""):
    """Read one header block. `initial` carries bytes already read (e.g. a
    pipelined next request left over from a previous request body)."""
    head = initial
    sock.settimeout(timeout)
    while b"\r\n\r\n" not in head:
        try:
            d = sock.recv(4096)
        except OSError:
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
    target = parts[1]
    # 迭代剥本 app 的前缀：注入 __STRATA_HOST__ 后，JS 里已重写过的字面量再
    # 叠加 base 会产生双前缀（/app/strata-app/app/strata-app/...）。
    # 只剥自己的 PREFIX —— 旧实现还会剥任意 /app/<段>/，网关偶尔带上别的
    # app 前缀时会把 URL 悄悄改错，且完全没有必要（网关只把本 app 的请求转来）。
    while target == PREFIX or target.startswith(PREFIX + b"/"):
        target = target[len(PREFIX):] or b"/"
    return parts[0] + b" " + target + b" " + parts[2]


JS_PATH_RE = re.compile(rb'(["`])/(api|icons|examples|brand|fonts|assets|site\.webmanifest|apple-touch-icon\.png|favicon[^\s"`]*|v1/chat/completions|v1/models)([/"`?])')
# template-interpolated hosts: `${ju}/api/..` -> literal prefix; `${ju}${c}` -> just ${c}
JS_TPL_REPL = [(b'${ju}/', PREFIX + b'/'), (b'${ju}${', b'${')]
HTML_PATH_RE = re.compile(rb'((?:href|src)=")(/[^"]*)(")')
CSS_URL_RE = re.compile(rb'url\((["\']?)/')


def rewrite_body(ctype, body):
    if ctype.startswith(b"text/html"):
        body = HTML_PATH_RE.sub(lambda m: m.group(1) + PREFIX + m.group(2) + m.group(3), body)
        # 0.2.3 起前端用 globalThis.__STRATA_HOST__ 作为 API/SSE base（EventSource
        # `${Qu}/api/events` 等绕过字面量重写，网关下必 404 → "未连接"横幅）。
        # 注入官方钩子即可，且不依赖压缩变量名，对上游改名免疫。
        hook = b'<script>window.__STRATA_HOST__="' + PREFIX + b'";</script>'
        i = body.lower().find(b"<head")
        if i != -1:
            gt = body.find(b">", i)
            if gt != -1:
                body = body[:gt + 1] + hook + body[gt + 1:]
        else:
            body = hook + body
        return body
    if ctype.startswith((b"application/javascript", b"text/javascript")):
        for a, b2 in JS_TPL_REPL:
            body = body.replace(a, b2)
        return JS_PATH_RE.sub(lambda m: m.group(1) + PREFIX + b"/" + m.group(2) + m.group(3), body)
    if ctype.startswith(b"text/css"):
        return CSS_URL_RE.sub(b"url(" + PREFIX + b"/", body)
    return body


def pump_exact(src, dst, initial=b"", limit=None):
    """copy up to `limit` bytes (None = until EOF). `initial` is clipped to the
    limit: bytes beyond a declared body belong to the next pipelined request."""
    sent = 0
    try:
        if initial:
            if limit is not None:
                initial = initial[:max(0, limit - sent)]
            if initial:
                dst.sendall(initial)
                sent += len(initial)
        while limit is None or sent < limit:
            want = CHUNK if limit is None else min(CHUNK, limit - sent)
            d = src.recv(want)
            if not d:
                break
            if limit is not None:
                d = d[:limit - sent]
            dst.sendall(d)
            sent += len(d)
    except OSError:
        pass
    return sent


def pump_bidir(a, b, initial_for_b=b""):
    """raw tunnel for WebSocket upgrades: copy a->b and b->a until either
    direction drains, with no read timeouts (WS frames can be minutes apart)."""
    for s in (a, b):
        try:
            s.settimeout(None)
        except OSError:
            pass

    def copy(src, dst, initial=b""):
        try:
            if initial:
                dst.sendall(initial)
            while True:
                d = src.recv(CHUNK)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t1 = threading.Thread(target=copy, args=(a, b, initial_for_b))
    t2 = threading.Thread(target=copy, args=(b, a))
    t1.start()
    t2.start()
    t1.join()
    t2.join()


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
                # chunk extensions (`5;foo=bar`) are legal HTTP/1.1
                n = int(self.buf[self.pos:j].split(b";", 1)[0], 16)
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


def _pump_chunked_request(conn, up, initial):
    """Forward a chunked REQUEST body verbatim (size lines + data + CRLF),
    stopping after the terminating 0-chunk / trailers. Return leftover bytes
    (a pipelined next request) for the next loop iteration."""
    buf = initial
    try:
        while True:
            while b"\r\n" not in buf:
                d = conn.recv(4096)
                if not d:
                    return b""
                buf += d
            size_line, _, rest = buf.partition(b"\r\n")
            try:
                n = int(size_line.split(b";", 1)[0], 16)
            except ValueError:
                up.sendall(size_line + b"\r\n")
                return rest
            up.sendall(size_line + b"\r\n")
            buf = rest
            if n == 0:
                # No data follows the 0-chunk: forward trailer lines verbatim
                # until the terminating empty line. Do NOT block waiting for
                # another \r\n\r\n here — that empty line is usually already
                # in buf and waiting would deadlock the client.
                while True:
                    idx = buf.find(b"\r\n")
                    if idx >= 0:
                        up.sendall(buf[:idx + 2])
                        buf = buf[idx + 2:]
                        if idx == 0:
                            return buf
                        continue
                    d = conn.recv(4096)
                    if not d:
                        return b""
                    buf += d
            need = n + 2                       # chunk data + trailing CRLF
            while len(buf) < need:
                d = conn.recv(min(CHUNK, need - len(buf)))
                if not d:
                    return b""
                buf += d
            up.sendall(buf[:need])
            buf = buf[need:]
    except OSError:
        return b""


def forward_request_body(conn, up, rest, hmap):
    """Forward the COMPLETE request body. Bytes beyond the declared body belong
    to a pipelined next request and are returned for the next loop iteration."""
    try:
        conn.settimeout(REQ_BODY_TIMEOUT)
        if b"chunked" in hmap.get(b"transfer-encoding", b""):
            return _pump_chunked_request(conn, up, rest)
        try:
            n = int(hmap.get(b"content-length", b"0"))
        except ValueError:
            return rest
        if n <= 0:
            return rest
        take = min(n, len(rest))
        if take:
            up.sendall(rest[:take])
        remaining = n - take
        while remaining > 0:
            d = conn.recv(min(CHUNK, remaining))
            if not d:
                break
            up.sendall(d)
            remaining -= len(d)
        return rest[take:]
    except OSError:
        return b""


def forward_http(conn, up, client_close=False, method=b"GET", sent_any=False):
    """Forward one HTTP response from up to conn; return True to keep the client
    connection alive. Textual responses are buffered and rewritten; everything
    else (including SSE) is streamed with its ORIGINAL framing intact."""
    rline, rheaders, rrest = recv_head(up, RESP_HEAD_TIMEOUT)
    if rline is None:
        if not sent_any:
            send_error(conn, 504, "Gateway Timeout")
        return False
    parts = rline.split(b" ", 2)
    is_1xx = len(parts) > 1 and parts[1].isdigit() and 100 <= int(parts[1]) < 200
    if is_1xx:
        conn.sendall(b"\r\n".join([rline] + rheaders) + b"\r\n\r\n")
        return forward_http(conn, up, client_close, method, True)
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        status = 0

    # Headers are in: streaming (SSE, chunked, long-poll, file download) must
    # never inherit a socket timeout. Clear it on BOTH ends before pumping.
    try:
        up.settimeout(None)
        conn.settimeout(None)
    except OSError:
        pass

    rmap = header_map(rheaders)
    ctype = rmap.get(b"content-type", b"").split(b";")[0].strip()
    is_textual = ctype in (b"text/html", b"application/javascript",
                           b"text/javascript", b"text/css")
    encoded = rmap.get(b"content-encoding", b"") not in (b"", b"identity")
    te_chunked = b"chunked" in rmap.get(b"transfer-encoding", b"")
    cl_resp = rmap.get(b"content-length")
    no_body = method == b"HEAD" or status in (204, 304)

    keep = not client_close
    conn_hdr = b"Connection: " + (b"keep-alive" if keep else b"close")
    base_hdrs = [h for h in rheaders
                 if not h.lower().startswith(
                     (b"connection:", b"keep-alive:", b"content-length:",
                      b"transfer-encoding:", b"date:"))]

    if no_body:
        hdrs = base_hdrs + [conn_hdr]
        conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
        return keep

    if is_textual and not encoded:
        # buffered + rewritten; identity guaranteed via our request
        if te_chunked:
            body = collect_chunked(ChunkReader(up, rrest))
        else:
            try:
                need = int(cl_resp or 0)
            except ValueError:
                need = 0
            body = rrest[:need]
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
            return False
        body2 = rewrite_body(ctype, body)
        hdrs = base_hdrs + [b"Content-Length: %d" % len(body2), conn_hdr]
        conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n" + body2)
        return keep

    if te_chunked:
        # SSE lands here: keep Transfer-Encoding so the browser decodes chunk
        # frames itself, instead of seeing `d\r\n` as event payload.
        hdrs = base_hdrs + [b"Transfer-Encoding: chunked",
                            b"Cache-Control: no-cache", b"X-Accel-Buffering: no",
                            conn_hdr]
        conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
        pump_chunked_forward(ChunkReader(up, rrest), conn)
        return keep

    if cl_resp is not None:
        try:
            need = int(cl_resp)
        except ValueError:
            need = 0
        hdrs = base_hdrs + [b"Content-Length: %d" % need, conn_hdr]
        conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
        sent = pump_exact(up, conn, rrest, need)
        return keep if sent >= need else False

    # unframed upstream response: stream to EOF, end the loop
    hdrs = base_hdrs + [b"Connection: close"]
    conn.sendall(b"\r\n".join([rline] + hdrs) + b"\r\n\r\n")
    pump_exact(up, conn, rrest)
    return False


def handle(conn):
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    pending = b""
    try:
        while True:
            line, headers, rest = recv_head(conn, IDLE_TIMEOUT, pending)
            pending = b""
            if line is None:
                return  # idle or closed: normal end of the keep-alive loop
            hmap = header_map(headers)
            method = line.split(b" ", 1)[0].upper()
            client_conn = hmap.get(b"connection", b"")
            client_close = b"close" in client_conn or (b"http/1.0" in line.lower() and client_conn != b"keep-alive")
            expect_continue = b"100-continue" in hmap.get(b"expect", b"")
            is_upgrade = (b"websocket" in hmap.get(b"upgrade", b"")
                          or b"websocket" in hmap.get(b"connection", b""))
            dlog("%s%s -> %s" % (line[:100], " [WS]" if is_upgrade else "",
                                 rewrite_path(line).split(b" ")[1:2]))

            out_headers = []
            for h in headers:
                low = h.lower()
                if low.startswith(b"host:"):
                    continue
                # The proxy is the trust boundary: client-supplied forwarding
                # headers must never reach the host (and Strata doesn't read
                # X-Forwarded-Prefix anyway — the SPA base comes from our hook).
                if low.startswith((b"x-forwarded-", b"forwarded:")):
                    continue
                # The host rejects non-localhost Origin on /api/; the gateway
                # hands over the browser's LAN origin, so it must not reach it.
                if low.startswith((b"origin:", b"referer:")):
                    continue
                if expect_continue and low.startswith(b"expect:"):
                    continue
                if is_upgrade:
                    # Upgrade/Connection must survive the handshake verbatim.
                    out_headers.append(h)
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
                up.sendall(head_bytes)
                if expect_continue:
                    # Release the client's body ourselves. The upstream never
                    # sees Expect, so it cannot emit a competing 100; the old
                    # code forwarded Expect and then waited for a body that the
                    # client would only send after seeing a 100 -> deadlock.
                    conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                pending = forward_request_body(conn, up, rest, hmap)

                if is_upgrade:
                    rline, rheaders, rrest = recv_head(up, RESP_HEAD_TIMEOUT)
                    if rline is None:
                        send_error(conn, 504, "Gateway Timeout")
                        return
                    conn.sendall(b"\r\n".join([rline] + rheaders) + b"\r\n\r\n" + rrest)
                    pump_bidir(conn, up)
                    return  # connection fully consumed by the tunnel

                keep = forward_http(conn, up, client_close, method)
            except OSError as e:
                dlog("upstream error: %s" % e)
                send_error(conn, 502, "Bad Gateway")
                return
            finally:
                if up is not None:
                    try:
                        up.close()
                    except OSError:
                        pass
            if not keep:
                return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    try:
        if os.path.exists(SOCK):
            os.unlink(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    try:
        os.chmod(SOCK, 0o660)
    except OSError:
        pass
    srv.listen(BACKLOG)

    def stop(*_):
        try:
            srv.close()
        except OSError:
            pass
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
