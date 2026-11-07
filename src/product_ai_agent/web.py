"""Allowlisted HTTP(S) with DNS pinning and TLS hostname verification.

Uses the standard library so the verified address is the address actually dialled.
Environment proxies, redirects, cookies and JavaScript are not implicit transports.
"""
import http.client
import io
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from .ingestion import source_output
from .storage import AgentError, fail


def canonical(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            fail("ACCESS_DENIED", "Only public HTTP(S) URLs without credentials are permitted.")
        if parts.fragment or "\\" in url or any(ord(c) < 33 for c in url):
            fail("ACCESS_DENIED", "Fragments, whitespace and backslashes are not permitted in URLs.")
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if host.endswith(".") or "%" in host:
            fail("ACCESS_DENIED", "Ambiguous hostname.")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port != (443 if parts.scheme == "https" else 80):
            fail("ACCESS_DENIED", "Nonstandard ports are not permitted.")
        authority = f"[{host}]" if ":" in host else host
        return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError):
        fail("ACCESS_DENIED", "Invalid URL.")


def public_address(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def resolve_public(host, port):
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise AgentError("NETWORK_ERROR", "DNS lookup failed.", True)
    addresses = list(dict.fromkeys(r[4][0] for r in records))
    if not addresses or any(not public_address(ip) for ip in addresses):
        fail("ACCESS_DENIED", "DNS includes a non-public address.")
    return addresses


class DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline, on_close=None):
        super().__init__()
        self.sock, self.deadline = sock, deadline
        self.on_close = on_close

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP deadline reached")
        self.sock.settimeout(remaining)
        return self.sock.recv_into(buffer)

    def close(self):
        if not self.closed and self.on_close:
            self.on_close()
        super().close()


class DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
        self.readers, self.closing = 0, False

    def sendall(self, payload):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP deadline reached")
        self.sock.settimeout(remaining)
        self.sock.sendall(payload)

    def makefile(self, mode):
        self.readers += 1
        return io.BufferedReader(DeadlineReader(self.sock, self.deadline, self.release_reader))

    def release_reader(self):
        self.readers -= 1
        if self.closing and self.readers == 0:
            self.sock.close()

    def settimeout(self, timeout):
        self.sock.settimeout(timeout)

    def close(self):
        self.closing = True
        if self.readers == 0:
            self.sock.close()


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, tls, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address, self.tls = address, tls

    def connect(self):
        deadline = time.monotonic() + self.timeout
        sock = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            peer = sock.getpeername()[0]
            if not public_address(peer) or ipaddress.ip_address(peer) != ipaddress.ip_address(self.address):
                fail("ACCESS_DENIED", "Connected peer does not match the verified public address.")
            if self.tls:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                sock.settimeout(remaining)
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = DeadlineSocket(sock, deadline)
        except BaseException:
            sock.close()
            raise


def request_once(url, address, timeout, max_bytes):
    parts = urlsplit(url)
    port = 443 if parts.scheme == "https" else 80
    connection = PinnedHTTP(parts.hostname, port, address, parts.scheme == "https", timeout)
    start = time.monotonic()
    try:
        connection.request("GET", (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                           headers={"User-Agent": "ProductAIAgent/0.1 (research; public sources only)",
                                    "Accept": "text/html", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        if response.status in {301, 302, 303, 307, 308}:
            return response.status, headers, b""
        if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            fail("UNSUPPORTED_FORMAT", "Compressed HTTP response is unsupported; server ignored Accept-Encoding.")
        if "content-length" in headers:
            try:
                if int(headers["content-length"]) > max_bytes:
                    fail("LIMIT_EXCEEDED", "HTTP content-length exceeds the byte limit.")
            except ValueError:
                fail("PARSE_ERROR", "Invalid HTTP content-length.")
        chunks, size = [], 0
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError()
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_bytes - size + 1))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                fail("LIMIT_EXCEEDED", "HTTP body exceeds the byte limit.")
        return response.status, headers, b"".join(chunks)
    except ssl.SSLError:
        raise AgentError("NETWORK_ERROR", "TLS validation or negotiation failed.", False)
    except (OSError, http.client.HTTPException):
        raise AgentError("NETWORK_ERROR", "HTTP connection failed or timed out.", True)
    finally:
        connection.close()


def fetch_html(url, policy, resolver=resolve_public, transport=request_once):
    requested = canonical(url)
    allowed = {canonical(value) for value in policy["allowed_urls"]}
    current, seen = requested, set()
    for _ in range(policy["max_redirects"] + 1):
        if current not in allowed:
            fail("ACCESS_DENIED", "URL or redirect is not in the project's exact allowlist.")
        if current in seen:
            fail("NETWORK_ERROR", "Redirect loop.")
        seen.add(current)
        parts = urlsplit(current)
        addresses = resolver(parts.hostname, 443 if parts.scheme == "https" else 80)
        if not addresses or any(not public_address(ip) for ip in addresses):
            fail("ACCESS_DENIED", "Non-public resolved address.")
        for attempt in range(policy["max_retries"] + 1):
            try:
                status, headers, body = transport(current, addresses[attempt % len(addresses)],
                                                   policy["timeout_seconds"], policy["max_bytes"])
                break
            except AgentError as exc:
                if not exc.retryable or attempt == policy["max_retries"]:
                    raise
        if status in {301, 302, 303, 307, 308}:
            if not headers.get("location"):
                fail("NETWORK_ERROR", "Redirect without Location.")
            current = canonical(urljoin(current, headers["location"]))
            if parts.scheme == "https" and urlsplit(current).scheme != "https":
                fail("ACCESS_DENIED", "HTTPS downgrade redirect is not permitted.")
            continue
        if status != 200:
            fail("NETWORK_ERROR", f"HTTP status {status}; source was not imported.")
        if "text/html" not in headers.get("content-type", "").lower():
            fail("UNSUPPORTED_FORMAT", "Expected an HTML page.")
        if len(body) > policy["max_bytes"]:
            fail("LIMIT_EXCEEDED", "HTTP body exceeds the byte limit.")
        return body, current
    fail("LIMIT_EXCEEDED", "Too many redirects.")


def ingest_url(store, url):
    from bs4 import BeautifulSoup
    data, final_url = fetch_html(url, store.policy())
    soup = BeautifulSoup(data, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else final_url
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        tag.decompose()
    body = soup.body or soup
    elements = body.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr", "pre", "blockquote"])
    texts = [element.get_text(" ", strip=True) for element in elements]
    texts = list(dict.fromkeys(text for text in texts if text))
    if not texts:
        texts = [body.get_text(" ", strip=True)]
    if len(" ".join(texts).split()) < 12:
        fail("PARSE_ERROR", "Insufficient static HTML text; page may require JavaScript.")
    extracted = {"schema_version": 1, "format": "html", "tables": [],
                 "blocks": [{"block_id": f"html-{i:06}", "text": text} for i, text in enumerate(texts, 1)]}
    return source_output(store, data, extracted, ".html", title, {"type": "url", "value": canonical(url)},
                         "public", ["Static HTML only; public page statements are not independent evidence of effectiveness."], final_url)
