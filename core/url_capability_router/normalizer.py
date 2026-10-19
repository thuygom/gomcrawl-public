from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, quote
import re

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid"
}

def normalize_url(url: str) -> str:
    """
    Normalizes a URL by:
    - lowercasing scheme and hostname
    - converting IDNA hostnames
    - removing trailing dots in hostname
    - removing default ports (80 for http, 443 for https)
    - resolving empty path to '/'
    - removing duplicate slashes in path
    - removing known tracking query parameters
    """
    url = url.strip()
    if not url:
        return ""
        
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc

    # Split userinfo, host, port
    userinfo = ""
    host = netloc
    port = ""
    
    if "@" in netloc:
        userinfo, host = netloc.split("@", 1)
        userinfo += "@"
        
    if ":" in host:
        # IPv6 handles might contain colons, but usually they are bracketed [::1]:80
        if "]" in host:
            idx = host.rfind(":")
            if idx > host.rfind("]"):
                port = host[idx+1:]
                host = host[:idx]
        else:
            host, port = host.rsplit(":", 1)

    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
        
    try:
        host = host.encode('idna').decode('ascii')
    except Exception:
        pass

    if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
        port = ""
        
    new_netloc = userinfo + host
    if port:
        new_netloc += ":" + port

    path = parsed.path
    if not path:
        path = "/"
    else:
        # Remove duplicate slashes
        path = re.sub(r'//+', '/', path)

    query = parsed.query
    if query:
        # Filter tracking params
        qsl = parse_qsl(query, keep_blank_values=True)
        filtered_qsl = [(k, v) for k, v in qsl if k.lower() not in TRACKING_PARAMS]
        query = urlencode(filtered_qsl)

    new_parsed = parsed._replace(
        scheme=scheme,
        netloc=new_netloc,
        path=path,
        query=query
    )
    return urlunparse(new_parsed)
