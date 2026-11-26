"""Тесты сетевого фундамента: протокол, состояние, транспорт, loopback.

Запуск из каталога движка::

    python -m unittest tests.test_net -v
"""

from __future__ import annotations

import os
import queue
import socket
import struct
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from net import protocol
from net.client import NetClient
from net.clock import ClientClockSync, ServerClock
from net.directives import parse_network_directives, role_from_object
from net.engine_world import EngineWorld
from net.input_queue import InputQueue
from net.input_source import FrameInputSource, snapshot_from_frame
from net.server import GameServer
from net.simulation import ServerSimulation
from net.keys import (
    BITSET_BYTES,
    KEY_NAMES,
    normalize_key,
    pack_keys,
    unpack_keys,
)
from net.loopback import LoopbackNetwork
from net.reconcile import CorrectionBuffer, PredictionHistory, check_claim
from net.protocol import (
    Correction,
    CorrectionReason,
    FrameFlags,
    InputFrame,
    MessageType,
    ProtocolError,
    RejectCode,
    Snapshot,
    SpawnRecord,
    StateRecord,
    Welcome,
)
from net.state import ObjectState, SnapshotState
from net.transport import (
    ConnectionClosed,
    ConnectionSet,
    FrameTooLarge,
    TcpConnection,
    TcpListener,
    TransportError,
)


def _accept(listener: TcpListener, timeout: float = 3.0) -> TcpConnection:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        connection = listener.accept()
        if connection is not None:
            return connection
        time.sleep(0.002)
    raise AssertionError("Не удалось принять соединение")


def _read_all(connection: TcpConnection, expected: int, timeout: float = 3.0) -> list[bytes]:
    payloads: list[bytes] = []
    deadline = time.perf_counter() + timeout
    while len(payloads) < expected and time.perf_counter() < deadline:
        payloads.extend(connection.receive())
        if len(payloads) < expected:
            time.sleep(0.001)
    return payloads


def _state_tuples(state: SnapshotState) -> dict[int, tuple]:
    return {
        render_id: (
            item.sprite,
            item.layer,
            item.x,
            item.y,
            item.rotation,
            item.scale,
            item.alpha,
        )
        for render_id, item in state.objects.items()
    }


class KeysTests(unittest.TestCase):
    def test_roundtrip_all_known_keys(self):
        bits, extra = pack_keys(KEY_NAMES)
        self.assertEqual(len(bits), BITSET_BYTES)
        self.assertEqual(extra, ())
        self.assertEqual(unpack_keys(bits), frozenset(KEY_NAMES))

    def test_roundtrip_subset(self):
        keys = {"W", "A", "SPACE", "LEFT", "F12"}
        bits, extra = pack_keys(keys)
        self.assertEqual(unpack_keys(bits, extra), frozenset(keys))

    def test_unknown_keys_go_to_extra(self):
        bits, extra = pack_keys({"W", "TILT", "SOMETHING_LONG"})
        self.assertIn("W", unpack_keys(bits, extra))
        self.assertIn("TILT", unpack_keys(bits, extra))
        self.assertLessEqual(len(extra), 8)

    def test_normalization(self):
        self.assertEqual(normalize_key("page_up"), "PAGEUP")
        bits, extra = pack_keys({"page_up"})
        self.assertEqual(extra, ())
        self.assertIn("PAGEUP", unpack_keys(bits))

    def test_extra_limited(self):
        unknown = {f"CUSTOM{index}" for index in range(32)}
        _, extra = pack_keys(unknown)
        self.assertLessEqual(len(extra), 8)


class ProtocolTests(unittest.TestCase):
    def test_header_roundtrip(self):
        payload = protocol.encode_message(MessageType.ACK, tick=1234, seq=9)
        message = protocol.decode_message(payload)
        self.assertIs(message.type, MessageType.ACK)
        self.assertEqual(message.tick, 1234)
        self.assertEqual(message.seq, 9)
        self.assertEqual(len(message.body), 0)

    def test_unknown_type_rejected(self):
        payload = struct.pack("!BBHI", 250, 0, 0, 0)
        with self.assertRaises(ProtocolError):
            protocol.decode_message(payload)

    def test_hello_roundtrip(self):
        payload = protocol.encode_hello(
            name="Игрок",
            scene_hash=0xDEADBEEF,
            scripts_hash=17,
            token="secret",
        )
        message = protocol.decode_message(payload)
        hello = protocol.decode_hello(message.body)
        self.assertEqual(hello.version, protocol.PROTOCOL_VERSION)
        self.assertEqual(hello.name, "Игрок")
        self.assertEqual(hello.token, "secret")
        self.assertEqual(hello.scene_hash, 0xDEADBEEF)
        self.assertEqual(hello.scripts_hash, 17)

    def test_welcome_roundtrip(self):
        welcome = Welcome(3, 100, 20, 2, 64, 123456, 77)
        payload = protocol.encode_welcome(welcome, server_tick=999)
        message = protocol.decode_message(payload)
        self.assertIs(message.type, MessageType.WELCOME)
        self.assertEqual(message.tick, 999)
        self.assertEqual(protocol.decode_welcome(message.body), welcome)
        self.assertEqual(
            protocol.decode_welcome(message.body).owner_render_id, 77
        )

    def test_input_roundtrip(self):
        crc = protocol.state_crc(1.0, 2.0, 45.0)
        frame = protocol.make_input_frame(
            100,
            5,
            {"W", "A", "SPACE"},
            mouse_x=1.5,
            mouse_y=-2.5,
            buttons=3,
            clicks=((7, 0), (9, 2)),
            claims=((7, 1.0, 2.0, 45.0, crc),),
        )
        decoded = protocol.decode_input_message(
            protocol.encode_input_message(frame)
        )
        self.assertEqual(decoded.tick, 100)
        self.assertEqual(decoded.seq, 5)
        self.assertEqual(decoded.mouse_x, 1.5)
        self.assertEqual(decoded.mouse_y, -2.5)
        self.assertEqual(decoded.buttons, 3)
        self.assertEqual(decoded.clicks, ((7, 0), (9, 2)))
        self.assertEqual(decoded.claims, ((7, 1.0, 2.0, 45.0, crc),))
        self.assertEqual(
            unpack_keys(decoded.keys, decoded.extra_keys),
            frozenset({"W", "A", "SPACE"}),
        )

    def test_input_rejects_too_many_clicks(self):
        frame = protocol.make_input_frame(
            1, 1, set(), clicks=tuple((index, 0) for index in range(9))
        )
        with self.assertRaises(ProtocolError):
            protocol.encode_input_message(frame)

    def test_input_truncated(self):
        frame = protocol.make_input_frame(1, 1, {"W"})
        payload = protocol.encode_input_message(frame)
        with self.assertRaises(ProtocolError):
            protocol.decode_input_message(payload[:20])

    def test_snapshot_roundtrip(self):
        snapshot = Snapshot(
            tick=77,
            base_tick=70,
            ack_seq=33,
            flags=int(FrameFlags.MORE),
            despawns=(3,),
            spawns=(
                SpawnRecord(1, "a.png", 1.0, 10.0, 20.0, 0.0, 1.0, 0.5),
                SpawnRecord(2, "b.png", 2.0, -3.0, 4.0, 90.0, 2.0, 1.0),
            ),
            records=(
                StateRecord(5, 0b000011, (5.5, 6.5)),
                StateRecord(6, protocol.MASK_SPRITE, (), "c.png"),
            ),
        )
        decoded = protocol.decode_snapshot_message(
            protocol.encode_snapshot_message(snapshot)
        )
        self.assertEqual(decoded.tick, 77)
        self.assertEqual(decoded.base_tick, 70)
        self.assertEqual(decoded.ack_seq, 33)
        self.assertEqual(decoded.flags, int(FrameFlags.MORE))
        self.assertEqual(decoded.despawns, (3,))
        self.assertEqual(decoded.spawns, snapshot.spawns)
        self.assertEqual(decoded.records, snapshot.records)

    def test_snapshot_masks_are_sorted(self):
        snapshot = Snapshot(
            tick=1,
            spawns=(SpawnRecord(9, "z"), SpawnRecord(2, "a")),
            despawns=(5, 1),
        )
        payload = protocol.encode_snapshot_message(snapshot)
        decoded = protocol.decode_snapshot_message(payload)
        self.assertEqual([item.render_id for item in decoded.spawns], [2, 9])
        self.assertEqual(decoded.despawns, (1, 5))

    def test_snapshot_record_mask_mismatch(self):
        with self.assertRaises(ProtocolError):
            protocol.encode_snapshot_message(
                Snapshot(records=(StateRecord(1, 0b11, (1.0,)),))
            )

    def test_snapshot_truncated(self):
        payload = protocol.encode_snapshot_message(Snapshot(tick=1))
        with self.assertRaises(ProtocolError):
            protocol.decode_snapshot_message(payload[:-3])

    def test_correction_roundtrip(self):
        correction = Correction(7, 42, 1.0, 2.0, 30.0, 1.0, 0.5, 11, CorrectionReason.SPEED)
        decoded = protocol.decode_correction_message(
            protocol.encode_correction_message(correction)
        )
        self.assertEqual(decoded, correction)

    def test_event_roundtrip(self):
        payload = protocol.encode_event_message("explosion", {"x": 1, "name": "тест"})
        name, data = protocol.decode_event_message(payload)
        self.assertEqual(name, "explosion")
        self.assertEqual(data, {"x": 1, "name": "тест"})

    def test_event_bad_json(self):
        raw = b"{not json}"
        body = bytes((1,)) + b"e" + struct.pack("!H", len(raw)) + raw
        payload = protocol.encode_message(MessageType.EVENT, body=body)
        with self.assertRaises(ProtocolError):
            protocol.decode_event_message(payload)

    def test_time_messages(self):
        payload = protocol.encode_time_message(MessageType.PING, t0=111)
        message = protocol.decode_message(payload)
        self.assertEqual(protocol.decode_time_message(message.body), (111, 0, 0, 0))

    def test_reject(self):
        payload = protocol.encode_reject(RejectCode.FULL, "сервер заполнен")
        message = protocol.decode_message(payload)
        code, text = protocol.decode_reject(message.body)
        self.assertEqual(code, RejectCode.FULL)
        self.assertEqual(text, "сервер заполнен")

    def test_state_crc(self):
        self.assertEqual(
            protocol.state_crc(1.23456, 2.0, 45.0),
            protocol.state_crc(1.23456, 2.0, 45.0),
        )
        self.assertNotEqual(
            protocol.state_crc(1.0, 2.0, 45.0),
            protocol.state_crc(1.0, 2.0, 46.0),
        )
        self.assertIsInstance(protocol.state_crc(float("nan"), 0.0, 0.0), int)


class SnapshotStateTests(unittest.TestCase):
    def _build_world(self, count: int = 4) -> SnapshotState:
        state = SnapshotState()
        for index in range(count):
            state.put(
                index + 1,
                ObjectState(
                    f"obj{index}.png",
                    float(index),
                    float(index * 10),
                    float(index * 20),
                    float(index * 3),
                    1.0,
                    1.0,
                ),
            )
        state.tick = 10
        return state

    def test_full_then_delta(self):
        server = self._build_world()
        full = server.build(10)
        self.assertTrue(full.flags & FrameFlags.RESET)
        self.assertEqual(full.base_tick, 0)
        self.assertEqual(len(full.spawns), 4)

        client = SnapshotState()
        self.assertTrue(client.apply(full))
        self.assertEqual(_state_tuples(client), _state_tuples(server))
        self.assertEqual(client.tick, 10)

        base = server.copy()
        server.objects[1].x = 999.0
        server.remove(2)
        server.put(50, ObjectState("new.png", 5.0, 7.0, 8.0, 0.0, 1.0, 1.0))
        server.tick = 11

        delta = server.build(11, base, ack_seq=4)
        self.assertEqual(delta.base_tick, 10)
        self.assertEqual(delta.despawns, (2,))
        self.assertEqual([item.render_id for item in delta.spawns], [50])
        self.assertTrue(any(item.render_id == 1 for item in delta.records))

        self.assertTrue(client.apply(delta))
        self.assertEqual(_state_tuples(client), _state_tuples(server))
        self.assertEqual(client.tick, 11)

    def test_wrong_base_is_rejected_without_changes(self):
        server = self._build_world()
        client = SnapshotState()
        client.apply(server.build(10))

        base = server.copy()
        server.objects[1].x = 42.0
        server.tick = 11
        delta = server.build(11, base)

        stale = SnapshotState()
        stale.apply(server.build(10))
        stale.tick = 5
        before = _state_tuples(stale)
        self.assertFalse(stale.apply(delta))
        self.assertEqual(_state_tuples(stale), before)

    def test_unknown_record_is_rejected_without_changes(self):
        client = SnapshotState()
        client.put(1, ObjectState("a.png"))
        client.tick = 3
        before = _state_tuples(client)
        broken = Snapshot(
            tick=4,
            base_tick=3,
            records=(StateRecord(999, 0b1, (1.0,)),),
        )
        self.assertFalse(client.apply(broken))
        self.assertEqual(_state_tuples(client), before)

    def test_delta_is_smaller_than_full(self):
        server = SnapshotState()
        for index in range(50):
            server.put(index + 1, ObjectState("s.png", 0.0, float(index), 0.0))
        server.tick = 1
        full = server.build(1)
        base = server.copy()
        server.objects[25].x += 1.0
        server.tick = 2
        delta = server.build(2, base)

        full_bytes = len(protocol.encode_snapshot_message(full))
        delta_bytes = len(protocol.encode_snapshot_message(delta))
        self.assertLess(delta_bytes, full_bytes // 4)

    def test_sprite_change_roundtrip(self):
        server = SnapshotState()
        server.put(1, ObjectState("a.png"))
        server.tick = 1
        client = SnapshotState()
        client.apply(server.build(1))

        base = server.copy()
        server.objects[1].sprite = "b.png"
        server.tick = 2
        delta = server.build(2, base)
        self.assertTrue(client.apply(delta))
        self.assertEqual(client.objects[1].sprite, "b.png")


class TcpTransportTests(unittest.TestCase):
    def setUp(self):
        self.listener = TcpListener("127.0.0.1", 0)
        self.raw = socket.create_connection(
            ("127.0.0.1", self.listener.port), timeout=3.0
        )
        self.connection = _accept(self.listener)

    def tearDown(self):
        self.connection.close()
        try:
            self.raw.close()
        except OSError:
            pass
        self.listener.close()

    def test_inbound_roundtrip(self):
        payloads = [b"x" * (index % 50 + 1) for index in range(100)]
        for payload in payloads:
            self.raw.sendall(struct.pack("!I", len(payload)) + payload)
        received = _read_all(self.connection, len(payloads))
        self.assertEqual(received, payloads)

    def test_outbound_roundtrip(self):
        self.assertTrue(self.connection.send_payload(b"hello"))
        self.assertTrue(self.connection.flush())
        self.raw.settimeout(3.0)
        data = b""
        while len(data) < 4 + 5:
            chunk = self.raw.recv(64)
            self.assertTrue(chunk)
            data += chunk
        length = struct.unpack("!I", data[:4])[0]
        self.assertEqual(length, 5)
        self.assertEqual(data[4:], b"hello")

    def test_oversize_frame_rejected(self):
        self.raw.sendall(struct.pack("!I", 100_000) + b"\x00" * 16)
        with self.assertRaises(FrameTooLarge):
            self.connection.receive()
        self.assertTrue(self.connection.closed)

    def test_backpressure(self):
        self.connection.pending_limit = 64
        self.assertFalse(self.connection.send_payload(b"a" * 100, droppable=True))
        self.assertEqual(self.connection.dropped, 1)
        self.assertTrue(self.connection.send_payload(b"b" * 10, droppable=True))
        with self.assertRaises(TransportError):
            self.connection.send_payload(b"c" * 100)

    def test_peer_close(self):
        self.raw.close()
        with self.assertRaises(ConnectionClosed):
            deadline = time.perf_counter() + 3.0
            while time.perf_counter() < deadline:
                self.connection.receive()
                time.sleep(0.002)

    def test_connection_set(self):
        connections = ConnectionSet()
        connections.register(self.connection, tag="client")
        try:
            self.raw.sendall(struct.pack("!I", 3) + b"abc")
            events = []
            deadline = time.perf_counter() + 3.0
            while not events and time.perf_counter() < deadline:
                events = connections.poll(0.05)
            self.assertTrue(events)
            received = self.connection.receive()
            self.assertEqual(received, [b"abc"])

            self.connection.send_payload(b"z" * 10)
            connections.update(self.connection)
            events = connections.poll(0.5)
            self.assertTrue(any(mask & 2 for _, mask in events))
        finally:
            connections.close()


class LoopbackTests(unittest.TestCase):
    def test_roundtrip(self):
        left, right = LoopbackNetwork().pair()
        self.assertTrue(left.send_payload(b"one"))
        self.assertTrue(left.send_payload(b"two"))
        self.assertEqual(right.receive(), [b"one", b"two"])

    def test_latency(self):
        left, right = LoopbackNetwork(latency=0.05).pair()
        left.send_payload(b"late")
        self.assertEqual(right.receive(), [])
        time.sleep(0.08)
        self.assertEqual(right.receive(), [b"late"])

    def test_loss(self):
        left, right = LoopbackNetwork(loss=1.0).pair()
        left.send_payload(b"lost")
        self.assertEqual(left.dropped, 1)
        self.assertEqual(right.receive(), [])

    def test_close(self):
        left, right = LoopbackNetwork().pair()
        left.close()
        with self.assertRaises(ConnectionClosed):
            left.receive()
        with self.assertRaises(ConnectionClosed):
            right.send_payload(b"x")


class _FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ClockTests(unittest.TestCase):
    def test_server_clock_is_monotonic(self):
        fake = _FakeClock()
        clock = ServerClock(100, start_tick=1, clock=fake)
        self.assertEqual(clock.tick(), 1)
        fake.advance(0.5)
        self.assertEqual(clock.tick(), 51)
        fake.value = 0.0
        self.assertEqual(clock.tick(), 51)
        fake.advance(0.6)
        self.assertEqual(clock.tick(), 61)

    def test_tick_mapping(self):
        fake = _FakeClock()
        clock = ServerClock(100, start_tick=1, clock=fake)
        self.assertAlmostEqual(clock.seconds_for_tick(51), 0.5)
        self.assertEqual(clock.tick_at(0.5), 51)

    def test_clock_sync(self):
        sync = ClientClockSync(100)
        offset = sync.add_sample(1000, 1010, 1020, 1035)
        self.assertEqual(offset, -2)
        self.assertEqual(sync.rtt_us, 25)
        self.assertEqual(sync.estimate_tick(1_050_000, 100, 1_000_000), 105)

    def test_clock_sync_ignores_bad_sample(self):
        sync = ClientClockSync(100)
        sync.add_sample(1000, 1010, 1020, 1035)
        before = sync.offset_us
        self.assertEqual(sync.add_sample(100, 0, 0, 50), before)

    def test_clock_sync_median_resists_outlier(self):
        sync = ClientClockSync(100, sample_limit=4)
        for _ in range(3):
            sync.add_sample(0, 100, 100, 200)
        self.assertEqual(sync.offset_us, 0)
        sync.add_sample(0, 10_000, 10_000, 200)
        self.assertEqual(sync.offset_us, 0)


class InputQueueTests(unittest.TestCase):
    def test_push_and_take(self):
        queue = InputQueue()
        frame = protocol.make_input_frame(10, 1, {"W"})
        self.assertTrue(queue.push(frame))
        self.assertEqual(queue.take(10), frame)
        self.assertEqual(queue.received, 1)

    def test_repeat_last_frame(self):
        queue = InputQueue(repeat_ticks=5)
        queue.push(protocol.make_input_frame(10, 1, {"W"}, clicks=((5, 0),)))
        queue.take(10)
        repeated = queue.take(12)
        self.assertEqual(repeated.tick, 12)
        self.assertEqual(unpack_keys(repeated.keys), frozenset({"W"}))
        self.assertEqual(repeated.clicks, ())
        self.assertEqual(queue.repeated, 1)

    def test_repeat_expires(self):
        queue = InputQueue(repeat_ticks=2)
        queue.push(protocol.make_input_frame(10, 1, {"W"}))
        queue.take(10)
        self.assertEqual(unpack_keys(queue.take(11).keys), frozenset({"W"}))
        self.assertEqual(unpack_keys(queue.take(12).keys), frozenset({"W"}))
        neutral = queue.take(13)
        self.assertEqual(unpack_keys(neutral.keys), frozenset())
        self.assertEqual(queue.underflow, 1)

    def test_rejects_consumed_and_duplicate_seq(self):
        queue = InputQueue(buffer_ticks=8)
        queue.push(protocol.make_input_frame(10, 1, {"W"}))
        queue.take(10)
        self.assertFalse(queue.push(protocol.make_input_frame(11, 1, {"A"})))
        self.assertTrue(queue.push(protocol.make_input_frame(11, 2, {"A"})))
        self.assertFalse(queue.push(protocol.make_input_frame(11, 2, {"A"})))
        self.assertEqual(queue.dropped, 2)

    def test_overflow_drops_oldest_frame(self):
        queue = InputQueue(buffer_ticks=3)
        for seq in (1, 2, 3, 4):
            self.assertTrue(queue.push(protocol.make_input_frame(seq, seq, {"W"})))
        self.assertEqual(queue.pending, 3)
        self.assertEqual(queue.dropped, 1)
        self.assertEqual(queue.take(5).seq, 2)
        self.assertEqual(queue.take(6).seq, 3)
        self.assertEqual(queue.take(7).seq, 4)

    def test_consumed_seq_tracks_last_frame(self):
        queue = InputQueue()
        for seq in (1, 2, 3):
            queue.push(protocol.make_input_frame(seq, seq, {"W"}))
        self.assertEqual([queue.take(tick).seq for tick in (1, 2, 3)], [1, 2, 3])
        self.assertEqual(queue.consumed_seq, 3)

    def test_second_take_without_input_repeats_state(self):
        queue = InputQueue()
        frame = protocol.make_input_frame(5, 1, {"A"})
        queue.push(frame)
        self.assertEqual(queue.take(5), frame)
        repeated = queue.take(6)
        self.assertEqual(repeated.tick, 6)
        self.assertEqual(unpack_keys(repeated.keys), frozenset({"A"}))


class ReconcileTests(unittest.TestCase):
    def test_history_ring(self):
        history = PredictionHistory(capacity=4)
        for tick in range(1, 7):
            history.record(tick, tick, tick, 0.0, 0.0)
        self.assertEqual(len(history), 4)
        self.assertIsNone(history.get(1))
        self.assertIsNone(history.get(2))
        self.assertIsNotNone(history.get(3))
        history.trim(5)
        self.assertEqual(len(history), 2)
        self.assertIsNotNone(history.get(6))

    def test_history_replaces_same_tick(self):
        history = PredictionHistory(capacity=4)
        history.record(3, 1, 1.0, 1.0, 0.0)
        history.record(3, 2, 2.0, 2.0, 0.0)
        self.assertEqual(len(history), 1)
        self.assertEqual(history.get(3).x, 2.0)

    def test_check_claim_tolerances(self):
        claim = (1, 10.0, 20.0, 45.0, 0)
        self.assertTrue(check_claim(claim, 10.03, 20.0, 45.2))
        self.assertFalse(check_claim(claim, 10.2, 20.0, 45.0))
        self.assertFalse(check_claim(claim, 10.0, 20.0, 46.0))
        self.assertTrue(check_claim((1, 0.0, 0.0, 0.2, 0), 0.0, 0.0, 359.8))

    def test_correction_decay(self):
        buffer = CorrectionBuffer(decay=0.2)
        buffer.add(7, 10.0, -4.0, 8.0, now=0.0)
        self.assertEqual(buffer.sample(7, now=0.0), (10.0, -4.0, 8.0))
        self.assertEqual(buffer.sample(7, now=0.1), (5.0, -2.0, 4.0))
        self.assertEqual(buffer.sample(7, now=0.2), (0.0, 0.0, 0.0))
        self.assertEqual(len(buffer), 0)


class DirectivesTests(unittest.TestCase):
    def test_default_is_local_object(self):
        role = parse_network_directives(None)
        self.assertFalse(role.networked)
        self.assertFalse(role.replicated)
        self.assertFalse(role.owner)
        self.assertFalse(role.takes_input)
        self.assertEqual(role.authority, "local")
        self.assertEqual(role, parse_network_directives({}))

    def test_replicated_server_object(self):
        role = parse_network_directives({
            "authority": "server",
            "replication": "all",
        })
        self.assertTrue(role.networked)
        self.assertTrue(role.replicated)
        self.assertFalse(role.owner)
        self.assertFalse(role.server_only)

    def test_owner_predicts_and_reads_input(self):
        role = parse_network_directives({
            "authority": "server",
            "replication": "all",
            "prediction": "owner",
            "input": "owner",
        })
        self.assertTrue(role.owner)
        self.assertTrue(role.predict)
        self.assertTrue(role.replicated)
        self.assertTrue(role.takes_input)
        self.assertFalse(role.server_only)

    def test_server_only_is_not_replicated(self):
        role = parse_network_directives({"authority": "server"})
        self.assertTrue(role.server_only)
        self.assertFalse(role.replicated)

    def test_input_owner_gets_owner_slot_without_prediction(self):
        role = parse_network_directives({
            "authority": "server",
            "input": "owner",
        })
        self.assertTrue(role.owner)
        self.assertFalse(role.predict)
        self.assertTrue(role.takes_input)
        self.assertFalse(role.replicated)

    def test_invalid_combinations_are_rejected(self):
        cases = (
            ({"authority": "local", "replication": "all"}, ValueError),
            ({"authority": "server", "replication": "none",
              "prediction": "owner"}, ValueError),
            ({"authority": "local", "input": "owner"}, ValueError),
            ({"authority": "client"}, ValueError),
            ({"teleport": "yes"}, ValueError),
            ({"authority": 1}, TypeError),
            (["replicate"], TypeError),
        )
        for value, error in cases:
            with self.subTest(value=value):
                with self.assertRaises(error):
                    parse_network_directives(value)

    def test_role_from_object(self):
        data = {
            "scripting": {
                "network": {"authority": "server", "replication": "all"}
            }
        }
        self.assertTrue(role_from_object(data).replicated)
        self.assertFalse(role_from_object({}).networked)


class InputSourceTests(unittest.TestCase):
    def test_advance_delivers_frames_in_seq_order(self):
        source = FrameInputSource(client_id=3)
        source.push(protocol.make_input_frame(1, 1, {"W"}))
        source.push(protocol.make_input_frame(2, 2, {"W", "D"}))

        self.assertTrue(source.advance(10))
        self.assertEqual(source.current.seq, 1)
        self.assertEqual(source.current.pressed_down, frozenset({"W"}))
        self.assertTrue(source.current.pressed.issuperset({"W"}))

        self.assertTrue(source.advance(11))
        self.assertEqual(source.current.seq, 2)
        self.assertEqual(source.current.pressed_down, frozenset({"D"}))
        self.assertEqual(source.current.pressed_up, frozenset())

    def test_repeat_keeps_state_but_not_clicks(self):
        source = FrameInputSource(client_id=1, repeat_ticks=3)
        source.push(
            protocol.make_input_frame(1, 1, {"A"}, clicks=((7, 0),))
        )
        source.advance(10)
        self.assertEqual(source.current.clicks, ((7, 0),))
        self.assertTrue(source.advance(11))
        self.assertEqual(source.current.pressed, frozenset({"A"}))
        self.assertEqual(source.current.pressed_down, frozenset())
        self.assertEqual(source.current.clicks, ())
        self.assertEqual(source.repeated, 1)

    def test_repeat_expires_to_neutral(self):
        source = FrameInputSource(client_id=1, repeat_ticks=1)
        source.push(protocol.make_input_frame(1, 1, {"W"}))
        source.advance(10)
        source.advance(11)
        source.advance(12)
        self.assertEqual(source.current.pressed, frozenset())
        self.assertEqual(source.current.pressed_up, frozenset({"W"}))
        self.assertEqual(source.underflow, 1)

    def test_claims_travel_with_tick(self):
        source = FrameInputSource(client_id=1)
        claim = (5, 1.0, 2.0, 3.0, 123)
        source.push(
            protocol.make_input_frame(1, 1, set(), claims=(claim,))
        )
        source.advance(10)
        self.assertEqual(source.current.claims, (claim,))

    def test_tick_is_monotonic(self):
        source = FrameInputSource(client_id=1)
        self.assertTrue(source.advance(5))
        self.assertFalse(source.advance(5))
        self.assertFalse(source.advance(4))

    def test_snapshot_from_frame(self):
        frame = protocol.make_input_frame(
            4, 9, {"SPACE"}, mouse_x=5.0, mouse_y=6.0, buttons=1
        )
        snapshot = snapshot_from_frame(frame, previous_pressed={"W"})
        self.assertEqual(snapshot.tick, 4)
        self.assertEqual(snapshot.seq, 9)
        self.assertEqual(snapshot.pressed_down, frozenset({"SPACE"}))
        self.assertEqual(snapshot.pressed_up, frozenset({"W"}))
        self.assertTrue(snapshot.button_pressed("left"))
        self.assertFalse(snapshot.button_pressed("right"))


class ServerSimulationTests(unittest.TestCase):
    def make_simulation(self):
        return ServerSimulation(repeat_ticks=2)

    def test_join_binds_render_id(self):
        simulation = self.make_simulation()
        source = simulation.join(3, 101)
        self.assertIs(simulation.source(3), source)
        self.assertIs(simulation.source_for_render(101), source)
        self.assertEqual(simulation.owner_of(101), 3)
        self.assertIsNone(simulation.source_for_render(999))

    def test_push_and_tick_advance_input(self):
        simulation = self.make_simulation()
        simulation.join(1, 10)
        payload = protocol.encode_input_message(
            protocol.make_input_frame(0, 0, {"D"})
        )
        self.assertTrue(simulation.push(1, payload))
        simulation.begin_tick(1)
        self.assertEqual(
            simulation.source(1).current.pressed, frozenset({"D"})
        )

    def test_bad_payload_is_counted_not_raised(self):
        simulation = self.make_simulation()
        simulation.join(1, 10)
        self.assertFalse(simulation.push(1, b"\x00\x01"))
        self.assertEqual(simulation.bad_frames, 1)

    def test_unknown_player_frame_is_ignored(self):
        simulation = self.make_simulation()
        self.assertFalse(simulation.push(7, b""))
        self.assertEqual(simulation.bad_frames, 0)

    def test_tick_skips_are_counted(self):
        simulation = self.make_simulation()
        simulation.join(1, 10)
        simulation.begin_tick(3)
        simulation.begin_tick(6)
        self.assertEqual(simulation.skipped, 2)
        self.assertEqual(simulation.tick, 6)

    def test_leave_removes_source_and_bindings(self):
        simulation = self.make_simulation()
        simulation.join(1, 10)
        simulation.join(2, 20)
        simulation.leave(1)
        self.assertIsNone(simulation.source(1))
        self.assertIsNone(simulation.owner_of(10))
        self.assertEqual(simulation.owner_of(20), 2)

    def test_take_clicks_returns_each_click_once_with_source(self):
        simulation = self.make_simulation()
        simulation.join(1, 10)
        simulation.join(2, 20)
        simulation.push(
            1,
            protocol.encode_input_message(
                protocol.make_input_frame(
                    0, 0, set(),
                    mouse_x=4.0, mouse_y=5.0,
                    clicks=((77, 1), (78, 4)),
                )
            ),
        )
        simulation.push(
            2,
            protocol.encode_input_message(
                protocol.make_input_frame(0, 0, set(), clicks=((79, 1),))
            ),
        )
        simulation.begin_tick(1)

        clicks = simulation.take_clicks()

        self.assertEqual(
            [
                (client_id, render_id, button)
                for client_id, _snapshot, render_id, button in clicks
            ],
            [(1, 77, 1), (1, 78, 4), (2, 79, 1)],
        )
        first_snapshot = clicks[0][1]
        self.assertEqual(
            (first_snapshot.mouse_x, first_snapshot.mouse_y), (4.0, 5.0)
        )

        # Клик — событие: повтор кадра на следующем тике его не воспроизводит.
        simulation.begin_tick(2)
        self.assertEqual(simulation.take_clicks(), [])

    def test_stats_are_aggregated(self):
        simulation = self.make_simulation()
        simulation.join(1, 10)
        simulation.push(
            1,
            protocol.encode_input_message(
                protocol.make_input_frame(0, 0, {"W"})
            ),
        )
        simulation.begin_tick(1)
        stats = simulation.stats()
        self.assertEqual(stats["players"], 1)
        self.assertEqual(stats["bound"], 1)
        self.assertGreaterEqual(stats["received"], 1)


class _FakeVisualCache:
    def __init__(self):
        self.values = {}

    def set(self, render_id, **values):
        values.setdefault("sprite", "player.png")
        values.setdefault("layer", 0.0)
        values.setdefault("x", 0.0)
        values.setdefault("y", 0.0)
        values.setdefault("rotation", 0.0)
        values.setdefault("scale", 1.0)
        values.setdefault("alpha", 1.0)
        self.values[int(render_id)] = values

    def get_many(self, render_id, fields):
        values = self.values.get(int(render_id))
        if values is None:
            raise KeyError(render_id)
        return {name: values[name] for name in fields}


class EngineWorldTests(unittest.TestCase):
    def make_world(self, **kwargs):
        self.worker = queue.SimpleQueue()
        self.visual = _FakeVisualCache()
        self.visual.set(10, x=1.0, y=2.0)
        self.visual.set(20, x=3.0, y=4.0)
        self.executed = types.SimpleNamespace(value=100)
        world = EngineWorld(
            worker_queue=self.worker,
            visual_cache=self.visual,
            owner_slots=[10, 20],
            replicated_ids=[10],
            executed_tick=self.executed,
            **kwargs,
        )
        return world

    def test_join_assigns_owner_slot_and_leave_frees_it(self):
        world = self.make_world()
        self.assertEqual(world.on_join(1), 10)
        self.assertEqual(world.on_join(2), 20)
        self.assertEqual(world.owner_of(10), 1)
        self.assertEqual(world.render_id_for(2), 20)

        join_message = self.worker.get_nowait()
        self.assertEqual(join_message["command"], "net_join")
        self.assertEqual(join_message["render_id"], 10)
        self.worker.get_nowait()  # net_join второго игрока

        world.on_leave(1)
        self.assertIsNone(world.owner_of(10))
        self.assertEqual(world._free_slots, [10])
        leave_message = self.worker.get_nowait()
        self.assertEqual(leave_message["command"], "net_leave")

    def test_apply_input_drops_repeated_seq_and_sends_batch(self):
        world = self.make_world()
        frame = protocol.make_input_frame(0, 0, {"D"})
        world.apply_input(1, frame, 1)
        world.apply_input(1, frame, 2)
        world.step(1)

        message = self.worker.get_nowait()
        self.assertEqual(message["command"], "net_tick")
        self.assertEqual(message["tick"], 1)
        self.assertEqual(len(message["inputs"]), 1)
        client_id, payload = message["inputs"][0]
        self.assertEqual(client_id, 1)
        decoded = protocol.decode_input_message(payload)
        self.assertEqual(decoded.seq, 0)
        self.assertEqual(unpack_keys(decoded.keys), frozenset({"D"}))

    def test_step_waits_for_executed_tick(self):
        world = self.make_world(tick_timeout=0.05)
        self.executed.value = 0
        world.step(50)
        self.assertEqual(world.missed_ticks, 1)

        self.executed.value = 60
        world.step(60)
        self.assertEqual(world.missed_ticks, 1)

    def test_snapshot_only_contains_replicated_ids(self):
        world = self.make_world()
        snapshot = world.snapshot_state()
        self.assertEqual(set(snapshot.objects), {10})
        state = snapshot.objects[10]
        self.assertEqual(state.sprite, "player.png")
        self.assertEqual((state.x, state.y), (1.0, 2.0))

    def test_missing_or_spriteless_object_is_skipped(self):
        world = self.make_world()
        self.assertIsNone(world.object_state(999))
        self.visual.set(20, sprite="")
        self.assertIsNone(world.object_state(20))

    def test_set_replicated_updates_snapshot(self):
        world = self.make_world()
        world.set_replicated([20])
        self.assertEqual(set(world.snapshot_state().objects), {20})

    def test_poll_network_events_adds_replicated_objects(self):
        events = queue.SimpleQueue()
        world = self.make_world(network_events=events)
        events.put({
            "render_id": 20,
            "network": {"authority": "server", "replication": "all"},
            "has_visual": True,
        })
        events.put({
            "render_id": 30, "network": {"authority": "server"},
            "has_visual": True,
        })
        events.put({
            "render_id": 10,
            "network": {"authority": "server", "replication": "all"},
            "has_visual": True,
        })
        events.put({
            "render_id": 40,
            "network": {"authority": "server", "replication": "all"},
            "has_visual": False,
        })
        events.put({
            "render_id": 0,
            "network": {"authority": "server", "replication": "all"},
            "has_visual": True,
        })
        events.put("not a dict")

        self.assertEqual(world.poll_network_events(), 1)
        self.assertEqual(world.replicated_ids, {10, 20})
        self.assertEqual(set(world.snapshot_state().objects), {10, 20})

        # Повторные события не считаются новыми объектами.
        events.put({
            "render_id": 20,
            "network": {"authority": "server", "replication": "all"},
            "has_visual": True,
        })
        self.assertEqual(world.poll_network_events(), 0)

    def test_poll_network_events_removes_deleted_objects(self):
        events = queue.SimpleQueue()
        world = self.make_world(network_events=events)
        events.put({"render_id": 10, "removed": True})
        events.put({"render_id": 999, "removed": True})

        self.assertEqual(world.poll_network_events(), 1)
        self.assertEqual(world.replicated_ids, set())
        self.assertEqual(world.snapshot_state().objects, {})

    def test_poll_network_events_without_queue_is_noop(self):
        world = self.make_world()
        self.assertEqual(world.poll_network_events(), 0)


class _TestWorld:
    """Мир для интеграционных тестов: один объект на игрока, без физики.

    ``WASD`` двигает объект на единицу за тик — этого достаточно, чтобы
    проверить применение ввода, дельту снапшота и проверку заявок.
    """

    def __init__(self):
        self.objects: dict[int, ObjectState] = {}
        self.owners: dict[int, int] = {}
        self.applied: list[tuple[int, int]] = []

    def on_join(self, client_id: int) -> None:
        self.owners[client_id] = client_id
        self.objects[client_id] = ObjectState("player.png", 0.0, 0.0, 0.0)

    def on_leave(self, client_id: int) -> None:
        self.owners.pop(client_id, None)
        self.objects.pop(client_id, None)

    def apply_input(self, client_id: int, frame: InputFrame, tick: int) -> None:
        state = self.objects.get(client_id)
        if state is None:
            return
        keys = unpack_keys(frame.keys, frame.extra_keys)
        dx = 1.0 if "D" in keys else (-1.0 if "A" in keys else 0.0)
        dy = 1.0 if "W" in keys else (-1.0 if "S" in keys else 0.0)
        state.x += dx
        state.y += dy
        self.applied.append((client_id, frame.seq))

    def step(self, tick: int) -> None:
        pass

    def snapshot_state(self) -> SnapshotState:
        snapshot = SnapshotState()
        for render_id, item in self.objects.items():
            snapshot.put(
                render_id,
                ObjectState(
                    item.sprite,
                    item.layer,
                    item.x,
                    item.y,
                    item.rotation,
                    item.scale,
                    item.alpha,
                ),
            )
        return snapshot

    def object_state(self, render_id: int) -> ObjectState | None:
        return self.objects.get(render_id)

    def owner_of(self, render_id: int) -> int | None:
        return self.owners.get(render_id)


class _ServerRunner:
    """Держит ``GameServer`` в фоновом потоке; ошибки поднимает в тест."""

    def __init__(self, server: GameServer):
        self.server = server
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.server.run_forever(0.002)
        except BaseException as error:  # noqa: BLE001 - тест должен увидеть сбой
            self.error = error

    def start(self) -> "_ServerRunner":
        self.thread.start()
        return self

    def stop(self) -> None:
        try:
            self.server.submit(self.server.stop)
        except Exception:
            self.server.stop()
        self.thread.join(timeout=3.0)
        if self.thread.is_alive():
            self.server.stop()
            self.thread.join(timeout=2.0)
        if self.error is not None:
            raise self.error


def _wait_for(predicate, timeout: float = 3.0, description: str = "условие"):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.003)
    raise AssertionError(f"Не дождались: {description}")


def _wait_for_object(client: NetClient, render_id: int, timeout: float = 3.0):
    def ready():
        client.pump(0.005)
        return render_id in client.state.objects

    return _wait_for(
        ready, timeout=timeout, description=f"объект {render_id} в снапшоте"
    )


class _ServerTestBase(unittest.TestCase):
    max_players = 2

    def setUp(self):
        self.world = _TestWorld()
        self.server = GameServer(
            "127.0.0.1",
            0,
            max_players=self.max_players,
            world=self.world,
            verbose=False,
        )
        self.runner = _ServerRunner(self.server).start()
        self.clients: list[NetClient] = []

    def tearDown(self):
        for client in self.clients:
            try:
                client.close()
            except Exception:
                pass
        self.runner.stop()

    def connect(self, name: str = "player"):
        client = NetClient("127.0.0.1", self.server.port, name=name)
        self.clients.append(client)
        welcome = client.connect()
        return client, welcome


class ServerIntegrationTests(_ServerTestBase):
    def test_join_returns_welcome_and_initial_snapshot(self):
        client, welcome = self.connect("one")
        self.assertEqual(welcome.client_id, 1)
        self.assertEqual(welcome.tick_rate, 100)
        self.assertEqual(welcome.snapshot_hz, 20)
        self.assertEqual(welcome.input_delay, 2)
        self.assertEqual(welcome.max_players, 2)
        self.assertEqual(self.server.by_id[1].name, "one")

        _wait_for_object(client, 1)
        self.assertEqual(client.state.objects[1].sprite, "player.png")

    def test_input_moves_object_and_snapshot_delta_applies(self):
        client, welcome = self.connect()
        _wait_for_object(client, welcome.client_id)

        client.send_input(protocol.make_input_frame(0, 0, {"D"}))
        _wait_for(
            lambda: self.world.objects[welcome.client_id].x >= 1.0,
            description="сервер применил ввод",
        )

        def delta_shows_motion():
            client.pump(0.005)
            state = client.state.objects.get(welcome.client_id)
            snapshot = client.last_snapshot
            return (
                state is not None
                and state.x > 0.5
                and snapshot is not None
                and snapshot.base_tick != 0
            )

        _wait_for(delta_shows_motion, description="дельта-снапшот после движения")
        self.assertGreaterEqual(
            self.world.applied.count((welcome.client_id, 1)), 1
        )

    def test_correct_claim_is_accepted_silently(self):
        client, welcome = self.connect()
        _wait_for_object(client, welcome.client_id)
        frame = protocol.make_input_frame(
            0,
            0,
            set(),
            claims=((welcome.client_id, 0.0, 0.0, 0.0, 0),),
        )
        client.send_input(frame)

        session = self.server.by_id[welcome.client_id]
        _wait_for(
            lambda: session.claims_checked >= 1,
            description="сервер проверил заявку",
        )
        for _ in range(10):
            client.pump(0.005)
        self.assertEqual(session.desyncs, 0)
        self.assertEqual(len(client.corrections), 0)

    def test_forged_claim_gets_correction_and_desync(self):
        client, welcome = self.connect()
        _wait_for_object(client, welcome.client_id)
        client.send_input(
            protocol.make_input_frame(
                0,
                0,
                set(),
                claims=((welcome.client_id, 999.0, -999.0, 0.0, 0),),
            )
        )

        def got_correction():
            client.pump(0.005)
            return list(client.corrections)

        corrections = _wait_for(got_correction, description="CORRECTION")
        session = self.server.by_id[welcome.client_id]
        self.assertGreaterEqual(session.desyncs, 1)
        self.assertGreaterEqual(session.claims_checked, 1)
        correction = corrections[0]
        self.assertIsInstance(correction, Correction)
        self.assertEqual(correction.render_id, welcome.client_id)
        self.assertEqual(correction.reason, int(CorrectionReason.DESYNC))
        self.assertLess(abs(correction.x), 10.0)
        self.assertLess(abs(correction.y), 10.0)
        self.assertTrue(client.connected)
        self.assertEqual(self.server.policy.mode, "log")

    def test_kick_disconnects_client(self):
        client, welcome = self.connect()
        _wait_for_object(client, welcome.client_id)

        self.server.submit(lambda: self.server.kick(welcome.client_id, "test kick"))
        _wait_for(lambda: not client.pump(0.01), description="клиент отключён")
        self.assertIsNotNone(client.rejected)
        self.assertEqual(client.rejected[1], "test kick")
        self.assertNotIn(welcome.client_id, self.server.by_id)
        self.assertTrue(client.connected is False)

    def test_wrong_scene_hash_rejected(self):
        client = NetClient(
            "127.0.0.1",
            self.server.port,
            name="wrong",
            scene_hash=0x12345678,
        )
        self.clients.append(client)
        with self.assertRaises(ConnectionError):
            client.connect()

    def test_anticheat_mode_switches_at_runtime(self):
        self.assertEqual(self.server.policy.mode, "log")
        self.server.submit(lambda: self.server.set_anticheat("kick"))
        _wait_for(
            lambda: self.server.policy.mode == "kick",
            description="режим kick",
        )
        self.server.submit(lambda: self.server.set_anticheat("log"))
        _wait_for(
            lambda: self.server.policy.mode == "log",
            description="режим log",
        )


class ClaimAlignmentUnitTests(unittest.TestCase):
    """Сверка заявок не зависит от часов, джиттера и повторов ввода.

    Сравнивается приращение, которое заявил клиент, с приращением сервера
    между теми же двумя новыми кадрами. Пропуск ввода (повтор последнего
    кадра) не должен превращаться в desync.
    """

    def setUp(self):
        self.world = _TestWorld()
        self.server = GameServer(
            "127.0.0.1", 0, max_players=1, world=self.world, verbose=False
        )
        self.server._running = True
        self.world.on_join(1)
        self.sent: list[bytes] = []

        class _FakeConnection:
            closed = False

            def __init__(self, sink):
                self._sink = sink

            def send_payload(self, payload, **kwargs):
                self._sink.append(payload)
                return True

        self.session = types.SimpleNamespace(
            client_id=1,
            ready=True,
            owner_render_id=1,
            last_executed_seq=0,
            claims_checked=0,
            desyncs=0,
            violations=0,
            last_error=0.0,
            input_queue=InputQueue(),
            connection=_FakeConnection(self.sent),
        )
        self.session.last_world = self.server._capture_claim_state(
            self.session
        )
        self.session.last_claim = None
        self.server.sessions = {self.session.connection: self.session}
        self.server.by_id = {1: self.session}

    def tearDown(self):
        self.server.listener.close()

    def _tick(self, tick):
        executed = self.server._apply_inputs(tick)
        self.world.step(tick)
        self.server._validate_claims(tick, executed)

    def _push(self, seq, claim, keys=("D",)):
        self.session.input_queue.push(
            protocol.make_input_frame(
                0, seq, set(keys), claims=((1, *claim, 0),)
            )
        )

    def test_claim_after_repeat_still_matches(self):
        self._push(1, (0.0, 0.0, 0.0))
        self._tick(1)
        self.assertEqual((self.world.objects[1].x, self.world.objects[1].y),
                         (1.0, 0.0))

        # Кадр 2 не пришёл: сервер повторяет ввод и уходит на тик вперёд.
        self._tick(2)
        self.assertEqual(self.world.objects[1].x, 2.0)

        # Клиент всё это время симулировал локально, поэтому к моменту
        # отправки кадра 2 его предсказание уже равно 2.0.
        self._push(2, (2.0, 0.0, 0.0))
        self._tick(3)

        self.assertEqual(self.session.desyncs, 0)
        self.assertEqual(self.session.claims_checked, 2)
        self.assertEqual(self.sent, [])

    def test_random_frame_pacing_has_no_false_desyncs(self):
        import random

        rng = random.Random(7)
        client_pos = 0.0
        seq = 0
        for tick in range(1, 301):
            send = tick == 1 or rng.random() < 0.9
            if send:
                seq += 1
                # Заявка — состояние до применения этого кадра.
                self._push(seq, (client_pos, 0.0, 0.0))
            self._tick(tick)
            # Локальная симуляция идёт каждый тик, даже если кадр не
            # отправлен: сеть могла отстать, но предсказание продолжается.
            # Сервер в это время повторяет последний ввод, поэтому
            # приращения клиента и сервера совпадают.
            client_pos += 1.0

        self.assertEqual(self.session.desyncs, 0)
        self.assertGreater(self.session.claims_checked, 200)
        self.assertEqual(self.sent, [])

    def test_delayed_frames_do_not_cause_false_desyncs(self):
        """Джиттер доставки сдвигает применение на 1-3 тика.

        Клиент шлёт кадр каждый тик, но сервер может получить его позже и
        повторить предыдущий ввод. Приращение сервера тогда больше
        клиентского на число повторов; это сетевой джиттер, а не читерство.
        """

        import random

        rng = random.Random(11)
        client_pos = 0.0
        seq = 0
        pending: list[tuple[int, object]] = []
        for tick in range(1, 301):
            seq += 1
            frame = protocol.make_input_frame(
                0, seq, {"D"}, claims=((1, client_pos, 0.0, 0.0, 0),)
            )
            pending.append((tick + rng.choice([0, 0, 1, 1, 2, 3]), frame))
            client_pos += 1.0
            for arrival, queued in list(pending):
                if arrival <= tick:
                    self.session.input_queue.push(queued)
                    pending.remove((arrival, queued))
            self._tick(tick)

        self.assertEqual(self.session.desyncs, 0)
        self.assertGreater(self.session.claims_checked, 200)
        self.assertEqual(self.sent, [])

    def test_forged_claim_is_corrected(self):
        self._push(1, (0.0, 0.0, 0.0))
        self._tick(1)

        self._push(2, (999.0, -999.0, 0.0))
        self._tick(2)

        self.assertEqual(self.session.desyncs, 1)
        self.assertEqual(len(self.sent), 1)
        correction = protocol.decode_correction_message(self.sent[0])
        self.assertEqual(correction.render_id, 1)
        self.assertEqual(correction.reason, int(CorrectionReason.DESYNC))


class ServerPlayerLimitTests(_ServerTestBase):
    max_players = 1

    def test_second_client_is_rejected_as_full(self):
        first, welcome = self.connect("first")
        _wait_for_object(first, welcome.client_id)

        second = NetClient("127.0.0.1", self.server.port, name="second")
        self.clients.append(second)
        with self.assertRaises(ConnectionError) as caught:
            second.connect()
        self.assertIn("Сервер заполнен", str(caught.exception))

    def test_max_players_is_not_hardcoded(self):
        self.assertEqual(self.server.max_players, 1)


if __name__ == "__main__":
    unittest.main()
