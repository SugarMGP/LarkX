"""富文本（markdown 渲染结果）的协议构造。

元素树来自真账号实测：根必须是 DOCS(22)，容器元素要把 property 显式写成空字节，
加粗/斜体/下划线是 TEXT 上的 style 而不是独立元素。任何一条不满足，网关要么回
400 unmarshal failed，要么客户端显示「本版本不支持查看这部分内容」。
"""

from types import SimpleNamespace

from larkx.client import LarkClient
from larkx.proto import builders, lark_all_pb2 as P


def element(rich, element_id):
    return rich.elements.dictionary[str(element_id)]


def walk(rich, element_id):
    """按 elementIds 顺序深度优先遍历，返回 [(tag, element)]。"""
    out = []

    def visit(eid):
        el = element(rich, eid)
        out.append((el.tag, el))
        for child in el.childIds:
            visit(child)

    for root in rich.elementIds:
        visit(root)
    return out


def tags(rich):
    return [tag for tag, _ in walk(rich, None)]


def test_root_is_a_docs_container_with_explicit_empty_property():
    rich = builders.build_rich_text([{"type": "paragraph", "spans": [{"type": "text", "text": "hi"}]}])
    assert len(rich.elementIds) == 1
    root = element(rich, rich.elementIds[0])
    assert root.tag == 22  # DOCS
    # 容器必须显式写 property：不写网关回 400 "PB payload in packet unmarshal failed"。
    assert root.HasField("property") and root.property == b""


def test_paragraph_holds_text_and_sets_inner_text():
    rich = builders.build_rich_text([{"type": "paragraph", "spans": [{"type": "text", "text": "hi"}]}])
    assert tags(rich) == [22, 3, 1]
    assert rich.innerText == "hi"
    root = element(rich, rich.elementIds[0])
    paragraph = element(rich, root.childIds[0])
    assert paragraph.HasField("property") and paragraph.property == b""


def test_text_styles_go_through_the_style_map():
    rich = builders.build_rich_text([{"type": "paragraph", "spans": [
        {"type": "text", "text": "a", "bold": True},
        {"type": "text", "text": "b", "italic": True},
        {"type": "text", "text": "c", "underline": True, "strike": True},
        {"type": "text", "text": "d"},
    ]}])
    elements = [el for tag, el in walk(rich, None) if tag == 1]
    assert elements[0].style["fontWeight"] == "bold"
    assert elements[1].style["fontStyle"] == "italic"
    assert elements[2].style["-lark-textDecoration"] == "underline lineThrough"
    assert not elements[3].style


def test_link_image_and_at_register_their_element_ids():
    rich = builders.build_rich_text([{"type": "paragraph", "spans": [
        {"type": "link", "text": "官网", "href": "https://example.com"},
        {"type": "image", "src": "img_v3_abc"},
        {"type": "at", "user_id": "42", "name": "张三"},
    ]}])
    assert tags(rich) == [22, 3, 6, 2, 5]
    assert len(rich.anchorIds) == 1 and len(rich.imageIds) == 1 and len(rich.atIds) == 1
    link = element(rich, rich.anchorIds[0])
    prop = P.entities.RichTextElement.AnchorProperty()
    prop.ParseFromString(link.property)
    assert (prop.href, prop.content, prop.textContent) == ("https://example.com", "官网", "官网")
    image = element(rich, rich.imageIds[0])
    image_prop = P.entities.RichTextElement.ImageProperty()
    image_prop.ParseFromString(image.property)
    assert image_prop.token == "img_v3_abc"
    at = element(rich, rich.atIds[0])
    at_prop = P.entities.RichTextElement.AtProperty()
    at_prop.ParseFromString(at.property)
    assert (at_prop.userId, at_prop.content) == ("42", "@张三")


def test_link_falls_back_to_href_when_label_is_empty():
    rich = builders.build_rich_text([{"type": "paragraph", "spans": [
        {"type": "link", "text": "", "href": "https://example.com"},
    ]}])
    link = element(rich, rich.anchorIds[0])
    prop = P.entities.RichTextElement.AnchorProperty()
    prop.ParseFromString(link.property)
    # content 为空的 A 元素在客户端完全不可见，所以标签回退成地址。
    assert prop.content == "https://example.com"


def test_heading_list_quote_and_code_block_map_to_their_tags():
    rich = builders.build_rich_text([
        {"type": "heading", "level": 2, "spans": [{"type": "text", "text": "标题"}]},
        {"type": "list", "ordered": True, "items": [[{"type": "text", "text": "甲"}]]},
        {"type": "quote", "blocks": [{"type": "paragraph", "spans": [{"type": "text", "text": "引用"}]}]},
        {"type": "divider"},
        {"type": "code", "language": "python", "text": "print(1)"},
    ])
    # DOCS > H2>TEXT / OL>LI>TEXT / QUOTE>P>TEXT / HR / CODE_BLOCK
    assert tags(rich) == [22, 24, 1, 27, 28, 1, 29, 3, 1, 32, 31]


def test_code_block_keeps_content_in_property_only():
    rich = builders.build_rich_text([{"type": "code", "language": "python", "text": "print(1)"}])
    code = [el for tag, el in walk(rich, None) if tag == 31][0]
    prop = P.entities.RichTextElement.CodeBlockProperty()
    prop.ParseFromString(code.property)
    assert prop.content == "print(1)"
    assert prop.language == builders.CODE_LANGUAGES["python"]
    # 再挂一个 TEXT 子元素会被解码器渲染两遍。
    assert not code.childIds


def test_rich_request_is_a_post_with_cid_and_reply():
    req = builders.build_rich_message_request(
        "c1", [{"type": "paragraph", "spans": [{"type": "text", "text": "hi"}]}],
        cid="fixed", reply_to="q1")
    assert (req.type, req.chatId, req.cid) == (2, "c1", "fixed")
    assert (req.rootId, req.parentId) == ("q1", "q1")


def test_at_user_id_list_only_takes_numeric_ids():
    req = builders.build_rich_message_request("c1", [{"type": "paragraph", "spans": [
        {"type": "at", "user_id": "42", "name": "张三"},
        {"type": "at", "user_id": "all", "name": "所有人"},
    ]}], cid="fixed")
    assert list(req.atUserIdList) == [42]


def test_empty_blocks_are_rejected():
    try:
        builders.build_rich_text([])
    except ValueError:
        return
    raise AssertionError("empty rich text should raise")


def test_send_rich_message_uses_command_5_and_returns_message_id(monkeypatch):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    client = LarkClient(auth)
    packets = []
    response = P.improto.Packet()
    response.payload = P.messages.PutMessageResponse(
        message=P.entities.Message(id="m1")).SerializeToString()
    monkeypatch.setattr(client, "_gateway_post",
                        lambda packet: packets.append(packet) or response.SerializeToString())

    message_id = client.send_rich_message(
        "c1", [{"type": "paragraph", "spans": [{"type": "text", "text": "hi"}]}], cid="fixed")

    assert message_id == "m1"
    assert packets[0].cmd == 5
    sent = P.messages.PutMessageRequest()
    sent.ParseFromString(packets[0].payload)
    assert (sent.type, sent.cid, sent.chatId) == (2, "fixed", "c1")
    assert sent.content.richText.elementIds
