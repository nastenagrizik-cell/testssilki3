from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeSurveyUrl(ValueError):
    pass


def validate_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeSurveyUrl("Нужна полная ссылка, начинающаяся с http:// или https://")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"}:
        raise UnsafeSurveyUrl("Локальные адреса не поддерживаются")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise UnsafeSurveyUrl("Не удалось определить адрес платформы") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise UnsafeSurveyUrl("Ссылки на внутренние и локальные адреса запрещены")
    return url.strip()
