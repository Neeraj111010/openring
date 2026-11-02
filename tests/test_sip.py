"""SIP trunks, on real UDP sockets on this computer: a little softphone plays the carrier."""

import random
import re
import socket
import struct
import time

import pytest
from fastapi.testclient import TestClient

from openring import sip
from openring.api import create_app
from openring.config import Settings
from openring.store import Store

from test_api import ASSISTANT, AUTH

RTP_LOW = 41000


class Softphone:
    """Sends and receives what a carrier would, on loopback."""

    def __init__(self):
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.1", 0))
        self.sip.settimeout(5)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("127.0.0.1", 0))
        self.rtp.settimeout(5)
        self.port = self.sip.getsockname()[1]
        self.rtp_port = self.rtp.getsockname()[1]

    def close(self):
        self.sip.close()
        self.rtp.close()

    def sdp(self, payloads="0 8 101"):
        return (f"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio {self.rtp_port} RTP/AVP {payloads}\r\na=rtpmap:101 telephone-event/8000\r\n")

    def invite_text(self, server_port, to="+15557654321", caller="+5511999990000", call_id="call-1", sdp=None,
                    branch="z9hG4bKa1"):
        body = self.sdp() if sdp is None else sdp
        return (f"INVITE sip:{to}@127.0.0.1:{server_port} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP 127.0.0.1:{self.port};branch={branch};rport\r\n"
                f"From: <sip:{caller}@127.0.0.1>;tag=caller1\r\nTo: <sip:{to}@127.0.0.1>\r\n"
                f"Call-ID: {call_id}\r\nCSeq: 1 INVITE\r\nContact: <sip:phone@127.0.0.1:{self.port}>\r\n"
                f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}")

    def send(self, text, server_port):
        self.sip.sendto(text.encode(), ("127.0.0.1", server_port))

    def receive(self, timeout=5):
        self.sip.settimeout(timeout)
        data, addr = self.sip.recvfrom(65535)
        return sip.parse(data), addr

    def until(self, want, timeout=8):
        """The next message `want` accepts, skipping the ones in between."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message, addr = self.receive(max(deadline - time.monotonic(), 0.01))
            if want(message):
                return message, addr
        raise AssertionError("nothing like that came")

    def in_dialog(self, method, answer, server_port, cseq=2):
        to = answer.get("to")
        return (f"{method} sip:openring@127.0.0.1:{server_port} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bK{method}{cseq};rport\r\n"
                f"From: {answer.get('from')}\r\nTo: {to}\r\nCall-ID: {answer.get('call-id')}\r\n"
                f"CSeq: {cseq} {method}\r\nContent-Length: 0\r\n\r\n")

    def rtp_packet(self, sequence=1, payload_type=0, payload=b"\xff" * 160):
        return struct.pack("!BBHII", 0x80, payload_type, sequence, sequence * 160, 7) + payload


@pytest.fixture
def phone():
    p = Softphone()
    yield p
    p.close()


@pytest.fixture
def server(monkeypatch, quiet_providers):
    """An OpenRing with a free UDP port, running, with one assistant."""
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(key, "fake")
    low = RTP_LOW + random.randrange(0, 4000, 2)
    settings = Settings(api_key="test-key", db_path=":memory:", sip_host="127.0.0.1", sip_port=0, sip_public_ip="",
                        sip_rtp_ports=f"{low}-{low + 40}")
    with TestClient(create_app(settings, Store(":memory:"))) as client:
        yield client


def _trunk(client, phone=None, **fields):
    body = {"provider": "byo-sip-trunk", "name": "Carrier",
            "gateways": [{"ip": "127.0.0.1", **({"port": phone.port} if phone else {})}], **fields}
    r = client.post("/credential", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


def _line(client, phone=None, trunk_fields=None, **number):
    """A trunk, an assistant and a number on it."""
    assistant = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    trunk = _trunk(client, phone, **(trunk_fields or {}))
    body = {"provider": "byo-phone-number", "number": "+15557654321", "credentialId": trunk["id"],
            "assistantId": assistant["id"], **number}
    r = client.post("/phone-number", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json(), trunk, assistant


def _port(client):
    return client.app.state.sip.port


def _wait(condition, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if result := condition():
            return result
        time.sleep(0.02)
    return None


def _calls(client):
    return client.get("/call", headers=AUTH).json()


def _ended(client):
    return _wait(lambda: [c for c in _calls(client) if c["status"] == "ended"])


def _answered(phone, port):
    answer, _ = phone.until(lambda m: m.is_response and m.status == 200)
    return answer


def _ack(phone, answer, port):
    phone.send(phone.in_dialog("ACK", answer, port, cseq=1), port)


def test_an_incoming_call_runs_and_hangs_up(server, phone):
    _line(server)
    port = _port(server)
    phone.send(phone.invite_text(port), port)
    trying, addr = phone.receive()
    assert trying.status == 100 and addr[1] == port
    answer = _answered(phone, port)
    offer = sip.read_sdp(answer.body)
    assert offer.payload == 0 and offer.events == 101
    assert server.get("/call", headers=AUTH).json()[0]["status"] == "queued"  # nothing runs before the ACK

    _ack(phone, answer, port)
    assert _wait(lambda: _calls(server)[0]["status"] == "in-progress")
    # Audio flows both ways: a packet every 20 ms, silence included, to where ours come from.
    phone.rtp.sendto(phone.rtp_packet(), ("127.0.0.1", offer.port))
    packets = [phone.rtp.recvfrom(2048)[0] for _ in range(5)]
    assert all(len(p) == 172 and p[1] & 0x7f == 0 and p[12:] == b"\xff" * 160 for p in packets)
    sequences = [struct.unpack("!H", p[2:4])[0] for p in packets]
    assert all((b - a) & 0xffff == 1 for a, b in zip(sequences, sequences[1:]))

    phone.send(phone.in_dialog("BYE", answer, port), port)
    bye_answer, _ = phone.until(lambda m: m.is_response and m.cseq_method == "BYE")
    assert bye_answer.status == 200
    call = _ended(server)[0]
    assert call["endedReason"] == "customer-ended-call" and call["type"] == "inboundPhoneCall"
    assert call["customer"]["number"] == "+5511999990000" and call["phoneCallProvider"] == "sip"
    assert not server.app.state.sip.sessions


def test_speech_goes_out_and_the_caller_comes_in(server, phone, monkeypatch):
    """Through the whole pipeline: the voice reaches the line, and the caller reaches speech-to-text."""
    from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, TTSSpeakFrame
    from pipecat.processors.frame_processor import FrameProcessor

    from openring import bot

    heard = []

    class Layer(FrameProcessor):
        openring_label, openring_price = "x", ("model", "none", None, None)

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, InputAudioRawFrame):
                heard.append(frame)
            if isinstance(frame, TTSSpeakFrame):
                for _ in range(10):
                    await self.push_frame(OutputAudioRawFrame(b"\x10\x20" * 160, sample_rate=8000, num_channels=1))
                return
            await self.push_frame(frame, direction)

    monkeypatch.setattr(bot, "build_services", lambda plan: (Layer(), Layer(), Layer()))
    _line(server)
    port = _port(server)
    phone.send(phone.invite_text(port), port)
    answer = _answered(phone, port)
    offer = sip.read_sdp(answer.body)
    _ack(phone, answer, port)
    assert _wait(lambda: _calls(server)[0]["status"] == "in-progress")
    phone.rtp.sendto(phone.rtp_packet(payload=b"\x7f" * 160), ("127.0.0.1", offer.port))  # where audio goes back to

    spoken = None
    for _ in range(200):
        packet = phone.rtp.recv(2048)
        if packet[12:] != b"\xff" * 160:
            spoken = packet
            break
    assert spoken is not None and len(spoken) == 172 and spoken[1] & 0x80  # marked as the start of speech

    for sequence in range(2, 20):
        phone.rtp.sendto(phone.rtp_packet(sequence=sequence), ("127.0.0.1", offer.port))
    assert _wait(lambda: heard)
    assert heard[0].sample_rate == 8000 and heard[0].num_channels == 1 and len(heard[0].audio) == 320
    phone.send(phone.in_dialog("BYE", answer, port), port)
    assert _ended(server)


def test_strangers_get_no_answer_at_all(server, phone):
    client = server
    assistant = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    trunk = client.post("/credential", json={"provider": "byo-sip-trunk", "gateways": [{"ip": "192.0.2.1"}]},
                        headers=AUTH).json()
    client.post("/phone-number", json={"provider": "byo-phone-number", "number": "+15557654321",
                                       "credentialId": trunk["id"], "assistantId": assistant["id"]}, headers=AUTH)
    port = _port(client)
    phone.send(phone.invite_text(port), port)
    phone.send("OPTIONS sip:x@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z\r\nCall-ID: x\r\n"
               "CSeq: 1 OPTIONS\r\n\r\n", port)
    with pytest.raises(TimeoutError):
        phone.receive(0.5)
    assert _calls(client) == []


def test_an_unknown_number_or_codec_is_refused(server, phone):
    _line(server)
    port = _port(server)
    phone.send(phone.invite_text(port, to="+19990000000", call_id="c2"), port)
    refused, _ = phone.until(lambda m: m.status >= 300)
    assert refused.status == 404 and _tag(refused)
    phone.send(phone.invite_text(port, call_id="c3", sdp=phone.sdp("9 18")), port)
    refused, _ = phone.until(lambda m: m.status >= 300)
    assert refused.status == 488
    assert _calls(server) == []


def _tag(message):
    return re.search(r"tag=", message.get("to"))


def test_the_call_stays_queued_and_ends_without_an_ack(server, phone, monkeypatch):
    monkeypatch.setattr(sip, "TRANSACTION_SECONDS", 0.6)
    _line(server)
    port = _port(server)
    phone.send(phone.invite_text(port), port)
    _answered(phone, port)
    repeated = _answered(phone, port)  # the answer is repeated while the ACK doesn't come
    assert repeated.status == 200
    call = _ended(server)[0]
    assert call["endedReason"] == "sip-failed-to-connect-call" and "startedAt" not in call


def test_an_ack_without_our_tag_starts_nothing(server, phone):
    _line(server)
    port = _port(server)
    phone.send(phone.invite_text(port), port)
    answer = _answered(phone, port)
    forged = phone.in_dialog("ACK", answer, port, cseq=1).replace(_to_tag(answer), "guess")
    phone.send(forged, port)
    time.sleep(0.2)
    assert _calls(server)[0]["status"] == "queued"
    phone.send(phone.in_dialog("BYE", answer, port).replace(_to_tag(answer), "guess"), port)
    nope, _ = phone.until(lambda m: m.is_response and m.cseq_method == "BYE")
    assert nope.status == 481
    _ack(phone, answer, port)
    assert _wait(lambda: _calls(server)[0]["status"] == "in-progress")
    phone.send(phone.in_dialog("BYE", answer, port), port)
    assert _ended(server)


def _to_tag(message):
    return re.search(r"tag=([^;\s]+)", message.get("to")).group(1)


def test_a_repeated_invite_gets_the_same_answer_and_one_call(server, phone):
    _line(server)
    port = _port(server)
    phone.send(phone.invite_text(port), port)
    first = _answered(phone, port)
    phone.send(phone.invite_text(port), port)
    again = _answered(phone, port)
    assert _to_tag(first) == _to_tag(again) and len(_calls(server)) == 1
    _ack(phone, first, port)
    assert _wait(lambda: _calls(server)[0]["status"] == "in-progress")
    phone.send(phone.in_dialog("BYE", first, port), port)
    assert _ended(server)


def test_a_caller_who_gives_up_is_cancelled(server, phone, monkeypatch):
    _line(server)
    port = _port(server)
    phone.send(phone.invite_text(port), port)
    answer = _answered(phone, port)
    cancel = (f"CANCEL sip:x@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{phone.port};branch=z9hG4bKa1;rport\r\n"
              f"From: {answer.get('from')}\r\nTo: <sip:+15557654321@127.0.0.1>\r\nCall-ID: call-1\r\n"
              f"CSeq: 1 CANCEL\r\nContent-Length: 0\r\n\r\n")
    phone.send(cancel, port)
    phone.until(lambda m: m.status == 487)
    assert _ended(server)[0]["endedReason"] == "customer-ended-call"


def test_options_from_a_gateway_are_answered(server, phone):
    _trunk(server)
    port = _port(server)
    phone.send(f"OPTIONS sip:x@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{phone.port};branch=z9o\r\n"
               f"From: <sip:a@b>;tag=1\r\nTo: <sip:x@127.0.0.1>\r\nCall-ID: o1\r\nCSeq: 1 OPTIONS\r\n\r\n", port)
    assert phone.receive()[0].status == 200


def _trunk_answers(phone, *statuses, challenge=None):
    """Plays the carrier for an outgoing call: `statuses` in order, the last being final. Returns the INVITEs."""
    invites = []
    invite, addr = phone.until(lambda m: not m.is_response and m.method == "INVITE")
    invites.append(invite)
    for status in statuses:
        if status == 401:
            _reply(phone, invite, addr, 401, extra=f'WWW-Authenticate: {challenge}\r\n')
            ack, _ = phone.until(lambda m: not m.is_response and m.method == "ACK")
            assert ack.cseq == invite.cseq
            invite, addr = phone.until(lambda m: not m.is_response and m.method == "INVITE")
            invites.append(invite)
        elif status == 200:
            _reply(phone, invite, addr, 200, extra=f"Contact: <sip:far@127.0.0.1:{phone.port}>\r\n",
                   body=phone.sdp("8"))
        else:
            _reply(phone, invite, addr, status)
    return invites, addr


def _reply(phone, invite, addr, status, extra="", body=""):
    phone.sip.sendto((f"SIP/2.0 {status} Status\r\nVia: {invite.get('via')}\r\nFrom: {invite.get('from')}\r\n"
                      f"To: {invite.get('to')};tag=far1\r\nCall-ID: {invite.get('call-id')}\r\n"
                      f"CSeq: {invite.get('cseq')}\r\n{extra}Content-Length: {len(body)}\r\n\r\n{body}").encode(), addr)


def _dial(client, number, **customer):
    return client.post("/call", json={"phoneNumberId": number["id"],
                                      "customer": customer or {"number": "+5511988887777"}}, headers=AUTH)


def test_an_outgoing_call_logs_in_and_runs(server, phone):
    plan = {"authUsername": "acme", "authPassword": "s3cret"}
    number, _, _ = _line(server, phone, trunk_fields={"outboundAuthenticationPlan": plan})
    created = _dial(server, number)
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "queued" and created.json()["phoneCallProvider"] == "sip"

    invites, addr = _trunk_answers(phone, 180, 401, 200, challenge='Digest realm="c", nonce="n1", qop="auth"')
    first, second = invites
    assert first.uri == f"sip:5511988887777@127.0.0.1:{phone.port}" and second.cseq == first.cseq + 1
    authorization = second.get("authorization")
    assert 'username="acme"' in authorization and "s3cret" not in authorization
    offer = sip.read_sdp(second.body)
    assert offer.payload == 0  # offers both; the carrier picked PCMA
    ack, _ = phone.until(lambda m: not m.is_response and m.method == "ACK")
    assert ack.cseq == second.cseq and _to_tag(ack) == "far1"

    assert _wait(lambda: _calls(server)[0]["status"] == "in-progress")
    phone.rtp.sendto(phone.rtp_packet(payload_type=8, payload=b"\xd5" * 160), ("127.0.0.1", offer.port))
    packet = phone.rtp.recv(2048)
    assert packet[1] & 0x7f == 8 and packet[12:] == b"\xd5" * 160  # G.711 A-law, as agreed

    # Their BYE ends it.
    phone.sip.sendto((f"BYE sip:openring@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{phone.port};branch=zb\r\n"
                      f"From: {ack.get('to')}\r\nTo: {ack.get('from')}\r\nCall-ID: {ack.get('call-id')}\r\n"
                      f"CSeq: 9 BYE\r\nContent-Length: 0\r\n\r\n").encode(), addr)
    assert _ended(server)[0]["endedReason"] == "customer-ended-call"


@pytest.mark.parametrize("status, reason", [
    (486, "customer-busy"), (480, "customer-did-not-answer"), (403, "sip-failed-to-connect-call"),
])
def test_an_outgoing_call_that_fails_says_why(server, phone, status, reason):
    number, _, _ = _line(server, phone)
    assert _dial(server, number).status_code == 201
    _trunk_answers(phone, status)
    ack, _ = phone.until(lambda m: not m.is_response and m.method == "ACK")
    assert ack.get("to").endswith("tag=far1")
    assert _ended(server)[0]["endedReason"] == reason


def test_a_wrong_login_does_not_loop(server, phone):
    plan = {"authUsername": "acme", "authPassword": "no"}
    number, _, _ = _line(server, phone, trunk_fields={"outboundAuthenticationPlan": plan})
    _dial(server, number)
    invites, addr = _trunk_answers(phone, 401, challenge='Digest realm="c", nonce="n"')
    _reply(phone, invites[-1], addr, 401, extra='WWW-Authenticate: Digest realm="c", nonce="n2"\r\n')
    phone.until(lambda m: not m.is_response and m.method == "ACK")
    assert _ended(server)[0]["endedReason"] == "sip-failed-to-connect-call"
    with pytest.raises(TimeoutError):  # no third try
        phone.receive(0.3)


def test_a_call_to_a_sip_address_goes_there(server, phone):
    number, _, _ = _line(server, phone)
    created = _dial(server, number, sipUri=f"sip:agent@127.0.0.1:{phone.port}")
    assert created.status_code == 201, created.text
    invite, _ = phone.until(lambda m: not m.is_response and m.method == "INVITE")
    assert invite.uri == f"sip:agent@127.0.0.1:{phone.port}"
    assert _dial(server, number, sipUri="not a uri").status_code == 400


def test_outgoing_numbers_keep_the_plus_when_the_trunk_says_so(server, phone):
    number, _, _ = _line(server, phone, trunk_fields={"outboundLeadingPlusEnabled": True})
    _dial(server, number)
    invite, _ = phone.until(lambda m: not m.is_response and m.method == "INVITE")
    assert invite.uri.startswith("sip:+5511988887777@")


def test_a_trunk_is_refused_with_the_reason(server):
    for body, why in [
        ({"provider": "11labs"}, "byo-sip-trunk"),
        ({"provider": "byo-sip-trunk"}, "gateways"),
        ({"provider": "byo-sip-trunk", "gateways": [{"ip": "a b"}]}, "ip"),
        ({"provider": "byo-sip-trunk", "gateways": [{"ip": "a.b", "port": 0}]}, "port"),
        ({"provider": "byo-sip-trunk", "gateways": [{"ip": "a.b"}],
          "outboundAuthenticationPlan": {"authUsername": 'a"\r\nX: y'}}, "one line"),
    ]:
        r = server.post("/credential", json=body, headers=AUTH)
        assert r.status_code == 400 and why in r.json()["detail"]


def test_the_password_goes_in_and_never_comes_out(server):
    plan = {"authUsername": "acme", "authPassword": "s3cret"}
    trunk = _trunk(server, outboundAuthenticationPlan=plan)
    assert "s3cret" not in str(trunk) and trunk["sipListening"] is True
    assert "s3cret" not in str(server.get("/credential", headers=AUTH).json())
    assert "s3cret" not in str(server.get(f"/credential/{trunk['id']}", headers=AUTH).json())
    # Saving what the API shows keeps the password.
    server.patch(f"/credential/{trunk['id']}", json={"outboundAuthenticationPlan": {"authUsername": "new"}},
                 headers=AUTH)
    saved = server.app.state.store.get("credentials", trunk["id"])
    assert saved["outboundAuthenticationPlan"] == {"authUsername": "new", "authPassword": "s3cret"}
    assert server.get("/credential").status_code == 401


def test_a_trunk_in_use_stays(server):
    number, trunk, _ = _line(server)
    assert server.delete(f"/credential/{trunk['id']}", headers=AUTH).status_code == 409
    server.delete(f"/phone-number/{number['id']}", headers=AUTH)
    assert server.delete(f"/credential/{trunk['id']}", headers=AUTH).status_code == 200
    assert server.app.state.sip.allowed == set()  # its gateway can't send calls any more


def test_numbers_on_a_trunk(server):
    trunk = _trunk(server)
    body = {"provider": "byo-phone-number", "number": "1000", "numberE164CheckEnabled": False}
    assert server.post("/phone-number", json=body, headers=AUTH).status_code == 400  # no trunk
    body["credentialId"] = trunk["id"]
    assert server.post("/phone-number", json=body, headers=AUTH).status_code == 201
    assert server.post("/phone-number", json={**body, "number": "a;b"}, headers=AUTH).status_code == 400
    plain = {k: v for k, v in body.items() if k != "numberE164CheckEnabled"}
    assert server.post("/phone-number", json=plain, headers=AUTH).status_code == 400  # not E.164


def test_nothing_listens_without_a_trunk(monkeypatch):
    settings = Settings(api_key="k", db_path=":memory:", sip_host="127.0.0.1", sip_port=0)
    with TestClient(create_app(settings, Store(":memory:"))) as client:
        assert client.app.state.sip.listening is False


def test_a_port_in_use_is_reported(monkeypatch):
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("127.0.0.1", 0))
    settings = Settings(api_key="test-key", db_path=":memory:", sip_host="127.0.0.1",
                        sip_port=taken.getsockname()[1])
    with TestClient(create_app(settings, Store(":memory:"))) as client:
        trunk = _trunk(client)
        assert trunk["sipListening"] is False and "UDP port" in trunk["sipError"]
    taken.close()


def test_registering_with_a_carrier(server, phone):
    plan = {"authUsername": "acme", "authPassword": "pw", "sipRegisterPlan": {"domain": "voice.example"}}
    body = {"provider": "byo-sip-trunk", "gateways": [{"ip": "127.0.0.1", "port": phone.port, "inboundEnabled": False}],
            "outboundAuthenticationPlan": plan}
    trunk = server.post("/credential", json=body, headers=AUTH).json()
    first, addr = phone.until(lambda m: not m.is_response and m.method == "REGISTER")
    assert first.uri == "sip:voice.example" and "acme@voice.example" in first.get("from")
    phone.sip.sendto((f"SIP/2.0 401 No\r\nVia: {first.get('via')}\r\nFrom: {first.get('from')}\r\nTo: {first.get('to')}"
                      f"\r\nCall-ID: {first.get('call-id')}\r\nCSeq: {first.get('cseq')}\r\n"
                      'WWW-Authenticate: Digest realm="r", nonce="n"\r\nContent-Length: 0\r\n\r\n').encode(), addr)
    second, addr = phone.until(lambda m: not m.is_response and m.method == "REGISTER")
    assert second.cseq == first.cseq + 1 and 'username="acme"' in second.get("authorization")
    phone.sip.sendto((f"SIP/2.0 200 OK\r\nVia: {second.get('via')}\r\nFrom: {second.get('from')}\r\n"
                      f"To: {second.get('to')}"
                      f"\r\nCall-ID: {second.get('call-id')}\r\nCSeq: {second.get('cseq')}\r\nContent-Length: 0\r\n\r\n"
                      ).encode(), addr)
    assert _wait(lambda: server.get(f"/credential/{trunk['id']}", headers=AUTH).json().get("sipRegistered"))
    # A registered carrier sends calls from its gateway, even with inboundEnabled false.
    assert "127.0.0.1" in server.app.state.sip.allowed


def test_digest_matches_rfc_2617():
    answer = sip.digest('Digest realm="testrealm@host.com", qop="auth", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093",'
                        'opaque="5ccc069c403ebaf9f0171e9517f40e41"', "GET", "/dir/index.html", "Mufasa",
                        "Circle Of Life")
    fields = dict(re.findall(r'(\w+)="?([^",]*)"?', answer))
    assert fields["opaque"] == "5ccc069c403ebaf9f0171e9517f40e41" and fields["qop"] == "auth"
    import hashlib

    def md5(text):
        return hashlib.md5(text.encode()).hexdigest()

    ha1, ha2 = md5("Mufasa:testrealm@host.com:Circle Of Life"), md5("GET:/dir/index.html")
    want = md5(f"{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:{fields['nc']}:{fields['cnonce']}:auth:{ha2}")
    assert fields["response"] == want
    without_qop = dict(re.findall(r'(\w+)="?([^",]*)"?', sip.digest('Digest realm="r", nonce="n"', "A", "u", "x", "y")))
    assert without_qop["response"] == md5(f"{md5('x:r:y')}:n:{md5('A:u')}")
    assert "SHA-256" in sip.digest('Digest realm="r", nonce="n", algorithm=SHA-256', "A", "u", "x", "y")
    with pytest.raises(sip.SipError):
        sip.digest('Digest realm="r", nonce="n", algorithm=AKAv1-MD5', "A", "u", "x", "y")


def test_sdp():
    good = "c=IN IP4 203.0.113.9\r\nm=audio 4000 RTP/AVP 8 0 101\r\na=rtpmap:101 telephone-event/8000\r\n"
    offer = sip.read_sdp(good.encode())
    assert (offer.address, offer.port, offer.payload, offer.events) == ("203.0.113.9", 4000, 0, 101)
    assert sip.read_sdp(good.replace("8 0 101", "8").encode()).payload == 8
    for bad in ("", "c=IN IP4 203.0.113.9\r\n", good.replace("8 0 101", "9"), good.replace("4000", "0"),
                good.replace("203.0.113.9", "169.254.169.254"), good.replace("203.0.113.9", "0.0.0.0"),
                good.replace("203.0.113.9", "239.1.1.1"), good.replace("4000", "99999")):
        assert sip.read_sdp(bad.encode()) is None
    assert b"RTP/AVP 0 8 101" in sip.write_sdp("198.51.100.1", 5000, None)
    assert b"RTP/AVP 8\r\n" in sip.write_sdp("198.51.100.1", 5000, 8, None)


def test_rtp_reads_what_the_standard_allows_and_only_from_who_it_should():
    rtp = sip.Rtp()
    rtp.allowed, rtp.payload = {"203.0.113.9"}, 0
    peer = ("203.0.113.9", 4000)
    audio = b"\xff" * 160
    packet = struct.pack("!BBHII", 0x80, 0, 1, 160, 7) + audio
    rtp.datagram_received(packet, ("198.51.100.5", 4000))  # someone else
    rtp.datagram_received(b"short", peer)
    rtp.datagram_received(struct.pack("!BBHII", 0x80, 101, 1, 160, 7) + audio, peer)  # a keypad tone
    assert rtp.received.empty() and rtp.remote is None
    rtp.datagram_received(packet, peer)
    assert rtp.received.get_nowait() == rtp.decode(audio) and rtp.remote == peer
    # With contributing sources, an extension and padding.
    extended = struct.pack("!BBHII", 0xB1, 0, 1, 160, 7) + b"\0" * 4 + struct.pack("!HH", 1, 1) + b"\0" * 4
    rtp.datagram_received(extended + audio + b"\0\0\x03", peer)
    assert rtp.received.get_nowait() == rtp.decode(audio)
    rtp.datagram_received(struct.pack("!BBHII", 0x90, 0, 1, 160, 7) + b"\0", peer)  # cut short
    rtp.datagram_received(struct.pack("!BBHII", 0xA0, 0, 1, 160, 7) + b"\xff", peer)  # all padding
    assert rtp.received.empty()
    for _ in range(sip.MAX_QUEUED_PACKETS + 5):  # a flood: bounded
        rtp.datagram_received(packet, peer)
    assert rtp.received.full()


def test_garbage_is_ignored(server, phone):
    _trunk(server)
    port = _port(server)
    rng = random.Random(7)
    good = phone.invite_text(port).encode()
    samples = [b"", b"\r\n\r\n", b"\x00" * 100, b"INVITE", b"SIP/2.0 abc", b"INVITE x SIP/2.0\r\nNoColon\r\n\r\n",
               b"SIP/2.0 200 OK\r\n\r\n", b"BYE x SIP/2.0\r\nCall-ID: nobody\r\n\r\n",
               *[bytes(rng.randrange(256) for _ in range(rng.randrange(300))) for _ in range(50)]]
    for _ in range(50):  # good messages with parts missing or cut
        cut = bytearray(good)
        del cut[rng.randrange(len(cut)):rng.randrange(len(cut))]
        samples.append(bytes(cut))
    for sample in samples:
        phone.sip.sendto(sample, ("127.0.0.1", port))
    # Still working.
    phone.send(f"OPTIONS sip:x@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{phone.port};branch=zz\r\n"
               f"From: <sip:a@b>;tag=1\r\nTo: <sip:x@127.0.0.1>\r\nCall-ID: alive\r\nCSeq: 1 OPTIONS\r\n\r\n", port)
    assert phone.until(lambda m: m.get("call-id") == "alive")


def test_user_of():
    assert sip.user_of("sip:+15551234567@carrier.example;user=phone") == "+15551234567"
    assert sip.user_of('"Ana" <sips:1000@10.0.0.1:5061>;tag=1') == "1000"
    assert sip.user_of("+15551234567") == "+15551234567"
    assert sip.user_of("") == ""


def test_a_twilio_sip_domain_call_finds_its_number(monkeypatch):
    for key in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(key, "fake")
    settings = Settings(api_key="test-key", public_url="https://x.test", db_path=":memory:",
                        validate_twilio_signature=False)
    client = TestClient(create_app(settings, Store(":memory:")))
    assistant = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": assistant["id"]}, headers=AUTH)
    r = client.post("/twilio/voice", data={"To": "sip:+15551234567@acme.sip.twilio.com", "From": "sip:ana@pbx.example",
                                           "CallSid": "CA1"})
    assert "<Stream" in r.text
    assert client.get("/call", headers=AUTH).json()[0]["customer"]["number"] == "ana"
