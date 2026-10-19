import socket
import ipaddress
from urllib.parse import urlparse

class SecurityError(Exception):
    pass

def is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        return False

def assert_safe_url(url: str, allow_private_network: bool = False) -> None:
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ("http", "https"):
        raise SecurityError(f"Unsupported scheme: {parsed.scheme}")

    if parsed.username or parsed.password:
        raise SecurityError("URL userinfo is not allowed")

    host = parsed.hostname
    if not host:
        raise SecurityError("Invalid hostname")

    # Cloud metadata endpoint string match
    if host == "169.254.169.254":
        if not allow_private_network:
            raise SecurityError("Cloud metadata endpoints are blocked")

    # Resolve IP and check
    if not allow_private_network:
        try:
            ip = socket.gethostbyname(host)
            if is_private_ip(ip):
                raise SecurityError(f"Private or local network access is blocked: {ip}")
        except socket.gaierror:
            # If DNS fails, we might just pass or raise, but for URL classifier, it's safer to just let it fail at probe time
            # or raise if we strictly want resolvable hosts. Let's not raise here for unresolvable to allow offline testing
            # of valid domains, but if it looks like an IP, check it directly.
            if is_private_ip(host):
                raise SecurityError(f"Private or local network access is blocked: {host}")
