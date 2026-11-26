"""TCP-транспорт с префиксом длины и неблокирующим вводом-выводом.

Транспорт ничего не знает о типах сообщений: он принимает и отдаёт готовые
payload'ы (заголовок протокола плюс тело). Это позволяет тестировать его
отдельно и позже заменить TCP на UDP, не трогая серверную логику.
"""

from __future__ import annotations

import selectors
import socket
import struct
import time

from .config import MAX_FRAME_BYTES, MAX_PENDING_BYTES

_LENGTH = struct.Struct("!I")
_COMPACT_THRESHOLD = 64 * 1024


class TransportError(OSError):
    """Базовая ошибка транспорта."""


class ConnectionClosed(TransportError):
    """Соединение закрыто другой стороной или разорвано."""


class FrameTooLarge(TransportError):
    """Кадр превышает разрешённый размер."""


class TcpConnection:
    __slots__ = (
        "sock",
        "peer",
        "max_frame",
        "pending_limit",
        "closed",
        "_in",
        "_in_offset",
        "_out",
        "_out_offset",
        "_peer_closed",
        "bytes_in",
        "bytes_out",
        "dropped",
        "last_recv",
        "last_send",
    )

    def __init__(
        self,
        sock: socket.socket,
        peer=None,
        *,
        max_frame: int = MAX_FRAME_BYTES,
        pending_limit: int = MAX_PENDING_BYTES,
    ):
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = sock
        self.peer = peer or sock.getpeername()
        self.max_frame = max_frame
        self.pending_limit = pending_limit
        self.closed = False
        self._in = bytearray()
        self._in_offset = 0
        self._out = bytearray()
        self._out_offset = 0
        self._peer_closed = False
        self.bytes_in = 0
        self.bytes_out = 0
        self.dropped = 0
        self.last_recv = time.monotonic()
        self.last_send = time.monotonic()

    # -- отправка ---------------------------------------------------------

    @property
    def pending_bytes(self) -> int:
        return len(self._out) - self._out_offset

    def send_payload(self, payload, *, droppable: bool = False) -> bool:
        """Ставит payload в очередь отправки.

        ``droppable=True`` используется для снапшотов: если клиент не успевает,
        старый кадр лучше потерять, чем копить задержку и память.
        """

        if self.closed:
            raise ConnectionClosed("Соединение закрыто")
        size = len(payload)
        if size > self.max_frame:
            raise FrameTooLarge(size)
        if self.pending_bytes + size + _LENGTH.size > self.pending_limit:
            if droppable:
                self.dropped += 1
                return False
            raise TransportError("Переполнен буфер отправки")
        self._out += _LENGTH.pack(size)
        self._out += payload
        return True

    def flush(self) -> bool:
        """Отправляет накопленное. ``True`` — буфер пуст."""

        if self.closed:
            raise ConnectionClosed("Соединение закрыто")
        while self._out_offset < len(self._out):
            try:
                sent = self.sock.send(memoryview(self._out)[self._out_offset:])
            except InterruptedError:
                continue
            except BlockingIOError:
                return False
            except OSError as error:
                self.close()
                raise ConnectionClosed(str(error)) from error
            if sent <= 0:
                self.close()
                raise ConnectionClosed("send() вернул 0")
            self._out_offset += sent
            self.bytes_out += sent
            self.last_send = time.monotonic()
        self._out.clear()
        self._out_offset = 0
        return True

    # -- приём ------------------------------------------------------------

    @property
    def pending_input(self) -> bool:
        return len(self._in) - self._in_offset >= _LENGTH.size

    def _compact_input(self) -> None:
        if self._in_offset == len(self._in):
            self._in.clear()
            self._in_offset = 0
        elif self._in_offset > _COMPACT_THRESHOLD:
            del self._in[:self._in_offset]
            self._in_offset = 0

    def receive(self, limit: int = 256) -> list[bytes]:
        """Читает доступные данные и возвращает завершённые payload'ы."""

        if self.closed:
            raise ConnectionClosed("Соединение закрыто")
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    self._peer_closed = True
                    break
                self._in += chunk
                self.bytes_in += len(chunk)
                self.last_recv = time.monotonic()
        except InterruptedError:
            pass
        except BlockingIOError:
            pass
        except OSError as error:
            self.close()
            raise ConnectionClosed(str(error)) from error

        payloads: list[bytes] = []
        while len(payloads) < limit:
            available = len(self._in) - self._in_offset
            if available < _LENGTH.size:
                break
            length = _LENGTH.unpack_from(self._in, self._in_offset)[0]
            if length > self.max_frame:
                self.close()
                raise FrameTooLarge(length)
            if available < _LENGTH.size + length:
                break
            start = self._in_offset + _LENGTH.size
            payloads.append(self._in[start:start + length])
            self._in_offset = start + length

        self._compact_input()
        if self._peer_closed and not payloads and not self.pending_input:
            self.close()
            raise ConnectionClosed("Пир закрыл соединение")
        return payloads

    # -- завершение -------------------------------------------------------

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class TcpListener:
    """Слушающий сокет. ``port=0`` выбирает свободный порт (удобно в тестах)."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 0,
        *,
        backlog: int = 64,
        max_frame: int = MAX_FRAME_BYTES,
        pending_limit: int = MAX_PENDING_BYTES,
    ):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(backlog)
        sock.setblocking(False)
        self.sock = sock
        self.max_frame = max_frame
        self.pending_limit = pending_limit

    @property
    def address(self) -> tuple[str, int]:
        return self.sock.getsockname()

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def accept(self) -> TcpConnection | None:
        try:
            sock, peer = self.sock.accept()
        except (BlockingIOError, InterruptedError):
            return None
        except OSError:
            if self.sock.fileno() < 0:
                return None
            raise
        return TcpConnection(
            sock,
            peer,
            max_frame=self.max_frame,
            pending_limit=self.pending_limit,
        )

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class ConnectionSet:
    """Набор соединений поверх ``selectors`` с автоматическим EVENT_WRITE."""

    def __init__(self):
        self._selector = selectors.DefaultSelector()
        self._connections: dict[TcpConnection, object] = {}

    def __len__(self) -> int:
        return len(self._connections)

    def __iter__(self):
        return iter(tuple(self._connections))

    def register(self, connection: TcpConnection, tag=None) -> None:
        if self._selector is None:
            return
        self._connections[connection] = tag
        self._selector.register(
            connection.sock, selectors.EVENT_READ, connection
        )

    def update(self, connection: TcpConnection) -> None:
        if self._selector is None:
            return
        if connection.closed:
            self.unregister(connection)
            return
        events = selectors.EVENT_READ
        if connection.pending_bytes:
            events |= selectors.EVENT_WRITE
        try:
            self._selector.modify(connection.sock, events, connection)
        except KeyError:
            self.register(connection, self._connections.get(connection))

    def unregister(self, connection: TcpConnection) -> None:
        self._connections.pop(connection, None)
        if self._selector is None:
            return
        try:
            self._selector.unregister(connection.sock)
        except (KeyError, ValueError, OSError):
            pass

    def tag(self, connection: TcpConnection):
        return self._connections.get(connection)

    def poll(self, timeout: float = 0.0):
        if self._selector is None:
            return []
        return [
            (key.data, mask)
            for key, mask in self._selector.select(timeout)
        ]

    def close(self) -> None:
        for connection in tuple(self._connections):
            self.unregister(connection)
        if self._selector is None:
            return
        selector = self._selector
        self._selector = None
        selector.close()
