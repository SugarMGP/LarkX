import json

from protobuf_to_dict import protobuf_to_dict

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
# 系统消息（群里有人进群之类的提示），不是对话内容，业务上一般不推给后端。
SYSTEM_MESSAGE_TYPE = 6


def enum_to_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        names = _entities().Message.Type.keys()
        if value in names:
            return _entities().Message.Type.Value(value)
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


def _element_order(element_id: str):
    """元素 id 一般是数字串，按数字排（排不出数字就按字符串）。"""
    try:
        return (0, int(element_id))
    except (TypeError, ValueError):
        return (1, str(element_id))


def rich_text_elements(rich_text_dict) -> list[tuple[dict, str]]:
    """按阅读顺序给出富文本元素和它渲染出来的文字。

    顺序走元素树：elementIds 是根，childIds 是子元素。**不能按字典顺序排**——
    protobuf 的 map 迭代顺序每个进程都不一样，按字典排出来的正文顺序是随机的。
    """
    rich_text_dict = rich_text_dict or {}
    dictionary = (rich_text_dict.get('elements') or {}).get('dictionary') or {}
    if not dictionary:
        return []
    roots = [str(i) for i in (rich_text_dict.get('elementIds') or [])] or sorted(dictionary, key=_element_order)
    ordered = []
    seen = set()

    def walk(element_id):
        element_id = str(element_id)
        if element_id in seen:
            return
        seen.add(element_id)
        element = dictionary.get(element_id)
        if not element:
            return
        text = render_element(element)
        ordered.append((element, text))
        # 容器元素（段落、标题、表格、正文容器…）本身没有文字，正文在子元素里；
        # 自己能渲染出文字的元素（链接、@、图片…）不再下钻，免得文字重复。
        if not text:
            for child in element.get('childIds') or []:
                walk(child)

    for root in roots:
        walk(root)
    # 协议异常时可能有走不到的元素，按 id 补上（顺序固定，别让正文顺序飘）。
    for element_id in sorted((k for k in dictionary if str(k) not in seen), key=_element_order):
        walk(element_id)
    return ordered


def extract_rich_text(rich_text_dict):
    """把富文本渲染成 markdown 风格的纯文本：文字照旧，链接渲染成 [文字](地址)。"""
    try:
        return ''.join(text for _, text in rich_text_elements(rich_text_dict))
    except Exception:
        # 消息内容是发送方可控的，解析失败只降级成空正文，不能打断消息处理。
        return '' 


def render_element(element: dict) -> str:
    """按元素类型渲染。tag 见 entities.RichTextElement.Tag。"""
    tag = element.get("tag", 1)
    prop = element.get("property")
    if tag in TAG_PLACEHOLDERS:
        # DOCS 带子元素时只是正文容器（消息正文装在它下面），不是云文档卡片。
        if tag == 22 and element.get("childIds"):
            return ""
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


def _entities():
    """消息内容类型（entities.*）：完整定义，含 cardHeader/chatters 等全部字段。"""
    return _elements().entities


def new_packet():
    """网关包（improto.Packet）。"""
    return _elements().improto.Packet()


def _frame():
    """网关帧头（网页端 basic.v1.Frame；lark_all 里的 frame.Frame 是另一套布局）。"""
    return _elements().WebFrame()


def _messages():
    """消息域类型（messages.*）。"""
    return _elements().messages


# 行内样式元素：tag -> 传给子文本的标记（下划线没有 markdown 对应，忽略）
_CARD_MARKS = {7: 'bold', 8: 'italic', 30: 'code', 31: 'code'}


def _prop(prop: bytes, class_name: str, field: str):
    """从元素属性里取一个字段（取不到给空串）。"""
    cls = getattr(_elements().entities.RichTextElement, class_name, None)
    if cls is None:
        return ''
    obj = cls()
    try:
        obj.ParseFromString(prop or b'')
    except Exception:
        return ''
    return getattr(obj, field, '') or ''


def card_blocks(content: dict) -> list[dict]:
    """把卡片正文按展示顺序拍平成块列表（原始值；markdown 由调用方拼）。

    老卡片（cardHeader + richtext 块树）按块树展开；新版卡片（jsonCard）按 elements 里的
    文字、按钮、图片拍平。块类型：text（带 bold/italic/code 标记）、image、button、link、
    at、divider、select。group 标出顶层块，调用方用它分段。
    """
    rich = (content or {}).get('richtext') or {}
    elements = (rich.get('elements') or {}).get('dictionary') or {}
    if not elements:
        return _json_card_blocks(_card_json(content or {}))
    blocks: list[dict] = []
    seen: set[str] = set()

    def child_text(element: dict) -> str:
        """按钮/链接的文字在子元素里；吃掉后这些子元素不再单独输出。"""
        text = ''
        for cid in element.get('childIds') or []:
            seen.add(str(cid))
            child = elements.get(str(cid)) or {}
            if child.get('tag') == 1:
                text += _prop(child.get('property', b''), 'TextProperty', 'content')
        return text

    def walk(element_id, link='', group='', bold=False, italic=False, code=False) -> int:
        """展开一个块，返回它产出的文本块个数。"""
        element_id = str(element_id)
        if element_id in seen:
            return 0
        seen.add(element_id)
        element = elements.get(element_id)
        if element is None:
            return 0
        tag = element.get('tag')
        prop = element.get('property', b'')
        style = element.get('style') or {}
        style_keys = element.get('styleKeys') or []
        if tag == 1:
            blocks.append({
                'type': 'text', 'text': _prop(prop, 'TextProperty', 'content'),
                'bold': bold or 'txt_bold' in style_keys or style.get('fontWeight') == 'bold',
                'italic': italic or style.get('fontStyle') == 'italic',
                'code': code, 'link': link, 'group': group,
            })
            return 1
        if tag == 2:
            blocks.append({
                'type': 'image',
                'key': _prop(prop, 'ImageProperty', 'token') or _prop(prop, 'ImageProperty', 'originKey'),
                'size': int(_prop(prop, 'ImageProperty', 'originSize') or 0), 'group': group,
            })
            return 0
        if tag == 5:
            blocks.append({
                'type': 'at', 'text': _prop(prop, 'AtProperty', 'content'),
                'user_id': _prop(prop, 'AtProperty', 'userId'), 'group': group,
            })
            return 0
        if tag == 6:
            # 链接文字在 AnchorProperty.content 里，子元素只是兜底。
            text = child_text(element)
            blocks.append({
                'type': 'link', 'text': _prop(prop, 'AnchorProperty', 'content') or text,
                'url': _element_link(element), 'group': group,
            })
            return 0
        if tag == 11:
            blocks.append({
                'type': 'button', 'text': child_text(element),
                'action_id': _prop(prop, 'ButtonProperty', 'actionId'),
                'mode': _prop(prop, 'ButtonProperty', 'mode'), 'group': group,
            })
            return 0
        if tag == 19:
            blocks.append({'type': 'select', 'text': _select_text(prop), 'group': group})
            return 0
        if tag == 32 or (tag == 14 and 'md_hr' in style_keys):
            blocks.append({'type': 'divider', 'group': group})
            return 0
        if tag == 17:
            # 整块内容是一个链接：子块照常展开，链接挂在文本块上。
            url = _element_link(element)
            count = 0
            for cid in element.get('childIds') or []:
                count += walk(cid, url, group, bold, italic, code)
            if not count:
                blocks.append({'type': 'link', 'text': '', 'url': url, 'group': group})
            return count
        mark = _CARD_MARKS.get(tag)
        count = 0
        for cid in element.get('childIds') or []:
            count += walk(cid, link, group, bold or mark == 'bold',
                          italic or mark == 'italic', code or mark == 'code')
        return count

    for element_id in rich.get('elementIds') or []:
        walk(element_id, group=str(element_id))
    for element_id in elements:
        walk(element_id, group=str(element_id))
    return blocks


def _json_card_blocks(card: dict) -> list[dict]:
    """新版卡片 JSON：把 elements 里的文字、按钮、图片按顺序拍平成块。"""
    blocks: list[dict] = []

    def text_of(node) -> str:
        if isinstance(node, str):
            return node
        if not isinstance(node, dict):
            return ''
        for key in ('content', 'text', 'title', 'placeholder'):
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                found = text_of(value)
                if found:
                    return found
        return ''

    for index, element in enumerate(card.get('elements') or []):
        if not isinstance(element, dict):
            continue
        group = f'json{index}'
        tag = element.get('tag')
        if tag == 'action':
            for action in element.get('actions') or []:
                if isinstance(action, dict):
                    blocks.append({'type': 'button', 'text': text_of(action.get('text') or action),
                                   'action_id': '', 'mode': str(action.get('type') or ''), 'group': group})
            continue
        if tag == 'img':
            blocks.append({'type': 'image', 'key': str(element.get('img_key') or ''),
                           'size': 0, 'group': group})
            continue
        text = text_of(element.get('text') or element.get('content') or element)
        if text:
            blocks.append({'type': 'text', 'text': text, 'bold': False, 'italic': False,
                           'code': False, 'link': '', 'group': group})
    return blocks


def _select_text(prop: bytes) -> str:
    """下拉菜单：占位文字 + 选项 + 确认语。"""
    cls = getattr(_elements().entities.RichTextElement, 'SelectMenuProperty', None)
    if cls is None:
        return ''
    obj = cls()
    try:
        obj.ParseFromString(prop or b'')
    except Exception:
        return ''
    parts = [obj.placeHolder or '']
    parts += [option.text or '' for option in obj.options]
    confirm = obj.confirm
    if confirm is not None:
        parts += [confirm.text or '', confirm.confirm or '']
    return ' '.join(part for part in parts if part)

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


def _calendar_facts(content: dict) -> dict:
    """日程事件在 calendarEvent 子消息里（lark.entities.CalendarEvent）。"""
    event = content.get("calendarEvent") or {}
    start, end = event.get("start"), event.get("end")
    return {"kind": "日程",
            "title": event.get("summary", ""),
            "start_ms": int(start) * 1000 if isinstance(start, int) else None,
            "end_ms": int(end) * 1000 if isinstance(end, int) else None,
            "attendees": int(content.get("attendeesCount") or 0)}


def _system_text(content: dict) -> str:
    """系统消息正文：模板里的 {变量} 换成实际值。"""
    values = {}
    for key, value in (content.get("contents") or {}).items():
        values[key] = value if isinstance(value, str) else _items_text(value)
    text = content.get("messageTemplate") or ""
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    if text:
        return text
    return " ".join(_items_text(value) for value in (content.get("sysContentValues") or {}).values())


def _items_text(value) -> str:
    """系统消息变量里的一段段文字。"""
    if isinstance(value, str):
        return value
    return " ".join(item["text"] for item in (value or {}).get("items", []) if item.get("text"))


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
            tc = _entities().TextContent()
            tc.ParseFromString(content_bytes)
            if not tc.HasField('richText'):
                return (tc.text, {'text': tc.text})
            d = protobuf_to_dict(tc)
            return (extract_rich_text(d.get('richText', {})) or d.get('text', ''), d)
        if message_type == 2:
            pc = _entities().PostContent()
            pc.ParseFromString(content_bytes)
            d = protobuf_to_dict(pc)
            title = d.get('title', '')
            text = extract_rich_text(d.get('richText', {})) or d.get('text', '')
            return ((f"{title}\n{text}" if title else text).strip(), d)
        class_name = _CONTENT_TYPES.get(message_type)
        if class_name is None:
            return (f'[{MSG_TYPE_NAMES.get(message_type, message_type)}类型消息]', None)
        content = getattr(_entities(), class_name)()
        content.ParseFromString(content_bytes)
        return ("", protobuf_to_dict(content))
    except Exception as e:
        return (f'[{MSG_TYPE_NAMES.get(message_type, message_type)}消息解析失败: {e}]', None)


def parse_ws_frame(raw: bytes):
    frame = _frame()
    frame.ParseFromString(raw)
    packet = new_packet()
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
    pmr = _messages().PushMessagesRequest()
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

    非文字消息的正文就是这些事实拼出来的：文件/视频给名字，卡片给标题/副标题/正文块/链接，
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
        return {"kind": "表情", "key": content.get("key", "")}
    if message_type == 14:
        header = content.get("cardHeader") or {}
        card = _card_json(content)
        blocks = card_blocks(content)
        link = next((b.get("url") for b in blocks if b.get("type") == "link" and b.get("url")), "")
        if not link and card:
            link = _first_url(card)
        return {"kind": "卡片",
                "title": header.get("title") or header.get("mainTitle") or _card_json_title(card),
                "subtitle": header.get("subtitle") or _card_json_subtitle(card),
                "theme": header.get("theme", ""),
                # description 是飞书给的官方描述；正文在 blocks 里。
                "description": content.get("cardDesc", ""),
                # url 是内容里的链接；action_url 是卡片动作接口，不是给后端看的内容地址。
                "url": link,
                "action_url": content.get("actionUrl", ""),
                "blocks": blocks}
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
        return {"kind": "合并转发",
                "name": _merge_title(content.get("groupChatName") or "",
                                     content.get("p2pCreatorName") or "",
                                     content.get("p2pPartnerName") or ""),
                "count": len(content.get("messages") or [])}
    if message_type == 8:
        return {"kind": "邮件", "title": content.get("title", "")}
    if message_type == 17:
        return {"kind": "红包", "subject": content.get("subject", "")}
    if message_type == 16:
        return _calendar_facts(content)
    if message_type == 6:
        return {"kind": "系统消息", "text": _system_text(content)}
    return {"kind": f"{MSG_TYPE_NAMES.get(message_type, message_type)}类型消息"}


def _merge_title(group: str, creator: str, partner: str) -> str:
    """合并转发的会话标题：群名优先，单聊用两个人的名字，都没有就留空。"""
    if group:
        return str(group)
    if creator and partner:
        return f"{creator}与{partner}的会话"
    return str(creator or partner)


def merge_forward(content_bytes: bytes) -> dict:
    """合并转发内容 -> 会话信息 + 消息树。

    内嵌消息是完整的 entities.Message（id/type/fromId/createTime/content…），
    chatters 是发送人 id -> 名字。嵌套的合并转发（内层消息 type=11）递归展开成 merge 子树。
    """
    mf = _entities().MergeForwardContent()
    mf.ParseFromString(content_bytes or b"")
    return _merge_tree(mf)


def _merge_tree(mf) -> dict:
    return {"title": _merge_title(mf.groupChatName, mf.p2pCreatorName, mf.p2pPartnerName),
            "chat_type": int(mf.chatType),
            "origin_chat_id": str(mf.originChatId),
            "chatters": {str(k): str(v.name) for k, v in mf.chatters.items()},
            "messages": [_merge_message(m) for m in mf.messages]}


def _merge_message(msg) -> dict:
    node = {"type": int(msg.type), "message_id": str(msg.id), "from_id": str(msg.fromId),
            "create_time": int(msg.createTime), "position": int(msg.position),
            "chat_id": str(msg.chatId), "content": msg.content, "merge": None}
    if msg.type == 11 and msg.content:
        try:
            nested = _entities().MergeForwardContent()
            nested.ParseFromString(msg.content)
            node["merge"] = _merge_tree(nested)
        except Exception:
            node["merge"] = None
    return node


def at_property(prop: bytes) -> dict:
    """AT 元素的属性字节 -> {'user_id', 'name'}；不是 AT 元素就返回空。"""
    cls = _entities().RichTextElement.AtProperty
    obj = cls()
    try:
        obj.ParseFromString(prop or b'')
    except Exception:
        return {}
    return {'user_id': obj.userId, 'name': (obj.content or '').removeprefix('@')}


def parse_packet(content: bytes) -> dict:
    """网关响应字节 -> 字典。proto3 的默认值不带出来，所以这几个字段补齐。"""
    packet = new_packet()
    packet.ParseFromString(content)
    data = protobuf_to_dict(packet)
    data.update(cmd=packet.cmd, cid=packet.cid, status=packet.status, payload=packet.payload)
    return data


def parse_push_request(payload: bytes) -> dict:
    """推送帧的 payload -> PushMessagesRequest 的字典。"""
    request = _messages().PushMessagesRequest()
    request.ParseFromString(payload)
    return protobuf_to_dict(request)


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
