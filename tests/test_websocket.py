import asyncio
from types import SimpleNamespace

from larkx.client import LarkClient
from larkx.proto import lark_all_pb2 as P


class Socket:
    def __init__(self, frames):
        self.frames = frames
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def send(self, data):
        self.sent.append(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def frame(sid="sid-1", text="hello"):
    message = P.entities.Message()
    message.id = "m1"
    message.type = 4
    message.chatId = "c1"
    message.chatType = 1
    message.fromId = "u1"
    message.content = P.entities.TextContent(text="hello").SerializeToString()
    push = P.messages.PushMessagesRequest()
    push.messages["m1"].CopyFrom(message)
    return P.WebFrame(payload=P.improto.Packet(
        cmd=6, sid=sid, payload=push.SerializeToString()
    ).SerializeToString()).SerializeToString()


def test_ws_acks_and_processes_messages_in_order(monkeypatch):
    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    c = LarkClient(auth)
    monkeypatch.setattr(c, "build_ws_url", lambda: "ws://test")
    monkeypatch.setattr("larkx.client.websockets.connect", lambda url, open_timeout=15: Socket([frame(), frame()]))

    async def check():
        output = []
        async def on_message(msg):
            await asyncio.sleep(0.01)
            output.append(msg["msg_id"])

        await c.connect_websocket(on_message)
        assert output == ["m1", "m1"]

    asyncio.run(check())


def test_ws_marks_recalled_and_edited_messages(monkeypatch):
    def status_frame(status, is_removed=False, edit_version=0):
        message = P.entities.Message()
        message.id = f"m{status}{int(is_removed)}{edit_version}"
        message.type = 4
        message.chatId = "c1"
        message.chatType = 1
        message.fromId = "u1"
        message.status = status
        if is_removed:
            message.isRemoved = True
        push = P.messages.PushMessagesRequest()
        push.messages[message.id].CopyFrom(message)
        return P.WebFrame(payload=P.improto.Packet(cmd=6, payload=push.SerializeToString()).SerializeToString()).SerializeToString()

    auth = SimpleNamespace(require_valid=lambda: None, app_key="k", user_id="u",
                           region=SimpleNamespace(locale="zh_CN"))
    c = LarkClient(auth)
    monkeypatch.setattr(c, "build_ws_url", lambda: "ws://test")
    monkeypatch.setattr("larkx.client.websockets.connect", lambda url, open_timeout=15: Socket([
        status_frame(1), status_frame(2), status_frame(3),
    ]))

    async def check():
        output = []
        async def on_message(msg):
            output.append(msg["recalled"] or msg["edited"])

        await c.connect_websocket(on_message)
        assert output == [False, True, True]

    asyncio.run(check())
