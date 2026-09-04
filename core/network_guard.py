"""SSRF-защита сетевых инструментов (P0 §4, Q05).

``web_fetch`` (и любой будущий сетевой инструмент) не должен ходить на
внутренние/зарезервированные адреса хоста пользователя или модели:

    * loopback: 127.0.0.0/8, ::1
    * private: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
    * link-local: 169.254.0.0/16 (вкл. облачные metadata 169.254.169.254)
    * неявный/неправильный: 0.0.0.0, пустой хост, не http/https схема,
      ``file://``, ``ftp://``, ``gopher://``, не-IP-литерал с опасным именем
      (``localhost``), а также «дефолтные» имена метаданных облаков.

Метод: разрешаем имя хоста в IP, проверяем все ответы DNS и подключаемся
к уже проверенному IP, сохраняя исходное имя для HTTP Host и TLS SNI.
Редиректы проверяются и пинятся отдельно на каждом переходе.

Публичный API:
    ``is_ssrf_blocked(url) -> bool`` — True, если URL заблокирован.
    ``assert_safe_url(url)`` — бросает ``ValueError`` при блокировке.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from core.utils.logger import get_logger

__all__ = [
    "is_ssrf_blocked", "assert_safe_url", "safe_redirect_url", "safe_urlopen",
    "safe_http_request", "safe_http_get", "safe_http_post", "SSRFBlocked",
]

log = get_logger(__name__)

#: Разрешённые схемы.
_ALLOWED_SCHEMES = {"http", "https"}

#: Имена/хосты, которые всегда блокируем явно (до резолва).
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "0.0.0.0",
    "0",
    "[::1]",
    "[::]",
    "metadata.google.internal",
    "metadata",
}

#: Облачные metadata endpoints (частая SSRF-цель).
_CLOUD_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),   # AWS/GCP/Azure/OCI
    ipaddress.ip_address("fd00:ec2::254"),       # AWS IPv6 metadata
}


class SSRFBlocked(ValueError):
    """URL заблокирован SSRF-защитой."""


def _ip_in_blocked_network(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True, если IP попадает в зарезервированную/внутреннюю сеть."""
    # IPv4-mapped IPv6 → сводим к IPv4.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _CLOUD_METADATA_IPS:
        return True
    # is_private покрывает 10/8, 172.16/12, 192.168/16, 127/8, 169.254/16,
    # 0.0.0.0/8, IPv4-link-local, ULA/IPv6 loopback и т.д.
    if ip.is_private:
        return True
    if ip.is_loopback:
        return True
    if ip.is_link_local:
        return True
    if ip.is_reserved:
        return True
    if ip.is_multicast:
        return True
    if ip.is_unspecified:  # 0.0.0.0 / ::
        return True
    return False


def is_ssrf_blocked(url: str) -> bool:
    """True, если URL ведёт на внутренний/зарезервированный адрес (SSRF)."""
    if not url or not url.strip():
        return True
    raw = url.strip()
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()

    if scheme not in _ALLOWED_SCHEMES:
        # file://, ftp://, gopher://, неизвестная/пустая схема — блок.
        return True
    if parsed.username or parsed.password:
        return True
    if parsed.port is not None and parsed.port in {21, 22, 23, 25, 110, 143, 445, 3389, 6379, 9200, 11211}:
        return True

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES:
        return True

    # Снимаем квадратные скобки у IPv6-литерала.
    bare = host.strip("[]")
    # Прямой IP-литерал — проверяем сразу, без резолва.
    try:
        direct = ipaddress.ip_address(bare)
        return _ip_in_blocked_network(direct)
    except ValueError:
        pass  # не IP-литерал → резолвим ниже

    # Резолвим имя в IP (ловим DNS-rebinding/внутреннее имя).
    try:
        infos = socket.getaddrinfo(bare, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        # Не смогли разрешить — трактуем как подозрительный (блок).
        return True

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if _ip_in_blocked_network(ip):
            return True
    return False



def _validated_ip_for_host(host: str, port: int | None) -> str:
    """Возвращает проверенный IP, к которому будет выполнено подключение."""
    parsed = urlparse(f"//{host}")
    hostname = (parsed.hostname or host).strip("[]").lower()
    target_port = int(port or parsed.port or 443)
    try:
        infos = socket.getaddrinfo(hostname, target_port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise SSRFBlocked(f"DNS не разрешён SSRF-защитой: {hostname}") from exc
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_in_blocked_network(ip):
            raise SSRFBlocked(f"DNS вернул внутренний IP для {hostname}")
        return addr
    raise SSRFBlocked(f"DNS не вернул TCP-адрес для {hostname}")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection, использующий IP, проверенный до connect()."""

    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
        super().__init__(host, port=port, timeout=timeout, **kwargs)
        self._validated_ip = _validated_ip_for_host(host, port or 80)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection с проверенным IP и исходным TLS server_hostname."""

    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
        super().__init__(host, port=port, timeout=timeout, **kwargs)
        self._validated_ip = _validated_ip_for_host(host, port or 443)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):  # type: ignore[override]
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):  # type: ignore[override]
        return self.do_open(
            _PinnedHTTPSConnection,
            req,
            context=self._context,
            check_hostname=self._check_hostname,
        )


def assert_safe_url(url: str) -> str:
    """Бросает ``SSRFBlocked``, если URL заблокирован; иначе возвращает URL.

    Возвращает нормализованный (strip) URL, чтобы вызывающий мог его использовать.
    """
    if is_ssrf_blocked(url):
        log.warning("SSRF-защита заблокировала URL: %s", (url or "").strip())
        raise SSRFBlocked(f"URL заблокирован SSRF-защитой: {(url or '').strip()}")
    return url.strip()


def safe_redirect_url(base_url: str, location: str) -> str:
    """Resolve and validate one redirect hop; every hop is re-resolved."""
    if not location or not str(location).strip():
        raise SSRFBlocked("Пустой redirect")
    target = urljoin(base_url, str(location).strip())
    return assert_safe_url(target)


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Revalidate every urllib redirect instead of trusting the first host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        safe_redirect_url(req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(request: object, *, timeout: float) -> Any:
    """Open an HTTP(S) request after validation and DNS pinning per hop."""
    url = getattr(request, "full_url", request)
    assert_safe_url(str(url))
    opener = urllib.request.build_opener(
        _ValidatedRedirectHandler(), _PinnedHTTPHandler(), _PinnedHTTPSHandler()
    )
    return opener.open(request, timeout=timeout)


#: Редиректы, которые requests обычно отрабатывает сам — здесь мы ведём их вручную.
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def safe_http_request(method: str, url: str, *, timeout: Any = 15, max_hops: int = 5, **kwargs: Any):
    """requests-вызов с SSRF-гвардом на каждом hop (ДЫРА 4).

    ``requests`` не даёт закрепить проверку IP без переписывания transport'а,
    поэтому минимум по контракту безопасности: ``assert_safe_url`` до запроса,
    ``allow_redirects=False`` и ручной follow с повторной проверкой каждого
    Location (включая повторный DNS-резолв) и лимитом hops.
    """
    method = str(method).upper()
    current = assert_safe_url(str(url))
    data = kwargs.pop("data", None)
    json_body = kwargs.pop("json", None)
    for _hop in range(max_hops + 1):
        assert_safe_url(current)
        resp = requests.request(
            method, current, data=data, json=json_body,
            timeout=timeout, allow_redirects=False, **kwargs,
        )
        if resp.status_code not in _REDIRECT_STATUSES:
            return resp
        location = resp.headers.get("Location", "")
        if not location.strip():
            return resp
        current = safe_redirect_url(current, location)
        # 301/302/303 на POST semantics: браузеры и requests переключают
        # метод на GET и сбрасывают тело — повторяем то же поведение.
        if method != "GET" and resp.status_code in (301, 302, 303):
            method, data, json_body = "GET", None, None
    raise SSRFBlocked(f"Слишком много редиректов (> {max_hops}) при запросе {url}")


def safe_http_get(url: str, **kwargs: Any):
    """GET с тем же SSRF-гвардом, что и ``safe_urlopen`` (ДЫРА 4)."""
    return safe_http_request("GET", url, **kwargs)


def safe_http_post(url: str, **kwargs: Any):
    """POST с тем же SSRF-гвардом, что и ``safe_urlopen`` (ДЫРА 4)."""
    return safe_http_request("POST", url, **kwargs)
