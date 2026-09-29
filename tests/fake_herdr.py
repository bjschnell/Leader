"""A stub herdr socket server that records requests and answers {"type":"ok"}."""

import json
import os
import socket
import tempfile
import threading


class FakeHerdr:
    def __init__(self, reply=None):
        self.dir = tempfile.mkdtemp(prefix="fake-herdr-")
        self.path = os.path.join(self.dir, "herdr.sock")
        self.requests = []
        self.reply = reply or (lambda req: {"type": "ok"})
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                if b"\n" in buf:
                    line, _ = buf.split(b"\n", 1)
                    req = json.loads(line)
                    self.requests.append(req)
                    resp = {"id": req.get("id"), "result": self.reply(req)}
                    conn.sendall((json.dumps(resp) + "\n").encode())
                    if req.get("method") != "events.subscribe":
                        return  # like herdr: one request per connection
                    self._stream(conn)
                    return

    def _stream(self, conn):
        """Hook for subscription tests; the base fake just holds the stream open."""
        return

    def close(self):
        self._stop = True
        self._sock.close()
        try:
            os.unlink(self.path)
            os.rmdir(self.dir)
        except OSError:
            pass
