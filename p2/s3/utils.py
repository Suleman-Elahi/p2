"""p2 S3 utility functions"""
import asyncio


def decode_aws_chunked(data: bytes) -> bytes:
    """Decode AWS chunked encoding (with or without chunk signatures).

    Format per chunk: <hex-size>[;chunk-signature=<sig>]\r\n<data>\r\n
    Terminal chunk: 0[;chunk-signature=<sig>]\r\n\r\n
    """
    out = bytearray()
    pos = 0
    while pos < len(data):
        # Find end of chunk header line
        end = data.find(b'\r\n', pos)
        if end == -1:
            break
        header = data[pos:end].split(b';')[0]  # strip optional ;chunk-signature=...
        try:
            chunk_size = int(header, 16)
        except ValueError:
            break
        if chunk_size == 0:
            break
        pos = end + 2  # skip \r\n after header
        out += data[pos:pos + chunk_size]
        pos += chunk_size + 2  # skip \r\n after data
    return bytes(out)


async def iter_request_body(request, chunk_size: int = 4 * 1024 * 1024):
    """Iterate request body in chunks for ASGI requests.

    Falls back to reading the full body if streaming APIs aren't available.
    """
    stream_attr = getattr(request, "stream", None)
    if stream_attr is None:
        data = await asyncio.to_thread(request.read)
        if data:
            yield data
        return

    stream = stream_attr() if callable(stream_attr) else stream_attr
    if hasattr(stream, "__aiter__"):
        async for chunk in stream:
            if chunk:
                yield chunk
        return

    for chunk in stream:
        if chunk:
            yield chunk
