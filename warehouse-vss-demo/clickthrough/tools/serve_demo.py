#!/usr/bin/env python3
"""Serve a captured walkthrough locally, including seekable evidence videos."""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import socketserver
from pathlib import Path


def parse_range(header: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or size <= 0:
        raise ValueError("Unsupported or unsatisfiable byte range")
    first, last = match.groups()
    if not first and not last:
        raise ValueError("Empty byte range")
    if not first:
        length = int(last)
        if length <= 0:
            raise ValueError("Empty suffix range")
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ValueError("Unsatisfiable byte range")
    return start, end


class Handler(http.server.SimpleHTTPRequestHandler):
    # One request per connection also lets the local server close cleanly
    # when a browser leaves a report or rejected range response unread.
    protocol_version = "HTTP/1.0"

    def send_head(self):
        self._range_remaining = None
        root = Path(self.directory).resolve()
        path = Path(self.translate_path(self.path))
        if not path.resolve().is_relative_to(root):
            self.send_error(403, "Outside walkthrough directory")
            return None
        if not path.is_file() or "Range" not in self.headers:
            return super().send_head()
        try:
            file = path.open("rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        size = path.stat().st_size
        try:
            start, end = parse_range(self.headers["Range"], size)
        except ValueError:
            file.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Last-Modified", self.date_time_string(path.stat().st_mtime))
        self.end_headers()
        file.seek(start)
        self._range_remaining = length
        return file

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def copyfile(self, source, outputfile):
        remaining = self._range_remaining
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining:
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                break
            outputfile.write(block)
            remaining -= len(block)


class LocalServer(http.server.ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer normally reverse-resolves the machine's hostname here.
        # A portable localhost demo must not depend on corporate/VPN DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not (directory / "index.html").is_file():
        parser.error("Directory must contain the built walkthrough index.html")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    handler = functools.partial(Handler, directory=str(directory))
    server = LocalServer(("127.0.0.1", args.port), handler)
    print(f"Local walkthrough: http://127.0.0.1:{args.port}/", flush=True)
    print("Press Ctrl+C to stop. No cluster or VPN is used.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
