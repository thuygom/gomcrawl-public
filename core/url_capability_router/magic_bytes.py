def detect_magic_type(data: bytes) -> str | None:
    if not data:
        return None
        
    if data.startswith(b"%PDF"):
        return "pdf"
        
    if data.startswith(b"PK\x03\x04"):
        # Could be docx, xlsx, pptx, zip, etc. 
        # For simplicity, we just classify as "zip_based" or "archive"
        # Since it's a file anyway, this is a strong indicator of a file download.
        return "zip_based"
        
    if data.startswith(b"\xFF\xD8\xFF"):
        return "jpg"
        
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
        
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
        
    if data.startswith(b"\x1F\x8B"):
        return "gz"
        
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "mp4"
        
    if data.startswith(b"ID3"):
        return "mp3"
        
    return None
