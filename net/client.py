"""Сетевой клиент.

Клиент отправляет только ввод и заявки на проверку, принимает снапшоты,
коррекции и события. Симуляция остаётся в процессе клиента: этот класс —
транспортный слой, который не знает про Arcade.
"""

from __future__ import annotations

import select
import socket
import time
from collections import deque

from .clock import ClientClockSync
from .config import HANDSHAKE_TIMEOUT, PROTOCOL_VERSION, TICK_RATE
from .protocol import (
    Correction,
    MessageType,
    ProtocolError,
    RejectCode,
    Welcome,
    decode_correction_message,
    decode_event_message,
    decode_message,
    decode_reject,
    decode_snapshot_message,
    decode_time_message,
    decode_welcome,
    encode_ack_message,
    encode_hello,
    encode_input_message,
    encode_ping,
    encode_pong,
    encode_reject,
    encode_resync_message,
)
from .state import SnapshotState
from .transport import (
    ConnectionClosed,
    FrameTooLarge,
    TcpConnection,
    TransportError,
)


def _now_us() -> int:
    return time.monotonic_ns() // 1000


class NetClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        name: str = "player",
        scene_hash: int = 0,
        scripts_hash: int = 0,
        token: str = "",
        tick_rate: int = TICK_RATE,
    ):
        self.host = host
        self.port = int(port)
        self.name = name
        self.scene_hash = scene_hash & 0xFFFFFFFF
        self.scripts_hash = scripts_hash & 0xFFFFFFFF
        self.token = token
        self.tick_rate = max(1, int(tick_rate))

        self.connection: TcpConnection | None = None
        self.connected = False
        self.welcome: Welcome | None = None
        self.client_id = 0
        self.rejected: tuple[int, str] | None = None

        self.state = SnapshotState()
        self.last_snapshot = None
        self.last_snapshot_tick = 0
        self.corrections: deque[Correction] = deque(maxlen=256)
        self.events: deque[tuple[str, dict]] = deque(maxlen=256)
        self.clock_sync = ClientClockSync(self.tick_rate)

        self._seq = 0
        self._anchor_tick = 0
        self._anchor_local_us = 0

    # -- подключение ------------------------------------------------------

    def connect(self, timeout: float = HANDSHAKE_TIMEOUT):
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        self.connection = TcpConnection(raw)
        self.connected = True
        self._send(
            encode_hello(
                name=self.name,
                scene_hash=self.scene_hash,
                scripts_hash=self.scripts_hash,
                token=self.token,
            )
        )
        self._flush()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payloads = self.connection.receive()
            except (ConnectionClosed, FrameTooLarge, TransportError) as error:
                self.connected = False
                raise ConnectionError("Соединение закрыто при рукопожатии") from error
            for payload in payloads:
                self._handle(payload)
            if self.welcome is not None:
                return self.welcome
            if self.rejected is not None:
                code, text = self.rejected
                raise ConnectionError(f"Сервер отклонил подключение [{code}]: {text}")
            if not payloads:
                time.sleep(0.002)
        self.close()
        raise TimeoutError("Сервер не ответил на HELLO")

    # -- цикл -------------------------------------------------------------

    def pump(self, timeout: float = 0.0) -> bool:
        if not self.connected or self.connection is None:
            return False
        try:
            readable, _, _ = select.select(
                [self.connection.sock], [], [], timeout
            )
        except (OSError, ValueError):
            self.connected = False
            return False
        if readable:
            try:
                payloads = self.connection.receive()
            except (ConnectionClosed, FrameTooLarge, TransportError):
                self.connected = False
                return False
            for payload in payloads:
                if not self.connected:
                    break
                self._handle(payload)
        self._flush()
        return self.connected

    def _handle(self, payload) -> None:
        try:
            message = decode_message(payload)
        except ProtocolError:
            self.connected = False
            return

        kind = message.type
        if kind is MessageType.WELCOME:
            self.welcome = decode_welcome(message.body)
            self.client_id = self.welcome.client_id
            self.tick_rate = self.welcome.tick_rate
            self._anchor(message.tick or 1)
            return
        if kind is MessageType.SNAPSHOT:
            self._on_snapshot(payload)
            return
        if kind is MessageType.CORRECTION:
            self.corrections.append(decode_correction_message(payload))
            return
        if kind is MessageType.EVENT:
            self.events.append(decode_event_message(payload))
            return
        if kind is MessageType.PONG:
            t0, t1, t2, _ = decode_time_message(message.body)
            self.clock_sync.add_sample(t0, t1, t2, _now_us())
            return
        if kind is MessageType.PING:
            t0, _, _, _ = decode_time_message(message.body)
            self._send(encode_pong(t0, _now_us(), _now_us(), 0))
            return
        if kind in (MessageType.REJECT, MessageType.BYE):
            self.rejected = decode_reject(message.body)
            self.connected = False
            if self.connection is not None:
                self.connection.close()
            return

    def _on_snapshot(self, payload) -> None:
        snapshot = decode_snapshot_message(payload)
        if self.state.apply(snapshot):
            self.last_snapshot = snapshot
            self.last_snapshot_tick = snapshot.tick
            self._anchor(snapshot.tick)
            self._send(encode_ack_message(snapshot.tick))
        else:
            self._send(encode_resync_message(self.state.tick))

    def _anchor(self, tick: int) -> None:
        self._anchor_tick = int(tick)
        self._anchor_local_us = _now_us()

    # -- отправка ---------------------------------------------------------

    def _send(self, payload) -> bool:
        if not self.connected or self.connection is None:
            return False
        try:
            return self.connection.send_payload(payload)
        except (ConnectionClosed, TransportError):
            self.connected = False
            return False

    def _flush(self) -> None:
        if self.connection is None:
            return
        try:
            self.connection.flush()
        except (ConnectionClosed, TransportError):
            self.connected = False

    def send_input(self, frame):
        if not self.connected:
            return 0
        if not frame.tick:
            frame.tick = self.next_tick()
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        if self._seq == 0:
            self._seq = 1
        frame.seq = self._seq
        if self._send(encode_input_message(frame)):
            self._flush()
            return self._seq
        return 0

    def send_ack(self, tick: int) -> bool:
        return self._send(encode_ack_message(tick))

    def send_resync(self) -> bool:
        return self._send(encode_resync_message(self.state.tick))

    def ping(self) -> bool:
        return self._send(encode_ping(_now_us()))

    # -- темп -------------------------------------------------------------

    def next_tick(self) -> int:
        if self._anchor_tick == 0:
            return 1
        elapsed_us = (
            _now_us() - self._anchor_local_us
            + self.clock_sync.rtt_us // 2
        )
        return self._anchor_tick + int(
            elapsed_us * self.tick_rate / 1_000_000
        )

    @property
    def rtt_us(self) -> int:
        return self.clock_sync.rtt_us

    def drain_corrections(self) -> list[Correction]:
        items = list(self.corrections)
        self.corrections.clear()
        return items

    def drain_events(self) -> list[tuple[str, dict]]:
        items = list(self.events)
        self.events.clear()
        return items

    def close(self) -> None:
        if self.connection is not None:
            if self.connected:
                try:
                    self.connection.send_payload(
                        encode_reject(RejectCode.TIMEOUT, "bye", kind=MessageType.BYE)
                    )
                    self.connection.flush()
                except (ConnectionClosed, TransportError):
                    pass
            self.connection.close()
        self.connected = False
