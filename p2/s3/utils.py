"""p2 S3 utility functions"""


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
