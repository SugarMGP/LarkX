from types import SimpleNamespace

import pytest

from larkx.client import LarkClient, NetworkError, ProtocolError, SendUnconfirmed
from larkx.proto import builders, lark_all_pb2 as P


def parsed(packet, cls=P.messages.PutMessageRequest):
    msg = cls(); msg.ParseFromString(packet.payload); return msg


def make_client(monkeypatch, reply=P.improto.Packet().SerializeToString()):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    c = LarkClient(auth)
    monkeypatch.setattr(c, "_gateway_post", lambda packet: reply)
    return c


def test_send_packet_only_supports_quoted_reply():
    req = parsed(builders.build_send_message_packet("hi", "c1", "req", reply_to="q1", cid="fixed"))
    assert (req.rootId, req.parentId, req.cid, req.isReplyInThread) == ("q1", "q1", "fixed", False)
    req = parsed(builders.build_send_message_packet("hi", "c1", "req", cid="fixed"))
    assert (req.rootId, req.parentId, req.isReplyInThread) == ("", "", False)


def test_group_and_p2p_chat_requests_differ_in_type(monkeypatch):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    c = LarkClient(auth)
    packets = []
    def post(packet):
        packets.append(packet)
        return P.improto.Packet().SerializeToString()
    monkeypatch.setattr(c, "_gateway_post", post)
    monkeypatch.setattr("larkx.client.generate_request_cid", lambda: "sdk-cid")
    assert c.create_group_chat(["1", "2"], group_name="项目群", description="描述") == ""
    assert c.create_p2p_chat("9") == ""
    group = P.chats.PutChatRequest(); group.ParseFromString(packets[0].payload)
    p2p = P.chats.PutChatRequest(); p2p.ParseFromString(packets[1].payload)
    assert (group.type, list(group.chatterIds), group.groupName, group.groupDesc, group.cid) == (2, ["1", "2"], "项目群", "描述", "sdk-cid")
    assert (p2p.type, list(p2p.chatterIds)) == (1, ["9"])


def test_send_message_request_uses_generated_cid_and_raises_unconfirmed(monkeypatch):
    import larkx.client as client_module
    monkeypatch.setattr(client_module, "generate_request_cid", lambda: "sdk-cid")
    c = make_client(monkeypatch, reply=P.improto.Packet().SerializeToString())
    with pytest.raises(SendUnconfirmed) as error:
        c.send_message_request(P.messages.PutMessageRequest())
    assert error.value.cid == "sdk-cid"


def test_network_errors_are_separate(monkeypatch):
    import requests
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u", gateway_app_id="1", cookies={},
                           region=SimpleNamespace(locale="zh_CN", web_origin="https://example", gateway_url="https://gateway"))
    c = LarkClient(auth)

    def fail(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "post", fail)
    with pytest.raises(NetworkError):
        c._gateway_post(P.improto.Packet(cmd=1))



def test_mark_read_uses_the_chat_read_request(monkeypatch):
    calls = []
    c = make_client(monkeypatch)
    monkeypatch.setattr(c, "api", lambda type_name, data: calls.append((type_name, data)) or {"readPosition": "10"})
    assert c.mark_chat_read("c1", 10, ["m1"]) == {"readPosition": "10"}
    assert calls == [
        ("messages.PutReadMessagesRequest", {"chatId": "c1", "maxPosition": 10, "messageIds": ["m1"]}),
    ]


def test_recall_and_edit_messages_use_protocol_confirmations(monkeypatch):
    c = make_client(monkeypatch)
    replies = [{"message": "ok"}, {"message": {"id": "m-new"}}]
    calls = []
    monkeypatch.setattr(c, "api", lambda type_name, data: calls.append((type_name, data)) or replies.pop(0))
    c.recall_message("m1")
    c.edit_message("7", "new text", cid="cid")
    assert calls == [
        ("messages.DeleteMessageRequest", {"id": "m1"}),
        ("messages.EditMessageRequest", {
            "msgId": 7, "cid": "cid", "type": 4,
            "content": {"richText": calls[1][1]["content"]["richText"]},
        }),
    ]
    assert calls[1][1]["content"]["richText"]["innerText"] == "new text"


def test_build_ws_url_contains_ticket_and_request_id(monkeypatch):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u", device_id="dev",
                           get_ticket=lambda: "ticket-1",
                           region=SimpleNamespace(locale="zh_CN", ws_url="wss://example/ws"))
    c = LarkClient(auth)
    url = c.build_ws_url()
    assert url.startswith("wss://example/ws?")
    assert "ticket=ticket-1" in url and "request_id=" in url and "access_key=" in url


def test_api_raises_on_protocol_error_status(monkeypatch):
    c = make_client(monkeypatch, P.improto.Packet(status=9).SerializeToString())
    with pytest.raises(ProtocolError, match="status=9"):
        c.api("users.PullUserProfileRequest", {"targetUserId": "1"})


def test_pull_chats_returns_chats_by_id(monkeypatch):
    c = make_client(monkeypatch)
    seen = {}

    def api(name, data=None, raw=None):
        seen["name"], seen["data"] = name, data
        return {"chats": {"c1": {"id": "c1", "type": 2, "name": "群"}}}

    monkeypatch.setattr(c, "api", api)
    assert c.pull_chats(["c1"]) == {"c1": {"id": "c1", "type": 2, "name": "群"}}
    assert seen["name"] == "chats.PullChatsByIdsRequest" and seen["data"] == {"chatIds": ["c1"]}


def test_pull_feed_uses_cursor_only_for_later_pages(monkeypatch):
    c = make_client(monkeypatch)
    seen = []

    def api(name, data=None, raw=None):
        seen.append(data)
        return {"chats": {}, "nextCursor": 7}

    monkeypatch.setattr(c, "api", api)
    c.pull_feed(count=20)
    c.pull_feed(count=20, cursor=7)
    assert seen[0]["pullType"] == 1 and "cursor" not in seen[0]
    assert seen[1]["pullType"] == 2 and seen[1]["cursor"] == 7


def test_pull_chatter_names_batches_ids(monkeypatch):
    c = make_client(monkeypatch)
    seen = {}

    def api(name, data=None, raw=None):
        seen.update(data or {})
        return {"chatters": {"1": {"name": "张三"}, "2": {"name": ""}}}

    monkeypatch.setattr(c, "api", api)
    assert c.pull_chatter_names(["1", "2"]) == {"1": "张三", "2": ""}
    assert seen["chatId"] == 0 and seen["chatterIds"] == [1, 2]


def test_get_user_profile_returns_none_without_name(monkeypatch):
    c = make_client(monkeypatch)
    monkeypatch.setattr(c, "api", lambda name, data=None, raw=None: {"email": "a@b.com"})
    assert c.get_user_profile("1") is None
    monkeypatch.setattr(c, "api", lambda name, data=None, raw=None: {"name": "张三", "email": "a@b.com"})
    assert c.get_user_profile("1")["email"] == "a@b.com"


def test_search_user_ids_by_email_only_keeps_exact_match(monkeypatch):
    c = make_client(monkeypatch)

    def api(name, data=None, raw=None):
        return {"entitiesInfos": {
            "san@dewu.com": {"entityInfo": [{"entityId": 123, "entityType": 1},
                                            {"entityId": 456, "entityType": 2}]},
            "other@dewu.com": {"entityInfo": [{"entityId": 789, "entityType": 1}]},
        }}

    monkeypatch.setattr(c, "api", api)
    assert c.search_user_ids_by_email("san@dewu.com") == ["123"]
    assert c.search_user_ids_by_email("SAN@DEWU.COM") == ["123"]


def test_send_message_request_sends_a_prepared_request(monkeypatch):
    c = make_client(monkeypatch)
    packets = []

    def post(packet):
        packets.append(packet)
        return P.improto.Packet().SerializeToString()

    monkeypatch.setattr(c, "_gateway_post", post)
    request = P.messages.PutMessageRequest()
    request.type = 4
    request.chatId = "c1"
    with pytest.raises(Exception):
        c.send_message_request(request, cid="fixed")
    sent = P.messages.PutMessageRequest()
    sent.ParseFromString(packets[0].payload)
    assert (sent.chatId, sent.cid) == ("c1", "fixed")


def test_get_user_profile_skips_non_numeric_ids(monkeypatch):
    """发送人是 "all" 这类非数字 id 时查资料会报错，直接当成查不到。"""
    from larkx.client import LarkClient
    from larkx.auth import LarkAuth

    class Client(LarkClient):
        def __init__(self):
            self.auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="1")
            self.calls = 0

        def api(self, type_or_cmd, data=None, raw=None):
            self.calls += 1
            return {"name": "张三"}

    client = Client()
    assert client.get_user_profile("all") is None
    assert client.calls == 0
    assert client.get_user_profile("7690398870819343329")["name"] == "张三"


def test_pull_chat_members_returns_members_by_id(monkeypatch):
    """群成员按 id 返回；总数在会话对象的 memberCount 里。"""
    from larkx.client import LarkClient
    from larkx.auth import LarkAuth

    class Client(LarkClient):
        def __init__(self):
            self.auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="1")

        def api(self, type_or_cmd, data=None, raw=None):
            assert type_or_cmd == "chats.PullChatChattersRequest"
            assert data == {"chatId": "c1", "needChatters": True}
            return {"chatters": {"123": {"id": "123", "name": "张三", "type": 1}}}

    members = Client().pull_chat_members("c1")
    assert members == {"123": {"id": "123", "name": "张三", "type": 1}}


def test_pull_chatter_profiles_returns_profiles_by_id(monkeypatch):
    """批量成员资料：一次拿多个 id，返回带 email 的资料。"""
    from larkx.client import LarkClient
    from larkx.auth import LarkAuth

    class Client(LarkClient):
        def __init__(self):
            self.auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="1")

        def api(self, type_or_cmd, data=None, raw=None):
            assert type_or_cmd == "chatters.PullChattersByIdsRequest"
            assert data == {"chatId": 0, "chatterIds": ["123", "456"], "needChatters": True}
            return {"chatters": {"123": {"id": "123", "name": "张三", "email": "san@dewu.com"},
                                "456": {"id": "456", "name": "机器人", "email": None}}}

    profiles = Client().pull_chatter_profiles(["123", "456"])
    assert profiles["123"]["email"] == "san@dewu.com"
    assert profiles["456"]["email"] is None


def test_forward_message_sends_cids_and_returns_new_message_id(monkeypatch):
    """转发必须带 cids（网关少了会回 400），并从响应里取新消息 id。"""
    from larkx.proto import gateway
    from larkx.proto import lark_all_pb2 as L
    sent = {}

    def fake_api(type_or_cmd, data=None, raw=None):
        sent["cmd"] = type_or_cmd
        sent["data"] = data
        return {"messages": {"c1": {"id": "m-new"}}}

    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    c = LarkClient(auth)
    monkeypatch.setattr(c, "api", fake_api)
    assert c.forward_message(["c1"], "m-src", cid="fixed") == "m-new"
    assert sent["cmd"] == "messages.PutTransmitMessageRequest"
    assert sent["data"]["chatIds"] == ["c1"] and sent["data"]["messageId"] == "m-src"
    assert list(sent["data"]["cids"]) == ["c1"]
