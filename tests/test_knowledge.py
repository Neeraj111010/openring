"""Knowledge bases: files through POST /file, searched by a `query` tool on this computer."""

import asyncio
import io
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from openring import knowledge
from openring.api import create_app
from openring.config import Settings
from openring.store import Store
from openring.tools import CallControls, ToolError, assistant_tools, check_tool, function_schema, make_handler

from test_api import AUTH

HOURS = ("Horários de funcionamento\nAbrimos de segunda a sexta, das 9h às 18h.\n\n"
         "Planos\nO plano Ouro custa R$ 90 por mês.")


@pytest.fixture
def client():
    settings = Settings(api_key="test-key", db_path=":memory:")
    return TestClient(create_app(settings, Store(":memory:")))


def _upload(client, name="faq.txt", data=HOURS.encode(), **kwargs):
    return client.post("/file", files={"file": (name, data, kwargs.get("mimetype", "text/plain"))}, headers=AUTH)


def _pdf(text: str) -> bytes:
    """The smallest PDF with one line of text."""
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    table = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % table


def _docx(*paragraphs: str, name="word/document.xml") -> bytes:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr(name, f"<w:document><w:body>{body}</w:body></w:document>")
    return buffer.getvalue()


def test_upload_search_and_delete(client):
    doc = _upload(client).json()
    assert doc["status"] == "done" and doc["name"] == "faq.txt" and doc["passages"] >= 1
    store = client.app.state.store
    # Without its accent, in the plural, in any case.
    assert "9h às 18h" in knowledge.search(store, [doc["id"]], "HORARIO funcionamento")[0]
    assert knowledge.search(store, ["another-file"], "horario") == []

    assert client.get(f"/file/{doc['id']}", headers=AUTH).status_code == 200
    assert client.patch(f"/file/{doc['id']}", json={"name": "x", "bytes": 1}, headers=AUTH).json()["bytes"] != 1
    assert [f["id"] for f in client.get("/file", headers=AUTH).json()] == [doc["id"]]
    client.delete(f"/file/{doc['id']}", headers=AUTH)
    assert client.get(f"/file/{doc['id']}", headers=AUTH).status_code == 404
    assert knowledge.search(store, [doc["id"]], "horario") == []  # its text is gone too


@pytest.mark.parametrize("query", ['"', 'a" OR "b', "NEAR(", "*", "col:x", "-", "file_id:1", ")(", "\x00"])
def test_a_question_cannot_act_as_query_syntax(client, query):
    doc = _upload(client).json()
    knowledge.search(client.app.state.store, [doc["id"]], query)  # no error, whatever the caller said


def test_upload_needs_the_key(client):
    assert client.post("/file", files={"file": ("a.txt", b"x")}).status_code == 401
    assert client.get("/file").status_code == 401


def test_file_types(client):
    store = client.app.state.store
    pdf = _upload(client, "a.pdf", _pdf("Garantia de dois anos"), mimetype="application/pdf").json()
    assert "dois anos" in knowledge.search(store, [pdf["id"]], "garantia")[0]
    docx = _upload(client, "a.docx", _docx("Entrega em &lt;3 dias", "Outro")).json()
    assert "Entrega em <3 dias" in knowledge.search(store, [docx["id"]], "entrega")[0]
    latin = _upload(client, "a.csv", "preço;média\n".encode("cp1252")).json()
    assert "preço" in knowledge.search(store, [latin["id"]], "preco")[0]


@pytest.mark.parametrize("name, data, why", [
    ("a.doc", b"x", ".docx"),
    ("a.exe", b"MZ", "can't be read"),
    ("a.txt", b"", "no text"),
    ("a.txt", b"bin\0ary", "isn't text"),
    ("a.pdf", b"not a pdf", "can't be read"),
    ("a.docx", b"not a zip", "can't be opened"),
])
def test_files_it_cannot_read_are_refused_with_the_reason(client, name, data, why):
    r = _upload(client, name, data)
    assert r.status_code == 400 and why in r.json()["detail"]
    assert client.get("/file", headers=AUTH).json() == []


def test_a_docx_with_no_document_is_refused(client):
    assert _upload(client, "a.docx", _docx("x", name="other.xml")).status_code == 400


def test_a_docx_bomb_stops_at_the_limit():
    bomb = _docx(*["a" * 1000] * (knowledge.MAX_TEXT_CHARS // 100))
    assert len(bomb) < 1_000_000
    assert len(knowledge.extract_text("a.docx", bomb)) <= knowledge.MAX_TEXT_CHARS


def test_a_path_in_the_file_name_goes_no_further(client):
    assert _upload(client, "..\\..\\x/y/a.txt").json()["name"] == "a.txt"


def test_too_big_is_refused(client, monkeypatch):
    monkeypatch.setattr(knowledge, "MAX_FILE_BYTES", 10)
    assert _upload(client, data=b"x" * 11).status_code == 413


def test_passages_overlap_and_keep_every_word():
    text = "\n".join(f"line number {i} " + "word " * 20 for i in range(100)) + "\n" + "long " * 1000
    passages = knowledge.chunks(text)
    assert all(len(p) <= knowledge.CHUNK_CHARS + knowledge.OVERLAP_CHARS + 2 for p in passages)
    joined = "\n".join(passages)
    assert all(f"line number {i} " in joined for i in range(100))
    assert any(set(a.split("\n")) & set(b.split("\n")) for a, b in zip(passages, passages[1:]))


def test_query_tool_checks():
    good = {"type": "query", "function": {"name": "faq"}, "knowledgeBases": [{"fileIds": ["1"]}]}
    check_tool(good)
    assert function_schema(good)["name"] == "faq" and function_schema(good)["required"] == ["query"]
    assert function_schema({**good, "function": {}})["name"] == "knowledge-search"
    described = {**good, "knowledgeBases": [{"fileIds": ["1"], "description": "prices"}]}
    assert "prices" in function_schema(described)["description"]
    for bad in ({"type": "query"}, {"type": "query", "knowledgeBases": []},
                {"type": "query", "knowledgeBases": [{"fileIds": []}]},
                {"type": "query", "knowledgeBases": ["x"]},
                {"type": "query", "knowledgeBases": [{"provider": knowledge.CUSTOM}]},
                {**good, "function": {"name": "a b"}}):
        with pytest.raises(ToolError):
            check_tool(bad)


def test_the_api_refuses_a_file_that_does_not_exist(client):
    tool = {"type": "query", "knowledgeBases": [{"fileIds": ["nope"]}]}
    assert client.post("/tool", json=tool, headers=AUTH).status_code == 400
    assistant = {"model": {"provider": "groq", "model": "x", "tools": [tool]}}
    assert client.post("/assistant", json=assistant, headers=AUTH).status_code == 400
    doc = _upload(client).json()
    tool["knowledgeBases"][0]["fileIds"] = [doc["id"]]
    assert client.post("/tool", json=tool, headers=AUTH).status_code == 201
    assert client.post("/assistant", json=assistant, headers=AUTH).status_code == 201


def _run(tool, arguments, store, messages=()):
    """What the model reads back from the tool."""
    results = []

    class Params:
        tool_call_id = "t1"

        def __init__(self):
            self.arguments = arguments

        async def result_callback(self, result, properties=None):
            results.append(result)

    async def nothing(*_):
        pass

    controls = CallControls(speak=nothing, end_call=nothing, transfer=None, current_call=lambda: {"id": "c1"},
                            assistant={}, default_url=None, store=store, messages=lambda: list(messages))
    asyncio.run(make_handler(tool, controls)(Params()))
    return results[0]


def test_the_model_reads_passages_or_is_told_there_are_none(client):
    store = client.app.state.store
    doc = _upload(client).json()
    tool = {"type": "query", "knowledgeBases": [{"provider": "google", "fileIds": [doc["id"]]}]}
    assert "R$ 90" in _run(tool, {"query": "plano ouro"}, store)
    long = _upload(client, data=("\n".join(f"item {i} " + "palavra " * 30 for i in range(100))).encode()).json()
    tool = {"type": "query", "knowledgeBases": [{"fileIds": [long["id"]]}]}
    assert _run(tool, {"query": "zzz"}, store) == knowledge.NOTHING
    assert _run(tool, {}, store) == knowledge.NOTHING


def test_a_short_file_goes_whole_when_the_words_differ(client):
    doc = _upload(client).json()
    found = knowledge.search(client.app.state.store, [doc["id"]], "quando vocês atendem?")
    assert "9h às 18h" in found[0]  # the model understands what the search can't


def test_words_match_by_their_start():
    assert knowledge.match_query("preços de ouro") == '"preç"* OR "de" OR "ouro"*'


def test_answers_have_a_limit(client, monkeypatch):
    store = client.app.state.store
    doc = _upload(client, data=("preço " * 5000).encode()).json()
    tool = {"type": "query", "knowledgeBases": [{"fileIds": [doc["id"]]}]}
    assert len(_run(tool, {"query": "preco"}, store)) <= knowledge.MAX_ANSWER_CHARS


def _server(monkeypatch, responder):
    real = httpx.AsyncClient
    sent = []

    def handler(request):
        sent.append(request)
        return responder(request)

    monkeypatch.setattr(knowledge.netguard, "client",
                        lambda **_: real(transport=httpx.MockTransport(handler)))

    async def safe(_url):
        return None

    monkeypatch.setattr(knowledge.netguard, "check_url_async", safe)
    return sent


CUSTOM = {"type": "query", "knowledgeBases": [{
    "provider": knowledge.CUSTOM, "server": {"url": "https://kb.test/search?token=SECRET", "secret": "s"}}]}


def test_your_own_search_gets_vapis_message(monkeypatch):
    sent = _server(monkeypatch, lambda r: httpx.Response(200, json={"documents": [
        {"content": "30 days", "similarity": 0.9}, {"similarity": 0.1}]}))
    said = [{"role": "user", "content": "returns?"}]
    assert _run(CUSTOM, {"query": "returns"}, None, said) == "30 days"
    body = json.loads(sent[0].content)["message"]
    assert body["type"] == "knowledge-base-request" and body["messages"] == said and body["query"] == "returns"
    assert sent[0].headers["x-vapi-secret"] == "s"


def test_your_own_search_can_answer_directly(monkeypatch):
    _server(monkeypatch, lambda r: httpx.Response(200, json={"message": {"type": "custom-message", "content": "Yes"}}))
    assert _run(CUSTOM, {"query": "x"}, None) == "Yes"


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, text="not json")])
def test_a_failing_search_never_shows_its_address(monkeypatch, response):
    _server(monkeypatch, lambda r: response)
    result = _run(CUSTOM, {"query": "x"}, None)
    assert "The tool failed" in result and "SECRET" not in result and "kb.test" not in result


def test_knowledge_base_id_on_an_assistant(client, monkeypatch):
    base = client.post("/knowledge-base", json={"provider": knowledge.CUSTOM, "server": {"url": "https://kb.test"}},
                       headers=AUTH).json()
    assert client.post("/knowledge-base", json={"provider": knowledge.CUSTOM}, headers=AUTH).status_code == 400
    assert client.post("/knowledge-base", json={"fileIds": ["nope"]}, headers=AUTH).status_code == 400
    model = {"provider": "groq", "model": "x", "knowledgeBaseId": base["id"]}
    assert client.post("/assistant", json={"model": model}, headers=AUTH).status_code == 201
    bad = {"provider": "groq", "model": "x", "knowledgeBaseId": "nope"}
    assert client.post("/assistant", json={"model": bad}, headers=AUTH).status_code == 400
    tools = assistant_tools({"model": model}, client.app.state.store)
    assert [t["type"] for t in tools] == ["query"]
    assert client.get(f"/knowledge-base/{base['id']}", headers=AUTH).status_code == 200
    assert len(client.get("/knowledge-base", headers=AUTH).json()) == 1
    assert client.patch(f"/knowledge-base/{base['id']}", json={"name": "x"}, headers=AUTH).json()["name"] == "x"
    client.delete(f"/knowledge-base/{base['id']}", headers=AUTH)
    assert assistant_tools({"model": model}, client.app.state.store) == []  # deleted later: the call goes on
