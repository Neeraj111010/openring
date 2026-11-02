"""SIP trunks, in Vapi's shape: calls through any carrier that speaks SIP, with no Twilio in between.

A `byo-sip-trunk` credential names the carrier's gateways and login, and a `byo-phone-number` number uses it. OpenRing
talks SIP over UDP itself (RFC 3261, as much of it as a phone call needs) and carries the audio as RTP in G.711, the
codec every carrier has. There's no dependency, and nothing listens until the first trunk is saved.

A server on the internet that answers SIP gets probed within minutes, by people looking for a way to make calls on
someone else's bill. So nothing is answered unless it comes from a gateway of a saved trunk (others are dropped
without a word), and a call only starts, and only costs anything, on an ACK, which needs the tag from our answer, so
a forged source address can't start one.

SIP needs what a web tunnel can't give: UDP from the carrier to this computer, on the SIP port and the RTP ports.
"""

from __future__ import annotations

import asyncio
import audioop
import hashlib
import random
import re
import secrets
import socket
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from . import netguard

MAX_MESSAGE_BYTES = 65_000
MAX_SESSIONS = 500
T1 = 0.5  # RFC 3261's retransmission timer
TRANSACTION_SECONDS = 32  # its 64*T1: how long a request is resent for
RING_SECONDS = 60  # how long an outgoing call rings before it's given up on
REGISTER_SECONDS = 300
RETRY_REGISTER_SECONDS = 30
REFRESH_SECONDS = 300  # gateways are hostnames: their addresses are looked up again this often
SAMPLE_RATE = 8000
PACKET_SECONDS = 0.02
PACKET_BYTES = 160  # 20 ms of G.711
MAX_QUEUED_PACKETS = 250  # 5 s of the caller's audio waiting for the pipeline; past that it's dropped
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
           "c": "content-type", "k": "supported", "o": "event"}
CODECS = {"PCMU": 0, "PCMA": 8}  # the static RTP payload types of G.711, most used first
SILENCE = {0: b"\xff", 8: b"\xd5"}
BUSY = {486, 600}
NO_ANSWER = {408, 480, 487}
NUMBER = re.compile(r"\+?[0-9*#]{2,32}")
SIP_URI = re.compile(r"sips?:(?P<user>[^\s@<>\";]+)@(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:(?P<port>\d{1,5}))?"
                     r"(;[^\s<>\"]*)?")


class SipError(ValueError):
    """A trunk or a call that SIP can't do."""


def tag() -> str:
    return secrets.token_hex(6)


def user_of(address: str) -> str:
    """The number in `sip:+15551234567@carrier.example` (also what Twilio and Telnyx send for a SIP call), or the
    address as it was given."""
    found = re.search(r"sips?:([^@;>\s]+)@", address or "")
    return found.group(1) if found else address


@dataclass
class Message:
    """One SIP request or response."""

    first: str
    headers: list[tuple[str, str]]
    body: bytes = b""

    @property
    def is_response(self) -> bool:
        return self.first.startswith("SIP/2.0 ")

    @property
    def status(self) -> int:
        return int(self.first.split(" ", 2)[1])

    @property
    def method(self) -> str:
        return self.first.split(" ", 1)[0]

    @property
    def uri(self) -> str:
        return self.first.split(" ", 2)[1]

    def get(self, name: str, default: str = "") -> str:
        name = name.lower()
        return next((v for k, v in self.headers if k == name), default)

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name.lower()]

    @property
    def cseq(self) -> int:
        found = re.match(r"\s*(\d+)", self.get("cseq"))
        return int(found.group(1)) if found else 0

    @property
    def cseq_method(self) -> str:
        return self.get("cseq").rsplit(" ", 1)[-1].upper()

    @property
    def branch(self) -> str:
        found = re.search(r";\s*branch=([^;,\s]+)", self.get("via"), re.I)
        return found.group(1) if found else ""


def parse(data: bytes) -> Message | None:
    """A datagram as a SIP message; None for anything else, whatever it holds."""
    if len(data) > MAX_MESSAGE_BYTES:
        return None
    head, _, body = data.partition(b"\r\n\r\n")
    try:
        lines = head.decode("utf-8", "replace").split("\r\n")
    except ValueError:
        return None
    first = lines[0]
    if not re.fullmatch(r"SIP/2\.0 \d{3}( .*)?|[A-Z]{3,12} \S+ SIP/2\.0", first):
        return None
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and headers:  # a header continued on the next line
            headers[-1] = (headers[-1][0], f"{headers[-1][1]} {line.strip()}")
            continue
        name, separator, value = line.partition(":")
        if not separator:
            return None
        name = name.strip().lower()
        headers.append((COMPACT.get(name, name), value.strip()))
    message = Message(first, headers, body)
    length = message.get("content-length")
    if length.isdigit():
        message.body = body[:int(length)]
    return message


def _tag_of(header: str) -> str:
    found = re.search(r";\s*tag=([^;\s]+)", header, re.I)
    return found.group(1) if found else ""


def _uri_in(header: str) -> str:
    """The address in `"Name" <sip:a@b>;tag=x`."""
    found = re.search(r"<([^>]*)>", header)
    return found.group(1) if found else header.split(";", 1)[0].strip()


def _host_port(uri: str) -> tuple[str, int] | None:
    found = re.search(r"sips?:(?:[^@]*@)?(\[[^\]]+\]|[^:;>\s]+)(?::(\d+))?", uri)
    if not found:
        return None
    return found.group(1).strip("[]"), int(found.group(2) or 5060)


def digest(challenge: str, method: str, uri: str, username: str, password: str) -> str:
    """The answer to a carrier's 401 or 407: RFC 8760 digest, MD5 unless it asks for SHA-256."""
    fields = {k.lower(): v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|[^,\s]+)', challenge)}
    realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
    algorithm = fields.get("algorithm", "MD5")
    if algorithm.upper() not in ("MD5", "SHA-256"):
        raise SipError(f"the carrier asks for digest algorithm {algorithm}, which isn't supported")
    hashed = hashlib.sha256 if algorithm.upper() == "SHA-256" else hashlib.md5

    def h(text: str) -> str:
        return hashed(text.encode()).hexdigest()

    ha1, ha2 = h(f"{username}:{realm}:{password}"), h(f"{method}:{uri}")
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"',
             f"algorithm={algorithm}"]
    if "auth" in re.split(r"[,\s]+", fields.get("qop", "")):
        count, cnonce = "00000001", secrets.token_hex(8)
        response = h(f"{ha1}:{nonce}:{count}:{cnonce}:auth:{ha2}")
        parts += ["qop=auth", f"nc={count}", f'cnonce="{cnonce}"']
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")
    parts.append(f'response="{response}"')
    if fields.get("opaque"):
        parts.append(f'opaque="{fields["opaque"]}"')
    return "Digest " + ", ".join(parts)


@dataclass
class Offer:
    """What the other side's SDP says about its audio."""

    address: str
    port: int
    payload: int  # the G.711 they and we share
    events: int | None  # their payload type for keypad tones


def read_sdp(body: bytes) -> Offer | None:
    """The audio of an SDP, None when it isn't G.711 at a usable address."""
    text = body.decode("utf-8", "replace")
    media = re.search(r"^m=audio (\d+) RTP/\S+ ([\d ]+)\s*$", text, re.M)
    connection = re.findall(r"^c=IN IP4 ([0-9.]+)\s*$", text, re.M)
    if not media or not connection or media.group(1) == "0":
        return None
    named = {name.upper(): int(number) for number, name in re.findall(r"^a=rtpmap:(\d+) ([\w-]+)/8000", text, re.M)}
    offered = [int(p) for p in media.group(2).split()]
    payload = next((CODECS[c] for c in CODECS if CODECS[c] in offered), None)
    address = connection[-1]
    try:
        # The address the audio goes to comes from the other side: never a cloud's metadata service or a broadcast.
        if netguard.blocked_address(address) or address == "0.0.0.0" or int(address.split(".")[0]) >= 224:
            return None
    except ValueError:
        return None
    if payload is None or not 0 < int(media.group(1)) < 65536:
        return None
    event = named.get("TELEPHONE-EVENT")
    return Offer(address, int(media.group(1)), payload, event if event in offered else None)


def write_sdp(address: str, port: int, payload: int | None, events: int | None = 101) -> bytes:
    """Our audio: `payload` when answering an offer, every G.711 when making one."""
    payloads = [payload] if payload is not None else list(CODECS.values())
    names = {number: name for name, number in CODECS.items()}
    lines = ["v=0", f"o=openring {random.getrandbits(32)} 1 IN IP4 {address}", "s=OpenRing", f"c=IN IP4 {address}",
             "t=0 0", f"m=audio {port} RTP/AVP " + " ".join(map(str, [*payloads, *([events] if events else [])]))]
    lines += [f"a=rtpmap:{p} {names[p]}/8000" for p in payloads]
    if events:
        lines += [f"a=rtpmap:{events} telephone-event/8000", f"a=fmtp:{events} 0-15"]
    lines += ["a=ptime:20", "a=sendrecv"]
    return ("\r\n".join(lines) + "\r\n").encode()


class Rtp(asyncio.DatagramProtocol):
    """One call's audio. Sends a packet every 20 ms, silence included: carriers hang up on a line with no audio."""

    def __init__(self) -> None:
        self.udp: asyncio.DatagramTransport | None = None
        self.port = 0
        self.remote: tuple[str, int] | None = None
        self.allowed: set[str] = set()  # the addresses audio may come from
        self.payload = 0
        self.received: asyncio.Queue[bytes] = asyncio.Queue(MAX_QUEUED_PACKETS)
        self._out = bytearray()
        self._sequence = random.getrandbits(16)
        self._timestamp = random.getrandbits(32)
        self._source = random.getrandbits(32)
        self._sending: asyncio.Task | None = None
        self._talking = False

    def connection_made(self, transport: Any) -> None:
        self.udp = transport

    def error_received(self, exc: Exception) -> None:  # the other side's port closed: its BYE or the limits end it
        logger.debug("RTP error: {}", exc)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr[0] not in self.allowed or len(data) < 12 or data[0] >> 6 != 2 or data[1] & 0x7f != self.payload:
            return
        start = 12 + 4 * (data[0] & 0x0f)
        if data[0] & 0x10:  # a header extension
            if len(data) < start + 4:
                return
            start += 4 + 4 * struct.unpack("!H", data[start + 2:start + 4])[0]
        end = len(data) - (data[-1] if data[0] & 0x20 else 0)
        if not start < end:
            return
        self.remote = addr  # behind a NAT, audio only gets back to where it came from
        if self.received.full():
            return
        self.received.put_nowait(self.decode(data[start:end]))

    def decode(self, payload: bytes) -> bytes:
        return audioop.ulaw2lin(payload, 2) if self.payload == 0 else audioop.alaw2lin(payload, 2)

    def encode(self, pcm: bytes) -> bytes:
        return audioop.lin2ulaw(pcm, 2) if self.payload == 0 else audioop.lin2alaw(pcm, 2)

    def start(self) -> None:
        if self._sending is None:
            self._sending = asyncio.ensure_future(self._send_forever())

    @property
    def buffered(self) -> int:
        return len(self._out)

    def queue(self, pcm: bytes) -> None:
        self._out += self.encode(pcm)

    def clear(self) -> None:
        self._out.clear()

    async def _send_forever(self) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time()
        while self.udp is not None:
            due += PACKET_SECONDS
            wait = due - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            elif wait < -0.2:  # the computer fell behind: catch up by skipping, not with a burst
                due = loop.time()
            chunk = bytes(self._out[:PACKET_BYTES])
            del self._out[:PACKET_BYTES]
            talking = bool(chunk)
            chunk = chunk.ljust(PACKET_BYTES, SILENCE[self.payload])
            second = self.payload | (0x80 if talking and not self._talking else 0)  # marks where speech starts
            self._talking = talking
            header = struct.pack("!BBHII", 0x80, second, self._sequence, self._timestamp, self._source)
            self._sequence = (self._sequence + 1) & 0xffff
            self._timestamp = (self._timestamp + PACKET_BYTES) & 0xffffffff
            if self.remote and self.udp is not None:
                self.udp.sendto(header + chunk, self.remote)

    def close(self) -> None:
        if self._sending is not None:
            self._sending.cancel()
        if self.udp is not None:
            self.udp.close()
            self.udp = None


@dataclass
class Session:
    """One call: its SIP dialog and its audio."""

    server: Server
    call_id: str
    address: tuple[str, int]  # where its SIP messages go
    local_tag: str
    outgoing: bool
    rtp: Rtp | None = None
    remote_tag: str = ""
    local_header: str = ""  # our From/To
    remote_header: str = ""  # their From/To
    remote_target: str = ""  # their Contact: where in-dialog requests are addressed
    routes: list[str] = field(default_factory=list)
    cseq: int = field(default_factory=lambda: random.randint(1, 10_000))
    credential: dict[str, Any] | None = None
    # While it's being set up.
    invite: Message | None = None
    answer: bytes | None = None
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    openring_call: str | None = None  # the call record this session belongs to
    over: bool = False
    on_peer_left: Callable[[], Awaitable[None]] | None = None
    refer_done: asyncio.Future | None = None

    def _in_dialog(self, method: str, extra: list[tuple[str, str]] | None = None) -> tuple[bytes, str]:
        self.cseq += 1
        branch = "z9hG4bK" + tag()
        host, port = self.server.contact_address(self.address)
        target = self.remote_target or _uri_in(self.remote_header)
        headers = [("Via", f"SIP/2.0/UDP {host}:{port};branch={branch};rport"), ("Max-Forwards", "70"),
                   ("From", self.local_header), ("To", self.remote_header), ("Call-ID", self.call_id),
                   ("CSeq", f"{self.cseq} {method}"), *[("Route", r) for r in self.routes], *(extra or []),
                   ("User-Agent", "OpenRing")]
        return _build(f"{method} {target} SIP/2.0", headers), branch

    async def hang_up(self) -> None:
        """End our side of the call. Doing it twice, or after they left, does nothing."""
        if self.over:
            return
        self.over = True
        self.server.forget(self)
        if self.rtp is not None:
            self.rtp.close()
        data, branch = self._in_dialog("BYE")
        try:
            await self.server.request(data, self.address, branch)
        except SipError as exc:
            logger.debug("SIP call {}: no answer to BYE: {}", self.call_id, exc)

    async def peer_left(self) -> None:
        if self.over:
            return
        self.over = True
        self.server.forget(self)
        if self.rtp is not None:
            self.rtp.close()
        if self.on_peer_left is not None:
            await self.on_peer_left()

    async def transfer(self, number: str) -> None:
        """Has the carrier move the call to `number` (SIP REFER). The carrier then ends this side of it."""
        host = (self.server.gateway_of(self.credential) or {}).get("ip") or self.address[0]
        loop = asyncio.get_running_loop()
        self.refer_done = loop.create_future()
        data, branch = self._in_dialog("REFER", [("Refer-To", f"<sip:{number}@{host}>"),
                                                 ("Referred-By", f"<{_uri_in(self.local_header)}>")])
        answer = await self.server.request(data, self.address, branch)
        if not 200 <= answer.status < 300:
            raise SipError(f"the carrier refused the transfer ({answer.first[8:]})")


def _build(first: str, headers: list[tuple[str, str]], body: bytes = b"") -> bytes:
    lines = [first, *[f"{k}: {v}" for k, v in headers]]
    if body:
        lines.append("Content-Type: application/sdp")
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _response(request: Message, status: int, reason: str, addr: tuple[str, int], *, to_tag: str = "",
              extra: list[tuple[str, str]] | None = None, body: bytes = b"") -> bytes:
    vias = request.all("via")
    if vias and re.search(r";\s*rport(?=;|$)", vias[0]):  # answer where the request really came from
        vias[0] = re.sub(r";\s*rport(?=;|$)", f";received={addr[0]};rport={addr[1]}", vias[0], count=1)
    to = request.get("to")
    if to_tag and not _tag_of(to):
        to = f"{to};tag={to_tag}"
    headers = [*[("Via", v) for v in vias], ("From", request.get("from")), ("To", to),
               ("Call-ID", request.get("call-id")), ("CSeq", request.get("cseq")), *(extra or []),
               ("User-Agent", "OpenRing")]
    return _build(f"SIP/2.0 {status} {reason}", headers, body)


Answer = Callable[[str, str, str], Awaitable["dict[str, Any] | str"]]
Run = Callable[[str, Session], Awaitable[None]]


class Server(asyncio.DatagramProtocol):
    """SIP on one UDP port: the calls it takes and the ones it places."""

    def __init__(
        self, *, host: str, port: int, public_ip: str, rtp_ports: tuple[int, int],
        trunks: Callable[[], list[dict[str, Any]]], answer: Answer, run: Run,
        failed: Callable[[str, str], None] | None = None,
    ):
        self.host, self.port, self.public_ip, self.rtp_ports = host, port, public_ip, rtp_ports
        self.trunks, self.answer, self.run, self.failed = trunks, answer, run, failed
        self.udp: asyncio.DatagramTransport | None = None
        self.error: str | None = None  # why it isn't listening
        self.allowed: set[str] = set()  # the addresses of the gateways that may send us calls
        self.sessions: dict[str, Session] = {}
        self.registered: dict[str, bool] = {}
        self._transactions: dict[str, asyncio.Queue[Message]] = {}
        self._tasks: set[asyncio.Future] = set()
        self._registering: dict[str, asyncio.Task] = {}
        self._refreshing: asyncio.Task | None = None
        self._warned: dict[str, float] = {}
        self._rtp_next = rtp_ports[0]

    @property
    def listening(self) -> bool:
        return self.udp is not None

    async def start(self) -> None:
        """Start listening, if it isn't yet, and see what the saved trunks say. A port in use is reported, not raised:
        the rest of OpenRing works without SIP."""
        if self.udp is None:
            loop = asyncio.get_running_loop()
            try:
                await loop.create_datagram_endpoint(lambda: self, local_addr=(self.host, self.port))
                self.error = None
            except OSError as exc:
                self.error = f"can't listen on UDP port {self.port}: {exc}"
                logger.error("SIP: {}", self.error)
                return
            logger.info("SIP listening on UDP {}:{}", self.host, self.port)
            self._refreshing = asyncio.ensure_future(self._refresh_forever())
        await self.refresh()

    async def stop(self) -> None:
        for session in list(self.sessions.values()):
            if session.acked.is_set():
                await session.hang_up()
        for task in [*self._tasks, *self._registering.values(), *([self._refreshing] if self._refreshing else [])]:
            task.cancel()
        self._registering.clear()
        if self.udp is not None:
            self.udp.close()
            self.udp = None

    def connection_made(self, transport: Any) -> None:
        self.udp = transport
        self.port = transport.get_extra_info("sockname")[1]

    def connection_lost(self, exc: Exception | None) -> None:
        self.udp = None

    def error_received(self, exc: Exception) -> None:
        logger.debug("SIP socket error: {}", exc)

    def spawn(self, work: Any) -> asyncio.Future:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def forget(self, session: Session) -> None:
        self.sessions.pop(session.call_id, None)

    def send(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.udp is not None:
            self.udp.sendto(data, addr)

    def contact_address(self, peer: tuple[str, int]) -> tuple[str, int]:
        """Where the other side can reach us: the public address, or the one this computer uses to reach them."""
        if self.public_ip:
            return self.public_ip, self.port
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(peer)
            return probe.getsockname()[0], self.port
        except OSError:
            return "127.0.0.1", self.port
        finally:
            probe.close()

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            try:
                await self.refresh()
            except Exception:
                logger.exception("SIP: refreshing the trunks failed")

    async def refresh(self) -> None:
        """Look up the gateways that may send calls, and register with the carriers that want it."""
        loop = asyncio.get_running_loop()
        allowed: set[str] = set()
        wanted: dict[str, dict[str, Any]] = {}
        for trunk in self.trunks():
            plan = trunk.get("outboundAuthenticationPlan") or {}
            registers = bool(plan.get("sipRegisterPlan"))
            if registers:
                wanted[trunk["id"]] = trunk
            for gateway in trunk.get("gateways") or []:
                if gateway.get("inboundEnabled") is False and not registers:
                    continue
                try:
                    found = await loop.getaddrinfo(gateway["ip"], None, family=socket.AF_INET, type=socket.SOCK_DGRAM)
                    allowed |= {row[4][0] for row in found}
                except OSError as exc:
                    logger.warning("SIP: can't look up gateway {}: {}", gateway.get("ip"), exc)
        self.allowed = allowed
        for trunk_id in set(self._registering) - set(wanted):
            self._registering.pop(trunk_id).cancel()
            self.registered.pop(trunk_id, None)
        for trunk_id, trunk in wanted.items():
            if trunk_id not in self._registering:
                self._registering[trunk_id] = asyncio.ensure_future(self._register_forever(trunk))

    def gateway_of(self, trunk: dict[str, Any] | None, *, outbound: bool = True) -> dict[str, Any] | None:
        gateways = (trunk or {}).get("gateways") or []
        return next((g for g in gateways if g.get("outboundEnabled") is not False or not outbound), None)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.strip(b"\r\n") == b"":  # a carrier keeping its NAT path open
            return
        message = parse(data)
        if message is None:
            return
        try:
            if message.is_response:
                self._on_response(message)
            elif addr[0] not in self.allowed:
                self._note_stranger(addr[0], message)
            else:
                self._on_request(message, addr)
        except (ValueError, IndexError, KeyError):  # a malformed message from a gateway: nothing to do with it
            logger.debug("SIP: ignored a malformed message from {}", addr[0])

    def _note_stranger(self, address: str, message: Message) -> None:
        """Nothing goes back: scanners get no sign anyone is here. The log says why a real gateway isn't answered."""
        now = time.monotonic()
        if now - self._warned.get(address, -3600.0) > 3600:
            if len(self._warned) > 10_000:
                self._warned.clear()
            self._warned[address] = now
            logger.warning("SIP: ignored {} from {}: not a gateway of a saved trunk", message.method, address)

    def _on_response(self, message: Message) -> None:
        queue = self._transactions.get(message.branch)
        if queue is not None:
            queue.put_nowait(message)
            return
        session = self.sessions.get(message.get("call-id"))
        if session is not None and message.cseq_method == "INVITE" and 200 <= message.status < 300:
            self._ack(session, message.cseq)  # our ACK got lost: the other side is repeating its answer

    def _on_request(self, message: Message, addr: tuple[str, int]) -> None:
        method = message.method
        session = self.sessions.get(message.get("call-id"))
        if method == "INVITE":
            self._on_invite(message, addr, session)
        elif method == "ACK":
            if session is not None and _tag_of(message.get("to")) == session.local_tag:
                session.answer = None
                session.acked.set()
        elif method == "OPTIONS":  # a carrier checking we're up
            self.send(_response(message, 200, "OK", addr, to_tag=tag(),
                                extra=[("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, NOTIFY")]), addr)
        elif session is None or not self._same_dialog(session, message):
            self.send(_response(message, 481, "Call/Transaction Does Not Exist", addr), addr)
        elif method == "CANCEL":
            self._on_cancel(message, addr, session)
        elif method == "BYE":
            self.send(_response(message, 200, "OK", addr), addr)
            self.spawn(session.peer_left())
        elif method == "NOTIFY":
            self.send(_response(message, 200, "OK", addr), addr)
            # How the transfer went: once the other line picked up, this side is done.
            if b"SIP/2.0 2" in message.body and session.refer_done is not None:
                self.spawn(session.hang_up())
        else:
            self.send(_response(message, 405, "Method Not Allowed", addr), addr)

    def _same_dialog(self, session: Session, message: Message) -> bool:
        if message.method == "CANCEL":  # comes before the dialog exists, with the INVITE's transaction
            return session.invite is not None and message.branch == session.invite.branch
        ours, theirs = (_tag_of(message.get("to")), _tag_of(message.get("from")))
        return ours == session.local_tag and theirs == session.remote_tag

    def _on_cancel(self, message: Message, addr: tuple[str, int], session: Session) -> None:
        self.send(_response(message, 200, "OK", addr, to_tag=session.local_tag), addr)
        if session.invite is not None and not session.acked.is_set():
            self.send(_response(session.invite, 487, "Request Terminated", addr, to_tag=session.local_tag), addr)
            session.over = True
            self.forget(session)
            if session.rtp is not None:
                session.rtp.close()
            if session.openring_call and self.failed:
                self.failed(session.openring_call, "customer-ended-call")

    def _on_invite(self, message: Message, addr: tuple[str, int], session: Session | None) -> None:
        if session is not None:
            if _tag_of(message.get("to")) == session.local_tag and session.acked.is_set():
                self._on_reinvite(session, message, addr)  # hold, or a media change, mid-call
            elif session.answer is not None:
                self.send(session.answer, addr)  # the other side didn't get our answer
            return
        call_id = message.get("call-id")
        if not call_id or _tag_of(message.get("to")) or len(self.sessions) >= MAX_SESSIONS:
            self.send(_response(message, 503 if call_id else 400, "Busy Here", addr, to_tag=tag()), addr)
            return
        self.send(_response(message, 100, "Trying", addr), addr)
        session = Session(self, call_id, addr, tag(), False, invite=message, remote_tag=_tag_of(message.get("from")),
                          remote_header=message.get("from"), remote_target=_uri_in(message.get("contact")),
                          routes=message.all("record-route"))
        self.sessions[call_id] = session
        self.spawn(self._take_call(session, message, addr))

    def _on_reinvite(self, session: Session, message: Message, addr: tuple[str, int]) -> None:
        offer = read_sdp(message.body)
        rtp = session.rtp
        if offer is None or rtp is None:
            self.send(_response(message, 488, "Not Acceptable Here", addr, to_tag=session.local_tag), addr)
            return
        if offer.payload != rtp.payload:
            rtp.clear()
            rtp.payload = offer.payload
        rtp.remote = (offer.address, offer.port)
        rtp.allowed |= {offer.address}
        local = self.contact_address(addr)
        self.send(_response(message, 200, "OK", addr, to_tag=session.local_tag,
                            extra=[("Contact", f"<sip:openring@{local[0]}:{local[1]}>")],
                            body=write_sdp(local[0], rtp.port, offer.payload, offer.events)), addr)

    async def _bind_rtp(self) -> Rtp:
        """A UDP port for a call's audio: an even one from the range, by RTP's custom."""
        loop = asyncio.get_running_loop()
        low, high = self.rtp_ports
        span = max((high - low) // 2, 1)
        for _ in range(span):
            port = self._rtp_next
            self._rtp_next = port + 2 if port + 2 <= high else low
            rtp = Rtp()
            try:
                await loop.create_datagram_endpoint(lambda protocol=rtp: protocol, local_addr=(self.host, port))
            except OSError:
                continue
            rtp.port = port
            return rtp
        raise SipError("no free RTP port")

    async def _take_call(self, session: Session, invite: Message, addr: tuple[str, int]) -> None:
        def final(status: int, reason: str) -> None:
            session.over = True
            self.send(_response(invite, status, reason, addr, to_tag=session.local_tag), addr)
            self.forget(session)
            if session.rtp is not None:
                session.rtp.close()

        offer = read_sdp(invite.body)
        if offer is None:
            final(488, "Not Acceptable Here")
            return
        caller, called = user_of(invite.get("from")), user_of(invite.get("to"))
        if not NUMBER.fullmatch(user_of(invite.get("to"))):
            called = user_of(invite.uri)
        try:
            answer = await self.answer(caller, called, session.call_id)
        except Exception:
            logger.exception("SIP: couldn't take a call to {}", called)
            final(500, "Server Internal Error")
            return
        if session.over:  # they gave up while we were finding the assistant
            if isinstance(answer, dict) and self.failed:
                self.failed(answer["id"], "customer-ended-call")
            return
        if isinstance(answer, str):
            logger.info("SIP: refused a call from {} to {}: {}", caller, called, answer)
            final(404, "Not Found")
            return
        session.openring_call = answer["id"]
        try:
            rtp = await self._bind_rtp()
        except SipError as exc:
            logger.error("SIP: {}", exc)
            final(503, "Service Unavailable")
            if self.failed:
                self.failed(answer["id"], "sip-failed-to-connect-call")
            return
        rtp.payload, rtp.remote = offer.payload, (offer.address, offer.port)
        rtp.allowed = {offer.address, addr[0]}
        session.rtp = rtp
        local = self.contact_address(addr)
        session.local_header = f"{invite.get('to')};tag={session.local_tag}"
        session.answer = _response(invite, 200, "OK", addr, to_tag=session.local_tag,
                                   extra=[("Contact", f"<sip:openring@{local[0]}:{local[1]}>")],
                                   body=write_sdp(local[0], rtp.port, offer.payload, offer.events))
        wait = T1
        started = time.monotonic()
        while not session.acked.is_set():
            if session.over:
                return
            if time.monotonic() - started > TRANSACTION_SECONDS:
                logger.warning("SIP: call {} got no ACK from {}; giving it up", session.call_id, addr[0])
                session.over = True
                self.forget(session)
                rtp.close()
                if self.failed:
                    self.failed(answer["id"], "sip-failed-to-connect-call")
                return
            self.send(session.answer, addr)
            try:
                await asyncio.wait_for(session.acked.wait(), wait)
            except TimeoutError:
                wait = min(wait * 2, 4.0)
        session.invite = None
        rtp.start()
        await self.run(answer["id"], session)

    async def request(self, data: bytes, addr: tuple[str, int], branch: str) -> Message:
        """Send a request other than INVITE until its final answer comes, the way RFC 3261 says on UDP."""
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self._transactions[branch] = queue
        wait, started = T1, time.monotonic()
        try:
            while time.monotonic() - started < TRANSACTION_SECONDS:
                self.send(data, addr)
                try:
                    answer = await asyncio.wait_for(queue.get(), wait)
                except TimeoutError:
                    wait = min(wait * 2, 4.0)
                    continue
                if answer.status >= 200:
                    return answer
                wait = 4.0  # someone is working on it
            raise SipError("no answer")
        finally:
            self._transactions.pop(branch, None)

    def _ack(self, session: Session, cseq: int) -> None:
        host, port = self.contact_address(session.address)
        target = session.remote_target or _uri_in(session.remote_header)
        self.send(_build(f"ACK {target} SIP/2.0", [
            ("Via", f"SIP/2.0/UDP {host}:{port};branch=z9hG4bK{tag()};rport"), ("Max-Forwards", "70"),
            ("From", session.local_header), ("To", session.remote_header), ("Call-ID", session.call_id),
            ("CSeq", f"{cseq} ACK"), *[("Route", r) for r in session.routes],
        ]), session.address)

    def _ack_failure(self, request_uri: str, branch: str, sent: list[tuple[str, str]], answer: Message,
                     addr: tuple[str, int]) -> None:
        """The ACK for a final answer that isn't 2xx: part of the INVITE's own transaction."""
        keep = {"Via", "From", "Call-ID", "Max-Forwards"}
        headers = [(k, v) for k, v in sent if k in keep]
        headers += [("To", answer.get("to")), ("CSeq", f"{answer.cseq} ACK")]
        self.send(_build(f"ACK {request_uri} SIP/2.0", headers), addr)

    def outbound_target(self, trunk: dict[str, Any], to: str) -> tuple[str, tuple[str, int]]:
        """The address of a call, and where its messages go: a `sip:` address itself, or the trunk's gateway."""
        if to.startswith("sip"):
            found = SIP_URI.fullmatch(to)
            if not found:
                raise SipError(f"'{to}' is not a SIP address like sip:name@host")
            return to, (found.group("host").strip("[]"), int(found.group("port") or 5060))
        gateway = self.gateway_of(trunk)
        if gateway is None:
            raise SipError("the trunk has no gateway for outbound calls")
        number = to if trunk.get("outboundLeadingPlusEnabled") else to.lstrip("+")
        host, port = str(gateway["ip"]), int(gateway.get("port") or 5060)
        return f"sip:{number}@{host}" + (f":{port}" if port != 5060 else ""), (host, port)

    async def place_call(self, call_id: str, to: str, caller: str, trunk: dict[str, Any]) -> None:
        """Dial; the call runs when it's answered, and `failed` hears about it if it isn't."""
        if not self.listening:
            raise SipError(self.error or "SIP isn't listening")
        uri, target = self.outbound_target(trunk, to)
        loop = asyncio.get_running_loop()
        found = await loop.getaddrinfo(target[0], target[1], family=socket.AF_INET, type=socket.SOCK_DGRAM)
        address = found[0][4][0]
        if netguard.blocked_address(address):
            raise SipError(f"{target[0]} points to {address}, an address OpenRing never calls")
        self.spawn(self._dial(call_id, uri, caller, trunk, (address, target[1])))

    async def _dial(self, call_id: str, uri: str, caller: str, trunk: dict[str, Any], addr: tuple[str, int]) -> None:
        rtp: Rtp | None = None
        session: Session | None = None
        reason = "sip-failed-to-connect-call"
        try:
            rtp = await self._bind_rtp()
            host, port = self.contact_address(addr)
            sip_id = f"{uuid.uuid4()}@openring"
            domain = (_host_port(uri) or addr)[0]
            local = f"<sip:{caller.lstrip('+') if not trunk.get('outboundLeadingPlusEnabled') else caller}@{domain}>"
            session = Session(self, sip_id, addr, tag(), True, rtp=rtp, credential=trunk,
                              local_header="", remote_header=f"<{uri}>", remote_target=uri, openring_call=call_id)
            session.local_header = f"{local};tag={session.local_tag}"
            self.sessions[sip_id] = session
            answer = await self._invite(session, uri, trunk, addr, (host, port))
            if answer is None:
                reason = "customer-did-not-answer"
            elif answer.status >= 300:
                reason = "customer-busy" if answer.status in BUSY else (
                    "customer-did-not-answer" if answer.status in NO_ANSWER else reason)
                logger.info("SIP call {} to {} failed: {}", call_id, uri, answer.first[8:])
            else:
                offer = read_sdp(answer.body)
                if offer is None:
                    await session.hang_up()
                    logger.warning("SIP call {}: the answer has no G.711 audio", call_id)
                    if self.failed:
                        self.failed(call_id, reason)
                    return
                session.remote_header = answer.get("to")
                session.remote_tag = _tag_of(answer.get("to"))
                session.remote_target = _uri_in(answer.get("contact")) or uri
                session.routes = list(reversed(answer.all("record-route")))
                rtp.payload, rtp.remote = offer.payload, (offer.address, offer.port)
                rtp.allowed = {offer.address, addr[0]}
                self._ack(session, answer.cseq)
                session.acked.set()
                rtp.start()
                await self.run(call_id, session)
                return
        except SipError as exc:
            logger.warning("SIP call {} to {} failed: {}", call_id, uri, exc)
        except OSError as exc:
            logger.warning("SIP call {} to {} failed: {}", call_id, uri, exc)
        if session is not None:
            session.over = True
            self.forget(session)
        if rtp is not None:
            rtp.close()
        if self.failed:
            self.failed(call_id, reason)

    async def _invite(self, session: Session, uri: str, trunk: dict[str, Any], addr: tuple[str, int],
                      local: tuple[str, int]) -> Message | None:
        """Send the INVITE, log in if asked, wait for the answer. None when nobody answered in time."""
        plan = trunk.get("outboundAuthenticationPlan") or {}
        body = write_sdp(local[0], session.rtp.port, None)
        authorization: tuple[str, str] | None = None
        session.cseq += 1
        for attempt in range(2):
            branch = "z9hG4bK" + tag()
            queue: asyncio.Queue[Message] = asyncio.Queue()
            self._transactions[branch] = queue
            headers = [("Via", f"SIP/2.0/UDP {local[0]}:{local[1]};branch={branch};rport"), ("Max-Forwards", "70"),
                       ("From", session.local_header), ("To", session.remote_header), ("Call-ID", session.call_id),
                       ("CSeq", f"{session.cseq} INVITE"), ("Contact", f"<sip:openring@{local[0]}:{local[1]}>"),
                       *([authorization] if authorization else []),
                       ("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, NOTIFY"),
                       ("User-Agent", "OpenRing")]
            data = _build(f"INVITE {uri} SIP/2.0", headers, body)
            try:
                answer = await self._await_invite(data, queue, addr, session, uri, branch, headers)
            finally:
                self._transactions.pop(branch, None)
            if answer is None:
                return None
            if answer.status in (401, 407) and attempt == 0 and plan.get("authUsername"):
                self._ack_failure(uri, branch, headers, answer, addr)
                header = "www-authenticate" if answer.status == 401 else "proxy-authenticate"
                authorization = ("Authorization" if answer.status == 401 else "Proxy-Authorization",
                                 digest(answer.get(header), "INVITE", uri, str(plan["authUsername"]),
                                        str(plan.get("authPassword") or "")))
                session.cseq += 1
                continue
            if answer.status >= 300:
                self._ack_failure(uri, branch, headers, answer, addr)
            return answer
        return None

    async def _await_invite(self, data: bytes, queue: asyncio.Queue[Message], addr: tuple[str, int], session: Session,
                            uri: str, branch: str, headers: list[tuple[str, str]]) -> Message | None:
        wait, started = T1, time.monotonic()
        heard = False
        self.send(data, addr)
        while time.monotonic() - started < RING_SECONDS:
            try:
                answer = await asyncio.wait_for(queue.get(), wait)
            except TimeoutError:
                if not heard:
                    if time.monotonic() - started > TRANSACTION_SECONDS:
                        raise SipError("the carrier didn't answer") from None
                    self.send(data, addr)
                    wait = min(wait * 2, 4.0)
                continue
            if answer.status >= 200:
                return answer
            heard, wait = True, 1.0
        # Rang for too long: cancel, on the INVITE's own branch.
        keep = [(k, v) for k, v in headers if k in ("Via", "From", "To", "Call-ID", "Max-Forwards")]
        keep.append(("CSeq", f"{session.cseq} CANCEL"))
        self.send(_build(f"CANCEL {uri} SIP/2.0", keep), addr)
        return None

    async def _register_forever(self, trunk: dict[str, Any]) -> None:
        """Keep a carrier that wants registration informed of where to send calls."""
        plan = trunk.get("outboundAuthenticationPlan") or {}
        register = plan.get("sipRegisterPlan") if isinstance(plan.get("sipRegisterPlan"), dict) else {}
        gateway = self.gateway_of(trunk, outbound=False) or {}
        domain = str(register.get("domain") or gateway.get("ip") or "")
        user = str(register.get("username") or plan.get("authUsername") or "")
        call_id = f"{uuid.uuid4()}@openring"
        from_tag, sequence = tag(), 0
        while True:
            pause: float = RETRY_REGISTER_SECONDS
            try:
                found = await asyncio.get_running_loop().getaddrinfo(
                    str(gateway.get("ip") or domain), int(gateway.get("port") or 5060),
                    family=socket.AF_INET, type=socket.SOCK_DGRAM)
                addr = found[0][4][0], found[0][4][1]
                host, port = self.contact_address(addr)
                uri = f"sip:{domain}"
                authorization: tuple[str, str] | None = None
                for attempt in range(2):
                    sequence += 1
                    branch = "z9hG4bK" + tag()
                    data = _build(f"REGISTER {uri} SIP/2.0", [
                        ("Via", f"SIP/2.0/UDP {host}:{port};branch={branch};rport"), ("Max-Forwards", "70"),
                        ("From", f"<sip:{user}@{domain}>;tag={from_tag}"), ("To", f"<sip:{user}@{domain}>"),
                        ("Call-ID", call_id), ("CSeq", f"{sequence} REGISTER"),
                        ("Contact", f"<sip:{user}@{host}:{port}>"), ("Expires", str(REGISTER_SECONDS)),
                        *([authorization] if authorization else []), ("User-Agent", "OpenRing"),
                    ])
                    answer = await self.request(data, addr, branch)
                    if answer.status in (401, 407) and attempt == 0:
                        name = "www-authenticate" if answer.status == 401 else "proxy-authenticate"
                        authorization = ("Authorization" if answer.status == 401 else "Proxy-Authorization",
                                         digest(answer.get(name), "REGISTER", uri,
                                                str(plan.get("authUsername") or user),
                                                str(plan.get("authPassword") or "")))
                        continue
                    break
                if 200 <= answer.status < 300:
                    self.registered[trunk["id"]] = True
                    pause = REGISTER_SECONDS * 0.8
                else:
                    self.registered[trunk["id"]] = False
                    logger.warning("SIP: {} refused registration: {}", domain, answer.first[8:])
            except (SipError, OSError) as exc:
                self.registered[trunk["id"]] = False
                logger.warning("SIP: can't register with {}: {}", domain, exc)
            await asyncio.sleep(pause)


def check_trunk(doc: dict[str, Any]) -> None:
    """Refuse a credential that can't work."""
    if doc.get("provider") != "byo-sip-trunk":
        raise SipError('only SIP trunks are kept as credentials for now: provider "byo-sip-trunk"')
    gateways = doc.get("gateways")
    if not isinstance(gateways, list) or not gateways:
        raise SipError('gateways is required: the carrier\'s SIP servers, like [{"ip": "sip.carrier.example"}]')
    for gateway in gateways:
        host = gateway.get("ip") if isinstance(gateway, dict) else None
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
            raise SipError("each gateway needs ip: an address or host name of the carrier's SIP server")
        port = gateway.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
            raise SipError("a gateway's port goes from 1 to 65535")
    plan = doc.get("outboundAuthenticationPlan")
    if plan is not None:
        if not isinstance(plan, dict):
            raise SipError("outboundAuthenticationPlan must be an object with authUsername and authPassword")
        for key in ("authUsername", "authPassword"):
            if plan.get(key) is not None and (not isinstance(plan[key], str) or re.search(r'[\r\n"]', plan[key])):
                raise SipError(f"outboundAuthenticationPlan.{key} must be text on one line, without quotes")
        register = plan.get("sipRegisterPlan")
        if register is not None and not isinstance(register, dict):
            raise SipError("sipRegisterPlan must be an object")
        for value in (register or {}).values():
            if not isinstance(value, str) or not re.fullmatch(r"[\w.+@-]{1,253}", value):
                raise SipError("sipRegisterPlan's values are a domain, a username and a realm")


def without_password(doc: dict[str, Any]) -> dict[str, Any]:
    """A credential as the API shows it: the password is written, never read back."""
    plan = doc.get("outboundAuthenticationPlan")
    if not isinstance(plan, dict):
        return doc
    return {**doc, "outboundAuthenticationPlan": {k: v for k, v in plan.items() if k != "authPassword"}}


def transport_for(session: Session) -> Any:
    """The Pipecat transport of a SIP call: its RTP audio, in and out, at phone quality."""
    from pipecat.frames.frames import ClientConnectedFrame, InputAudioRawFrame, InterruptionFrame
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport, TransportParams

    rtp = session.rtp
    # 20 ms at a time, the size of an RTP packet.
    params = TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_out_10ms_chunks=2)

    class Input(BaseInputTransport):
        def __init__(self, transport: Any):
            super().__init__(params)
            self._transport = transport
            self._receiving: asyncio.Task | None = None

        async def start(self, frame: Any) -> None:
            await super().start(frame)
            await self._transport._call_event_handler("on_client_connected", session)
            await self.push_frame(ClientConnectedFrame())
            if self._receiving is None:
                self._receiving = self.create_task(self._receive())
            await self.set_transport_ready(frame)

        async def _receive(self) -> None:
            while True:
                pcm = await rtp.received.get()
                await self.push_audio_frame(InputAudioRawFrame(pcm, sample_rate=SAMPLE_RATE, num_channels=1))

        async def _stop_receiving(self) -> None:
            if self._receiving is not None:
                await self.cancel_task(self._receiving)
                self._receiving = None

        async def stop(self, frame: Any) -> None:
            await super().stop(frame)
            await self._stop_receiving()

        async def cancel(self, frame: Any) -> None:
            await super().cancel(frame)
            await self._stop_receiving()

    class Output(BaseOutputTransport):
        def __init__(self) -> None:
            super().__init__(params)

        async def start(self, frame: Any) -> None:
            await super().start(frame)
            await self.set_transport_ready(frame)

        async def process_frame(self, frame: Any, direction: Any) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, InterruptionFrame):  # what wasn't said yet goes unsaid
                rtp.clear()

        async def write_audio_frame(self, frame: Any) -> bool:
            if session.over:
                return False
            rtp.queue(frame.audio)
            while rtp.buffered > PACKET_BYTES * 2 and not session.over:  # as fast as the caller hears it
                await asyncio.sleep(0.005)
            return True

    class SipTransport(BaseTransport):
        def __init__(self) -> None:
            super().__init__()
            self._input = Input(self)
            self._output = Output()
            self._register_event_handler("on_client_connected")
            self._register_event_handler("on_client_disconnected")

        def input(self) -> Any:
            return self._input

        def output(self) -> Any:
            return self._output

    transport = SipTransport()

    async def peer_left() -> None:
        await transport._call_event_handler("on_client_disconnected", session)

    session.on_peer_left = peer_left
    return transport
