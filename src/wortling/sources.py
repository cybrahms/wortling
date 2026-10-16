"""Bounded public-web fetching, with address checks at the actual connection boundary."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from html.parser import HTMLParser

import httpcore
import httpx
from httpcore._backends.sync import SyncBackend

MAX_SOURCE_BYTES = 1_048_576


class UnsafeSource(ValueError):
    pass


def checked_url(value: str) -> httpx.URL:
    url = httpx.URL(value)
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise UnsafeSource("Source must be an HTTP(S) URL without embedded credentials")
    if url.port not in {None, 80, 443}:
        raise UnsafeSource("Source URL must use a standard web port")
    try:
        address = ipaddress.ip_address(url.host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise UnsafeSource("Source URL is not a public address")
    return url


class PublicNetworkBackend(SyncBackend):
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = list(
            dict.fromkeys(
                info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            )
        )
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise UnsafeSource("Source hostname resolved to a non-public address")
        # Pin the connection to the checked address. TLS SNI and certificate checks
        # still use the original httpcore origin hostname, not this numeric address.
        return super().connect_tcp(addresses[0], port, timeout, local_address, socket_options)


def public_transport() -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport(trust_env=False, retries=0)
    # Audited against httpx 0.28.1/httpcore 1.0.9, pinned by uv.lock.
    transport._pool.close()
    transport._pool = httpcore.ConnectionPool(
        network_backend=PublicNetworkBackend(), max_connections=2, max_keepalive_connections=0
    )
    return transport


class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "header", "footer", "noscript", "svg"}:
            self.ignored.append(tag)
        if not self.ignored and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()
        if not self.ignored and tag in {"p", "div", "li", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


@dataclass
class FetchedSource:
    final_url: str
    content_type: str
    body: bytes
    text: str


def fetch_source(url: str, *, transport=None) -> FetchedSource:
    current = checked_url(url)
    with httpx.Client(
        transport=transport or public_transport(),
        follow_redirects=False,
        trust_env=False,
        timeout=10,
        headers={
            "User-Agent": "Wortling/0.1 (personal language-learning capture)",
            "Accept": "text/html, text/plain;q=0.9",
        },
    ) as client:
        for _ in range(5):
            current = checked_url(str(current))
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not response.headers.get("location"):
                        raise ValueError("Source redirect has no destination")
                    current = checked_url(str(current.join(response.headers["location"])))
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                    raise ValueError(
                        "Source must be an HTML page or plain text; share an image instead"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_SOURCE_BYTES:
                        raise ValueError("Source exceeds the 1 MiB document limit")
                text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                if content_type != "text/plain":
                    parser = ArticleText()
                    parser.feed(text)
                    text = parser.text()
                text = text[:100_000]
                if not text.strip():
                    raise ValueError(
                        "Source contains no readable text; share selected text or an image"
                    )
                return FetchedSource(str(current), content_type, bytes(body), text)
    raise ValueError("Source exceeded the redirect limit")
