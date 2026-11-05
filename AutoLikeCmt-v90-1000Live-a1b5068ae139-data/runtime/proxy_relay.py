"""
Local proxy relay — forward auth cho undetected_chromedriver.

Ly do ton tai:
  undetected_chromedriver KHONG nap duoc Chrome extension (uc chan de chong
  phat hien), nen cach cu (extension xu ly proxy auth) khong hoat dong — Chrome
  bypass proxy va dung IP goc. selenium-wire thi hong voi Python 3.14.

Giai phap:
  Chay mot local proxy khong-auth tai 127.0.0.1:<port_ngau_nhien>. Chrome tro
  --proxy-server vao local nay (flag native, uc chap nhan). Local relay nhan
  request roi forward len upstream proxy (co auth) bang cach TU chen header
  Proxy-Authorization: Basic <base64>. Ho tro ca CONNECT (HTTPS tunnel) lan
  HTTP thuong.

Moi ProxyRelay chay tren 1 thread daemon, dung duoc da luong (moi session mot
relay rieng, hoac dung chung mot relay cho cung upstream).
"""
import base64
import select
import socket
import struct
import threading


class ProxyRelay:
    """Local relay forward request len upstream proxy co Basic auth."""

    def __init__(self, up_host: str, up_port: int, user: str, password: str):
        self.up_host = up_host
        self.up_port = int(up_port)
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._srv = None
        self.port = None
        self._closed = False
        self._connections = set()
        self._connections_lock = threading.Lock()

    # ── Vong doi ────────────────────────────────────────────────────────────
    def start(self) -> int:
        """Mo listen socket tren cong ngau nhien, tra ve port. Chay accept loop."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(100)
        self._srv = srv
        self.port = srv.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self.port

    def close(self):
        self._closed = True
        with self._connections_lock:
            connections = list(self._connections)
        for connection in connections:
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: connection.close()
            except OSError: pass
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass
            self._srv = None

    # ── Noi bo ──────────────────────────────────────────────────────────────
    def _accept_loop(self):
        while not self._closed:
            try:
                client, _ = self._srv.accept()
            except Exception:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        """Doc request dau tu Chrome, mo ket noi upstream, chen auth, tunnel."""
        up = None
        with self._connections_lock:
            if self._closed:
                client.close()
                return
            self._connections.add(client)
        try:
            client.settimeout(15)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = client.recv(4096)
                if not chunk:
                    return
                req += chunk
                if len(req) > 65536:  # chan request header qua lon
                    return

            first_line = req.split(b"\r\n", 1)[0].decode(errors="ignore")
            parts = first_line.split(" ")
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]

            up = socket.create_connection((self.up_host, self.up_port), timeout=15)
            with self._connections_lock:
                if self._closed:
                    return
                self._connections.add(up)

            if method.upper() == "CONNECT":
                # HTTPS tunnel: gui CONNECT co auth toi upstream
                up.sendall(
                    f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                    f"Proxy-Authorization: Basic {self._auth}\r\n"
                    f"Proxy-Connection: Keep-Alive\r\n\r\n".encode()
                )
                resp = b""
                while b"\r\n\r\n" not in resp:
                    c = up.recv(4096)
                    if not c:
                        break
                    resp += c
                # Kiem tra upstream chap nhan tunnel (200)
                if b" 200 " in resp.split(b"\r\n", 1)[0]:
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    client.sendall(resp)  # tra loi upstream (vd 407) ve Chrome
                    up.close()
                    return
            else:
                # HTTP thuong: chen header auth vao ngay sau dong dau roi forward
                injected = req.replace(
                    b"\r\n",
                    f"\r\nProxy-Authorization: Basic {self._auth}\r\n".encode(),
                    1,
                )
                up.sendall(injected)

            self._pipe(client, up)
        except Exception:
            try:
                client.close()
            except Exception:
                pass

        finally:
            for connection in (client, up):
                if connection is None: continue
                with self._connections_lock:
                    self._connections.discard(connection)
                try: connection.close()
                except OSError: pass

    def _pipe(self, a: socket.socket, b: socket.socket):
        """Chuyen du lieu 2 chieu giua Chrome (a) va upstream (b) den khi dong."""
        _pipe_sockets(a, b, stopped=lambda: self._closed)


def _pipe_sockets(a: socket.socket, b: socket.socket, stopped=lambda: False):
    """Chuyen du lieu 2 chieu giua 2 socket den khi mot ben dong."""
    try:
        while not stopped():
            r, _, _ = select.select([a, b], [], [], 30)
            if not r:
                break
            for s in r:
                other = b if s is a else a
                data = s.recv(65536)
                if not data:
                    return
                other.sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


class Socks5Relay:
    """Local relay nhan HTTP/CONNECT tu Chrome, ket noi upstream qua SOCKS5.

    Ly do: Chrome ho tro --proxy-server=socks5://host:port nhung KHONG ho tro
    xac thuc user/pass cho SOCKS5. Nen voi SOCKS5 co auth, ta cho Chrome tro
    vao local HTTP relay nay; relay tu bat tay SOCKS5 (RFC 1928 + auth RFC 1929)
    len upstream, roi tunnel du lieu.
    """

    def __init__(self, up_host: str, up_port: int, user: str = "", password: str = ""):
        self.up_host = up_host
        self.up_port = int(up_port)
        self.user = user or ""
        self.password = password or ""
        self._srv = None
        self.port = None
        self._closed = False

    def start(self) -> int:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(100)
        self._srv = srv
        self.port = srv.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self.port

    def close(self):
        self._closed = True
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass
            self._srv = None

    def _accept_loop(self):
        while not self._closed:
            try:
                client, _ = self._srv.accept()
            except Exception:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        try:
            # Doc request dau tu Chrome de biet dich (host:port)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = client.recv(4096)
                if not chunk:
                    return
                req += chunk
                if len(req) > 65536:
                    return

            first_line = req.split(b"\r\n", 1)[0].decode(errors="ignore")
            parts = first_line.split(" ")
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]

            if method.upper() == "CONNECT":
                dst_host, dst_port = self._split_hostport(target, default_port=443)
            else:
                # HTTP thuong: lay host tu header Host, port mac dinh 80
                host_hdr = self._header(req, b"host")
                dst_host, dst_port = self._split_hostport(host_hdr, default_port=80)

            up = self._socks5_connect(dst_host, dst_port)
            if up is None:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return

            if method.upper() == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                up.sendall(req)   # forward nguyen request HTTP da doc

            _pipe_sockets(client, up)
        except Exception:
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _header(req: bytes, name: bytes) -> str:
        for line in req.split(b"\r\n")[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                if k.strip().lower() == name:
                    return v.strip().decode(errors="ignore")
        return ""

    @staticmethod
    def _split_hostport(s: str, default_port: int):
        s = s.strip()
        if s.startswith("http://"):
            s = s[7:].split("/", 1)[0]
        if ":" in s:
            host, port = s.rsplit(":", 1)
            try:
                return host, int(port)
            except ValueError:
                return host, default_port
        return s, default_port

    def _socks5_connect(self, dst_host: str, dst_port: int):
        """Bat tay SOCKS5 len upstream, tra ve socket da ket noi toi dst, hoac None."""
        try:
            up = socket.create_connection((self.up_host, self.up_port), timeout=15)
            up.settimeout(15)

            # 1) Greeting: chao cac phuong thuc auth (0=none, 2=user/pass)
            if self.user and self.password:
                up.sendall(b"\x05\x02\x00\x02")
            else:
                up.sendall(b"\x05\x01\x00")
            ver, method = up.recv(2)
            if ver != 0x05:
                up.close(); return None

            # 2) Auth user/pass neu upstream yeu cau (RFC 1929)
            if method == 0x02:
                u = self.user.encode(); p = self.password.encode()
                up.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                astat = up.recv(2)
                if len(astat) < 2 or astat[1] != 0x00:
                    up.close(); return None
            elif method == 0xFF:
                up.close(); return None   # upstream tu choi moi phuong thuc

            # 3) CONNECT toi dich (dung ATYP=3 domain de proxy tu resolve DNS)
            host_b = dst_host.encode()
            req = (b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                   + struct.pack(">H", dst_port))
            up.sendall(req)
            resp = up.recv(4)
            if len(resp) < 2 or resp[1] != 0x00:
                up.close(); return None
            # Doc not phan BND.ADDR + BND.PORT de sach buffer
            atyp = resp[3]
            if atyp == 0x01:
                up.recv(4 + 2)
            elif atyp == 0x03:
                ln = up.recv(1)[0]
                up.recv(ln + 2)
            elif atyp == 0x04:
                up.recv(16 + 2)
            up.settimeout(None)
            return up
        except Exception:
            return None
