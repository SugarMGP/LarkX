import json
from types import SimpleNamespace

import requests

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
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error")
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
    fake = FakeResponse(status_code=400, content=b"invalid token")
    monkeypatch.setattr(media.requests, "get", lambda url, **kwargs: fake)
    with pytest.raises(KeyError, match="资源 key"):
        media.open_message_resource(auth_stub(), "file_v3_bad", "m3")
    assert fake.closed


def test_open_message_resource_keeps_other_errors_as_http_errors(monkeypatch):
    """上游因为参数/权限回 400 时，不能当成"资源不存在"。"""
    fake = FakeResponse(status_code=400, content=b"permission denied")
    monkeypatch.setattr(media.requests, "get", lambda url, **kwargs: fake)
    with pytest.raises(Exception) as error:
        media.open_message_resource(auth_stub(), "file_v3_x", "m3")
    assert not isinstance(error.value, KeyError)


def test_attachment_name_decodes_percent_encoding():
    from larkx.media import _attachment_name
    assert _attachment_name("attachment; filename*=UTF-8''%E6%96%87%E4%BB%B6.txt") == "文件.txt"
    assert _attachment_name("attachment; filename*=utf-8''%E6%96%87%E4%BB%B6.mp4") == "文件.mp4"


def _varint(value: int) -> bytes:
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        out += bytes([byte | (0x80 if value else 0)])
        if not value:
            return out


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
    # 日程事件在协议里是 bytes：8=开始(秒)、10=结束(秒)、29=标题
    title = "日程标题".encode()
    event.calendarEvent = (b"\x40" + _varint(1791874800) + b"\x50" + _varint(1791878400)
                           + b"\xea\x01" + _varint(len(title)) + title)
    facts = decoders.message_facts(16, protobuf_to_dict(event))
    assert facts["title"] == "日程标题"
    assert facts["start_ms"] == 1791874800000
    assert facts["end_ms"] == 1791878400000
    assert facts["attendees"] == 3

    # 文字类消息的正文由调用方从富文本自己渲染，这里不加工
    assert decoders.decode_message_content(3, P.FileContent(key="k", name="a.txt").SerializeToString())[0] == ""


def test_message_facts_survive_broken_card_json():
    """卡片 JSON 是发送方可控的，结构不对只能降级，不能抛异常打断消息处理。"""
    for raw in ('{"config":"x"}', '{"config":{"header":"y"}}', '{"config":{"header":{"title":["a"]}}}', "不是 json"):
        facts = decoders.message_facts(14, {"jsonCard": raw})
        assert facts["kind"] == "卡片" and facts["title"] == ""


def test_decode_message_content_parses_every_known_type():
    """每种类型的 content 都要能解析成字典（文字类另外给正文）。"""
    samples = {
        3: P.FileContent(key="file_v3_1", name="a.txt"),
        5: P.ImageContent(),
        7: P.AudioContent(key="file_v3_2", duration=3000),
        8: P.EmailContent(title="通知"),
        9: P.ShareGroupChatContent(),
        10: P.StickerContent(),
        11: P.MergeForwardContent(),
        12: P.CalendarContent(),
        14: P.CardContent(),
        15: P.MediaContent(key="file_v3_3"),
        16: P.ShareCalendarEventContent(),
        17: P.HongbaoContent(),
        19: P.VideoChatContent(),
        20: P.LocationContent(),
        23: P.ShareUserCardContent(name="张三"),
    }
    for message_type, content in samples.items():
        text, data = decoders.decode_message_content(message_type, content.SerializeToString())
        assert text == "", message_type
        assert isinstance(data, dict), message_type
    # 文字给正文，富文本给"标题\n正文"
    assert decoders.decode_message_content(4, P.TextContent(text="hello").SerializeToString())[0] == "hello"
    post = P.PostContent(title="周报", text="正文")
    assert decoders.decode_message_content(2, post.SerializeToString())[0] == "周报\n正文"
    # 没有对应结构的类型只给通用占位
    assert decoders.decode_message_content(13, b"")[1] is None


def test_rich_text_media_becomes_attachments():
    """图文混排：正文富文本里内嵌的图片/视频也要给出 key，否则后端下不到。"""
    from larkx.proto import lark_all_pb2 as L
    image = L.entities.RichTextElement.ImageProperty()
    image.token = "img_v3_1"
    image.originKey = "img_v3_1"
    image.originSize = 180
    media_prop = L.entities.RichTextElement.MediaProperty()
    media_prop.key = "file_v3_2"
    media_prop.name = "clip.mp4"
    media_prop.size = 112
    media_prop.mime = "video/mp4"
    content = {"richText": {"elements": {"dictionary": {
        "text": {"tag": 1, "property": b""},
        "img": {"tag": 2, "property": image.SerializeToString()},
        "media": {"tag": 18, "property": media_prop.SerializeToString()},
    }}}}
    assert media.message_attachments(4, content) == [
        {"kind": "image", "key": "img_v3_1", "name": "", "size": 180},
        {"kind": "video", "key": "file_v3_2", "name": "clip.mp4", "size": 112},
    ]
    # 属性坏掉时不要抛异常
    broken = {"richText": {"elements": {"dictionary": {"img": {"tag": 2, "property": b"\xff"}}}}}
    assert media.message_attachments(4, broken) == []


def test_docs_container_is_not_rendered_as_a_document():
    """DOCS 元素带子元素时只是正文容器，不该显示成"[文档]"。"""
    from larkx.proto import decoders
    rich = {"elementIds": ["doc"], "elements": {"dictionary": {
        "doc": {"tag": 22, "property": b"", "childIds": ["p"]},
        "p": {"tag": 1, "property": b"", "childIds": []},
    }}}
    text = decoders.extract_rich_text(rich)
    assert "[文档]" not in text


def test_card_facts_use_the_real_card_structure():
    """真实卡片：标题在 cardHeader，描述和链接在 richtext 里（actionUrl 是动作接口，不是给人点的）。"""
    from larkx.proto import lark_all_pb2 as L
    text = L.entities.RichTextElement.TextProperty(content="Go to learn")
    link = L.entities.RichTextElement.LinkProperty(url="https://applink.feishu.cn/client/web_app/open?appId=x")
    content = {
        "cardHeader": {"title": "学习计划通知", "theme": "green"},
        "actionUrl": "https://internal-api.feishu.cn/lark/card/api/actionv2",
        "richtext": {"elements": {"dictionary": {
            "1": {"tag": 1, "property": text.SerializeToString()},
            "2": {"tag": 17, "property": link.SerializeToString()},
        }}},
    }
    facts = decoders.message_facts(14, content)
    assert facts["title"] == "学习计划通知"
    assert facts["description"] == "Go to learn"
    assert facts["url"] == "https://applink.feishu.cn/client/web_app/open?appId=x"


def test_system_message_fills_the_template():
    facts = decoders.message_facts(6, {
        "messageTemplate": "{to_chatters} joined the group via a QR Code shared by {from_user}.",
        "contents": {"to_chatters": "袁滕谊", "from_user": "琳(Lynn)"}})
    assert facts["text"] == "袁滕谊 joined the group via a QR Code shared by 琳(Lynn)."


def test_sticker_is_an_attachment_and_uses_the_static_path(monkeypatch):
    """表情包的 key 是 v3_ 开头，走文件服务静态资源口。"""
    assert media.message_attachments(10, {"key": "v3_00mu_abc", "stickerId": "751"}) == [
        {"kind": "image", "key": "v3_00mu_abc", "name": "", "size": 0}]
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return FakeResponse(content=b"GIF89a", headers={"content-type": "image/gif"})

    monkeypatch.setattr(media.requests, "get", fake_get)
    _, content_type = media.open_message_resource(auth_stub(), "v3_00mu_abc", "m1")
    assert content_type == "image/gif"
    assert captured["url"].endswith("/static-resource/image/v3_00mu_abc")
