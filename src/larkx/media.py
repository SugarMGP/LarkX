import mimetypes
import os
import re
from urllib.parse import unquote, urlsplit

import requests
from loguru import logger
from .auth import LarkAuth
from .config import load_config
from .region import get_region

UPLOAD_TIMEOUT = 300
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'


def _web_headers(auth: LarkAuth) -> dict:
    return {
        'origin': auth.region.web_origin,
        'referer': f'{auth.region.web_origin}/',
        'user-agent': USER_AGENT,
        'x-source': 'web',
        'x-appid': auth.gateway_app_id,
    }


def upload_image(auth: LarkAuth, data: bytes, name: str='image.png') -> str:
    """上传图片，返回图片 key（img_v3_…）；发图片消息前先调它。"""
    resp = requests.post(f'{auth.region.file_host}/api/image',
                         files={'image': (name, data)}, cookies=auth.cookies,
                         headers=_web_headers(auth), timeout=UPLOAD_TIMEOUT)
    resp.raise_for_status()
    key = re.search(r'img_[\w-]+', resp.text)
    if not key:
        raise ValueError(f'图片上传失败: {resp.text[:200]}')
    logger.info(f'图片已上传: {key.group(0)} ({len(data)} 字节)')
    return key.group(0)


def upload_file(auth: LarkAuth, chat_id: str, data: bytes, name: str) -> dict:
    """上传文件到会话（文件归属于会话），返回 {key, name, size, mime}。"""
    resp = requests.post(f'{auth.region.file_host}/api/chats/{chat_id}/files',
                         files={'file': (name, data)}, cookies=auth.cookies,
                         headers=_web_headers(auth), timeout=UPLOAD_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    info = body.get('data') or {}
    if body.get('status') != 'success' or not info.get('key'):
        raise ValueError(f"文件上传失败: {body.get('message') or body.get('status') or '响应里没有文件 key'}")
    out = {'key': str(info['key']), 'name': info.get('name') or name,
           'size': int(info.get('size') or len(data)),
           'mime': info.get('mimetype') or 'application/octet-stream'}
    logger.info(f"文件已上传: {out['key']} ({out['size']} 字节)")
    return out


def image_keys(content: dict) -> list[str]:
    """从消息内容里取图片 key（img_v3_…）。新版图片在 imageV2 里，旧版在 image 里。"""
    content = content or {}
    v2 = content.get("imageV2") or {}
    if v2.get("imageKey"):
        return [str(v2["imageKey"])]
    image = content.get("image") or {}
    origin = image.get("origin") or {}
    key = origin.get("key") or image.get("imageKey") or image.get("key")
    return [str(key)] if key else []


def open_message_resource(auth: LarkAuth, key: str, message_id: str = '') -> tuple[requests.Response, str]:
    """打开消息里的资源（图片/文件/视频/语音通用），返回 (上游响应, content-type)。

    上游响应是流式的，调用方负责 close；key 不存在时上游回 400，这里抛 KeyError。
    content-type 按上游 Content-Disposition 里的文件名判断（飞书对视频也给 text/plain）。
    """
    url = f'{auth.region.file_host}/download/messages/{message_id}/keys/{key}'
    resp = requests.get(url, cookies=auth.cookies, headers=_web_headers(auth), timeout=60, stream=True)
    if resp.status_code >= 400:
        body = resp.content[:200]
        resp.close()
        # 只有上游明说 token 不认才当成"资源不存在"，别的 400 是参数/权限问题。
        if resp.status_code == 400 and b"invalid token" in body:
            raise KeyError(f'飞书不认这个资源 key: {key}')
        resp.raise_for_status()
    name = _attachment_name(resp.headers.get('content-disposition', ''))
    content_type = mimetypes.guess_type(name)[0] or resp.headers.get('content-type') or 'application/octet-stream'
    return resp, content_type


def _attachment_name(disposition: str) -> str:
    """从 Content-Disposition 里取文件名（百分号编码的会解码）。"""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition or '', re.IGNORECASE)
    if not match:
        return ""
    return unquote(match.group(1).strip())


def message_attachments(message_type: int, content: dict) -> list[dict]:
    """把消息内容里的资源整理成统一的附件列表：[{kind, key, name, size}]。

    kind 区分资源类型（图片/文件/视频/语音），因为它们的 key 前缀和下载方式都不一样；
    位置、名片这类没有资源的类型不进附件。
    """
    content = content or {}
    if message_type == 5:
        return [{"kind": "image", "key": key, "name": "", "size": int(content.get("originSize") or 0)}
                for key in image_keys(content)]
    if message_type in (3, 13):
        return _attachments("file", content)
    if message_type == 15:
        return _attachments("video", content)
    if message_type == 7:
        return _attachments("audio", content, name="")
    return []


def _attachments(kind: str, content: dict, name: str = None) -> list[dict]:
    """附件列表；没有 key 的（比如 CLOUD_FILE 解析不出 key）就不产出。"""
    key = content.get("key", "")
    if not key:
        return []
    return [{"kind": kind, "key": key,
             "name": content.get("name", "") if name is None else name,
             "size": int(content.get("size") or 0)}]
