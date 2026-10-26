import json
from datetime import datetime

from protobuf_to_dict import protobuf_to_dict
from . import proto_pb2 as P

TAG_PLACEHOLDERS = {2: '[图片]', 10: '[表情]', 18: '[视频]', 22: '[文档]'}
# 富文本元素 tag -> 取文本内容的属性类（tag 见 entities.RichTextElement.Tag）
_CONTENT_CLASSES = {
    1: 'TextProperty', 5: 'AtProperty', 7: 'BoldProperty', 8: 'ItalicProperty',
    9: 'UnderlineProperty', 30: 'CodeProperty', 31: 'CodeBlockProperty',
    36: 'MentionProperty', 37: 'CodeBlockV2Property', 49: 'MarkdownProperty',
}
_LARK_ALL = None
MSG_TYPE_NAMES = {
    0: 'UNKNOWN', 2: 'POST', 3: 'FILE', 4: 'TEXT', 5: 'IMAGE', 6: 'SYSTEM',
    7: 'AUDIO', 8: 'EMAIL', 9: 'SHARE_GROUP_CHAT', 10: 'STICKER',
    11: 'MERGE_FORWARD', 12: 'CALENDAR', 13: 'CLOUD_FILE', 14: 'CARD',
    15: 'MEDIA', 16: 'SHARE_CALENDAR_EVENT', 17: 'HONGBAO',
    18: 'GENERAL_CALENDAR', 19: 'VIDEO_CHAT', 20: 'LOCATION',
    22: 'COMMERCIALIZED_HONGBAO', 23: 'SHARE_USER_CARD', 24: 'TODO', 25: 'FOLDER',
}
CHAT_TYPE_NAMES = {0: 'UNKNOWN', 1: 'P2P', 2: 'GROUP', 3: 'TOPIC_GROUP'}


def enum_to_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        names = P.entities.Message.Type.keys()
        if value in names:
            return P.entities.Message.Type.Value(value)
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def message_flags(message: dict) -> dict:
    """把网页协议的 status/isRemoved 包装成召回与编辑标记。"""
    status = enum_to_int(message.get('status', 0))
    return {
        'recalled': status == 2 or bool(message.get('isRemoved')),
        'edited': status == 3,
    }


def extract_rich_text(rich_text_dict):
    """把富文本渲染成 markdown 风格的纯文本：文字照旧，链接渲染成 [文字](地址)。"""
    text = ''
    try:
        dictionary = rich_text_dict['elements']['dictionary']
        element_ids = rich_text_dict.get('elementIds') or []
        if element_ids:
            listed = set(element_ids)
            ordered = [(k, dictionary[k]) for k in element_ids if k in dictionary]
            ordered += [(k, v) for k, v in dictionary.items() if k not in listed]
        else:
            try:
                ordered = sorted(dictionary.items(), key=lambda item: int(item[0]))
            except Exception:
                ordered = list(dictionary.items())
        for k, v in ordered:
            text += _render_element(v.get('tag', 1), v.get('property'))
    except Exception:
        pass
    return text


def _render_element(tag: int, prop) -> str:
    """按元素类型渲染。tag 见 entities.RichTextElement.Tag。"""
    if tag in TAG_PLACEHOLDERS:
        return TAG_PLACEHOLDERS[tag]
    if not prop:
        return ''
    if tag == 6:
        return _link_text(prop)
    if tag == 17:
        return _link_only(prop)
    if tag == 47:
        group = _group_name(prop)
        return f'@{group}' if group else ''
    if tag == 7:
        content = _content(prop, 'BoldProperty')
        return f'**{content}**' if content else ''
    if tag == 8:
        content = _content(prop, 'ItalicProperty')
        return f'*{content}*' if content else ''
    class_name = _CONTENT_CLASSES.get(tag)
    if class_name:
        return _content(prop, class_name)
    return TAG_PLACEHOLDERS.get(tag, '')


def _content(prop: bytes, class_name: str) -> str:
    """从元素属性里取文本内容。"""
    cls = getattr(_elements().entities.RichTextElement, class_name, None)
    if cls is None:
        return ''
    obj = cls()
    try:
        obj.ParseFromString(prop)
    except Exception:
        return ''
    return getattr(obj, 'content', '') or ''


def _group_name(prop: bytes) -> str:
    """@群：取默认群名。"""
    cls = getattr(_elements().entities.RichTextElement, 'AtUserGroupProperty', None)
    if cls is None:
        return ''
    obj = cls()
    try:
        obj.ParseFromString(prop)
    except Exception:
        return ''
    return obj.defaultGroupName or obj.i18nGroupName or ''


def _link_text(prop: bytes) -> str:
    """链接：显示文字和地址都有就渲染成 [文字](地址)，相同则只给地址。"""
    anchor = _elements().entities.RichTextElement.AnchorProperty()
    try:
        anchor.ParseFromString(prop)
    except Exception:
        return '[链接]'
    href, label = anchor.href, anchor.content
    if href and label and label != href:
        return f'[{label}]({href})'
    return href or label or '[链接]'


def _link_only(prop: bytes) -> str:
    """卡片里的链接只有地址。"""
    link = _elements().entities.RichTextElement.LinkProperty()
    try:
        link.ParseFromString(prop)
    except Exception:
        return '[链接]'
    return f"[链接]({link.url})" if link.url else '[链接]'


def _elements():
    """富文本元素类型在完整 proto 里，延迟导入避免启动时加载大文件。"""
    global _LARK_ALL
    if _LARK_ALL is None:
        from . import lark_all_pb2
        _LARK_ALL = lark_all_pb2
    return _LARK_ALL


def _card_link(content: dict) -> str:
    """卡片里的链接：actionUrl、正文里的链接、卡片 JSON 里的第一个 url。"""
    url = content.get("actionUrl") or ""
    if url:
        return url
    rich = content.get("richtext") or {}
    elements = (rich.get("elements") or {}).get("dictionary") or {}
    for element in elements.values():
        link = _element_link(element)
        if link:
            return link
    raw = content.get("jsonCard") or ""
    try:
        return _first_url(json.loads(raw))
    except (ValueError, TypeError):
        return ""


def _element_link(element: dict) -> str:
    """富文本元素里的链接地址。"""
    class_name = {6: "AnchorProperty", 17: "LinkProperty"}.get(element.get("tag"))
    if not class_name:
        return ""
    cls = getattr(_elements().entities.RichTextElement, class_name, None)
    if cls is None:
        return ""
    obj = cls()
    try:
        obj.ParseFromString(element.get("property", b""))
    except Exception:
        return ""
    return getattr(obj, "href", "") or getattr(obj, "url", "") or ""


def _first_url(node) -> str:
    """卡片 JSON 里的第一个 http 链接。"""
    if isinstance(node, dict):
        for key in ("url", "href", "link", "default_url", "pc_url", "ios_url", "android_url"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        for value in node.values():
            found = _first_url(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _first_url(item)
            if found:
                return found
    return ""


# 消息类型 -> content 的 proto 类（文字和富文本特殊处理，见 decode_message_content）
_CONTENT_TYPES = {
    3: "FileContent", 5: "ImageContent", 6: "SystemContent", 7: "AudioContent",
    8: "EmailContent", 9: "ShareGroupChatContent", 10: "StickerContent", 11: "MergeForwardContent",
    12: "CalendarContent", 14: "CardContent", 15: "MediaContent", 16: "ShareCalendarEventContent",
    17: "HongbaoContent", 19: "VideoChatContent", 20: "LocationContent", 23: "ShareUserCardContent",
}


def decode_message_content(message_type, content_bytes):
    """解析消息内容：文字类给正文，其它类型只给内容字典（展示层由调用方拼）。"""
    try:
        if message_type == 4:
            tc = P.TextContent()
            tc.ParseFromString(content_bytes)
            if not tc.HasField('richText'):
                return (tc.text, {'text': tc.text})
            d = protobuf_to_dict(tc)
            return (extract_rich_text(d.get('richText', {})) or d.get('text', ''), d)
        if message_type == 2:
            pc = P.PostContent()
            pc.ParseFromString(content_bytes)
            d = protobuf_to_dict(pc)
            title = d.get('title', '')
            text = extract_rich_text(d.get('richText', {})) or d.get('text', '')
            return ((f"{title}\n{text}" if title else text).strip(), d)
        class_name = _CONTENT_TYPES.get(message_type)
        if class_name is None:
            return (f'[{MSG_TYPE_NAMES.get(message_type, message_type)}类型消息]', None)
        content = getattr(P, class_name)()
        content.ParseFromString(content_bytes)
        return ("", protobuf_to_dict(content))
    except Exception as e:
        return (f'[{MSG_TYPE_NAMES.get(message_type, message_type)}消息解析失败: {e}]', None)


def parse_ws_frame(raw: bytes):
    frame = P.Frame()
    frame.ParseFromString(raw)
    packet = P.Packet()
    packet.ParseFromString(frame.payload)
    return ({"seqid": frame.seqid, "logid": frame.logid}, {
        "sid": packet.sid, "cmd": packet.cmd, "status": packet.status, "payload": packet.payload,
        "cid": packet.cid,
    })


def decode_push_messages(raw: bytes):
    _, packet = parse_ws_frame(raw)
    out = []
    if not packet.get('payload'):
        return out
    pmr = P.PushMessagesRequest()
    pmr.ParseFromString(packet['payload'])
    at_me_map = {key: value for key, value in pmr.messagesAtMe.items()}
    for k, message in pmr.messages.items():
        v = {
            'id': message.id, 'type': message.type, 'chatId': message.chatId,
            'channelId': message.channelId, 'chatType': message.chatType,
            'fromId': message.fromId, 'rootId': message.rootId,
            'threadId': message.threadId, 'parentId': message.parentId,
            'cid': message.cid, 'position': message.position,
            'createTime': message.createTime, 'content': message.content,
            'status': message.status, 'isRemoved': message.isRemoved,
        }
        message_type = enum_to_int(v.get('type', 0))
        chat_type = enum_to_int(v.get('chatType', 0))
        flags = message_flags(v)
        summary, data = decode_message_content(message_type, v.get('content', b''))
        root_id = v.get('rootId') or ''
        out.append({'msg_id': v.get('id'), 'msg_type': message_type, 'msg_type_name': MSG_TYPE_NAMES.get(message_type, str(message_type)), 'from_id': v.get('fromId'), 'chat_id': v.get('chatId') or v.get('channelId'), 'chat_type': chat_type, 'chat_type_name': CHAT_TYPE_NAMES.get(chat_type, str(chat_type)), 'at_me': bool(at_me_map.get(v.get('id')) or at_me_map.get(k)), 'root_id': root_id, 'parent_id': v.get('parentId') or '', 'cid': v.get('cid') or '', 'position': v.get('position'), 'create_time': v.get('createTime'), 'content': summary, 'content_data': data, **flags})
    return out


def message_facts(message_type: int, content: dict) -> dict:
    """按类型挑出消息里"意图上重要"的信息（原始值；怎么拼成文本由调用方决定）。

    非文字消息的正文就是这些事实拼出来的：文件/视频给名字，卡片给标题+副标题+描述+链接，
    视频会议给主题+起止时间，位置给名称+地址，名片给人名+用户 id，日程给参会人数。
    """
    try:
        return _message_facts(message_type, content)
    except Exception:
        # 消息内容是发送方可控的，解析失败只降级成通用占位，不能打断消息处理。
        return {"kind": f"{MSG_TYPE_NAMES.get(message_type, message_type)}类型消息"}


def _message_facts(message_type: int, content: dict) -> dict:
    content = content or {}
    if message_type in (3, 13):
        return {"kind": "文件", "name": content.get("name", "")}
    if message_type == 15:
        return {"kind": "视频", "name": content.get("name", "")}
    if message_type == 7:
        return {"kind": "语音"}
    if message_type == 5:
        return {"kind": "图片"}
    if message_type == 10:
        return {"kind": "表情"}
    if message_type == 14:
        header = content.get("cardHeader") or {}
        card = _card_json(content)
        return {"kind": "卡片",
                "title": header.get("title") or header.get("mainTitle") or _card_json_title(card),
                "subtitle": header.get("subtitle") or _card_json_subtitle(card),
                "description": content.get("cardDesc", "") or _card_json_body(card),
                "url": _card_link(content)}
    if message_type == 19:
        card = content.get("meetingCard") or {}
        return {"kind": "视频会议", "topic": card.get("topic", ""),
                "start_ms": card.get("startTimeMs"), "end_ms": card.get("endTimeMs")}
    if message_type == 20:
        desc = content.get("locationDescription") or {}
        return {"kind": "位置", "name": desc.get("name", ""), "description": desc.get("description", "")}
    if message_type == 23:
        return {"kind": "名片", "name": content.get("name", ""), "user_id": content.get("userId", "")}
    if message_type == 9:
        chat = content.get("chat") or {}
        return {"kind": "群分享", "name": chat.get("name", "")}
    if message_type == 11:
        name = content.get("groupChatName") or \
            f"{content.get('p2pCreatorName', '')}与{content.get('p2pPartnerName', '')}的会话"
        return {"kind": "合并转发", "name": name}
    if message_type == 8:
        return {"kind": "邮件", "title": content.get("title", "")}
    if message_type == 17:
        return {"kind": "红包", "subject": content.get("subject", "")}
    if message_type == 16:
        # 日程事件在协议里是 bytes，我们的 proto 没有日程结构，只能给参会人数。
        return {"kind": "日程", "attendees": int(content.get("attendeesCount") or 0)}
    if message_type == 6:
        texts = []
        for value in (content.get("sysContentValues") or {}).values():
            for item in (value or {}).get("items", []):
                if item.get("text"):
                    texts.append(item["text"])
        return {"kind": "系统消息", "text": " ".join(texts)}
    return {"kind": f"{MSG_TYPE_NAMES.get(message_type, message_type)}类型消息"}


_CARD_TEXT_TAGS = ("markdown", "plain_text", "div", "note", "text_tag")


def _card_json(content: dict) -> dict:
    """卡片 JSON（新卡片的内容都在这，老卡片是 cardHeader）。"""
    raw = content.get("jsonCard") or ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_dict(value) -> dict:
    """只当字典用；卡片 JSON 是发送方可控的，类型不对就当没有。"""
    return value if isinstance(value, dict) else {}


def _card_json_title(card: dict) -> str:
    """卡片 JSON 里的标题（新卡片没有 cardHeader，标题在 config.header.title 里）。"""
    header = _as_dict(_as_dict(card.get("config")).get("header"))
    title = header.get("title") or ""
    if isinstance(title, str):
        return title
    title = _as_dict(title)
    return title.get("content") or title.get("title") or ""


def _card_json_subtitle(card: dict) -> str:
    """卡片 JSON 里的副标题。"""
    header = _as_dict(_as_dict(card.get("config")).get("header"))
    subtitle = header.get("subtitle") or ""
    if isinstance(subtitle, str):
        return subtitle
    subtitle = _as_dict(subtitle)
    return subtitle.get("content") or subtitle.get("subtitle") or ""


def _card_json_body(card: dict) -> str:
    """卡片正文里第一段文字（markdown/plain_text/div/note 这类元素）。"""
    for element in card.get("elements") or []:
        if not isinstance(element, dict) or element.get("tag") not in _CARD_TEXT_TAGS:
            continue
        text = _card_text(element)
        if text:
            return text
    return ""


def _card_text(element: dict) -> str:
    """卡片元素里的文字。"""
    if isinstance(element.get("content"), str):
        return element["content"]
    text = element.get("text")
    if isinstance(text, dict) and isinstance(text.get("content"), str):
        return text["content"]
    for child in element.get("elements") or []:
        if isinstance(child, dict):
            found = _card_text(child)
            if found:
                return found
    return ""
