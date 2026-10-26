import json
from types import SimpleNamespace

import pytest

from larkx import media
from larkx.proto import builders, decoders, lark_all_pb2 as L, proto_pb2 as P


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


def test_placeholders_carry_the_important_facts():
    """非文字消息的占位文本要带上意图相关的关键信息。"""
    card = P.CardContent()
    card.cardHeader.title = "审批通过"
    card.actionUrl = "https://example.com/approval/1"
    assert decoders.decode_message_content(14, card.SerializeToString())[0] == \
        "[卡片:审批通过 · https://example.com/approval/1]"

    card2 = P.CardContent()
    card2.cardHeader.title = "报销单"
    card2.jsonCard = json.dumps({"config": {"actions": [{"behaviors": [
        {"type": "open_url", "default_url": "https://example.com/expense/9"}]}]}})
    assert "https://example.com/expense/9" in decoders.decode_message_content(14, card2.SerializeToString())[0]

    meeting = P.VideoChatContent()
    meeting.meetingCard.topic = "双周会"
    meeting.meetingCard.startTimeMs = 1792738800000
    meeting.meetingCard.endTimeMs = 1792742400000
    assert decoders.decode_message_content(19, meeting.SerializeToString())[0].startswith("[视频会议:双周会 · ")

    place = P.LocationContent()
    place.locationDescription.name = "望京SOHO"
    place.locationDescription.description = "T1 18层"
    assert decoders.decode_message_content(20, place.SerializeToString())[0] == "[位置:望京SOHO · T1 18层]"

    event = P.ShareCalendarEventContent()
    event.attendeesCount = 3
    assert decoders.decode_message_content(16, event.SerializeToString())[0] == "[日程:3人参加]"

    card_msg = P.ShareUserCardContent()
    card_msg.name = "张三"
    card_msg.userId = "7690398870819343329"
    assert decoders.decode_message_content(23, card_msg.SerializeToString())[0] == "[名片:张三]"
