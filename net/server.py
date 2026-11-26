"""Авторитетный сетевой сервер.

Сервер не доверяет клиенту состояние: принимает только ввод, сам крутит
симуляцию и сверяет присланные клиентом «заявки» со своим результатом.
Снапшоты уходят дельтой от последнего подтверждённого клиентом тика.
"""

from __future__ import annotations

import math
import queue
import selectors
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .clock import ServerClock
from .config import (
    DEFAULT_HOST,
    DEFAULT_MAX_PLAYERS,
    DEFAULT_PORT,
    HANDSHAKE_TIMEOUT,
    IDLE_TIMEOUT,
    INPUT_DELAY_TICKS,
    MAX_CATCH_UP_TICKS,
    MAX_CLAIM_DRIFT,
    MAX_INPUT_PER_SEC,
    MAX_NAME_BYTES,
    CLAIM_DELTA_ROTATION_TOLERANCE,
    CLAIM_DELTA_TOLERANCE,
    MAX_SINGLE_ERROR,
    POSITION_TOLERANCE,
    PROTOCOL_VERSION,
    ROTATION_TOLERANCE,
    SNAPSHOT_HISTORY,
    SNAPSHOT_HZ,
    SNAPSHOT_INTERVAL_TICKS,
    TICK_RATE,
    AnticheatPolicy,
)
from .input_queue import InputQueue
from .protocol import (
    Correction,
    CorrectionReason,
    MessageType,
    ProtocolError,
    RejectCode,
    Welcome,
    decode_ack_message,
    decode_hello,
    decode_input_message,
    decode_message,
    decode_resync_message,
    decode_time_message,
    encode_correction_message,
    encode_event_message,
    encode_pong,
    encode_reject,
    encode_snapshot_message,
    encode_welcome,
)
from .state import SnapshotState
from .transport import (
    ConnectionClosed,
    ConnectionSet,
    FrameTooLarge,
    TcpConnection,
    TcpListener,
    TransportError,
)


def _now_us() -> int:
    return time.monotonic_ns() // 1000


def _angle(degrees: float) -> float:
    """Нормализует угол в диапазон (-180, 180]."""

    return (float(degrees) + 180.0) % 360.0 - 180.0


@dataclass(slots=True)
class ClientSession:
    connection: TcpConnection
    client_id: int = 0
    name: str = ""
    ready: bool = False
    joined_at: float = 0.0
    last_seen: float = 0.0
    last_ack_tick: int = 0
    last_ack_seq: int = 0
    last_input_seq: int = 0
    last_executed_seq: int = 0
    owner_render_id: int = 0
    # Сверка идёт по приращению: клиентское «было → стало» против серверного.
    # ``last_world`` — состояние ПЕРЕД предыдущим новым кадром, поэтому
    # разность даёт движение сервера за интервал между кадрами (с поправкой
    # на джиттер/повторы в _validate_claims).
    last_claim: dict[int, tuple[float, float, float]] | None = None
    last_world: dict[int, tuple[float, float, float]] = field(
        default_factory=dict
    )
    rtt_us: int = 0
    desyncs: int = 0
    violations: int = 0
    claims_checked: int = 0
    last_error: float = 0.0
    force_full: bool = True
    input_window_start: float = 0.0
    input_window_count: int = 0
    sent_snapshots: int = 0
    input_queue: InputQueue = field(default_factory=InputQueue)


class NullWorld:
    """Пустой мир: сервер работает, но реплицировать нечего."""

    def apply_input(self, client_id: int, frame, tick: int) -> None:
        pass

    def step(self, tick: int) -> None:
        pass

    def snapshot_state(self) -> SnapshotState:
        return SnapshotState()

    def object_state(self, render_id: int):
        return None

    def owner_of(self, render_id: int):
        return None


class GameServer:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        max_players: int = DEFAULT_MAX_PLAYERS,
        tick_rate: int = TICK_RATE,
        snapshot_hz: int = SNAPSHOT_HZ,
        input_delay: int = INPUT_DELAY_TICKS,
        world=None,
        anticheat: AnticheatPolicy | None = None,
        scene_hash: int = 0,
        scripts_hash: int = 0,
        token: str = "",
        world_seed: int = 0,
        verbose: bool = True,
        clock: ServerClock | None = None,
    ):
        self.max_players = max(1, int(max_players))
        self.tick_rate = max(1, int(tick_rate))
        self.snapshot_hz = max(1, int(snapshot_hz))
        self.snapshot_interval = max(
            1, min(SNAPSHOT_INTERVAL_TICKS, self.tick_rate // self.snapshot_hz)
        )
        self.input_delay = max(0, int(input_delay))
        self.scene_hash = scene_hash & 0xFFFFFFFF
        self.scripts_hash = scripts_hash & 0xFFFFFFFF
        self.token = token
        self.world_seed = world_seed & 0xFFFFFFFF
        self.world = world if world is not None else NullWorld()
        self.policy = anticheat or AnticheatPolicy("log")
        self.verbose = verbose

        self.listener = TcpListener(host, port)
        self.connections = ConnectionSet()
        self.sessions: dict[TcpConnection, ClientSession] = {}
        self.by_id: dict[int, ClientSession] = {}
        self.clock = (
            clock
            if clock is not None
            else ServerClock(self.tick_rate, start_tick=1 + self.input_delay)
        )
        self._commands: queue.SimpleQueue = queue.SimpleQueue()
        self._next_client_id = 1
        self._last_sim_tick = 0
        self._running = False
        self._history: OrderedDict[int, SnapshotState] = OrderedDict()
        self.dropped_ticks = 0

    # -- жизненный цикл ---------------------------------------------------

    @property
    def port(self) -> int:
        return self.listener.port

    @property
    def tick(self) -> int:
        return self._last_sim_tick

    def start(self) -> None:
        self._running = True

    def submit(self, callback) -> None:
        """Ставит действие в очередь серверного цикла (консоль, другие потоки).

        Серверный цикл — единственный владелец сокетов и сессий, поэтому
        команды извне выполняются в ``poll``, а не напрямую.
        """

        self._commands.put(callback)

    def _drain_commands(self) -> None:
        while True:
            try:
                callback = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                callback()
            except Exception as error:  # команда не должна ронять сервер
                if self.verbose:
                    print(f"[NET] Команда сервера упала: {error}", flush=True)

    def run_forever(self, poll_interval: float = 0.004) -> None:
        self.start()
        try:
            while self._running:
                self.poll(poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        if not self._running and not self.sessions:
            self._close_listener()
            return
        self._running = False
        for session in tuple(self.sessions.values()):
            if session.ready:
                self._send(
                    session,
                    encode_reject(
                        RejectCode.TIMEOUT,
                        "Сервер остановлен",
                        kind=MessageType.BYE,
                    ),
                )
                try:
                    session.connection.flush()
                except TransportError:
                    pass
            self._remove(session, "stop")
        self.connections.close()
        self._close_listener()

    def _close_listener(self) -> None:
        try:
            self.listener.close()
        except OSError:
            pass

    # -- основной цикл ----------------------------------------------------

    def poll(self, timeout: float = 0.0) -> None:
        self._drain_commands()
        if not self._running:
            return
        now = time.monotonic()
        self._accept(now)
        try:
            events = self.connections.poll(timeout)
        except (OSError, ValueError):
            return
        for connection, mask in events:
            session = self.sessions.get(connection)
            if session is None:
                continue
            if mask & selectors.EVENT_READ:
                try:
                    payloads = connection.receive()
                except (ConnectionClosed, FrameTooLarge, TransportError):
                    self._remove(session, "transport")
                    continue
                for payload in payloads:
                    self._handle_message(session, payload)
                    if connection.closed:
                        break
            if connection.closed:
                continue
            if mask & selectors.EVENT_WRITE:
                try:
                    connection.flush()
                except TransportError:
                    self._remove(session, "flush")
                    continue
            self.connections.update(connection)

        self._check_timeouts(time.monotonic())
        self._advance()
        self._flush_all()

    def _accept(self, now: float) -> None:
        limit = self.max_players + 32
        while len(self.sessions) < limit:
            connection = self.listener.accept()
            if connection is None:
                return
            session = ClientSession(
                connection=connection,
                joined_at=now,
                last_seen=now,
            )
            self.sessions[connection] = session
            self.connections.register(connection)

    def _flush_all(self) -> None:
        for session in tuple(self.sessions.values()):
            connection = session.connection
            if connection.closed:
                self._remove(session, "closed")
                continue
            try:
                connection.flush()
            except (ConnectionClosed, TransportError):
                self._remove(session, "flush")
                continue
            self.connections.update(connection)

    # -- сообщения --------------------------------------------------------

    def _handle_message(self, session: ClientSession, payload) -> None:
        session.last_seen = time.monotonic()
        try:
            message = decode_message(payload)
        except ProtocolError:
            self._reject(session, RejectCode.PROTOCOL, "Некорректный кадр")
            return

        kind = message.type
        if kind is MessageType.HELLO:
            self._on_hello(session, message)
            return
        if not session.ready:
            self._reject(session, RejectCode.PROTOCOL, "Сначала HELLO")
            return

        if kind is MessageType.INPUT:
            self._on_input(session, payload)
        elif kind is MessageType.PING:
            t0, _, _, _ = decode_time_message(message.body)
            self._send(
                session,
                encode_pong(t0, _now_us(), _now_us(), 0),
            )
        elif kind is MessageType.ACK:
            session.last_ack_tick = decode_ack_message(message.body)
            session.last_ack_seq = message.tick
        elif kind is MessageType.RESYNC:
            session.force_full = True
        elif kind is MessageType.BYE:
            self._remove(session, "bye")

    def _on_hello(self, session: ClientSession, message) -> None:
        if session.ready:
            self._reject(session, RejectCode.PROTOCOL, "Повторный HELLO")
            return
        try:
            hello = decode_hello(message.body)
        except ProtocolError:
            self._reject(session, RejectCode.PROTOCOL, "Некорректный HELLO")
            return
        if hello.version != PROTOCOL_VERSION:
            self._reject(
                session,
                RejectCode.VERSION,
                f"Версия протокола {hello.version} != {PROTOCOL_VERSION}",
            )
            return
        if hello.scene_hash != self.scene_hash:
            self._reject(session, RejectCode.SCENE, "Другая сцена")
            return
        if hello.scripts_hash != self.scripts_hash:
            self._reject(session, RejectCode.SCENE, "Другие скрипты")
            return
        if self.token and hello.token != self.token:
            self._reject(session, RejectCode.AUTH, "Неверный токен")
            return
        if len(self.by_id) >= self.max_players:
            self._reject(session, RejectCode.FULL, "Сервер заполнен")
            return

        session.client_id = self._next_client_id
        self._next_client_id += 1
        session.name = (hello.name or f"player{session.client_id}")[
            :MAX_NAME_BYTES
        ]
        session.ready = True
        session.joined_at = time.monotonic()
        session.last_seen = session.joined_at
        self.by_id[session.client_id] = session

        hook = getattr(self.world, "on_join", None)
        if hook is not None:
            try:
                hook(session.client_id)
            except Exception:
                if self.verbose:
                    print(f"[NET] on_join({session.client_id}) упал", flush=True)

        owner_render_id = 0
        lookup = getattr(self.world, "render_id_for", None)
        if lookup is not None:
            try:
                owner_render_id = int(lookup(session.client_id) or 0)
            except Exception:
                owner_render_id = 0
        session.owner_render_id = owner_render_id
        # Первая заявка сверяется абсолютно с состоянием на момент входа,
        # дальше — по приращению относительно предыдущей заявки.
        session.last_world = self._capture_claim_state(session)
        session.last_claim = None

        self._send(
            session,
            encode_welcome(
                Welcome(
                    client_id=session.client_id,
                    tick_rate=self.tick_rate,
                    snapshot_hz=self.snapshot_hz,
                    input_delay=self.input_delay,
                    max_players=self.max_players,
                    world_seed=self.world_seed,
                    owner_render_id=owner_render_id,
                ),
                server_tick=max(1, self._last_sim_tick),
            ),
        )
        self._send_initial_snapshot(session)
        if self.verbose:
            print(
                f"[NET] + {session.name} (id={session.client_id}) "
                f"из {session.connection.peer}",
                flush=True,
            )

    def _on_input(self, session: ClientSession, payload) -> None:
        now = time.monotonic()
        if now - session.input_window_start >= 1.0:
            session.input_window_start = now
            session.input_window_count = 0
        session.input_window_count += 1
        if session.input_window_count > MAX_INPUT_PER_SEC:
            session.violations += 1
            return
        try:
            frame = decode_input_message(payload)
        except ProtocolError:
            session.violations += 1
            return
        if frame.seq > session.last_input_seq:
            session.last_input_seq = frame.seq
        session.input_queue.push(frame)

    # -- симуляция --------------------------------------------------------

    def _advance(self) -> None:
        target = self.clock.tick() - self.input_delay
        if target < 1:
            return
        if self._last_sim_tick == 0:
            self._last_sim_tick = target - 1
        steps = target - self._last_sim_tick
        if steps <= 0:
            return
        if steps > MAX_CATCH_UP_TICKS:
            self.dropped_ticks += steps - MAX_CATCH_UP_TICKS
            self._last_sim_tick = target - MAX_CATCH_UP_TICKS
            steps = MAX_CATCH_UP_TICKS
        for _ in range(steps):
            self._last_sim_tick += 1
            tick = self._last_sim_tick
            executed = self._apply_inputs(tick)
            self.world.step(tick)
            # Заявки сверяются с состоянием ДО текущего кадра; это же
            # состояние становится базой для следующего.
            self._validate_claims(tick, executed)
            if tick % self.snapshot_interval == 0:
                self._broadcast_snapshot(tick)

    def _claim_render_ids(self, session: ClientSession):
        """render_id объектов, за которые отвечает клиент."""

        render_id = int(session.owner_render_id or 0)
        if render_id:
            return (render_id,)
        lookup = getattr(self.world, "render_id_for", None)
        if lookup is not None:
            try:
                render_id = int(lookup(session.client_id) or 0)
            except Exception:
                render_id = 0
            if render_id:
                session.owner_render_id = render_id
                return (render_id,)
        # Простые миры (и тесты) используют client_id как render_id.
        return (int(session.client_id),) if session.client_id else ()

    def _capture_claim_state(self, session: ClientSession) -> dict:
        """Снимок позиций владельца для сверки заявок (значения — копии)."""

        state_of = getattr(self.world, "object_state", None)
        if state_of is None:
            return {}
        states = {}
        for render_id in self._claim_render_ids(session):
            state = state_of(render_id)
            if state is not None:
                states[render_id] = (
                    float(state.x),
                    float(state.y),
                    float(state.rotation),
                )
        return states

    def _apply_inputs(self, tick: int):
        executed = []
        for session in tuple(self.sessions.values()):
            if not session.ready:
                continue
            frame = session.input_queue.take(tick)
            # Повтор последнего кадра не двигает заявки: клиент его уже
            # применил, и сверять по нему нечего.
            is_new = frame.seq > session.last_executed_seq
            before = None
            if is_new:
                session.last_executed_seq = frame.seq
                # Состояние ДО применения кадра: с ним сравнивается заявка.
                before = self._capture_claim_state(session)
            self.world.apply_input(session.client_id, frame, tick)
            executed.append((session, frame, is_new, before))
        return executed

    def _validate_claims(self, tick: int, executed) -> None:
        for session, frame, is_new, before in executed:
            if not is_new:
                continue
            if before is None:
                before = {}
            if not frame.claims:
                # Кадр без заявок не с чем сверять: следующий будет опорным.
                session.last_claim = None
                if before:
                    session.last_world = before
                continue
            previous_claim = session.last_claim
            previous_world = session.last_world
            for claim in frame.claims:
                render_id = claim[0]
                if self.world.owner_of(render_id) != session.client_id:
                    session.violations += 1
                    continue
                truth = before.get(render_id)
                if truth is None:
                    # Объект ещё не появился в кэше: не выдумываем расхождение.
                    continue
                session.claims_checked += 1
                drift = math.hypot(
                    claim[1] - truth[0], claim[2] - truth[1]
                )
                if (
                    previous_claim is not None
                    and render_id in previous_claim
                    and render_id in previous_world
                ):
                    previous = previous_claim[render_id]
                    previous_truth = previous_world[render_id]
                    server_dx = truth[0] - previous_truth[0]
                    server_dy = truth[1] - previous_truth[1]
                    error = math.hypot(
                        (claim[1] - previous[0]) - server_dx,
                        (claim[2] - previous[1]) - server_dy,
                    )
                    server_rotation = _angle(truth[2] - previous_truth[2])
                    rotation_error = abs(
                        _angle(claim[3] - previous[2]) - server_rotation
                    )
                    # Джиттер мог задержать кадр, и сервер повторил ввод на
                    # несколько тиков. Тогда его приращение законно больше
                    # клиентского ровно на эти повторы, поэтому допуск растёт
                    # вместе с движением сервера. Подделка «на месте» или
                    # скачок всё равно выходит за него, а накопленный сдвиг
                    # отдельно ловит MAX_CLAIM_DRIFT.
                    allowed_position = max(
                        CLAIM_DELTA_TOLERANCE,
                        math.hypot(server_dx, server_dy) + POSITION_TOLERANCE,
                    )
                    allowed_rotation = max(
                        CLAIM_DELTA_ROTATION_TOLERANCE,
                        abs(server_rotation) + ROTATION_TOLERANCE,
                    )
                else:
                    error = drift
                    rotation_error = abs(_angle(claim[3] - truth[2]))
                    allowed_position = CLAIM_DELTA_TOLERANCE
                    allowed_rotation = CLAIM_DELTA_ROTATION_TOLERANCE
                delta_ok = (
                    error <= allowed_position
                    and rotation_error <= allowed_rotation
                )
                # Приращение может сойтись, но абсолютный сдвиг остаться
                # (телепорт с постоянным офсетом). Большой дрейф ловим
                # отдельно; небольшой — это нормальная сетевая задержка.
                if delta_ok and drift <= MAX_CLAIM_DRIFT:
                    continue
                if drift > MAX_CLAIM_DRIFT:
                    error = max(error, drift)
                session.desyncs += 1
                session.last_error = error
                current = self.world.object_state(render_id)
                if current is not None:
                    self._send_correction(
                        session, tick, render_id, current, frame.seq
                    )
                if error > MAX_SINGLE_ERROR:
                    session.violations += 1
                if self.policy.should_kick(session.desyncs):
                    self.kick(session.client_id, "desync")
                    return
            # Клиентское состояние запоминаем даже при ошибке: следующий кадр
            # снова проверяется по приращению, а не по накопленному сдвигу.
            if before:
                session.last_world = before
            session.last_claim = {
                int(claim[0]): (
                    float(claim[1]),
                    float(claim[2]),
                    float(claim[3]),
                )
                for claim in frame.claims
            }

    def _send_correction(self, session, tick, render_id, truth, seq) -> None:
        self._send(
            session,
            encode_correction_message(
                Correction(
                    render_id=render_id,
                    tick=tick,
                    x=truth.x,
                    y=truth.y,
                    rotation=truth.rotation,
                    scale=truth.scale,
                    alpha=truth.alpha,
                    last_ack_seq=seq,
                    reason=int(CorrectionReason.DESYNC),
                )
            ),
        )

    def _poll_world_network(self) -> None:
        """Даёт миру подхватить объекты, созданные скриптами между тиками."""

        poll = getattr(self.world, "poll_network_events", None)
        if poll is None:
            return
        try:
            poll()
        except Exception as error:  # сеть не должна падать из-за мира
            if self.verbose:
                print(f"[NET] poll_network_events упал: {error}", flush=True)

    def _send_initial_snapshot(self, session: ClientSession) -> None:
        self._poll_world_network()
        tick = max(1, self._last_sim_tick)
        state = self.world.snapshot_state().copy()
        state.tick = tick
        self._history[tick] = state
        snapshot = state.build(tick, None, ack_seq=0)
        self._send(session, encode_snapshot_message(snapshot))

    def _broadcast_snapshot(self, tick: int) -> None:
        self._poll_world_network()
        current = self.world.snapshot_state().copy()
        current.tick = tick
        self._history[tick] = current
        while len(self._history) > SNAPSHOT_HISTORY:
            self._history.popitem(last=False)

        for session in tuple(self.sessions.values()):
            if not session.ready or session.connection.closed:
                continue
            base = (
                None
                if session.force_full
                else self._history.get(session.last_ack_tick)
            )
            if base is not None and base.tick >= tick:
                base = None
            snapshot = current.build(
                tick, base, ack_seq=session.last_input_seq
            )
            if base is None:
                session.force_full = False
            if self._send(
                session, encode_snapshot_message(snapshot), droppable=True
            ):
                session.sent_snapshots += 1

    # -- служебное --------------------------------------------------------

    def _send(self, session: ClientSession, payload, *, droppable=False) -> bool:
        if session.connection.closed:
            return False
        try:
            return session.connection.send_payload(payload, droppable=droppable)
        except (ConnectionClosed, TransportError):
            return False

    def _reject(self, session: ClientSession, code, text: str) -> None:
        self._send(session, encode_reject(code, text))
        try:
            session.connection.flush()
        except TransportError:
            pass
        self._remove(session, text)

    def _remove(self, session: ClientSession, reason: str = "") -> None:
        connection = session.connection
        if self.sessions.pop(connection, None) is None:
            connection.close()
            return
        self.connections.unregister(connection)
        if session.ready:
            self.by_id.pop(session.client_id, None)
            hook = getattr(self.world, "on_leave", None)
            if hook is not None:
                try:
                    hook(session.client_id)
                except Exception:
                    pass
            if self.verbose:
                print(
                    f"[NET] - {session.name} (id={session.client_id})"
                    + (f": {reason}" if reason else ""),
                    flush=True,
                )
        connection.close()

    def _check_timeouts(self, now: float) -> None:
        for session in tuple(self.sessions.values()):
            limit = IDLE_TIMEOUT if session.ready else HANDSHAKE_TIMEOUT
            if now - session.last_seen > limit:
                self._remove(session, "timeout")

    def kick(self, client_id: int, reason: str = "kicked") -> bool:
        session = self.by_id.get(int(client_id))
        if session is None:
            return False
        self._send(
            session,
            encode_reject(RejectCode.AUTH, reason, kind=MessageType.BYE),
        )
        try:
            session.connection.flush()
        except TransportError:
            pass
        self._remove(session, reason)
        return True

    def set_anticheat(self, mode: str) -> str:
        return self.policy.set_mode(mode)

    def broadcast_event(self, name: str, payload: dict | None = None) -> int:
        message = encode_event_message(name, payload)
        sent = 0
        for session in tuple(self.sessions.values()):
            if session.ready and self._send(session, message):
                sent += 1
        return sent

    def stats(self) -> dict:
        players = []
        for session in self.by_id.values():
            players.append(
                {
                    "client_id": session.client_id,
                    "name": session.name,
                    "peer": session.connection.peer,
                    "rtt_us": session.rtt_us,
                    "desyncs": session.desyncs,
                    "violations": session.violations,
                    "claims": session.claims_checked,
                    "last_error": session.last_error,
                    "pending_input": session.input_queue.pending,
                    "last_ack_tick": session.last_ack_tick,
                    "last_executed_seq": session.last_executed_seq,
                    "snapshots": session.sent_snapshots,
                    "bytes_in": session.connection.bytes_in,
                    "bytes_out": session.connection.bytes_out,
                    "dropped_frames": session.connection.dropped,
                }
            )
        return {
            "tick": self._last_sim_tick,
            "players": players,
            "clients": len(players),
            "max_players": self.max_players,
            "anticheat": self.policy.mode,
            "dropped_ticks": self.dropped_ticks,
            "history": len(self._history),
        }
