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


def _placeholder(kind: str, *parts: str) -> str:
    """非文字内容的占位文本：类型 + 关键信息，多段用 · 分隔。"""
    info = " · ".join(part for part in parts if part)
    return f"[{kind}:{info}]" if info else f"[{kind}]"


def _time(ms) -> str:
    """毫秒时间戳转成 10-26 15:00（服务所在时区）。"""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _time_range(start_ms, end_ms) -> str:
    """时间段：10-26 15:00-16:00（同一天不重复日期）。"""
    start, end = _time(start_ms), _time(end_ms)
    if not start or not end:
        return start or end
    if start.split(" ")[0] == end.split(" ")[0]:
        return f"{start}-{end.split(' ', 1)[1]}"
    return f"{start}-{end}"


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


def decode_message_content(message_type, content_bytes):
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
        if message_type == 5:
            ic = P.ImageContent()
            ic.ParseFromString(content_bytes)
            d = protobuf_to_dict(ic)
            v2 = d.get('imageV2') or {}
            if v2:
                key = v2.get('imageKey', '')
                width = height = '?'
                for variant in v2.get('variants', []):
                    if variant.get('type') == 1:
                        size = variant.get('size') or {}
                        width, height = (size.get('width', '?'), size.get('height', '?'))
                        break
                has_crypto = 'crypto' in v2
                return ("[图片]", d)
            img = (d.get('image') or {}).get('origin') or {}
            key = img.get('key') or (d.get('image') or {}).get('key') or (d.get('image') or {}).get('imageKey', '')
            return ("[图片]", d)
        if message_type == 3:
            fc = P.FileContent()
            fc.ParseFromString(content_bytes)
            d = protobuf_to_dict(fc)
            return (_placeholder("文件", d.get('name', '')), d)
        if message_type == 15:
            mc = P.MediaContent()
            mc.ParseFromString(content_bytes)
            d = protobuf_to_dict(mc)
            return (_placeholder("视频", d.get('name', '')), d)
        if message_type == 7:
            ac = P.AudioContent()
            ac.ParseFromString(content_bytes)
            d = protobuf_to_dict(ac)
            return ("[语音]", d)
        if message_type == 10:
            sc = P.StickerContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            return ("[表情]", d)
        if message_type == 14:
            cc = P.CardContent()
            cc.ParseFromString(content_bytes)
            d = protobuf_to_dict(cc)
            header = d.get('cardHeader') or {}
            title = header.get('title') or header.get('mainTitle') or ''
            return (_placeholder("卡片", title, _card_link(d)), d)
        if message_type == 11:
            mfc = P.MergeForwardContent()
            mfc.ParseFromString(content_bytes)
            d = protobuf_to_dict(mfc)
            name = d.get('groupChatName') or f"{d.get('p2pCreatorName', '')}与{d.get('p2pPartnerName', '')}的会话"
            return (_placeholder("合并转发", name), d)
        if message_type == 20:
            lc = P.LocationContent()
            lc.ParseFromString(content_bytes)
            d = protobuf_to_dict(lc)
            desc = d.get('locationDescription') or {}
            return (_placeholder("位置", desc.get('name', ''), desc.get('description', '')), d)
        if message_type == 6:
            sc = P.SystemContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            texts = []
            for cv in (d.get('sysContentValues') or {}).values():
                for item in (cv or {}).get('items', []):
                    if item.get('text'):
                        texts.append(item['text'])
            return (f"[系统消息] {' '.join(texts)}", d)
        if message_type == 23:
            sc = P.ShareUserCardContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            return (_placeholder("名片", d.get('name', '')), d)
        if message_type == 9:
            sc = P.ShareGroupChatContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            chat = d.get('chat') or {}
            chat_name = chat.get('name', '')
            chat_id = d.get('chatId') or chat.get('id', '')
            return (_placeholder("群分享", chat_name), d)
        if message_type == 16:
            sc = P.ShareCalendarEventContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            # 日程事件本身在协议里是 bytes，我们的 proto 没有日程结构，只能给参会人数。
            count = int(d.get('attendeesCount') or 0)
            return (_placeholder("日程", f"{count}人参加" if count else ""), d)
        if message_type == 17:
            hc = P.HongbaoContent()
            hc.ParseFromString(content_bytes)
            d = protobuf_to_dict(hc)
            return (_placeholder("红包", d.get('subject', '')), d)
        if message_type == 12:
            cc = P.CalendarContent()
            cc.ParseFromString(content_bytes)
            d = protobuf_to_dict(cc)
            return ("[日历]", d)
        if message_type == 19:
            vc = P.VideoChatContent()
            vc.ParseFromString(content_bytes)
            d = protobuf_to_dict(vc)
            card = d.get('meetingCard') or {}
            return (_placeholder("视频会议", card.get('topic', ''),
                                 _time_range(card.get('startTimeMs'), card.get('endTimeMs'))), d)
        if message_type == 8:
            ec = P.EmailContent()
            ec.ParseFromString(content_bytes)
            d = protobuf_to_dict(ec)
            return (_placeholder("邮件", d.get('title', '')), d)
    except Exception as e:
        return (f'[{MSG_TYPE_NAMES.get(message_type, message_type)}消息解析失败: {e}]', None)
    return (f'[{MSG_TYPE_NAMES.get(message_type, message_type)}类型消息]', None)


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
