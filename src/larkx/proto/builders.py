import time

from google.protobuf.json_format import MessageToDict

from .ids import generate_request_cid


def _all():
    """完整协议定义（延迟导入，避免启动时加载大文件）。"""
    from . import lark_all_pb2
    return lark_all_pb2


def wrap_packet(cmd: int, payload_msg, request_id: str):
    pkt = _all().improto.Packet()
    pkt.payloadType = 1
    pkt.cmd = cmd
    pkt.cid = request_id
    pkt.payload = payload_msg.SerializeToString()
    return pkt


def build_message_request(msg_type: int, chat_id: str, text: str = '', reply_to: str = None,
                          cid: str = None, thread_id: str = None):
    """组装一条消息请求；用的是完整协议类型，调用方可以再往正文里插真实 AT 节点。

    thread_id 是话题首帖的消息 id。THREAD_V2 话题群里回复进话题靠 rootId+parentId **成对**
    出现（只给 rootId 会新开一个话题），且**不能**设 isReplyInThread——那样网关回
    400 `replyInThread not support chat`。thread_id 优先于 reply_to。
    """
    from . import lark_all_pb2 as L
    req = L.messages.PutMessageRequest()
    req.type = msg_type
    req.chatId = str(chat_id)
    req.cid = str(cid) if cid else generate_request_cid()
    req.isNotified = 1
    req.version = 1
    if thread_id:
        req.rootId = req.parentId = str(thread_id)
    elif reply_to:
        req.rootId = str(reply_to)
        req.parentId = str(reply_to)
    if text:
        fill_text_rich_text(req.content.richText, text)
    return req


def build_send_message_packet(text: str, chat_id: str, request_id: str,
                              reply_to: str=None, cid: str=None):
    """发文字。reply_to 是引用回复：rootId=parentId=被回复消息，且不设 isReplyInThread，
    普通群里就是普通引用样式（isReplyInThread 会让消息进话题，本库不支持话题）。"""
    req = _new_message_request(4, chat_id, cid)
    _set_reply(req, reply_to)
    fill_text_rich_text(req.content.richText, text)
    return wrap_packet(5, req, request_id)


def build_image_message_packet(image_key: str, chat_id: str, request_id: str,
                               reply_to: str=None, cid: str=None):
    """发图片。image_key 由 media.upload_image 上传得到，放在 content.cryptoToken。"""
    req = _new_message_request(5, chat_id, cid)
    _set_reply(req, reply_to)
    req.content.cryptoToken = str(image_key)
    return wrap_packet(5, req, request_id)


def build_file_message_packet(file_key: str, file_name: str, file_size: int, file_mime: str,
                              chat_id: str, request_id: str, reply_to: str=None, cid: str=None):
    """发文件。文件先经 media.upload_file 上传到该会话。"""
    req = _new_message_request(3, chat_id, cid)
    _set_reply(req, reply_to)
    req.content.fileKey = str(file_key)
    req.content.fileName = str(file_name)
    req.content.fileSize = int(file_size)
    req.content.fileMime = str(file_mime)
    return wrap_packet(5, req, request_id)


def wrap_frame(packet) -> bytes:
    """把网关包包进网页端的帧头（basic.v1.Frame）。"""
    frame = _all().WebFrame()
    seconds = int(time.time())
    frame.seqid = seconds
    frame.logid = seconds
    frame.service = 1
    frame.method = 1
    entry = _all().WebFrame.ExtendedEntry()
    entry.key = 'x-request-time'
    entry.value = str(int(time.time() * 1000))
    frame.headers.append(entry)
    frame.payloadType = 'pb'
    frame.payload = packet.SerializeToString()
    return frame.SerializeToString()


def _new_message_request(msg_type: int, chat_id: str, cid: str=None):
    req = _all().messages.PutMessageRequest()
    req.type = msg_type
    req.chatId = str(chat_id)
    req.cid = str(cid) if cid else generate_request_cid()
    req.isNotified = 1
    req.version = 1
    return req


def _set_reply(req, reply_to: str=None) -> None:
    """引用回复：rootId=parentId=被回复消息，不设 isReplyInThread。"""
    if reply_to:
        req.rootId = str(reply_to)
        req.parentId = str(reply_to)


def fill_text_rich_text(rich, text: str) -> None:
    """网页协议纯文本必须带一个 TEXT 元素；inner_text 是展示文本。"""
    element_id = generate_request_cid()
    rich.elementIds.append(element_id)
    rich.innerText = str(text)
    rich.elements.dictionary[element_id].tag = 1
    tp = _all().entities.RichTextElement.TextProperty()
    tp.content = str(text)
    rich.elements.dictionary[element_id].property = tp.SerializeToString()


def at_element(user_id: str, name: str) -> bytes:
    """AT 元素的 property 字节（放进富文本元素的 property 字段）。"""
    prop = _all().entities.RichTextElement.AtProperty(userId=str(user_id), content=f"@{name}")
    return prop.SerializeToString()


def text_element(content: str) -> bytes:
    """文字元素的 property 字节。"""
    return _all().entities.RichTextElement.TextProperty(content=str(content)).SerializeToString()


def build_text_rich_text(text: str) -> dict:
    """编辑消息用 api() 的 richText protobuf JSON。"""
    rich = _all().entities.RichText()
    fill_text_rich_text(rich, text)
    return MessageToDict(rich, preserving_proto_field_name=True)


def decode_put_message_response(content: bytes) -> str:
    pkt = _all().improto.Packet()
    pkt.ParseFromString(content)
    resp = _all().messages.PutMessageResponse()
    resp.ParseFromString(pkt.payload)
    return resp.message.id




# ---------- 富文本（markdown 渲染结果） ----------

# RichTextElement.Tag：块级容器 + 行内元素
_RICH_TAGS = {
    "docs": 22, "paragraph": 3, "figure": 4, "quote": 29, "divider": 32,
    "h1": 23, "h2": 24, "h3": 25, "ul": 26, "ol": 27, "li": 28,
    "text": 1, "image": 2, "at": 5, "link": 6, "code": 30, "markdown": 49,
}

# CodeBlockProperty.Language
CODE_LANGUAGES = {
    "js": 0, "javascript": 0, "apache": 1, "bash": 2, "sh": 2, "shell": 20,
    "cs": 3, "csharp": 3, "cpp": 4, "c++": 4, "coffeescript": 5, "html": 6,
    "xml": 7, "http": 8, "json": 9, "java": 10, "makefile": 11, "make": 11,
    "markdown": 12, "md": 12, "nginx": 13, "objc": 14, "objective-c": 14,
    "php": 15, "perl": 16, "python": 17, "py": 17, "ruby": 18, "rb": 18,
    "sql": 19, "go": 21, "golang": 21, "scss": 22, "lua": 23, "django": 24,
    "kotlin": 25, "kt": 25,
}


class _RichTextWriter:
    """segment IR -> RichText。元素 id 顺序生成，网关会重新编号。"""

    def __init__(self):
        self.rt = _all().entities.RichText()
        self.rt.version = 1
        self._seq = 0

    def node(self, tag: str, prop: bytes | None = None, children=None) -> str:
        self._seq += 1
        element_id = str(self._seq)
        element = self.rt.elements.dictionary[element_id]
        element.tag = _RICH_TAGS[tag]
        # 容器元素也必须显式写 property（空字节）：不写网关回 400 unmarshal failed。
        element.property = prop if prop is not None else b""
        for child in children or []:
            element.childIds.append(child)
        return element_id

    def text(self, content: str, *, bold=False, italic=False, underline=False, strike=False) -> str:
        entities = _all().entities.RichTextElement
        element_id = self.node("text", entities.TextProperty(content=content).SerializeToString())
        style = {}
        if bold:
            style["fontWeight"] = "bold"
        if italic:
            style["fontStyle"] = "italic"
        decoration = [name for name, on in (("underline", underline), ("lineThrough", strike)) if on]
        if decoration:
            style["-lark-textDecoration"] = " ".join(decoration)
        element = self.rt.elements.dictionary[element_id]
        for key, value in style.items():
            element.style[key] = value
        return element_id

    def link(self, text: str, href: str) -> str:
        entities = _all().entities.RichTextElement
        prop = entities.AnchorProperty(href=href, content=text, textContent=text)
        element_id = self.node("link", prop.SerializeToString())
        self.rt.anchorIds.append(element_id)
        return element_id

    def image(self, token: str) -> str:
        prop = _all().entities.RichTextElement.ImageProperty(token=token)
        element_id = self.node("image", prop.SerializeToString())
        self.rt.imageIds.append(element_id)
        return element_id

    def at(self, user_id: str, name: str) -> str:
        prop = _all().entities.RichTextElement.AtProperty(
            userId=str(user_id), content=f"@{name}" if name else "@")
        element_id = self.node("at", prop.SerializeToString())
        self.rt.atIds.append(element_id)
        return element_id

    def code(self, content: str) -> str:
        child = self.text(content)
        return self.node("code", None, [child])

    def code_block(self, content: str, language: str) -> str:
        """代码块用 MARKDOWN(49) 元素装一段围栏代码，不要用 CODE_BLOCK(31)/CODE_BLOCK_V2(37)。

        真账号截图实测（三种写法发到同一个群对比）：
        - tag 31 + CodeBlockProperty → 客户端渲染成只有一个行号槽的**空框**，正文完全不显示；
        - tag 31 + upgradeElement=V2 → 一样是空框（服务端把 upgradeElement 丢掉了）；
        - tag 37 + CodeBlockV2Property → 能显示代码，但只有行号和等宽字，没有语言栏、没有复制按钮、没有语法高亮；
        - **tag 49 + MarkdownProperty("```lang\n…\n```") → 语言栏、复制按钮、语法高亮齐全**，和客户端自己发的一致。

        所以按 tag 49 发，正文就是围栏代码块本身。
        """
        language = str(language or "").strip().lower()
        fence = "```"
        body = "\n".join(_code_lines(content))
        return self.node("markdown", _all().entities.RichTextElement.MarkdownProperty(
            content=f"{fence}{language}\n{body}\n{fence}").SerializeToString())


def _code_lines(content: str) -> list[str]:
    """按行切分；去掉结尾的空行，中间的空行保留。"""
    lines = str(content or "").split("\n")
    while len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    return lines


def _spans(writer: _RichTextWriter, spans) -> list[str]:
    out: list[str] = []
    for span in spans or []:
        kind = span.get("type")
        if kind == "text":
            content = str(span.get("text") or "")
            if span.get("code"):
                out.append(writer.code(content))
            else:
                out.append(writer.text(
                    content,
                    bold=bool(span.get("bold")), italic=bool(span.get("italic")),
                    underline=bool(span.get("underline")), strike=bool(span.get("strike")),
                ))
        elif kind == "link":
            href = str(span.get("href") or "")
            label = str(span.get("text") or "") or href
            if href and label:
                out.append(writer.link(label, href))
            elif label:
                out.append(writer.text(label))
        elif kind == "image":
            token = str(span.get("src") or "")
            if token:
                out.append(writer.image(token))
        elif kind == "at":
            user_id = str(span.get("user_id") or "")
            if user_id:
                out.append(writer.at(user_id, str(span.get("name") or "")))
    return out


def _block(writer: _RichTextWriter, block: dict) -> list[str]:
    kind = block.get("type")
    if kind == "paragraph":
        return [writer.node("paragraph", None, _spans(writer, block.get("spans")))]
    if kind == "heading":
        level = min(max(int(block.get("level") or 1), 1), 3)
        return [writer.node(f"h{level}", None, _spans(writer, block.get("spans")))]
    if kind == "list":
        items = [writer.node("li", None, _spans(writer, item)) for item in block.get("items") or []]
        return [writer.node("ol" if block.get("ordered") else "ul", None, items)] if items else []
    if kind == "quote":
        lines = [writer.node("paragraph", None, _spans(writer, item.get("spans")))
                 for item in block.get("blocks") or []]
        return [writer.node("quote", None, lines)] if lines else []
    if kind == "divider":
        return [writer.node("divider", None)]
    if kind == "code":
        return [writer.code_block(str(block.get("text") or ""), str(block.get("language") or ""))]
    return []


def rich_text_plain(blocks) -> str:
    """IR 的纯文本版本（给 innerText 用，服务端搜索和预览依赖它）。"""
    lines: list[str] = []

    def spans_text(spans) -> str:
        parts = []
        for span in spans or []:
            kind = span.get("type")
            if kind == "text":
                parts.append(str(span.get("text") or ""))
            elif kind == "link":
                parts.append(str(span.get("text") or "") or str(span.get("href") or ""))
            elif kind == "image":
                parts.append("[图片]")
            elif kind == "at":
                parts.append(f"@{span.get('name') or ''}")
        return "".join(parts)

    for block in blocks or []:
        kind = block.get("type")
        if kind in ("paragraph", "heading"):
            lines.append(spans_text(block.get("spans")))
        elif kind == "list":
            for index, item in enumerate(block.get("items") or [], 1):
                prefix = f"{index}. " if block.get("ordered") else "- "
                lines.append(prefix + spans_text(item))
        elif kind == "quote":
            for item in block.get("blocks") or []:
                lines.append("> " + spans_text(item.get("spans")))
        elif kind == "divider":
            lines.append("---")
        elif kind == "code":
            lines.append(str(block.get("text") or ""))
    return "\n".join(line for line in lines if line is not None)


def at_user_ids(blocks) -> list[str]:
    """IR 里所有 AT 的数字 user id（"all" 塞不进 int64 的 atUserIdList）。"""
    ids: list[str] = []

    def walk(spans):
        for span in spans or []:
            if span.get("type") == "at":
                user_id = str(span.get("user_id") or "")
                if user_id and user_id not in ids:
                    ids.append(user_id)

    for block in blocks or []:
        kind = block.get("type")
        if kind in ("paragraph", "heading"):
            walk(block.get("spans"))
        elif kind == "list":
            for item in block.get("items") or []:
                walk(item)
        elif kind == "quote":
            for item in block.get("blocks") or []:
                walk(item.get("spans"))
    return ids


def build_rich_text(blocks):
    """segment IR -> entities.RichText（根节点是 DOCS 容器，和飞书客户端一致）。"""
    writer = _RichTextWriter()
    children: list[str] = []
    for block in blocks or []:
        children.extend(_block(writer, block))
    if not children:
        raise ValueError("富文本没有可发送的内容")
    writer.rt.elementIds.append(writer.node("docs", None, children))
    writer.rt.innerText = rich_text_plain(blocks)
    return writer.rt


def build_rich_message_request(chat_id: str, blocks, cid: str = None, reply_to: str = None,
                               thread_id: str = None):
    """组装一条富文本消息（type=2 POST）。thread_id 语义同 build_message_request。"""
    req = _all().messages.PutMessageRequest()
    req.type = 2
    req.chatId = str(chat_id)
    req.cid = str(cid) if cid else generate_request_cid()
    req.isNotified = 1
    req.version = 1
    if thread_id:
        req.rootId = req.parentId = str(thread_id)
    elif reply_to:
        req.rootId = str(reply_to)
        req.parentId = str(reply_to)
    req.content.richText.CopyFrom(build_rich_text(blocks))
    for user_id in at_user_ids(blocks):
        if user_id.isdigit() and 0 < int(user_id) < 2 ** 63:
            req.atUserIdList.append(int(user_id))
    return req
