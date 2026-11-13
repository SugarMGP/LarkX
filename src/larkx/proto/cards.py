"""卡片消息：把 CardContent 里的完整卡片 JSON 拍平成块，供正文和附件共用。

飞书 web 协议里，新版卡片（schema 2.0）的正文在 ``jsonCard.newBody`` 的
``property.elements`` 里；旧版卡片在 ``jsonCard.body`` / ``jsonCard.elements``。
``jsonCard`` 顶层的 ``body`` 经常只是给旧客户端的降级壳（一张图 + "升级客户端"），
``richtext`` 也是同一份降级内容，所以有完整 ``newBody`` 时必须优先用它。

块结构对齐 ``decoders.card_blocks``（text/image/button/link/at/select/divider），
并补上 schema 2.0 的 markdown/table/column_set/action/code/quote/list 等。
"""
from __future__ import annotations

import json
from typing import Any

_CARD_URL_KEYS = ("url", "href", "link", "default_url", "pc_url", "ios_url", "android_url")


def facts(content: dict | None) -> dict:
    """卡片消息的 facts：标题/副标题/正文块/链接（原始值，文本由调用方拼）。"""
    content = content or {}
    card = card_json(content)
    blocks = _json_blocks(card, content)
    link = next((b.get("url") for b in blocks if b.get("type") == "link" and b.get("url")), "")
    if not link and card:
        link = _first_url(card)
    header = _header(card)
    # jsonCard 的 header 是拆开的标题/副标题；cardHeader 是老协议的合并标题，后者只能兜底。
    title = _header_text(header, "title") or _extract_text((content.get("cardHeader") or {}).get("title"))
    subtitle = _header_text(header, "subtitle") or \
        _extract_text((content.get("cardHeader") or {}).get("subtitle"))
    if not title:
        title = _extract_text(card.get("title"))
    # 老 cardHeader 的 title 常常把副标题也拼进去（"标题\n副标题"）；拆开时别重复。
    if title and subtitle and title.endswith(subtitle):
        trimmed = title[:-len(subtitle)].rstrip("\n").rstrip()
        if trimmed:
            title = trimmed
    return {
        "kind": "卡片",
        "title": title,
        "subtitle": subtitle,
        "theme": (content.get("cardHeader") or {}).get("theme") or _header_text(header, "template"),
        "description": content.get("cardDesc", ""),
        "url": link,
        "action_url": content.get("actionUrl", ""),
        "blocks": blocks,
    }


def json_blocks(content: dict | None) -> list[dict]:
    """完整卡片 JSON 的块；没有 jsonCard 时返回空（richtext 由 decoders 兜底）。"""
    content = content or {}
    return _json_blocks(card_json(content), content)


def has_full_json(content: dict | None) -> bool:
    """jsonCard 里是否有 schema 2.0 的 newBody；有就必须压过 richtext 降级壳。"""
    card = card_json(content or {})
    return isinstance(card.get("newBody"), dict) and bool(_body_elements(card))


def first_url(card: dict) -> str:
    return _first_url(card or {})


def card_json(content: dict) -> dict:
    """jsonCard 是字符串；解析失败就当没有。"""
    raw = content.get("jsonCard") or ""
    if isinstance(raw, dict):
        card = raw
    else:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            card = json.loads(raw)
        except (ValueError, TypeError):
            card = {}
    if not isinstance(card, dict):
        card = {}
    # 兼容 OpenAPI raw_card_content 的外层包装 {"json_card": "<card json>"}。
    if isinstance(card.get("json_card"), str):
        try:
            nested = json.loads(card["json_card"])
        except (ValueError, TypeError):
            nested = None
        if isinstance(nested, dict):
            card = nested
    if not isinstance(card.get("newBody"), dict):
        # 部分卡片把完整 JSON 放在 cardPackInfo.replacedJsonCard / i18n 里，
        # 顶层 jsonCard 只是降级壳（body 也是降级壳时同样适用）。
        replaced = _replaced_json(content)
        if replaced:
            return replaced
    return card


def _replaced_json(content: dict) -> dict:
    pack = content.get("cardPackInfo") or {}
    candidates = [pack.get("replacedJsonCard") if isinstance(pack, dict) else ""]
    i18n = content.get("jsonCardI18nInfo") or {}
    if isinstance(i18n, dict):
        localized = i18n.get("i18nJsonCard") or {}
        if isinstance(localized, dict):
            candidates.extend(localized.get(locale) for locale in
                              ("zh_cn", "zh-CN", "en_us", "en-US", "ja_jp"))
    for raw in candidates:
        if isinstance(raw, dict):
            card = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                card = json.loads(raw)
            except (ValueError, TypeError):
                continue
        else:
            continue
        if isinstance(card, dict) and (_body_elements(card) or
                                      _flatten_legacy_elements(card.get("elements"))):
            return card
    return {}


def _json_blocks(card: dict, content: dict) -> list[dict]:
    elements = _body_elements(card)
    if not elements:
        elements = _flatten_legacy_elements(card.get("elements"))
    if not elements:
        return []
    blocks: list[dict] = []
    for index, element in enumerate(elements):
        if isinstance(element, dict):
            _walk_one(element, f"e{index}", content, blocks)
    return blocks


def _body_elements(card: dict) -> list:
    for key in ("newBody", "body"):
        body = card.get(key)
        if not isinstance(body, dict):
            continue
        prop = body.get("property") if isinstance(body.get("property"), dict) else body
        elements = prop.get("elements") or body.get("elements")
        if isinstance(elements, list) and elements:
            return elements
    return []


def _flatten_legacy_elements(value: Any) -> list:
    """老 schema 2.0 的 elements 可能是数组的数组（按行）。"""
    if not isinstance(value, list):
        return []
    out: list = []
    for item in value:
        if isinstance(item, list):
            out.extend(item)
        elif isinstance(item, dict):
            out.append(item)
    return out


def _header(card: dict) -> dict:
    header = card.get("header")
    if not isinstance(header, dict):
        config = card.get("config") if isinstance(card.get("config"), dict) else {}
        header = config.get("header")
    return header if isinstance(header, dict) else {}


def _header_text(header: dict, key: str) -> str:
    if not header:
        return ""
    prop = header.get("property") if isinstance(header.get("property"), dict) else header
    value = prop.get(key) or header.get(key)
    # 标题只认字符串/带 content 的对象；恶意或损坏的 list 不当标题（LarkX 旧行为）。
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _extract_text(value)
    return ""


def _prop(element: dict) -> dict:
    prop = element.get("property")
    return prop if isinstance(prop, dict) else element


def _walk_one(element: dict, group: str, content: dict, out: list[dict], depth: int = 0) -> None:
    tag = str(element.get("tag") or "")
    prop = _prop(element)

    if tag in ("plain_text", "text", "markdown_content"):
        _emit_text(prop, group, out)
        return
    if tag == "markdown":
        nested = _markdown_children(prop)
        if nested:
            # markdown 里可能嵌列表/表格等块（真卡片实测），用 _walk 而不是纯行内拼接。
            _walk(nested, group, content, out, depth + 1)
            return
        text = prop.get("content")
        if isinstance(text, str) and text:
            out.append(_text_block(text, group))
        return
    if tag == "br":
        out.append(_text_block("\n", group))
        return
    if tag == "link":
        text = _extract_text(prop.get("content")) or "链接"
        url = _link_url(prop)
        out.append({"type": "link", "text": text, "url": url, "group": group})
        return
    if tag == "at_all":
        out.append(_text_block("@所有人", group))
        return
    if tag == "hr":
        out.append({"type": "divider", "group": group})
        return
    if tag in ("img", "image"):
        block = _image_block(prop, group, content)
        if block:
            out.append(block)
        return
    if tag == "img_combination":
        for image in prop.get("imgList") or prop.get("images") or []:
            if isinstance(image, dict):
                block = _image_block(_prop(image), group, content)
                if block:
                    out.append(block)
        return
    if tag in ("column_set", "columns"):
        for index, column in enumerate(prop.get("columns") or []):
            if isinstance(column, dict):
                _walk_blocks(column.get("elements") or _prop(column).get("elements") or [],
                             f"{group}.c{index}", content, out, depth + 1)
        return
    if tag == "column":
        _walk_blocks(prop.get("elements") or [], group, content, out, depth + 1)
        return
    if tag == "table":
        block = _table_block(prop, group)
        if block:
            out.append(block)
        return
    if tag == "div":
        _div(prop, group, content, out, depth)
        return
    if tag == "note":
        note = _collect_inline(prop.get("elements") or [], content, depth)
        if note:
            out.append(_text_block("📝 " + note, group))
        return
    if tag in ("action", "actions"):
        for action in prop.get("actions") or []:
            if isinstance(action, dict):
                _walk_one(action, f"{group}.a{len(out)}", content, out, depth + 1)
        return
    if tag == "button":
        block = _button_block(prop, group)
        if block:
            out.append(block)
        return
    if tag == "overflow":
        options = []
        for option in prop.get("options") or []:
            if isinstance(option, dict):
                text = _extract_text(option.get("text")) or str(option.get("value") or "")
                if text:
                    options.append(text)
        if options:
            out.append(_text_block("⋮ " + ", ".join(options), group))
        return
    if tag in ("select_static", "select_person", "multi_select_static", "multi_select_person"):
        text = _select_text(prop)
        if text:
            out.append({"type": "select", "text": text, "group": group})
        return
    if tag == "person":
        text = _extract_text(prop.get("name")) or _extract_text(prop.get("notation")) or \
            str(prop.get("userID") or "")
        if text:
            out.append({"type": "person", "text": text, "user_id": str(prop.get("userID") or ""),
                        "group": group})
        return
    if tag == "at":
        user_id = str(prop.get("userID") or prop.get("user_id") or "")
        text = _extract_text(prop.get("content")).lstrip("@") or _at_name(content, user_id) or user_id
        if text:
            out.append({"type": "at", "text": text, "user_id": user_id, "group": group})
        return
    if tag == "person_list":
        names = [_extract_text(p.get("name")) or str(p.get("id") or "")
                 for p in prop.get("persons") or [] if isinstance(p, dict)]
        if any(names):
            out.append(_text_block("、".join(name for name in names if name), group))
        return
    if tag == "list":
        _list_blocks(prop, group, content, out, depth)
        return
    if tag == "blockquote":
        text = _extract_text(prop.get("content")) or _collect_inline(prop.get("elements") or [], content, depth)
        if text:
            out.append({"type": "quote", "text": text, "group": group})
        return
    if tag == "code_block":
        text = _code_text(prop)
        if text:
            out.append({"type": "code", "text": text, "language": str(prop.get("language") or ""),
                        "group": group})
        return
    if tag == "code_span":
        text = _extract_text(prop.get("content") or prop.get("text"))
        if text:
            out.append(_text_block(text, group, code=True))
        return
    if tag == "heading":
        text = _extract_text(prop.get("content")) or _collect_inline(prop.get("elements") or [], content, depth)
        if text:
            level = _int(prop.get("level"), 1)
            out.append({"type": "heading", "text": text, "level": max(1, min(6, level)), "group": group})
        return
    if tag == "text_tag":
        text = _extract_text(prop.get("text"))
        if text:
            out.append(_text_block("「" + text + "」", group))
        return
    if tag == "number_tag":
        text = _extract_text(prop.get("text"))
        if text:
            url = _link_url(prop)
            out.append(_text_block(text, group, link=url))
        return
    if tag == "emoji":
        key = str(prop.get("key") or "")
        out.append({"type": "emotion", "key": key, "group": group})
        return
    if tag == "chart":
        out.append(_text_block(_chart_text(prop), group))
        return
    if tag in ("audio", "video"):
        key = str(prop.get("fileID") or prop.get("audioID") or prop.get("videoID") or "")
        label = "[音频]" if tag == "audio" else "[视频]"
        out.append(_text_block(f"{label}({key})" if key else label, group))
        return
    if tag == "select_img":
        out.append(_text_block(_select_img_text(prop), group))
        return
    if tag == "input":
        out.append(_text_block(_input_text(prop), group))
        return
    if tag in ("date_picker", "picker_time", "picker_datetime"):
        out.append(_text_block(_picker_text(prop, tag), group))
        return
    if tag == "checker":
        checked = bool(prop.get("checked"))
        text = _extract_text(prop.get("text"))
        out.append(_text_block(("[x] " if checked else "[ ] ") + text, group))
        return
    if tag == "local_datetime":
        text = str(prop.get("fallbackText") or prop.get("milliseconds") or "")
        if text:
            out.append(_text_block(text, group))
        return
    if tag in ("person_v1",):
        text = _extract_text(prop.get("name")) or _extract_text(prop.get("notation")) or \
            str(prop.get("userID") or prop.get("openID") or "")
        if text:
            out.append({"type": "person", "text": text,
                        "user_id": str(prop.get("userID") or prop.get("openID") or ""), "group": group})
        return
    if tag == "avatar":
        text = _extract_text(prop.get("name")) or str(prop.get("userID") or "")
        if text:
            out.append(_text_block(text, group))
        return
    if tag == "collapsible_panel":
        header = prop.get("header") if isinstance(prop.get("header"), dict) else {}
        title = _extract_text(header.get("title")) or _extract_text(prop.get("title")) or "详情"
        out.append(_text_block(("▼ " if prop.get("expanded") else "▶ ") + title, group))
        _walk_blocks(prop.get("elements") or [], group, content, out, depth + 1)
        return
    if tag in ("form", "interactive_container", "repeat", "fallback_text",
               "fallback", "card_header", "custom_icon", "standard_icon"):
        _walk_blocks(prop.get("elements") or [], group, content, out, depth + 1)
        return

    # 未知块：尽量把能读到的文字取出来，不整块丢掉。
    _unknown(prop, group, content, out, depth)


def _walk(elements: Any, group: str, content: dict, out: list[dict], depth: int = 0) -> None:
    """同一段里的行内元素：共用一个 group，连成一段。"""
    for element in elements or []:
        if isinstance(element, dict):
            _walk_one(element, group, content, out, depth)


def _walk_blocks(elements: Any, group: str, content: dict, out: list[dict], depth: int = 0) -> None:
    """容器里的子块：每块单独一个 group，换块时正文分段。"""
    for index, element in enumerate(elements or []):
        if isinstance(element, dict):
            _walk_one(element, f"{group}.b{index}", content, out, depth)


def _inline(elements: Any, group: str, content: dict, out: list[dict], depth: int = 0,
            link: str = "") -> None:
    """markdown 行内元素：文字、链接、@、换行都拼到同一个块组里。"""
    for element in elements or []:
        if isinstance(element, str):
            if element:
                out.append(_text_block(element, group, link=link))
            continue
        if not isinstance(element, dict):
            continue
        tag = str(element.get("tag") or "")
        prop = _prop(element)
        if tag in ("plain_text", "text"):
            text = _extract_text(prop.get("content") or prop.get("text"))
            if text:
                out.append(_text_block(text, group, prop, link=link))
            continue
        if tag == "markdown":
            text = _extract_text(prop.get("content"))
            if text:
                out.append(_text_block(text, group, link=link))
            elif isinstance(prop.get("elements"), list):
                _inline(prop["elements"], group, content, out, depth, link)
            continue
        if tag == "link":
            text = _extract_text(prop.get("content")) or "链接"
            url = _link_url(prop)
            out.append({"type": "link", "text": text, "url": url, "group": group})
            continue
        if tag == "at":
            user_id = str(prop.get("userID") or prop.get("user_id") or "")
            text = _extract_text(prop.get("content")).lstrip("@") or _at_name(content, user_id) or user_id
            if text:
                out.append({"type": "at", "text": text, "user_id": user_id, "group": group})
            continue
        if tag == "at_all":
            out.append(_text_block("@所有人", group))
            continue
        if tag == "br":
            out.append(_text_block("\n", group))
            continue
        if tag == "emoji":
            out.append({"type": "emotion", "key": str(prop.get("key") or ""), "group": group})
            continue
        if tag == "code_span":
            text = _extract_text(prop.get("content") or prop.get("text"))
            if text:
                out.append(_text_block(text, group, code=True))
            continue
        if tag in ("img", "image"):
            block = _image_block(prop, group, content)
            if block:
                alt = block.get("alt") or ""
                out.append(_text_block(f"[图片:{alt}]" if alt else "[图片]", group))
            continue
        if tag == "list":
            items = [_collect_inline(item.get("elements") or [], content, depth)
                     for item in prop.get("items") or [] if isinstance(item, dict)]
            text = " ".join(item for item in items if item)
            if text:
                out.append(_text_block(text, group))
            continue
        if tag == "text_tag":
            text = _extract_text(prop.get("text"))
            if text:
                out.append(_text_block("「" + text + "」", group))
            continue
        if tag == "person":
            text = _extract_text(prop.get("name")) or _extract_text(prop.get("notation")) or \
                str(prop.get("userID") or "")
            if text:
                out.append({"type": "person", "text": text,
                            "user_id": str(prop.get("userID") or ""), "group": group})
            continue
        if tag in ("fallback_text", "fallback"):
            text = _extract_text(prop.get("text")) or _collect_inline(
                prop.get("elements") or [], content, depth)
            if text:
                out.append(_text_block(text, group))
            continue
        # 未知行内元素：有 content/text 就当文字，有 elements 就继续展开。
        text = _extract_text(prop.get("content") or prop.get("text"))
        if text:
            out.append(_text_block(text, group))
        elif isinstance(prop.get("elements"), list):
            _inline(prop["elements"], group, content, out, depth, link)


def _div(prop: dict, group: str, content: dict, out: list[dict], depth: int) -> None:
    text = prop.get("text")
    start = len(out)
    if isinstance(text, dict):
        _walk_one(text, group, content, out, depth + 1)
    elif isinstance(text, str) and text:
        out.append(_text_block(text, group))
    # lark-cli 把 notation 字号的小字当注释，前面加 📝；卡片里常用它做“使用指南”这类提示。
    text_prop = _prop(text) if isinstance(text, dict) else {}
    text_style = text_prop.get("textStyle") if isinstance(text_prop.get("textStyle"), dict) else {}
    if text_style.get("size") == "notation":
        for block in out[start:]:
            if block.get("type") == "text" and block.get("text"):
                block["text"] = "📝 " + block["text"]
                break
    fields = prop.get("fields")
    if isinstance(fields, list):
        for index, field in enumerate(fields):
            if isinstance(field, dict):
                if isinstance(field.get("text"), dict):
                    _walk_one(field["text"], f"{group}.f{index}", content, out, depth + 1)
                else:
                    text = _extract_text(field.get("text"))
                    if text:
                        out.append(_text_block(text, f"{group}.f{index}"))
    extra = prop.get("extra")
    if isinstance(extra, dict):
        _walk_one(extra, f"{group}.x", content, out, depth + 1)


def _collect_inline(elements: Any, content: dict, depth: int) -> str:
    blocks: list[dict] = []
    _inline(elements, "inline", content, blocks, depth)
    return "".join(_block_text(block) for block in blocks)


def _emit_text(prop: dict, group: str, out: list[dict]) -> None:
    text = _extract_text(prop.get("content") or prop.get("text"))
    if text:
        out.append(_text_block(text, group, prop))


def _text_block(text: str, group: str, prop: dict | None = None, link: str = "",
                code: bool = False) -> dict:
    prop = prop or {}
    style = prop.get("textStyle") if isinstance(prop.get("textStyle"), dict) else {}
    attrs = style.get("attributes") if isinstance(style.get("attributes"), list) else []
    attrs = {str(item).lower() for item in attrs if isinstance(item, (str, int))}
    bold = "bold" in attrs or style.get("fontWeight") == "bold" or prop.get("fontWeight") == "bold"
    italic = "italic" in attrs or style.get("fontStyle") == "italic" or prop.get("fontStyle") == "italic"
    strike = "strikethrough" in attrs or "line_through" in attrs or style.get("textDecoration") == "line-through"
    return {"type": "text", "text": text, "bold": bold, "italic": italic,
            "strike": strike, "code": code, "link": link, "group": group}


def _image_block(prop: dict, group: str, content: dict) -> dict | None:
    image_id = str(prop.get("imageID") or prop.get("image_id") or prop.get("img_key") or
                   prop.get("imageKey") or prop.get("image_key") or prop.get("token") or "")
    key = _image_key(content, image_id)
    alt = _extract_text(prop.get("alt")) or _extract_text(prop.get("title")) or ""
    if not key and not image_id and not alt:
        return None
    return {"type": "image", "key": key or image_id, "size": _int(prop.get("originSize"), 0),
            "alt": alt, "group": group}


def _image_key(content: dict, image_id: str) -> str:
    if not image_id:
        return ""
    if image_id.startswith(("img_", "v2_", "v3_")):
        return image_id
    attachment = content.get("jsonAttachment") or {}
    images = attachment.get("images") if isinstance(attachment, dict) else {}
    if isinstance(images, dict):
        entry = images.get(image_id)
        if not isinstance(entry, dict):
            entry = next((value for key, value in images.items()
                          if str(key) == image_id and isinstance(value, dict)), None)
        if isinstance(entry, dict):
            for key in ("token", "originKey", "origin_key", "imageKey", "middleKey", "thumbKey"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    return value
    attachment = content.get("attachment") or {}
    images = attachment.get("images") if isinstance(attachment, dict) else {}
    if isinstance(images, dict):
        entry = images.get(image_id)
        if isinstance(entry, dict):
            for key in ("key", "token", "origin_key"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""


def _button_block(prop: dict, group: str) -> dict | None:
    text = _extract_text(prop.get("text")) or "按钮"
    url = _action_url(prop)
    return {"type": "button", "text": text, "url": url, "group": group,
            "disabled": bool(prop.get("disabled"))}


def _table_block(prop: dict, group: str) -> dict | None:
    columns = prop.get("columns")
    rows = prop.get("rows")
    if (not isinstance(columns, list) or not columns) and isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict):
            columns = list(first.keys())
    if not isinstance(columns, list) or not columns:
        return None
    names: list[str] = []
    keys: list[str] = []
    for column in columns:
        if isinstance(column, str):
            names.append(column)
            keys.append(column)
            continue
        if not isinstance(column, dict):
            continue
        display = _extract_text(column.get("displayName") or column.get("display_name") or
                                column.get("title") or column.get("name"))
        key = str(column.get("name") or display or "")
        names.append(display or key)
        keys.append(key)
    if not names:
        return None
    out_rows: list[list[str]] = []
    for row in rows or []:
        if isinstance(row, dict):
            out_rows.append([_cell_value(_table_cell(row, key)) for key in keys])
        elif isinstance(row, list):
            out_rows.append([_cell_value(value) for value in row[:len(keys)]])
    return {"type": "table", "columns": names, "rows": out_rows, "group": group}


def _table_cell(row: dict, key: str) -> Any:
    value = row.get(key)
    if isinstance(value, dict) and "data" in value:
        return value.get("data")
    return value


def _cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_cell(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return _clean_cell("、".join(_extract_text(item) for item in value if _extract_text(item)))
    if isinstance(value, dict):
        if isinstance(value.get("text"), dict):
            return _clean_cell(_extract_text(value["text"]))
        return _clean_cell(_extract_text(value.get("content") or value.get("text") or value))
    return _clean_cell(str(value))


def _clean_cell(value: str) -> str:
    # 竖线由 feishu._render_table 统一转义，这里只把换行压平，避免转义两次。
    return str(value).replace("\n", " ").strip()


def _select_text(prop: dict) -> str:
    placeholder = _extract_text(prop.get("placeholder"))
    options = []
    for option in prop.get("options") or []:
        if isinstance(option, dict):
            text = _extract_text(option.get("text")) or str(option.get("value") or "")
            if text:
                options.append(text)
    parts = ([placeholder] if placeholder else []) + options
    return " / ".join(parts)


def _list_blocks(prop: dict, group: str, content: dict, out: list[dict], depth: int) -> None:
    for index, item in enumerate(prop.get("items") or []):
        if not isinstance(item, dict):
            continue
        text = _collect_inline(item.get("elements") or [], content, depth)
        if not text:
            text = _extract_text(item.get("content"))
        if not text:
            continue
        out.append({"type": "list", "text": text, "level": _int(item.get("level"), 0),
                    "ordered": str(item.get("type") or "") == "ol",
                    "index": _int(item.get("order"), index + 1), "group": f"{group}.l{index}"})


def _code_text(prop: dict) -> str:
    lines = []
    for line in prop.get("contents") or []:
        if not isinstance(line, dict):
            continue
        for part in line.get("contents") or []:
            if isinstance(part, dict):
                lines.append(str(part.get("content") or ""))
    if lines:
        return "\n".join(lines)
    return _extract_text(prop.get("content"))


def _unknown(prop: dict, group: str, content: dict, out: list[dict], depth: int) -> None:
    for key in ("content", "text", "title", "label", "placeholder"):
        value = prop.get(key)
        if isinstance(value, str) and value:
            out.append(_text_block(value, group))
            return
        if isinstance(value, dict):
            text = _extract_text(value)
            if text:
                out.append(_text_block(text, group))
                return
    elements = prop.get("elements")
    if isinstance(elements, list) and elements:
        _walk(elements, group, content, out, depth + 1)


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "".join(_extract_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("property"), dict):
        return _extract_text(value["property"])
    i18n = value.get("i18nContent") or value.get("i18n")
    if isinstance(i18n, dict):
        for locale in ("zh_cn", "zh-CN", "en_us", "en-US", "ja_jp"):
            text = _extract_text(i18n.get(locale))
            if text:
                return text
    for key in ("content", "text", "title", "label", "placeholder"):
        text = _extract_text(value.get(key))
        if text:
            return text
    elements = value.get("elements")
    if isinstance(elements, list):
        return "".join(_extract_text(item) for item in elements)
    return ""


def _link_url(prop: dict) -> str:
    url = prop.get("url")
    if isinstance(url, dict):
        for key in _CARD_URL_KEYS:
            value = url.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(url, str):
        return url
    for key in _CARD_URL_KEYS:
        value = prop.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


def _markdown_children(prop: dict) -> list:
    """markdown 的子元素：elements 和 markdownElements 取内容更多的一份（两者常重复）。"""
    candidates = [prop.get("elements"), prop.get("markdownElements")]
    best: list = []
    for candidate in candidates:
        if isinstance(candidate, list) and _inline_weight(candidate) > _inline_weight(best):
            best = candidate
    return best


def _inline_weight(elements: Any) -> int:
    if not isinstance(elements, list):
        return 0
    weight = 0
    for element in elements:
        if not isinstance(element, dict):
            continue
        tag = str(element.get("tag") or "")
        if tag in ("list", "table", "div", "note", "column_set", "action", "button"):
            weight += 10
        elif tag == "plain_text":
            prop = _prop(element)
            weight += 2 if _extract_text(prop.get("content")) else 0
        elif tag in ("link", "at", "img", "image", "code_block"):
            weight += 3
        elif tag == "br":
            weight += 1
        else:
            weight += 1
    return weight


def _at_name(content: dict, user_id: str) -> str:
    """卡片 JSON 的 at 元素只有 open_id，展示名在 jsonAttachment.atUsers 里。"""
    if not user_id:
        return ""
    attachment = content.get("jsonAttachment") or {}
    at_users = attachment.get("atUsers") if isinstance(attachment, dict) else {}
    if not isinstance(at_users, dict):
        return ""
    entry = at_users.get(user_id)
    if not isinstance(entry, dict):
        return ""
    return _extract_text(entry.get("content")) or _extract_text(entry.get("name")) or ""


def _action_url(prop: dict) -> str:
    """按钮链接在 actions[].action.url 里，不在 property.url。"""
    direct = _link_url(prop)
    if direct:
        return direct
    # 单个按钮自己带 action：{"tag":"button","action":{"type":"open_url","url":"..."}}
    own = prop.get("action") if isinstance(prop.get("action"), dict) else {}
    url = _link_url(own)
    if url:
        return url
    for action in prop.get("actions") or []:
        if not isinstance(action, dict):
            continue
        if str(action.get("type") or "") not in ("open_url", ""):
            continue
        data = action.get("action") if isinstance(action.get("action"), dict) else action
        url = _link_url(data)
        if url:
            return url
    return ""


def _chart_text(prop: dict) -> str:
    spec = prop.get("chartSpec") if isinstance(prop.get("chartSpec"), dict) else {}
    title = _extract_text(spec.get("title")) or _extract_text(prop.get("title")) or "图表"
    return "📊 " + title


def _select_img_text(prop: dict) -> str:
    options = []
    for option in prop.get("options") or []:
        if not isinstance(option, dict):
            continue
        text = _extract_text(option.get("text")) or str(option.get("value") or "")
        if text:
            options.append(text)
    placeholder = _extract_text(prop.get("placeholder"))
    parts = ([placeholder] if placeholder else []) + options
    return " / ".join(parts)


def _input_text(prop: dict) -> str:
    label = _extract_text(prop.get("label"))
    value = _extract_text(prop.get("defaultValue")) or _extract_text(prop.get("placeholder"))
    body = value + "_____" if value else "_____"
    return f"{label}: {body}" if label else body


def _picker_text(prop: dict, kind: str) -> str:
    if kind == "date":
        value = _extract_text(prop.get("initialDate"))
    elif kind == "time":
        value = _extract_text(prop.get("initialTime"))
    else:
        value = _extract_text(prop.get("initialDatetime"))
    value = value or _extract_text(prop.get("placeholder")) or "选择"
    return ("📅 " if kind != "time" else "🕐 ") + value


def _first_url(node: Any) -> str:
    if isinstance(node, dict):
        for key in _CARD_URL_KEYS:
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


def _block_text(block: dict) -> str:
    kind = block.get("type")
    if kind == "link":
        return f"[{block.get('text') or '链接'}]({block.get('url') or ''})"
    if kind == "emotion":
        return f"[表情:{block.get('key') or ''}]"
    text = str(block.get("text") or "")
    if block.get("code"):
        text = f"`{text}`"
    if block.get("bold"):
        text = f"**{text}**"
    if block.get("italic"):
        text = f"*{text}*"
    if block.get("strike"):
        text = f"~~{text}~~"
    if block.get("link"):
        text = f"[{text}]({block['link']})"
    return text


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
