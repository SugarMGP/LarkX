import json
from types import SimpleNamespace

import pytest

from larkx import media
from larkx.proto import builders, decoders, lark_all_pb2 as L, proto_pb2 as P
from protobuf_to_dict import protobuf_to_dict


def anchor(href: str, content: str) -> bytes:
    prop = L.entities.RichTextElement.AnchorProperty(href=href, content=content)
    return prop.SerializeToString()


def rich_text(*elements) -> dict:
    dictionary = {}
    element_ids = []
    for index, (tag, prop) in enumerate(elements):
        element_id = f"e{index}"
        dictionary[element_id] = {"tag": tag, "property": prop}
        element_ids.append(element_id)
    return {"elementIds": element_ids, "elements": {"dictionary": dictionary}}


def test_link_element_is_rendered_as_markdown():
    text = decoders.extract_rich_text(rich_text(
        (1, P.TextProperty(content="看看这个").SerializeToString()),
        (6, anchor("https://example.com/a", "文档")),
    ))
    assert text == "看看这个[文档](https://example.com/a)"


def test_link_without_different_text_shows_url_only():
    text = decoders.extract_rich_text(rich_text((6, anchor("https://example.com/b", "https://example.com/b"))))
    assert text == "https://example.com/b"


def test_rich_text_element_types_are_all_rendered():
    elements = [
        (1, P.TextProperty(content="普通").SerializeToString()),
        (7, L.entities.RichTextElement.BoldProperty(content="粗").SerializeToString()),
        (8, L.entities.RichTextElement.ItalicProperty(content="斜").SerializeToString()),
        (9, L.entities.RichTextElement.UnderlineProperty(content="下划").SerializeToString()),
        (5, P.AtProperty(userId="u1", content="@张三").SerializeToString()),
        (36, L.entities.RichTextElement.MentionProperty(content="提及").SerializeToString()),
        (47, L.entities.RichTextElement.AtUserGroupProperty(
            userGroupId=1, defaultGroupName="群").SerializeToString()),
        (49, L.entities.RichTextElement.MarkdownProperty(content="**md**").SerializeToString()),
        (31, L.entities.RichTextElement.CodeBlockProperty(
            language=1, content="code()").SerializeToString()),
    ]
    text = decoders.extract_rich_text(rich_text(*elements))
    assert text == "普通**粗***斜*下划@张三提及@群**md**code()"


def test_media_elements_use_placeholders():
    text = decoders.extract_rich_text(rich_text(
        (2, b"\x12\x08img-key"),
        (10, L.entities.RichTextElement.EmotionProperty(key="e1").SerializeToString()),
        (18, L.entities.RichTextElement.MediaProperty(key="k", name="v.mp4").SerializeToString()),
        (22, L.entities.RichTextElement.DocsProperty().SerializeToString()),
    ))
    assert text == "[图片][表情][视频][文档]"


def test_image_and_file_packets_carry_the_uploaded_keys():
    image = builders.build_image_message_packet("img_v3_1", "c1", "req", cid="cid1")
    req = P.PutMessageRequest(); req.ParseFromString(image.payload)
    assert (req.type, req.chatId, req.cid, req.content.cryptoToken) == (5, "c1", "cid1", "img_v3_1")

    file = builders.build_file_message_packet("file_v3_1", "a.pdf", 12, "application/pdf", "c1", "req", cid="cid2")
    req = P.PutMessageRequest(); req.ParseFromString(file.payload)
    assert (req.type, req.content.fileKey, req.content.fileName, req.content.fileSize, req.content.fileMime) == (
        3, "file_v3_1", "a.pdf", 12, "application/pdf")
    assert req.cid == "cid2"


def test_reply_relations_use_root_and_parent_only():
    req = P.PutMessageRequest(); req.ParseFromString(
        builders.build_send_message_packet("hi", "c1", "req", reply_to="m1").payload)
    assert (req.rootId, req.parentId, req.isReplyInThread) == ("m1", "m1", False)


class FakeResponse:
    def __init__(self, text="", body=None, content=b"", headers=None, status_code=200):
        self.text = text
        self._body = body or {}
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        return None

    def json(self):
        return self._body

    def close(self):
        self.closed = True


def auth_stub():
    return SimpleNamespace(
        cookies={"session": "x"},
        region=SimpleNamespace(file_host="https://file.example", web_origin="https://web.example"),
        gateway_app_id="app1",
    )


def test_upload_image_returns_key(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["files"] = kwargs["files"]
        return FakeResponse(text="0img_v3_abc-123")

    monkeypatch.setattr(media.requests, "post", fake_post)
    assert media.upload_image(auth_stub(), b"png-bytes", "a.png") == "img_v3_abc-123"
    assert captured["url"].endswith("/api/image")
    assert captured["files"]["image"][0] == "a.png"


def test_upload_file_returns_file_info(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        return FakeResponse(body={"status": "success", "data": {
            "key": "file_v3_1", "name": "a.pdf", "size": 12, "mimetype": "application/pdf"}})

    monkeypatch.setattr(media.requests, "post", fake_post)
    info = media.upload_file(auth_stub(), "c1", b"pdf-bytes", "a.pdf")
    assert info == {"key": "file_v3_1", "name": "a.pdf", "size": 12, "mime": "application/pdf"}
    assert captured["url"].endswith("/api/chats/c1/files")


def test_upload_file_reports_failure(monkeypatch):
    monkeypatch.setattr(media.requests, "post", lambda url, **kwargs: FakeResponse(body={"status": "fail", "message": "太大"}))
    with pytest.raises(ValueError, match="太大"):
        media.upload_file(auth_stub(), "c1", b"x", "a.pdf")


class _Response:
    def __init__(self, body=b"x"):
        self.body = body
        self.status_code = 200
        self.headers = {"content-type": "application/octet-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, size):
        yield self.body


def test_download_only_sends_cookies_to_the_same_region(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["cookies"] = kwargs.get("cookies")
        return _Response()

    monkeypatch.setattr("larkx.media.requests.get", fake_get)
    auth = SimpleNamespace(cookies={"session": "s"},
                           region=SimpleNamespace(domain="feishu.cn"))
    media.download(auth, "https://internal-api-lark-file.feishu.cn/x")
    assert seen["cookies"] == {"session": "s"}
    media.download(auth, "https://evil.example.com/x")
    assert seen["cookies"] == {}


def test_open_message_resource_streams_and_guesses_type(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["stream"] = kwargs.get("stream")
        return FakeResponse(headers={
            "content-type": "text/plain; charset=utf-8",
            "content-disposition": 'attachment; filename="note.txt"'})

    monkeypatch.setattr(media.requests, "get", fake_get)
    response, content_type = media.open_message_resource(auth_stub(), "file_v3_1", "m1")
    assert content_type == "text/plain"
    assert captured["url"] == "https://file.example/download/messages/m1/keys/file_v3_1"
    assert captured["stream"] is True


def test_open_message_resource_guesses_type_from_name(monkeypatch):
    monkeypatch.setattr(media.requests, "get", lambda url, **kwargs: FakeResponse(
        headers={"content-type": "text/plain", "content-disposition": 'attachment; filename="clip.mp4"'}))
    _, content_type = media.open_message_resource(auth_stub(), "file_v3_2", "m2")
    assert content_type == "video/mp4"


def test_open_message_resource_reports_unknown_key(monkeypatch):
    fake = FakeResponse(status_code=400)
    monkeypatch.setattr(media.requests, "get", lambda url, **kwargs: fake)
    with pytest.raises(KeyError, match="资源 key"):
        media.open_message_resource(auth_stub(), "file_v3_bad", "m3")
    assert fake.closed


def test_message_facts_expose_the_important_fields():
    """非文字消息要把意图上重要的原始信息挑出来，文本怎么拼由调用方决定。"""
    card = P.CardContent()
    card.cardHeader.title = "审批通过"
    card.cardHeader.subtitle = "报销单"
    card.cardDesc = "9月"
    card.actionUrl = "https://example.com/approval/1"
    facts = decoders.message_facts(14, protobuf_to_dict(card))
    assert facts == {"kind": "卡片", "title": "审批通过", "subtitle": "报销单",
                     "description": "9月", "url": "https://example.com/approval/1"}

    card2 = P.CardContent()
    card2.jsonCard = json.dumps({"config": {"header": {"title": {"content": "新卡片"},
                                                     "subtitle": {"content": "副标题"}}},
                                 "elements": [{"tag": "markdown", "content": "正文第一段"}]})
    facts = decoders.message_facts(14, protobuf_to_dict(card2))
    assert (facts["title"], facts["subtitle"], facts["description"]) == ("新卡片", "副标题", "正文第一段")

    meeting = P.VideoChatContent()
    meeting.meetingCard.topic = "双周会"
    meeting.meetingCard.startTimeMs = 1792738800000
    meeting.meetingCard.endTimeMs = 1792742400000
    facts = decoders.message_facts(19, protobuf_to_dict(meeting))
    assert facts == {"kind": "视频会议", "topic": "双周会",
                     "start_ms": 1792738800000, "end_ms": 1792742400000}

    place = P.LocationContent()
    place.locationDescription.name = "望京SOHO"
    place.locationDescription.description = "T1 18层"
    assert decoders.message_facts(20, protobuf_to_dict(place)) == \
        {"kind": "位置", "name": "望京SOHO", "description": "T1 18层"}

    person = P.ShareUserCardContent()
    person.name = "张三"
    person.userId = "7690398870819343329"
    assert decoders.message_facts(23, protobuf_to_dict(person)) == \
        {"kind": "名片", "name": "张三", "user_id": "7690398870819343329"}

    event = P.ShareCalendarEventContent()
    event.attendeesCount = 3
    assert decoders.message_facts(16, protobuf_to_dict(event)) == {"kind": "日程", "attendees": 3}

    # 文字类消息的正文由调用方从富文本自己渲染，这里不加工
    assert decoders.decode_message_content(3, P.FileContent(key="k", name="a.txt").SerializeToString())[0] == ""
