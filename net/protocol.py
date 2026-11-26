"""Бинарный протокол pyengine-net.

Горячий путь (INPUT / SNAPSHOT / CORRECTION) кодируется только ``struct``:
никакого JSON, ``pickle`` или исполняемого кода. JSON допустим лишь в
``EVENT`` и никогда не передаётся в ``eval``.

Кадр на проводе::

    uint32 length | header (8 байт) | body

Заголовок: ``!BBHI`` — тип, флаги, seq, tick.
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from .config import (
    MAX_CLAIMS_PER_FRAME,
    MAX_CLICKS_PER_FRAME,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
)
from .keys import BITSET_BYTES, MAX_EXTRA_KEYS, MAX_KEY_BYTES, pack_keys

HEADER = struct.Struct("!BBII")
HEADER_SIZE = HEADER.size

MAX_EVENT_BYTES = 8 * 1024
MAX_SPRITE_BYTES = 1024


class MessageType(IntEnum):
    HELLO = 1
    WELCOME = 2
    REJECT = 3
    INPUT = 4
    SNAPSHOT = 5
    CORRECTION = 6
    EVENT = 7
    RESYNC = 8
    PING = 9
    PONG = 10
    ACK = 11
    BYE = 12


class FrameFlags(IntFlag):
    MORE = 1
    RESET = 2
    RELIABLE = 4


class RejectCode(IntEnum):
    VERSION = 1
    FULL = 2
    AUTH = 3
    SCENE = 4
    PROTOCOL = 5
    TIMEOUT = 6
    BAD_INPUT = 7


class CorrectionReason(IntEnum):
    DESYNC = 0
    TELEPORT = 1
    SPEED = 2
    OWNERSHIP = 3
    RESYNC = 4
    SERVER_STATE = 5


class ProtocolError(ValueError):
    """Пакет не соответствует протоколу; соединение нужно закрыть."""


VISUAL_FIELDS = ("x", "y", "rotation", "scale", "alpha", "layer")
MASK_FIELDS = (1 << len(VISUAL_FIELDS)) - 1
MASK_SPRITE = 1 << len(VISUAL_FIELDS)


# --------------------------------------------------------------------------
# Общий заголовок
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ParsedMessage:
    type: MessageType
    flags: int
    seq: int
    tick: int
    body: memoryview


def encode_message(
    kind: MessageType | int,
    *,
    tick: int = 0,
    seq: int = 0,
    flags: int = 0,
    body: bytes = b"",
) -> bytes:
    """Собирает payload: заголовок плюс тело (без префикса длины)."""

    if len(body) + HEADER_SIZE > MAX_FRAME_BYTES:
        raise ProtocolError("Сообщение превышает лимит кадра")
    frame = bytearray(HEADER_SIZE + len(body))
    HEADER.pack_into(
        frame,
        0,
        int(kind) & 0xFF,
        int(flags) & 0xFF,
        int(seq) & 0xFFFFFFFF,
        int(tick) & 0xFFFFFFFF,
    )
    frame[HEADER_SIZE:] = body
    return bytes(frame)


def decode_message(payload) -> ParsedMessage:
    view = memoryview(payload)
    if len(view) < HEADER_SIZE:
        raise ProtocolError("Сообщение короче заголовка")
    msg_type, flags, seq, tick = HEADER.unpack_from(view, 0)
    try:
        kind = MessageType(msg_type)
    except ValueError as error:
        raise ProtocolError(f"Неизвестный тип сообщения: {msg_type}") from error
    return ParsedMessage(kind, flags, seq, tick, view[HEADER_SIZE:])


def _need(data: memoryview, offset: int, size: int) -> None:
    if offset + size > len(data):
        raise ProtocolError("Сообщение обрезано")


def _put_str8(out: bytearray, text: str) -> None:
    raw = str(text).encode("utf-8")
    if len(raw) > 255:
        raise ProtocolError("Строка длиннее 255 байт")
    out.append(len(raw))
    out += raw


def _read_str8(data: memoryview, offset: int) -> tuple[str, int]:
    if offset >= len(data):
        raise ProtocolError("Нет длины строки")
    length = data[offset]
    offset += 1
    _need(data, offset, length)
    text = bytes(data[offset:offset + length]).decode("utf-8", "replace")
    return text, offset + length


# --------------------------------------------------------------------------
# HELLO / WELCOME / REJECT / BYE
# --------------------------------------------------------------------------

HELLO_FIXED = struct.Struct("!HHIIBB")


@dataclass(slots=True)
class Hello:
    version: int
    flags: int
    scene_hash: int
    scripts_hash: int
    name: str
    token: str


def encode_hello(
    *,
    name: str = "",
    scene_hash: int = 0,
    scripts_hash: int = 0,
    token: str = "",
    flags: int = 0,
) -> bytes:
    name_raw = name.encode("utf-8")
    token_raw = token.encode("utf-8")
    if len(name_raw) > 255 or len(token_raw) > 255:
        raise ProtocolError("Имя или токен длиннее 255 байт")
    body = bytearray(HELLO_FIXED.size)
    HELLO_FIXED.pack_into(
        body,
        0,
        PROTOCOL_VERSION,
        flags,
        scene_hash & 0xFFFFFFFF,
        scripts_hash & 0xFFFFFFFF,
        len(name_raw),
        len(token_raw),
    )
    body += name_raw
    body += token_raw
    return encode_message(MessageType.HELLO, body=bytes(body))


def decode_hello(body: memoryview) -> Hello:
    _need(body, 0, HELLO_FIXED.size)
    version, flags, scene_hash, scripts_hash, name_len, token_len = (
        HELLO_FIXED.unpack_from(body, 0)
    )
    offset = HELLO_FIXED.size
    _need(body, offset, name_len + token_len)
    name = bytes(body[offset:offset + name_len]).decode("utf-8", "replace")
    offset += name_len
    token = bytes(body[offset:offset + token_len]).decode("utf-8", "replace")
    return Hello(version, flags, scene_hash, scripts_hash, name, token)


WELCOME_FIXED = struct.Struct("!HHBBHII")


@dataclass(slots=True)
class Welcome:
    client_id: int
    tick_rate: int
    snapshot_hz: int
    input_delay: int
    max_players: int
    world_seed: int
    # render_id объекта, которым владеет этот клиент. Клиент использует его
    # для предсказания; 0 — сервер не выдал слот (наблюдатель).
    owner_render_id: int = 0


def encode_welcome(welcome: Welcome, *, server_tick: int = 0) -> bytes:
    body = WELCOME_FIXED.pack(
        welcome.client_id & 0xFFFF,
        welcome.tick_rate,
        welcome.snapshot_hz,
        welcome.input_delay,
        welcome.max_players,
        welcome.world_seed & 0xFFFFFFFF,
        welcome.owner_render_id & 0xFFFFFFFF,
    )
    return encode_message(MessageType.WELCOME, tick=server_tick, body=body)


def decode_welcome(body: memoryview) -> Welcome:
    _need(body, 0, WELCOME_FIXED.size)
    return Welcome(*WELCOME_FIXED.unpack_from(body, 0))


REJECT_FIXED = struct.Struct("!HH")


def encode_reject(code: RejectCode | int, text: str = "", *, kind=MessageType.REJECT) -> bytes:
    text_raw = text.encode("utf-8")[:1024]
    body = REJECT_FIXED.pack(int(code) & 0xFFFF, len(text_raw)) + text_raw
    return encode_message(kind, body=body)


def decode_reject(body: memoryview) -> tuple[int, str]:
    _need(body, 0, REJECT_FIXED.size)
    code, length = REJECT_FIXED.unpack_from(body, 0)
    offset = REJECT_FIXED.size
    _need(body, offset, length)
    text = bytes(body[offset:offset + length]).decode("utf-8", "replace")
    return code, text


# --------------------------------------------------------------------------
# INPUT
# --------------------------------------------------------------------------

INPUT_BODY = struct.Struct("!32sffBBH")
CLICK_STRUCT = struct.Struct("!IB")
CLAIM_STRUCT = struct.Struct("!IfffI")


@dataclass(slots=True)
class InputFrame:
    tick: int = 0
    seq: int = 0
    keys: bytes = bytes(BITSET_BYTES)
    mouse_x: float = 0.0
    mouse_y: float = 0.0
    buttons: int = 0
    clicks: tuple[tuple[int, int], ...] = ()
    claims: tuple[tuple[int, float, float, float, int], ...] = ()
    extra_keys: tuple[str, ...] = ()


def state_crc(
    x: float,
    y: float,
    rotation: float,
    scale: float = 1.0,
    alpha: float = 1.0,
) -> int:
    """CRC предсказанного состояния с квантованием.

    Служит диагностике детерминизма: сервер сравнивает позиции с допуском, а
    CRC позволяет отличить «идеально совпало» от «совпало в пределах допуска».
    """

    def quantize(value: float, factor: int) -> int:
        if not math.isfinite(value):
            return 0
        return max(-2_000_000_000, min(2_000_000_000, int(round(value * factor))))

    packed = struct.pack(
        "!iiiii",
        quantize(x, 1000),
        quantize(y, 1000),
        quantize(rotation, 100),
        quantize(scale, 1000),
        quantize(alpha, 1000),
    )
    return zlib.crc32(packed) & 0xFFFFFFFF


def encode_input_message(frame: InputFrame) -> bytes:
    if len(frame.keys) != BITSET_BYTES:
        raise ProtocolError("Маска клавиш должна быть 32 байта")
    if len(frame.clicks) > MAX_CLICKS_PER_FRAME:
        raise ProtocolError("Слишком много кликов в кадре ввода")
    if len(frame.claims) > MAX_CLAIMS_PER_FRAME:
        raise ProtocolError("Слишком много заявок в кадре ввода")
    if len(frame.extra_keys) > MAX_EXTRA_KEYS:
        raise ProtocolError("Слишком много неизвестных клавиш")

    body = bytearray(INPUT_BODY.size)
    INPUT_BODY.pack_into(
        body,
        0,
        frame.keys,
        float(frame.mouse_x),
        float(frame.mouse_y),
        int(frame.buttons) & 0xFF,
        len(frame.clicks),
        len(frame.claims),
    )
    for render_id, button in frame.clicks:
        body += CLICK_STRUCT.pack(int(render_id) & 0xFFFFFFFF, int(button) & 0xFF)
    for render_id, x, y, rotation, crc in frame.claims:
        body += CLAIM_STRUCT.pack(
            int(render_id) & 0xFFFFFFFF,
            float(x),
            float(y),
            float(rotation),
            int(crc) & 0xFFFFFFFF,
        )
    body.append(len(frame.extra_keys))
    for key in frame.extra_keys:
        _put_str8(body, key)
    return encode_message(
        MessageType.INPUT, tick=frame.tick, seq=frame.seq, body=bytes(body)
    )


def make_input_frame(
    tick: int,
    seq: int,
    keys,
    *,
    mouse_x: float = 0.0,
    mouse_y: float = 0.0,
    buttons: int = 0,
    clicks=(),
    claims=(),
) -> InputFrame:
    """Собирает кадр ввода из имён клавиш (удобно для скриптов и тестов)."""

    bits, extra = pack_keys(keys)
    return InputFrame(
        tick=tick,
        seq=seq,
        keys=bits,
        mouse_x=mouse_x,
        mouse_y=mouse_y,
        buttons=buttons,
        clicks=tuple(clicks),
        claims=tuple(claims),
        extra_keys=extra,
    )


def decode_input_message(payload) -> InputFrame:
    message = decode_message(payload)
    if message.type is not MessageType.INPUT:
        raise ProtocolError("Ожидался INPUT")
    data = message.body
    _need(data, 0, INPUT_BODY.size)
    keys, mouse_x, mouse_y, buttons, n_clicks, n_claims = INPUT_BODY.unpack_from(data, 0)
    if n_clicks > MAX_CLICKS_PER_FRAME or n_claims > MAX_CLAIMS_PER_FRAME:
        raise ProtocolError("Недопустимое число элементов ввода")
    offset = INPUT_BODY.size

    clicks = []
    for _ in range(n_clicks):
        _need(data, offset, CLICK_STRUCT.size)
        clicks.append(CLICK_STRUCT.unpack_from(data, offset))
        offset += CLICK_STRUCT.size

    claims = []
    for _ in range(n_claims):
        _need(data, offset, CLAIM_STRUCT.size)
        claims.append(CLAIM_STRUCT.unpack_from(data, offset))
        offset += CLAIM_STRUCT.size

    if offset >= len(data):
        raise ProtocolError("Нет счётчика дополнительных клавиш")
    n_extra = data[offset]
    offset += 1
    if n_extra > MAX_EXTRA_KEYS:
        raise ProtocolError("Слишком много дополнительных клавиш")
    extra = []
    for _ in range(n_extra):
        if offset >= len(data):
            raise ProtocolError("Нет длины клавиши")
        length = data[offset]
        offset += 1
        if length > MAX_KEY_BYTES:
            raise ProtocolError("Слишком длинное имя клавиши")
        _need(data, offset, length)
        extra.append(bytes(data[offset:offset + length]).decode("utf-8", "replace"))
        offset += length

    return InputFrame(
        tick=message.tick,
        seq=message.seq,
        keys=bytes(keys),
        mouse_x=mouse_x,
        mouse_y=mouse_y,
        buttons=buttons,
        clicks=tuple(clicks),
        claims=tuple(claims),
        extra_keys=tuple(extra),
    )


# --------------------------------------------------------------------------
# SNAPSHOT
# --------------------------------------------------------------------------

SNAPSHOT_BODY = struct.Struct("!IIHHH")
DESPAWN_STRUCT = struct.Struct("!I")
SPAWN_STRUCT = struct.Struct("!IffffffH")
RECORD_STRUCT = struct.Struct("!IB")


@dataclass(slots=True)
class SpawnRecord:
    render_id: int
    sprite: str
    layer: float = 0.0
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    scale: float = 1.0
    alpha: float = 1.0


@dataclass(slots=True)
class StateRecord:
    render_id: int
    mask: int = 0
    values: tuple[float, ...] = ()
    sprite: str | None = None


@dataclass(slots=True)
class Snapshot:
    tick: int = 0
    base_tick: int = 0
    ack_seq: int = 0
    flags: int = 0
    despawns: tuple[int, ...] = ()
    spawns: tuple[SpawnRecord, ...] = ()
    records: tuple[StateRecord, ...] = ()


def make_state_record(
    render_id: int,
    fields: dict[str, float],
    sprite: str | None = None,
) -> StateRecord:
    mask = 0
    values = []
    for index, name in enumerate(VISUAL_FIELDS):
        if name in fields:
            mask |= 1 << index
            values.append(float(fields[name]))
    if sprite is not None:
        mask |= MASK_SPRITE
    return StateRecord(render_id, mask, tuple(values), sprite)


def _count_mask_fields(mask: int) -> int:
    return bin(mask & MASK_FIELDS).count("1")


def encode_snapshot_message(snapshot: Snapshot) -> bytes:
    despawns = tuple(sorted(int(item) for item in snapshot.despawns))
    spawns = tuple(sorted(snapshot.spawns, key=lambda item: item.render_id))
    records = tuple(sorted(snapshot.records, key=lambda item: item.render_id))

    body = bytearray(SNAPSHOT_BODY.size)
    SNAPSHOT_BODY.pack_into(
        body,
        0,
        snapshot.base_tick & 0xFFFFFFFF,
        snapshot.ack_seq & 0xFFFFFFFF,
        len(despawns),
        len(spawns),
        len(records),
    )
    for render_id in despawns:
        body += DESPAWN_STRUCT.pack(render_id & 0xFFFFFFFF)
    for spawn in spawns:
        sprite_raw = spawn.sprite.encode("utf-8")
        if len(sprite_raw) > MAX_SPRITE_BYTES:
            raise ProtocolError("Путь спрайта слишком длинный")
        body += SPAWN_STRUCT.pack(
            spawn.render_id & 0xFFFFFFFF,
            float(spawn.layer),
            float(spawn.x),
            float(spawn.y),
            float(spawn.rotation),
            float(spawn.scale),
            float(spawn.alpha),
            len(sprite_raw),
        )
        body += sprite_raw
    for record in records:
        body += RECORD_STRUCT.pack(record.render_id & 0xFFFFFFFF, record.mask & 0xFF)
        if _count_mask_fields(record.mask) != len(record.values):
            raise ProtocolError("Маска записи не совпадает с числом значений")
        for value in record.values:
            body += struct.pack("!f", float(value))
        if record.mask & MASK_SPRITE:
            sprite_raw = (record.sprite or "").encode("utf-8")
            if len(sprite_raw) > MAX_SPRITE_BYTES:
                raise ProtocolError("Путь спрайта слишком длинный")
            body += struct.pack("!H", len(sprite_raw))
            body += sprite_raw
    return encode_message(
        MessageType.SNAPSHOT,
        tick=snapshot.tick,
        flags=snapshot.flags,
        body=bytes(body),
    )


def decode_snapshot_message(payload) -> Snapshot:
    message = decode_message(payload)
    if message.type is not MessageType.SNAPSHOT:
        raise ProtocolError("Ожидался SNAPSHOT")
    data = message.body
    _need(data, 0, SNAPSHOT_BODY.size)
    base_tick, ack_seq, n_despawns, n_spawns, n_records = SNAPSHOT_BODY.unpack_from(
        data, 0
    )
    minimum = (
        SNAPSHOT_BODY.size
        + n_despawns * DESPAWN_STRUCT.size
        + n_spawns * SPAWN_STRUCT.size
        + n_records * RECORD_STRUCT.size
    )
    if minimum > len(data):
        raise ProtocolError("Снапшот обрезан")
    offset = SNAPSHOT_BODY.size

    despawns = []
    for _ in range(n_despawns):
        despawns.append(DESPAWN_STRUCT.unpack_from(data, offset)[0])
        offset += DESPAWN_STRUCT.size

    spawns = []
    for _ in range(n_spawns):
        _need(data, offset, SPAWN_STRUCT.size)
        (
            render_id,
            layer,
            x,
            y,
            rotation,
            scale,
            alpha,
            sprite_len,
        ) = SPAWN_STRUCT.unpack_from(data, offset)
        offset += SPAWN_STRUCT.size
        if sprite_len > MAX_SPRITE_BYTES:
            raise ProtocolError("Слишком длинный путь спрайта")
        _need(data, offset, sprite_len)
        sprite = bytes(data[offset:offset + sprite_len]).decode("utf-8", "replace")
        offset += sprite_len
        spawns.append(
            SpawnRecord(
                render_id, sprite, layer, x, y, rotation, scale, alpha
            )
        )

    records = []
    for _ in range(n_records):
        _need(data, offset, RECORD_STRUCT.size)
        render_id, mask = RECORD_STRUCT.unpack_from(data, offset)
        offset += RECORD_STRUCT.size
        values = []
        for _ in range(_count_mask_fields(mask)):
            _need(data, offset, 4)
            values.append(struct.unpack_from("!f", data, offset)[0])
            offset += 4
        sprite = None
        if mask & MASK_SPRITE:
            _need(data, offset, 2)
            sprite_len = struct.unpack_from("!H", data, offset)[0]
            offset += 2
            if sprite_len > MAX_SPRITE_BYTES:
                raise ProtocolError("Слишком длинный путь спрайта")
            _need(data, offset, sprite_len)
            sprite = bytes(data[offset:offset + sprite_len]).decode(
                "utf-8", "replace"
            )
            offset += sprite_len
        records.append(StateRecord(render_id, mask, tuple(values), sprite))

    return Snapshot(
        tick=message.tick,
        base_tick=base_tick,
        ack_seq=ack_seq,
        flags=message.flags,
        despawns=tuple(despawns),
        spawns=tuple(spawns),
        records=tuple(records),
    )


# --------------------------------------------------------------------------
# CORRECTION / EVENT / RESYNC / ACK / PING-PONG
# --------------------------------------------------------------------------

CORRECTION_BODY = struct.Struct("!IfffffIB")


@dataclass(slots=True)
class Correction:
    render_id: int = 0
    tick: int = 0
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    scale: float = 1.0
    alpha: float = 1.0
    last_ack_seq: int = 0
    reason: int = CorrectionReason.DESYNC


def encode_correction_message(correction: Correction) -> bytes:
    body = CORRECTION_BODY.pack(
        correction.render_id & 0xFFFFFFFF,
        float(correction.x),
        float(correction.y),
        float(correction.rotation),
        float(correction.scale),
        float(correction.alpha),
        correction.last_ack_seq & 0xFFFFFFFF,
        int(correction.reason) & 0xFF,
    )
    return encode_message(MessageType.CORRECTION, tick=correction.tick, body=body)


def decode_correction_message(payload) -> Correction:
    message = decode_message(payload)
    if message.type is not MessageType.CORRECTION:
        raise ProtocolError("Ожидался CORRECTION")
    _need(message.body, 0, CORRECTION_BODY.size)
    (
        render_id,
        x,
        y,
        rotation,
        scale,
        alpha,
        last_ack_seq,
        reason,
    ) = CORRECTION_BODY.unpack_from(message.body, 0)
    return Correction(
        render_id=render_id,
        tick=message.tick,
        x=x,
        y=y,
        rotation=rotation,
        scale=scale,
        alpha=alpha,
        last_ack_seq=last_ack_seq,
        reason=reason,
    )


def encode_event_message(name: str, payload: dict | None = None) -> bytes:
    raw = json.dumps(
        payload or {}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(raw) > MAX_EVENT_BYTES:
        raise ProtocolError("Слишком большой EVENT")
    name_raw = str(name).encode("utf-8")[:255]
    body = bytes((len(name_raw),)) + name_raw + struct.pack("!H", len(raw)) + raw
    return encode_message(MessageType.EVENT, body=body)


def decode_event_message(payload) -> tuple[str, dict]:
    message = decode_message(payload)
    if message.type is not MessageType.EVENT:
        raise ProtocolError("Ожидался EVENT")
    body = message.body
    if len(body) < 1:
        raise ProtocolError("Пустой EVENT")
    name_len = body[0]
    offset = 1
    _need(body, offset, name_len + 2)
    name = bytes(body[offset:offset + name_len]).decode("utf-8", "replace")
    offset += name_len
    payload_len = struct.unpack_from("!H", body, offset)[0]
    offset += 2
    _need(body, offset, payload_len)
    try:
        event_payload = json.loads(
            bytes(body[offset:offset + payload_len]).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError) as error:
        raise ProtocolError("Некорректный JSON в EVENT") from error
    if not isinstance(event_payload, dict):
        event_payload = {"value": event_payload}
    return name, event_payload


U32_STRUCT = struct.Struct("!I")


def encode_resync_message(last_tick: int) -> bytes:
    return encode_message(
        MessageType.RESYNC, tick=last_tick, body=U32_STRUCT.pack(last_tick & 0xFFFFFFFF)
    )


def decode_resync_message(body: memoryview) -> int:
    _need(body, 0, U32_STRUCT.size)
    return U32_STRUCT.unpack_from(body, 0)[0]


def encode_ack_message(tick: int) -> bytes:
    return encode_message(
        MessageType.ACK, tick=tick, body=U32_STRUCT.pack(tick & 0xFFFFFFFF)
    )


def decode_ack_message(body: memoryview) -> int:
    _need(body, 0, U32_STRUCT.size)
    return U32_STRUCT.unpack_from(body, 0)[0]


TIME_STRUCT = struct.Struct("!QQQQ")


def encode_time_message(
    kind: MessageType,
    t0: int = 0,
    t1: int = 0,
    t2: int = 0,
    t3: int = 0,
) -> bytes:
    return encode_message(kind, body=TIME_STRUCT.pack(t0, t1, t2, t3))


def encode_ping(t0: int = 0) -> bytes:
    return encode_time_message(MessageType.PING, t0)


def encode_pong(t0: int = 0, t1: int = 0, t2: int = 0, t3: int = 0) -> bytes:
    return encode_time_message(MessageType.PONG, t0, t1, t2, t3)


def decode_time_message(body: memoryview) -> tuple[int, int, int, int]:
    _need(body, 0, TIME_STRUCT.size)
    return TIME_STRUCT.unpack_from(body, 0)
