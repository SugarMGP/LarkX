"""话题群（THREAD_V2）：话题标记、推送字段、话题读取接口。

真账号实测的两条硬约束，别按直觉改回去：

- 话题群回复进话题靠 ``rootId + parentId`` **成对**出现；只给 rootId 会新开一个话题。
- **不能**设 ``isReplyInThread``：THREAD_V2 群里网关直接回
  ``400 replyInThread not support chat``（它只在普通群里有效）。
"""

from types import SimpleNamespace

from larkx.client import LarkClient
from larkx.proto import builders, decoders, lark_all_pb2 as P


def make_client(monkeypatch):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    client = LarkClient(auth)
    captured = []

    def post(packet):
        captured.append(packet)
        return P.improto.Packet().SerializeToString()

    monkeypatch.setattr(client, "_gateway_post", post)
    return client, captured


def payload_of(packet, cls):
    msg = cls()
    msg.ParseFromString(packet.payload)
    return msg


def test_push_message_exposes_topic_fields():
    message = P.entities.Message(
        id="m1", type=4, chatId="c1", chatType=2, fromId="u2",
        rootId="", threadId="t1", threadPosition=0,
        threadReplyCount=3, threadMessageType=P.entities.Message.THREAD_REPLY_MESSAGE,
    )
    request = P.messages.PushMessagesRequest()
    request.messages["m1"].CopyFrom(message)
    packet = P.improto.Packet()
    packet.cmd = 6
    packet.payload = request.SerializeToString()

    decoded = decoders.decode_push_messages(builders.wrap_frame(packet))

    assert len(decoded) == 1
    assert decoded[0]["thread_id"] == "t1"
    assert decoded[0]["thread_position"] == 0
    assert decoded[0]["thread_reply_count"] == 3
    assert decoded[0]["thread_message_type"] == 2
    # 话题群的消息 chatType 仍然是 2（GROUP），话题标识只能看 thread_id
    assert decoded[0]["chat_type"] == 2


def test_reply_into_topic_is_root_and_parent_paired_without_reply_in_thread():
    req = builders.build_message_request(4, "c1", "hi", cid="fixed", thread_id="t-root")
    assert (req.rootId, req.parentId) == ("t-root", "t-root")
    assert not req.isReplyInThread
    assert not req.isThreadGroupTopic


def test_thread_id_wins_over_reply_to():
    req = builders.build_message_request(4, "c1", "hi", cid="fixed",
                                         reply_to="m-old", thread_id="t-root")
    assert (req.rootId, req.parentId) == ("t-root", "t-root")
    quoted = builders.build_message_request(4, "c1", "hi", cid="fixed", reply_to="m-old")
    assert (quoted.rootId, quoted.parentId) == ("m-old", "m-old")


def test_rich_message_also_accepts_thread_id():
    req = builders.build_rich_message_request(
        "c1", [{"type": "paragraph", "spans": [{"type": "text", "text": "hi"}]}],
        cid="fixed", reply_to="m-old", thread_id="t-root")
    assert (req.type, req.rootId, req.parentId) == (2, "t-root", "t-root")
    assert not req.isReplyInThread


def test_pull_threads_by_positions_uses_position_window(monkeypatch):
    client, captured = make_client(monkeypatch)
    client.pull_threads_by_positions("c1", [3, 4], last_message_count=2)
    sent = payload_of(captured[0], P.thread.PullThreadsByPositionsRequest)
    assert (sent.channelId, list(sent.positions), sent.lastMessageCount) == ("c1", [3, 4], 2)
    assert sent.type == 1  # CHAT_CHANNEL


def test_pull_threads_by_ids_skips_blank_ids(monkeypatch):
    client, captured = make_client(monkeypatch)
    assert client.pull_threads_by_ids([]) == {}
    assert not captured
    client.pull_threads_by_ids(["t1", "t2"])
    sent = payload_of(captured[0], P.thread.PullThreadsByIDsRequest)
    assert list(sent.threadIds) == ["t1", "t2"]


def test_pull_thread_messages_keeps_thread_positions(monkeypatch):
    client, captured = make_client(monkeypatch)
    client.pull_thread_messages_by_positions("t1", "c1", [0, 1])
    sent = payload_of(captured[0], P.thread.PullThreadMessagesByPositionsRequest)
    assert (sent.threadId, sent.channelId, list(sent.positions)) == ("t1", "c1", [0, 1])


def test_mark_threads_read_uses_thread_request(monkeypatch):
    client, captured = make_client(monkeypatch)
    client.mark_threads_read("c1", ["t1", "t2"], 5)
    sent = payload_of(captured[0], P.thread.PutReadThreadsRequest)
    assert (sent.channelId, list(sent.threadIds), sent.readPosition) == ("c1", ["t1", "t2"], 5)


def test_gateway_declares_a_client_version_new_enough_for_topic_chats(monkeypatch):
    """群切到话题模式后，飞书按客户端版本门禁发帖；旧版本会被 400 拒绝。

    实测 `5.7.0` 被拒、`7.20.0` 可用，而且只抬 x-web-version 不够——x-command-version
    才是门禁依据。这条用例防的是有人把常量改回旧值。
    """
    from larkx import client as client_module

    class Response:
        content = b""

        @staticmethod
        def raise_for_status():
            return None

    captured = {}

    def fake_post(url, headers=None, cookies=None, data=None, timeout=None):
        captured.update(headers or {})
        return Response()

    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           gateway_app_id="app", cookies={},
                           region=SimpleNamespace(gateway_url="https://example/gateway",
                                                  web_origin="https://example"))
    gateway = LarkClient(auth)
    monkeypatch.setattr(gateway._session, "post", fake_post)

    request = P.messages.PutMessageRequest(type=4, chatId="c1", cid="c1", version=1)
    # 假响应没有消息体，发送结果必然解析失败；这条用例只关心请求头。
    try:
        gateway._send_packet(builders.wrap_packet(5, request, "req"), "c1")
    except Exception:
        pass

    for name in ("x-command-version", "x-web-version"):
        major = int(str(captured[name]).split(".")[0])
        assert major >= 7, f"{name} 退回旧版本会让话题群的发送被网关 400 拒绝：{captured[name]}"
