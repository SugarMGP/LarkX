import asyncio
import time
from urllib.parse import urlencode
import requests
import websockets
from loguru import logger


class NetworkError(RuntimeError):
    pass


class HttpError(RuntimeError):
    pass


class SendUnconfirmed(RuntimeError):
    def __init__(self, message: str, cid: str):
        super().__init__(message)
        self.cid = cid


class MessageActionUnconfirmed(RuntimeError):
    pass


class ProtocolError(RuntimeError):
    """飞书网关返回了错误状态（status != 0）。"""


from .auth import LarkAuth
from .proto import builders, decoders, proto_pb2 as P
from .proto.ids import generate_access_key, generate_long_request_id, generate_request_cid, generate_request_id


class LarkClient:
    def __init__(self, auth: LarkAuth):
        self.auth = auth
        # 复用连接，避免每次网关调用都重新握手 TLS。
        self._session = requests.Session()
        # 云空间客户端也复用：它内部缓存 CSRF 令牌和 token 解析结果。
        self._space = None
        self.auth.require_valid()
        if not self.auth.app_key or not self.auth.user_id:
            self.auth.validate(fetch_profile=True)
            self.auth.save()

    @property
    def space(self) -> 'SpaceClient':
        """同一份凭证下的云空间/云文档接口（同一个实例，CSRF 令牌只取一次）。"""
        if self._space is None:
            from .space import SpaceClient
            self._space = SpaceClient(self.auth)
        return self._space

    def build_ws_url(self) -> str:
        device_id = self.auth.device_id
        access_key = generate_access_key(f'2{self.auth.app_key}{device_id}f8a69f1719916z')
        ticket = self.auth.get_ticket()
        params = {
            'accept_encoding': 'gzip',
            'sdk_version': '7.72.8',
            'version_code': '7.72.8',
            'type': 'sdk_main',
            'platform': '',
            'terminal_type': '2',
            'device_model': '',
            'lark_version': '7.72.0',
            'net_status': '5',
            'lark_env': 'online',
            'request_id': generate_request_id(),
            'aid': '1',
            'fpid': '2',
            'device_id': device_id,
            'access_key': access_key,
            'ticket': ticket,
        }
        return f'{self.auth.region.ws_url}?{urlencode(params)}'

    @staticmethod
    def _wrap_frame(packet: P.Packet) -> bytes:
        frame = P.Frame()
        seconds = int(time.time())
        frame.seqid = seconds
        frame.logid = seconds
        frame.service = 1
        frame.method = 1
        entry = P.ExtendedEntry()
        entry.key = 'x-request-time'
        entry.value = str(int(time.time() * 1000))
        frame.headers.append(entry)
        frame.payloadType = 'pb'
        frame.payload = packet.SerializeToString()
        return frame.SerializeToString()

    async def send_ack(self, ws, sid: str):
        pkt = P.Packet()
        pkt.cmd = 1
        pkt.payloadType = 1
        pkt.sid = sid
        pkt.payload = b'\x08\x00'
        await ws.send(self._wrap_frame(pkt))

    async def send_heartbeat(self, ws):
        pkt = P.Packet()
        pkt.cmd = 4
        pkt.payloadType = 1
        await ws.send(self._wrap_frame(pkt))

    async def heartbeat_loop(self, ws, interval=30):
        try:
            while True:
                await asyncio.sleep(interval)
                await self.send_heartbeat(ws)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f'心跳停止: {e}')

    async def connect_websocket(self, on_message):
        url = await asyncio.to_thread(self.build_ws_url)
        async with websockets.connect(url, open_timeout=15) as ws:
            logger.info('WS 已连接')
            heartbeat_task = asyncio.create_task(self.heartbeat_loop(ws))
            chain = asyncio.sleep(0)
            try:
                async for raw in ws:
                    try:
                        _, packet = decoders.parse_ws_frame(raw)
                        if packet.get("sid") is not None:
                            await self.send_ack(ws, packet["sid"])
                        if packet.get("cmd") != 6:
                            continue
                        for msg in decoders.decode_push_messages(raw):
                            if msg.get("from_id"):
                                chain = asyncio.create_task(self._chain(chain, msg, on_message))
                    except Exception as e:
                        logger.debug(f'跳过异常帧: {e}')
            finally:
                heartbeat_task.cancel()
                if asyncio.iscoroutine(chain):
                    chain = asyncio.create_task(chain)
                await asyncio.gather(heartbeat_task, chain, return_exceptions=True)

    async def _chain(self, chain, msg, on_message):
        await chain
        try:
            await on_message(msg)
        except Exception as e:
            logger.error(f'消息回调异常: {e}')

    def _gateway_post(self, packet: P.Packet) -> bytes:
        headers = {
            'content-type': 'application/x-protobuf',
            'accept': '*/*',
            'origin': self.auth.region.web_origin,
            'referer': self.auth.region.web_origin + '/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
            'x-appid': self.auth.gateway_app_id,
            'x-command': str(packet.cmd),
            'x-command-version': '5.7.0',
            'x-lgw-os-type': '1',
            'x-lgw-terminal-type': '2',
            'x-source': 'web',
            'x-web-version': '3.9.32',
            'x-request-id': generate_long_request_id(),
        }
        try:
            resp = self._session.post(self.auth.region.gateway_url, headers=headers, cookies=self.auth.cookies,
                                      data=packet.SerializeToString(), timeout=15)
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise HttpError(str(e)) from e
        except requests.RequestException as e:
            raise NetworkError(str(e)) from e
        return resp.content

    def send_msg(self, text: str, chat_id: str, cid: str=None, reply_to: str=None):
        """发送文本；reply_to 是引用回复（被回复消息 id）；cid 是幂等键，由调用方生成并在重试时复用。"""
        packet_cid = cid or generate_request_cid()
        pkt = builders.build_send_message_packet(
            text, str(chat_id), generate_long_request_id(), reply_to=reply_to, cid=packet_cid,
        )
        return self._send_packet(pkt, packet_cid)

    def send_image(self, chat_id: str, image_key: str, cid: str=None, reply_to: str=None):
        """发送图片；image_key 由 media.upload_image 上传得到；cid 是幂等键。"""
        packet_cid = cid or generate_request_cid()
        pkt = builders.build_image_message_packet(
            str(image_key), str(chat_id), generate_long_request_id(), reply_to=reply_to, cid=packet_cid,
        )
        return self._send_packet(pkt, packet_cid)

    def send_file(self, chat_id: str, file_key: str, file_name: str, file_size: int, file_mime: str,
                  cid: str=None, reply_to: str=None):
        """发送文件；文件先经 media.upload_file 上传到该会话；cid 是幂等键。"""
        packet_cid = cid or generate_request_cid()
        pkt = builders.build_file_message_packet(
            str(file_key), str(file_name), int(file_size), str(file_mime), str(chat_id),
            generate_long_request_id(), reply_to=reply_to, cid=packet_cid,
        )
        return self._send_packet(pkt, packet_cid)

    def send_message_request(self, request, cid: str=None) -> str:
        """发送一个已经组装好的 PutMessageRequest；要在正文里插真实 AT 节点时用这个。

        request 是 larkx.proto.proto_pb2.PutMessageRequest，cid 是幂等键。
        """
        packet_cid = cid or generate_request_cid()
        request.cid = str(packet_cid)
        pkt = builders.wrap_packet(5, request, generate_long_request_id())
        return self._send_packet(pkt, packet_cid)

    def _send_packet(self, pkt, packet_cid: str) -> str:
        content = self._gateway_post(pkt)
        resp = P.Packet()
        resp.ParseFromString(content)
        if resp.status:
            raise ProtocolError(f'飞书命令 {pkt.cmd} 失败: status={resp.status}')
        try:
            msg_id = builders.decode_put_message_response(content)
        except Exception as e:
            raise SendUnconfirmed(f'服务端已接受发送请求，但响应解析失败: {e}', packet_cid) from e
        if not msg_id:
            raise SendUnconfirmed('服务端已接受发送请求，但响应里没有消息 id', packet_cid)
        return msg_id

    def create_group_chat(self, member_ids, group_name: str=None, description: str=None,
                          cid: str=None) -> str:
        """建群并拉人，返回 chat_id；cid 是幂等键，由调用方生成并在重试时复用。"""
        packet_cid = cid or generate_request_cid()
        data = {"type": 2, "chatterIds": [str(member_id) for member_id in member_ids], "cid": packet_cid}
        if group_name:
            data["groupName"] = str(group_name)
        if description:
            data["groupDesc"] = str(description)
        response = self.api("chats.PutChatRequest", data)
        return str(((response or {}).get("chat") or {}).get("id") or "")

    def create_p2p_chat(self, user_id: str) -> str:
        """和某个用户的单聊：已有则返回原会话，没有则服务端新建。"""
        response = self.api("chats.PutChatRequest", {"type": 1, "chatterIds": [str(user_id)]})
        return str(((response or {}).get("chat") or {}).get("id") or "")

    def recall_message(self, msg_id: str) -> None:
        """撤回消息；服务端没有明确返回 ok 时不视为成功。"""
        response = self.api('messages.DeleteMessageRequest', {'id': str(msg_id)})
        if (response or {}).get('message') != 'ok':
            raise MessageActionUnconfirmed(f'撤回失败: {(response or {}).get("message") or "服务端没有确认"}')

    def edit_message(self, msg_id: str, text: str, cid: str=None) -> None:
        """编辑自己发的纯文本消息；cid 是幂等键，由调用方生成并在重试时复用。"""
        packet_cid = cid or generate_request_cid()
        response = self.api('messages.EditMessageRequest', {
            'msgId': int(msg_id), 'cid': packet_cid, 'type': 4,
            'content': {'richText': builders.build_text_rich_text(text)},
        })
        if not ((response or {}).get('message') or {}).get('id'):
            raise MessageActionUnconfirmed('编辑失败: 服务端没有确认')

    def mark_chat_read(self, chat_id: str, max_position: int, message_ids=None) -> dict:
        """标记会话已读，返回飞书的真实响应（已读位置、未读数等）。"""
        data = {'chatId': str(chat_id), 'maxPosition': int(max_position)}
        if message_ids:
            data['messageIds'] = [str(m) for m in message_ids]
        return self.api('messages.PutReadMessagesRequest', data)

    def api(self, type_or_cmd, data: dict=None, raw: bytes=None):
        from .proto import gateway
        cmd, req_cls, resp_cls = gateway.resolve_cmd(type_or_cmd)
        req_msg = None
        if req_cls is not None:
            req_msg = gateway.request_from_dict(req_cls, data or {})
        pkt = gateway.build_packet(cmd, generate_long_request_id(), req_msg=req_msg, req_bytes=raw if req_msg is None else None)
        content = self._gateway_post(pkt)
        resp_pkt = P.Packet()
        resp_pkt.ParseFromString(content)
        if resp_pkt.status:
            raise ProtocolError(f'飞书命令 {type_or_cmd} 失败: status={resp_pkt.status}')
        if resp_cls is not None and resp_pkt.payload:
            return gateway.response_to_dict(resp_cls, resp_pkt.payload)
        from protobuf_to_dict import protobuf_to_dict
        return protobuf_to_dict(resp_pkt)

    # ---------- 通用查询 ----------

    def pull_chats(self, chat_ids: list[str]) -> dict:
        """按 chat_id 取会话信息：群名、会话类型、单聊的 key（两个成员 id）等。"""
        response = self.api('chats.PullChatsByIdsRequest', {'chatIds': [str(c) for c in chat_ids]})
        return dict((response or {}).get('chats') or {})

    def pull_chat_members(self, chat_id: str) -> dict:
        """群成员：{成员 id: 成员信息}。

        服务端一次最多给 2000 个成员且不分页（limit/offset 被忽略、没有 nextCursor），
        成员总数看会话对象的 memberCount。
        """
        response = self.api('chats.PullChatChattersRequest',
                            {'chatId': str(chat_id), 'needChatters': True})
        return dict((response or {}).get('chatters') or {})

    def pull_chatter_profiles(self, chatter_ids: list[str]) -> dict:
        """批量拿成员资料：{id: {name, email, ...}}，一次最多 2000 个 id。

        真人成员带 email（域账号就是邮箱 @ 前的前缀），机器人没有。
        """
        response = self.api('chatters.PullChattersByIdsRequest',
                            {'chatId': 0, 'chatterIds': [str(i) for i in chatter_ids], 'needChatters': True})
        return dict((response or {}).get('chatters') or {})

    def pull_feed(self, count: int = 50, cursor: int | None = None) -> dict:
        """收件箱：最近会话和消息。cursor 传上一页的 next_cursor 取更早的一页。"""
        data = {'type': 1, 'pullFeedType': 2, 'businessType': 1, 'count': int(count)}
        if cursor:
            data['pullType'] = 2
            data['cursor'] = int(cursor)
        else:
            data['pullType'] = 1
        return self.api('feed.PullFeedCardsRequest', data) or {}

    def pull_chatter_names(self, user_ids: list[str]) -> dict:
        """批量取用户昵称：user_id -> 昵称。chatId 传 0 表示不限会话。"""
        response = self.api('chats.PullChatChattersByIDsRequest',
                            {'chatId': 0, 'chatterIds': [int(u) for u in user_ids], 'needChatters': True})
        return {str(key): str(value.get('name') or '')
                for key, value in ((response or {}).get('chatters') or {}).items()}

    def pull_messages_by_positions(self, chat_id: str, positions: list[int]) -> dict:
        """按 position 窗口取消息，返回 {position: 消息}。"""
        response = self.api('messages.PullMessagesByPositionsRequest',
                            {'chatId': str(chat_id), 'positions': [int(p) for p in positions]})
        return dict((response or {}).get('messages') or {})

    def get_user_profile(self, user_id: str) -> dict | None:
        """单个用户资料（姓名、邮箱、英文名、部门）；查不到返回 None。"""
        try:
            # targetUserId 是 int64，个别消息的发送人是 "all" 这类非数字 id，直接查不到。
            target = int(user_id)
        except (TypeError, ValueError):
            return None
        response = self.api('users.PullUserProfileRequest', {'targetUserId': target})
        if not response or not response.get('name'):
            return None
        return response

    def search_user_ids_by_email(self, email: str) -> list[str]:
        """按完整邮箱精确查人，返回匹配到的飞书数字 user id（可能多个）。"""
        response = self.api('calendarevents.SearchUserByEmailCalendarRequest',
                            {'ContactContents': [email]})
        entities = (response or {}).get('entitiesInfos') or {}
        found = [value for key, value in entities.items() if key.casefold() == email.casefold()]
        return [str(entity.get('entityId')) for value in found
                for entity in (value.get('entityInfo') or []) if entity.get('entityType') == 1]
