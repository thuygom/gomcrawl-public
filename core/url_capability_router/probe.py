import httpx

from core.url_capability_router.models import ProbeResult, ClassificationOptions
from core.url_capability_router.security import assert_safe_url, SecurityError
from core.url_capability_router.magic_bytes import detect_magic_type

def probe_url(url: str, options: ClassificationOptions) -> ProbeResult:
    result = ProbeResult(performed=True)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        # We manually handle redirects to validate each step for SSRF
        client = httpx.Client(
            follow_redirects=False,
            timeout=options.timeout_ms / 1000.0,
            verify=False # We want to probe even if cert is bad
        )
        
        current_url = url
        redirects = 0
        
        # First try HEAD
        response = None
        while redirects <= options.max_redirects:
            try:
                assert_safe_url(current_url, allow_private_network=options.allow_private_network)
            except SecurityError as e:
                result.error = str(e)
                return result

            response = client.head(current_url, headers=headers)
            if response.is_redirect:
                if options.follow_redirects:
                    redirects += 1
                    current_url = response.headers.get("Location")
                    if not current_url:
                        break
                    # Make absolute if relative
                    if current_url.startswith("/"):
                        current_url = str(response.url.join(current_url))
                    continue
                else:
                    break
            else:
                break
                
        result.redirect_count = redirects
        if response is not None:
            result.status_code = response.status_code
            result.content_type = response.headers.get("Content-Type")
            result.content_disposition = response.headers.get("Content-Disposition")
            cl = response.headers.get("Content-Length")
            if cl and cl.isdigit():
                result.content_length = int(cl)
                
        # Now try to get magic bytes using a Range GET if HEAD didn't fail and it's not a clear HTML
        if response and response.status_code == 200:
            if not result.content_type or not result.content_type.startswith("text/html"):
                range_headers = headers.copy()
                range_headers["Range"] = "bytes=0-4095"
                try:
                    get_resp = client.get(current_url, headers=range_headers)
                    if get_resp.status_code in (200, 206):
                        result.magic_type = detect_magic_type(get_resp.content)
                except httpx.RequestError:
                    pass
        
    except httpx.RequestError as e:
        result.error = str(e)
        
    return result
