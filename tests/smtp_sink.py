"""A tiny SMTP server for tests: speaks enough of the protocol for smtplib to deliver a
message to it over a real local socket, and can misbehave on purpose.

    with SmtpSink() as sink:
        ...point SmtpEmailProvider at 127.0.0.1:sink.port with security "none"...
        sink.messages      # raw bytes of every message accepted
        sink.connections   # how many TCP connections were opened
        sink.drop_idle()   # close every open connection, as a mail server does to idle clients
        sink.greeting_delay = 0.3   # hold back the 220 banner, as Gmail sometimes does

It listens on 127.0.0.1 only and never talks to the network.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        sink: SmtpSink = self.server.sink  # type: ignore[attr-defined]
        with sink._lock:
            sink.connections += 1
            sink._open.append(self.request)
        try:
            if sink.greeting_delay:
                time.sleep(sink.greeting_delay)
            self._say("220 sink ESMTP ready")
            data_mode = False
            lines: list[bytes] = []
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                if data_mode:
                    if line == b".\r\n":
                        data_mode = False
                        with sink._lock:
                            sink.messages.append(b"".join(lines))
                        self._say("250 2.0.0 OK queued")
                    else:
                        lines.append(line[1:] if line.startswith(b"..") else line)
                    continue
                command = line.decode("ascii", errors="replace").strip()
                verb = command.split(" ", 1)[0].upper()
                with sink._lock:
                    sink.commands.append(verb)
                if verb in ("EHLO", "HELO"):
                    self.wfile.write(b"250-sink\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
                elif verb == "AUTH":
                    self._say("235 2.7.0 Accepted")
                elif verb == "RCPT" and sink.refuse_recipient is not None:
                    self._say(sink.refuse_recipient)
                elif verb in ("MAIL", "RCPT", "RSET", "NOOP"):
                    self._say("250 OK")
                elif verb == "DATA":
                    if sink.refuse_data is not None:
                        self._say(sink.refuse_data)
                    else:
                        data_mode, lines = True, []
                        self._say("354 End data with <CR><LF>.<CR><LF>")
                elif verb == "QUIT":
                    self._say("221 bye")
                    return
                else:
                    self._say("502 command not implemented")
        except OSError:
            return
        finally:
            with sink._lock:
                if self.request in sink._open:
                    sink._open.remove(self.request)

    def _say(self, text: str) -> None:
        self.wfile.write((text + "\r\n").encode("ascii"))


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SmtpSink:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.commands: list[str] = []
        self.connections = 0
        self.greeting_delay = 0.0
        self.refuse_recipient: str | None = None  # e.g. "550 5.1.1 no such user"
        self.refuse_data: str | None = None  # e.g. "550 5.4.5 Daily user sending limit exceeded"
        self._lock = threading.Lock()
        self._open: list[socket.socket] = []
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.sink = self  # type: ignore[attr-defined]
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> SmtpSink:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self.drop_idle()

    def drop_idle(self) -> None:
        """Close every open connection from the server side."""
        with self._lock:
            sockets, self._open = self._open, []
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def count(self, verb: str) -> int:
        with self._lock:
            return self.commands.count(verb)
