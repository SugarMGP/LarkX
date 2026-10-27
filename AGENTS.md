# LarkX Agent Guide

## 这是什么

飞书**真实个人账号**网页版协议的 Python 实现（库，不是服务）：扫码/Cookie 登录、长连接收消息、发消息/回复/撤回/编辑、标已读、群聊与单聊、云空间与云文档。

唯一使用者是同机的 `feishu_connector`（另一个仓库），它通过 `pyproject.toml` 的 `larkx = { git = "…/LarkX.git", branch = "master" }` 依赖本库。**协议能力都放在这里，不要复制或 vendoring 到下游。**

本仓库是公开仓库：不要提交 cookie、凭证、二维码、真实聊天原文。

## 模块地图

| 文件 | 内容 |
|---|---|
| `auth.py` | 扫码登录（`QrLogin`）、Cookie 导入、票据、凭证读写（`~/.larkx/credentials.json`，可用 `LARKX_HOME` 改目录） |
| `client.py` | IM 网关客户端：protobuf over HTTP + WebSocket 长连接，`api(cmd, data)` 统一收发 |
| `region.py` | 国内版 feishu.cn 与国际版 larksuite.com 的域名差异（同一套协议，只换域名/语言） |
| `space.py` | 云空间与云文档：JSON over HTTP，和 IM 网关**不是一套协议** |
| `media.py` | 图片/文件上传（`upload_image`/`upload_file`）、消息资源下载（`open_message_resource`，流式） |
| `proto/` | 协议资产：`lark_all_pb2.py`（消息定义）、`cmd_map.json`（命令号↔类型）、`gateway.py`（按名字解析命令） |

## 两套协议，别混

| | IM 网关 | 云空间 / 云文档 |
|---|---|---|
| 形态 | protobuf over HTTP + WebSocket | JSON over HTTP |
| 主机 | `internal-api-lark-api.<domain>` | `internal-api.<domain>`（explorer）/ `internal-api-space.<domain>`（box、docx、权限） |
| 鉴权 | 登录 cookie + ticket | 同一套登录 cookie |
| 写操作 | 不需要 CSRF | explorer/suite 接口要 `X-CSRFToken` |

文档站自己的源是 `poizon.feishu.cn`（云空间/文档请求实际发到这里；`internal-api-space` 同样可用）。

**origin 和 referer 必须一致**（都填同一个域），不一致会报 `referer check fail`。
**`X-CSRFToken` 大小写敏感**（`x-csrf-token` 会被拒 403）：值就是 `_csrf_token` cookie，任意一次 space/api 读接口的响应就会下发它；一次读接口可以同时拿到多个接口要用的 token。

## 已验证的能力

IM 网关：

| 命令 | cmd | 用途 |
|---|---|---|
| `users.PullUserProfileRequest` | 5017 | 拉单个用户资料（name/email/enName/departmentName） |
| `calendarevents.SearchUserByEmailCalendarRequest` | 1011020 | **邮箱 → user id 索引**，`ContactContents` 可重复（批量），返回按邮箱分键，`entityType=1` 是人 |
| `chats.PullChatsByIdsRequest` | 64 | 按 chat_id 取会话对象（`name`/`type`/`key`） |
| `chats.PullChatChattersByIDsRequest` | 5023 | **批量拿人名**：`chatterIds` 可重复，`chatId` 传 0，一次能拿几十个人 |
| `feed.PullFeedCardsRequest` | 1000 | 收件箱（最近会话 + 消息），`pullType=1` 第一页、`pullType=2`+cursor 翻页 |
| `chats.PullChatChattersRequest` | 44 | 群成员（单个会话） |

`LarkClient` 已经封好的通用方法（下游直接用，不要自己拼 `client.api`）：

| 方法 | 用途 |
|---|---|
| `pull_chats(chat_ids)` | 按 chat_id 批量取会话信息，返回 `{chat_id: 会话}` |
| `pull_feed(count, cursor)` | 收件箱：最近会话 + 消息；cursor 传上一页的 next_cursor |
| `pull_chatter_names(user_ids)` | 批量取昵称 `{user_id: 昵称}`（`chatId` 传 0，不限会话） |
| `get_user_profile(user_id)` | 单个用户资料（姓名/邮箱/英文名/部门），查不到返回 None |
| `search_user_ids_by_email(email)` | 按完整邮箱精确查人，返回命中的数字 user id 列表 |
| `client.space` | 同一份凭证下的 `SpaceClient`（云空间/文档） |

协议层的错误语义都在库里：`api()` 遇到 `status != 0` 抛 `ProtocolError`；空间接口的 HTTP 错误统一抛 `SpaceError`；`validate()` 里网络异常向上抛，不会被当成"凭证过期"。

云空间/文档：

| 接口 | 用途 |
|---|---|
| `GET /space/api/explorer/v3/children/list/` | 我的云空间列表，**根目录节点 = `type==4` 且 `token == obj_token` 的那个** |
| `GET /space/api/explorer/folder/children/` | 某个目录下的文件 |
| `GET /space/api/wiki/v2/tree/get_node/` | wiki 节点 → 真实文档 token、空间 id、标题 |
| `GET /space/api/docx/pages/client_vars` | 文档正文的所有块（`block_map` + `block_sequence`） |
| `POST /space/api/box/upload/prepare\|finish` + `box/stream/upload/merge_block` | 上传字节，换 `file_token` |
| `POST /space/api/import/create` + `GET import/result/{ticket}` | 把上传的文件转成云文档；`point.mount_type` 1=云空间文件夹、2=wiki 节点 |
| `GET /space/api/search/permission_candidates/?query=` | 云空间搜人/搜群，返回 `owner_id`/`owner_name`/`owner_type` |
| `POST /space/api/suite/permission/members/create/` | **给文档加协作者** |
| `GET /space/api/suite/permission/members.v2/?token=&type=22` | 读文档的协作者（可访问人）列表，members + entities.users |
| `GET /space/api/meta/?token=&type=22` | 文档元数据：标题、所有者、创建/最后修改人、创建/修改时间、密级 |
| `GET /space/api/explorer/v2/obj/path/?obj_token=&obj_type=22` | 文档所在位置（云空间层级；wiki 里的文档返回空） |
| `POST /space/api/doc.v2/update_title` | 改标题，`{"token":..,"obj_type":22,"title":".."}` |
| `POST /space/api/suite/permission/members/delete/` | 移除协作者，表单 `token/type/owner_id/owner_type`（单个，不是数组） |
| `POST /space/api/suite/permission/check_lock/public/update/` | 改可见范围前的校验，`{"token":..,"type":22,"link_share_entity_v2":N}` → `{"need_lock":bool}` |
| `POST /space/api/suite/permission/members/delete/` | 移除协作者 |
| `POST /space/api/explorer/obj/delete/` | 删文档（参数 `obj_token` + `obj_type`） |

文档权限的细节（真账号验证过）：

- 协作者是**用户（owner_type=1）或群组（owner_type=2）**；加协作者表单里的 `permission` 是 1=可读、4=可编辑。
- 读回来的 `members[].permissions` 是另一个位掩码：1=可读、含 4=可编辑、所有者是 1039。判断能不能编辑看 `permissions & 4`。
- 权限/元数据接口只认**真实文档 token**（`doxcn...`），wiki 链接要先换成它；`space.document_token(url_or_token)` 做这件事。
- 链接可见范围（谁拿到链接能打开）**在得物是租户策略写死的，不用读也不用改**：`public.v4` 那个接口和 `check_lock/public/update` 都不用碰。
- **没逆出来的**：真正写链接可见范围的接口（`suite/permission/public/update/` 存在但参数不对，网页端那步的请求没抓到），所以 LarkX 只读不写可见范围。
- 协作者候选人在网页端是走 IM 网关搜的（`SEARCH_DOC_WEB`），`space.search_user()` 走的是云空间的 `search/permission_candidates/`，两个都能搜到人。

## 文档接口的返回约定

`SpaceClient` 的文档接口**统一返回整理过的稳定字段**，不把飞书原始响应透传给调用方：

| 方法 | 返回 |
|---|---|
| `document_info` | `{token, title, type, url, owner{id,name}, creator{id,name}, last_editor{id,name}, create_time, update_time, path[], total, members[{id,name,kind(user/group),is_owner,permission(read/edit)}]}`（wiki 文档的 path 走知识库节点树、url 给 wiki 链接；云空间文档走云空间路径、url 给 docx 链接） |
| `wiki_node` / `wiki_path` | 知识库节点信息 / 节点在知识库里的层级（从最上层到它自己） |
| `document_outline` | `{token, title, sections: [{id, level, title, blocks: [{id, type, text}]}]}` |
| `update_block_text` / `set_block_type` / `delete_block` / `insert_block` | `{block_id, version, ...}`，version 是改完后的块版本号 |
| `document_outline` | 章节结构，块里带 `children` 子块树（和 markdown 的层级一致） |
| `delete_section` | `{blocks: [被删的块 id]}` |
| `add_member` / `remove_member` / `update_title` | `{success_count, fail_owners}` / `{removed}` / `{token, title}` |

要原始结构就用 `document_blocks`（块树本身就是协议结构）。

块树有两层：**块在正文流里的顺序**（`block_sequence`，标题下的正文就是靠顺序排在标题后面，不是它的子块）和**父子层级**（`data.children`，只有列表子项、表格单元格这类才用得上）。`insert_block(parent_id=...)` 往第二层插，读取侧要认这个层级才对得上：`document_outline` 的块带 `children` 子块树，markdown 里嵌套列表缩进两格。上游字段名（`owner_user_name`、`permissions` 这种）只出现在 LarkX 内部，别泄漏到调用方。

加协作者的细节（`SpaceClient.add_member`）：

- 表单编码，`owners` 是 JSON 数组 `[{"owner_id": "<飞书数字 user id>", "owner_type": 1, "permission": 4}]`
- `owner_type`：1=人、2=群
- `permission` 是位掩码：1=可阅读、2=可评论、4=可编辑、8=可分享、1024=完全权限；加"可编辑"后成员表里是 7（读+评论+编辑）
- 必须带 `X-CSRFToken`；`notify_lark=0` 表示不通知对方（链接要自己发）
- 混入无效 user id 会整个请求失败（`code=2`）；全是无效 id 反而返回成功但不生效——**要自己校验 `success_count`**

单聊没有群名（`Chat.name` 和 `i18nNames` 都是空的），"标题"就是对方昵称：

- 对方 id 从 `Chat.key` 取（格式 `"id1:id2"`，**顺序不固定**，取不是自己的那个）；`ownerId` 不可靠（自己建的会话里 ownerId 是自己）
- 昵称：`chats.PullChatChattersByIDsRequest` 一次批量拿（响应里**没有邮箱**），或用 users 表缓存

## 走不通的路（别再试）

- `users.SearchUsersByContactPointRequest`（cmd 5019，按联系方式搜人）→ **只回空包**，userProfiles 永远是空。cmd 表里 5019 还兼作 PUSH_RESOURCE。
- `users.BatchPullUserProfileRequest`（cmd 5026，批量拉资料）→ 同样**只回空包**。所以批量拉资料没有接口，只能一个个 `PullUserProfileRequest`。
- `contact.v1.*` 那批类型在协议资产里**不存在**；`contact.v1.SearchUserByContactPointRequest` 在 cmd 表里也没有命令号，构造不出来。
- `chats.PullContactsRequest(inChatId=…)` → 返回的是公共联系人/群，不是单聊对方，**不能拿来当会话标题**。
- `GET /space/api/explorer/root/` → 返回 `{"code":0,"data":{}}`，空的，拿不到根目录 token（要从 children/list 里找）。
- `GET /space/api/explorer/root/get/`、`/explorer/v2/root_folder/meta/` → 不是 404 页面就是 HTML，没有这个接口。
- `chats.PullChatChattersByIDsRequest` 只按 `chatterIds` 批量，`chatId` 传 0 就行，不用给真实会话。
- **改已有文档正文不用 WebSocket**：编辑器的实时协作通道（box-stream）只在浏览器里用，保存走 HTTP `POST https://poizon.feishu.cn/space/api/docx/blocks/user_change`（见下）。导入只能**新建**文档，不能覆盖已有文档。
- 要按 lark-cli 的形式改文档（块锚定的增删改），用 `lark-cli docs +update`（开放平台身份）不要自己造；但我们是真账号，只能走上面的 user_change 自己改。
- `users.BatchPullUserProfileRequest` 和 `users.SearchUsersByContactPointRequest` 一样只回空包；`users.PullUserProfileRequest`（单个）和 `calendarevents.SearchUserByEmailCalendarRequest`（按邮箱索引）能用。

## 改文档正文：已跑通的 user_change 协议

编辑器保存正文走一个 HTTP 接口（**不需要 WebSocket**），已用真实账号验证能改、能删、能整段替换。

```text
POST https://poizon.feishu.cn/space/api/docx/blocks/user_change
头：X-CSRFToken(=_csrf_token cookie)、Content-Type: application/json;charset=UTF-8、Referer: <docx url>
体：{"member_id":"10000000000000", "uuid":<uuid4>, "page_id":<文档 token>,
     "change_map":{"<块 id>":{"id":<块 id>, "version":<块当前版本>,
       "payload":{"ops":[{"p":["text"],"subType":{"t":"easysync","o":{
          "zone_changesets":{"0":"<changeset>"},
          "apool":{"numToAttrib":{"0":["author","<我的 user id>"]},"nextNum":1,
                    "attribToNum":{"author,<我的 user id>":0}}}}]}}}}
```

changeset 串格式（数字是 **base36 小写**，`0-9a-z`，`k`=20、`j`=19）：

```text
<itemType><zoneId>:<oldLen><长度差><ops>[#<charBank 长度>]$<插入的文字>
```

- `itemType`：`Z`=正文块（`R`/`C` 是别的类型）
- `zoneId`：默认 `"0"`，为 0 时省略
- `oldLen`：块当前正文长度
- `长度差`：变长写 `>N`、变短写 `<N`（N 是新旧长度差）
- `ops`：Etherpad 风格操作串，`=N` 保留 N 字、`-N` 删 N 字、`*N` 设样式 N
- `$` 后面是本次插入的文字

例子（块原文 20 个字符）：

| 动作 | changeset | 结果 |
|---|---|---|
| 末尾追加 ABC | `Z:k>3=k*0+3$ABC` | 原文+ABC |
| 删掉最后一个字 | `Z:k<1=j-1$` | 少最后一个字 |
| 整段替换 | `Z:k<j=0-k*0+j$新文字` | 只剩新文字 |

坑：

- 插入的正文要带样式：`*0+N`（0 号样式=author，N=插入字数）；**删掉的字不要带样式**（`*0-1` 会被服务端拒）。
- changeset 里的"长度差"是**改完之后正文总长减旧长度**：追加 3 个字到 7 个字的块上是 `>3`（`Z:7>3=7*0+3$新字`）。服务端目前不校验这个字段，但写错了以后服务端一旦校验就会报 `algorithm process fail`。
- **长度一律按块里的原始正文算**（`data.text.initialAttributedTexts.text` 拼起来的纯文本），不是按 markdown 渲染后的文本：`这是**加粗**文字` 原始是 6 个字、markdown 是 10 个字，用 10 写进 changeset 服务端会报 `algorithm process fail`。
- **长度按 UTF-16 code unit 算**（飞书服务端是 JS 字符串）：emoji 算 2 个，`len(s.encode("utf-16-le")) // 2`。用 Python 的字符数会少算，插入时最后几个字会静默丢掉。
- 块的版本号（乐观锁）0 是合法值，取版本要用 `int(block["version"])`，别用 `or 1` 把它变成 1。
- `member_id` 用常量 `10000000000000` 即可，随机 14 位数字也能过网关，但计算阶段会失败。
- `version` 用块的当前版本（`client_vars` 里每个块都有 `version`），版本不对会被拒。
- 块的正文结构：`data.text.initialAttributedTexts = {"attribs": {"0": "*0+23"}, "text": {"0": "正文"}}`。
- 新块只能**空着建**（`oi` 里带正文会被判 `schema mismatch`），正文用同一次请求里的第二条 changeset op 写（`Z:0>N*0+N$正文`）。
- 删一个章节时，正文里的**嵌套块**（比如列表项下面的子项）会跟着父块一起没，不能再单独删，否则报"块不存在"；同一个父块下的多个块要**从后往前**删，序号才不错位。
- 服务端失败时返回 `{"code":4000028,"msg":"algorithm process fail"}`，多半是 changeset 语法或 version 不对。

**块树操作**（改块结构，走同一个 user_change 接口）：

- 删一个子块：change_map 的 key 是**父块 id**，`ops` 是 `{"p":["children",序号],"action":{"ld":"<子块 id>"}}`。
- **新建块**：change_map 带**两条**——新块自己（`version:0`，`ops` 是 `{"p":[],"action":{"oi":<整块数据>}}`）和它的父块（`{"p":["children",序号],"action":{"li":"<新块 id>"}}`）。`oi` 的字段：
  `{"parent_id":<父块 id>,"type":"text","children":[],"comments":[],"revisions":[],"author":<我的 user id>,"text":{"initialAttributedTexts":{"text":{"0":""},"attribs":{"0":""}},"apool":{"numToAttrib":{},"nextNum":0}},"folded":false}`
  序号 `["children", N]` 是插入位置（0 基，插在第 N 个子块之前）。**新块必须空着建**，`oi` 里带正文会被服务端拒（`schema mismatch`）；正文用同一次请求里的第二条 op 写（`{"p":["text"],"subType":{...}}`，changeset 用 `Z:0>N*0+N$正文`）。
- **换块类型**：在块上发一条 `{"p":["type"],"action":{"od":<旧类型>,"oi":<新类型>}}` 即可，version 用块的当前版本。
- 块类型取值：`page`、`heading1`~`heading9`、`text`、`bullet`、`ordered`、`todo`、`quote`、`code`、`divider`、`table`、`image`。`ordered` 不带 `seq` 也能正常显示编号（前端按位置算），`todo` 不带 `done` 默认未完成。

- **局部样式**（选中一段加粗）：还是 `{"p":["text"],"subType":{...}}`，changeset 用 `*<属性号>=<字数>` 而不是 `*<属性号>+<字数>`，例如给 10 个字的块里第 6~10 个字加粗是 `Z:a>0*0=5$`，`$` 后面没有新文字，属性定义放在同一条 op 的 `apool.numToAttrib` 里（`{"0":["bold","true"]}`）。属性名至少有 `bold`、`italic`、`link`、`color`、`background` 等，读取侧 `_decode_line` 已经能解析。
- **块移动/排序**：没抓到。网页端拖拽块、`Cmd+Shift+↑/↓` 都没产生 user_change（LarkX 里删块+插入块可以拼出同样的效果，先删后插）。

抓这些 op 的办法（headless 里 `Input.dispatchKeyEvent` 不生效，必须 headful）：`/Applications/Google Chrome.app/... --remote-debugging-port=9336 --user-data-dir=/tmp/x`，CDP 连上后先 `Network.setCookie` 注入 cookie、导航到文档，**用 `Input.dispatchMouseEvent` 真点**进某个块（`Input.insertText` 会改 DOM 但光标键不响应，`Home`/方向键在未真点聚焦时是空操作），再用 `Input.dispatchKeyEvent` 发回车/字符。注意 CDP 客户端要用单个 reader 任务分发消息，`call()` 里顺手 `recv()` 会把 `Network.requestWillBeSent` 事件吞掉。

## 消息类型：24 种，一个接口全发

协议枚举 `entities.Message.Type` 有 24 种（0/2~20/22~25，1 和 21 没用）。**发消息只有一个接口**：`messages.PutMessageRequest`（网关 cmd 5），靠 `type` 区分，每种类型用 `content` 里的不同字段：

| type | 名称 | content 字段 | 实测 |
|---|---|---|---|
| 4 | TEXT 文字 | `richText`（可含 IMG 元素 → 图文混排） | ✅ |
| 2 | POST 富文本 | `title` + `richText` | ✅（带 IMG 元素会 400） |
| 5 | IMAGE 图片 | `cryptoToken`=image key | ✅ |
| 3 / 13 | FILE / CLOUD_FILE | `fileKey` `fileName` `fileMime` `fileSize` | ✅ |
| 15 | MEDIA 视频 | `fileKey` `fileName` `fileMime` `fileSize` + `duration` | ✅ |
| 7 | AUDIO 语音 | `audioKey` + `audioDuration` | ✅（缺音频上传口） |
| 23 | SHARE_USER_CARD 用户名片 | `shareUserId` | ✅ |
| 24 | TODO 待办 | `todoOperationContent` | ✅ |
| 20 | LOCATION 位置 | `location`（LocationContent） | ✗ 400，参数没试出来 |
| 9 | SHARE_GROUP_CHAT 分享群聊 | `chatId` | ✗ 400，参数没试出来 |
| 10 | STICKER 表情 | `stickerId` `stickerSetId` | 需要真贴纸 id |
| 14 | CARD 卡片 | `cardType` + `richText` | ✅ 发得出去但**丢标题**；要原样发用转发（见「卡片消息」一节） |
| 11 | MERGE_FORWARD 合并转发 | `attachments` | 复杂 |
| 12/16/18 | 日历系列 | `calendarEvent` `calendarType` | 依赖飞书日历对象 |
| 6/8/17/19/22/25 | 系统/邮件/红包/会议/商业化红包/文件夹 | 服务端产生 | 用不上 |

**图文混排**（一条消息里既有文字又有图片）：用 type=4 的普通文字消息，`richText` 里放一个 tag=2(IMG) 的元素，元素的 `property` 是 `entities.ImImage{imageKey, fsUnit, crypto}`，并把元素 id 放进 `richText.imageIds`；实测发出去、收回来是"看图 [图片]"一条消息。

## 消息资源：上传、key、下载

- **上传图片**：`POST {file_host}/api/image`（multipart）→ 返回图片 key `img_v3_…`（`media.upload_image`）。
- **上传文件**：`POST {file_host}/api/chats/{chat_id}/files`（multipart）→ `{key, name, size, mimetype}`（`media.upload_file`），文件归属于会话。
- **消息里的资源**：图片在 `ImageContent`（类型 5）的 `imageV2` 里（`imageKey`、`fsUnit`、`crypto`、`variants`、`thumbnailInline`）；文件/视频/语音分别是 `FileContent`(3/13)、`MediaContent`(15)、`AudioContent`(7)，里面是 `key`（`file_v3_…`）、`name`、`size`、`fsUnit`，视频还有 `duration`。
- **取回字节（唯一能用的下载口）**：`GET {file_host}/download/messages/{message_id}/keys/{key}`（带登录 cookie）→ **明文**，图片/文件/视频/语音通用。封装是 `media.download_message_resource(auth, key, message_id)`，返回 `(字节, content-type)`。
  - 图片的 key 绑在消息上，`message_id` 必须对得上；文件/视频/语音只认 key，`message_id` 随便传。
  - 响应带 `Content-Disposition: attachment; filename="xxx"`，但 `Content-Type` 对视频也是默认的 `text/plain`，所以按文件名重猜（`mimetypes`）。
  - key 不存在时上游回 400 `invalid token`，封装里抛 `KeyError`。
  - `media.open_message_resource(auth, key, message_id)` 返回 `(上游响应, content-type)`，响应是 `stream=True` 的，调用方负责 `close()`；上游的 `Content-Disposition`（真实文件名）在响应头上，下载侧可以直接透传。
- **走不通的下载路**（别再试）：
  - `GET {file_host}/static-resource/file/{key}` 和 `/static-resource/image/{key}` 返回的是**密文**（比明文多 16 字节 GCM tag）。图片的密钥在消息的 `crypto` 里能解开，但文件/视频/语音的消息内容里**没有密钥**，这条路对它们是死的。
  - IM 网关的 `media.v1.*`（`DownloadFile` 1033、`DownloadFiles` 1151、`DownloadResource` 1044、`GetResourcePath` 1168、`GetResourceUrls` 3163、`DecryptFile` 1155…）在本租户 appid 报 `command: N not found`；就算用 `cmd` 直连网关也拼不出 payload——这些 proto 的定义在网页端的 Rust/WASM SDK 里，前端 JS chunk 只有「命令号↔消息名」对照表，猜字段一律返回 `code 100053 call server api unknown error`。
  - 网页端自己下载走的是 `LarkAPI.transport.callServerApi`（WASM 内部编码），浏览器里能调，但服务端对文件下载这套命令直接拒绝，网页端最终也是落到上面那个文件服务 HTTP 口。
- 参考：TS 版同源项目 <https://github.com/fnfun/lark-agent-x-ts> 的 `src/media.ts` 用的是同一个下载 URL，可以对照。

## 消息内容：LarkX 只给原始信息，文本由下游拼

`decoders.decode_message_content(type, bytes)` 返回 `(正文, 内容字典)`：**只有文字(4)和富文本(2)给正文**，其它类型正文是空串，内容全在字典里（LarkX 不做展示层拼装）。

`decoders.message_facts(type, content)` 按类型挑出"意图上重要"的原始值（不格式化，怎么拼由调用方决定）：

| 类型 | facts |
|---|---|
| 3/13 文件、15 视频 | `kind` `name` |
| 7 语音、5 图片、10 表情 | `kind` |
| 14 卡片 | `kind` `title` `subtitle` `description` `url`（老卡片取 cardHeader，新卡片取 jsonCard：标题/副标题在 `config.header`，正文是第一个 markdown/plain_text/div/note 元素；链接依次找 `actionUrl`、richtext 里的 anchor/link、jsonCard 里第一个 url） |
| 19 视频会议 | `kind` `topic` `start_ms` `end_ms` |
| 20 位置 | `kind` `name` `description` |
| 23 名片 | `kind` `name` `user_id`（域账号由下游按用户缓存补） |
| 9 群分享、11 合并转发 | `kind` `name` |
| 8 邮件、17 红包 | `kind` `title`/`subject` |
| 16 日程 | `kind` `attendees`（**日程事件在协议里是 bytes，我们的 proto 没有 CalendarEvent 结构，时间地点解析不出来**；要逆出来需要一条真实日程消息样本） |
| 6 系统 | `kind` `text` |
| 其它 | `kind` = "XXX类型消息" |

## 卡片消息（type=14）：能解析什么、怎么原样发

飞书卡片在协议里有两套：`cardVersion=2` 的**老卡片**（`cardHeader` + `richtext` 块树）和**新卡片**（`jsonCard` 字段，整张卡片 JSON）。本租户机器人发的**全是老卡片**（实测某机器人会话 31 条消息里 12 条卡片，`jsonCard` 全为空）。

**老卡片的正文全在消息里**，结构是 `richtext.elements.dictionary` 的一棵树：

- `tag=1` TEXT → `property` 是 `TextProperty{content}`；`tag=5` AT → `AtProperty{userId,content}`；`tag=6` 链接 → `LinkProperty{url}`
- `tag=2` IMG → `ImageProperty{token,originKey,…}`，图片 key 是 `img_v3_…`，照常能下载
- `tag=11` BUTTON → `ButtonProperty{actionId,mode}`，**只有动作 id、没有跳转 URL**（`actionId` 长这样 `convert|7a4d2922-…`）
- 块的语义靠 `styleKeys` 区分：`block_markdown`、`block_div_txt`、`block_action_p_*`、`block_columnset_p`、`block_column_p`、`block_image_p`、`block_hr`、`block_note_p`、`block_button_txt_default`…
- 带 markdown 块的卡片，文本可能只有 `Upgrade to the latest app version to view the content` 这种**降级提示**（真正文不在消息里，客户端自己取）；纯文本块的卡片正文完整（实测一张招聘卡有 18 个文本节点 + 12 个按钮 + 2 张图）
- **卡片封面图现在不在 attachments 里**：`media.message_attachments` 对 type=14 取的是 `content["richText"]`，而卡片字段叫 `richtext`（少个 h），取不到
- `actionUrl`（`https://internal-api.feishu.cn/lark/card/api/actionv2`）是**卡片动作接口**，不是内容链接，别当卡片地址给下游

**原样发出去只有一条路：转发。**

| 命令 | cmd | 参数 |
|---|---|---|
| `messages.PutTransmitMessageRequest` | 51 | `chatIds` + `messageId` + **`cids`（map，键=chat_id，值=cid；缺了网关回 400 `CIDs required`）** |

实测：把自己收到的卡片转发到群/单聊都成功，落库消息与源卡片逐字段一致（`cardHeader`/`cardVersion`/`botId`/`richtext` 全同），只有**图片 key 会换成新的**（飞书重新托管），用新消息 id 照样能下载。

**自己造卡片发出去会丢标题**：`messages.PutMessageRequest`（cmd 5）`type=14` + `content.cardType` + `content.richText` 能发出去（实测 HTTP 200），但 `PutMessageRequest.Content` 里**没有 cardHeader 字段**，收到的卡片 `cardHeader` 为空——标题和主题色丢了；`content.title` 也不行。

**走不通的造卡路（别再试）**：

- `card_utils.CardJsonToPBRequest`（cmd 2070，卡片 JSON → PB）：`clientId` 随便填都返回 `status=1001`（填空返回 1000），拿不到 `i18nCard`——普通用户身份走不通，估计要 app/bot 身份。
- `thriftMessage` 塞卡片原始 bytes → 网关 502 `call kite service fail`。
- 按钮跳转 URL：`actionUrl` 直接 POST 报 `code 300000`（参数不全），`openplatform.v1.*` 那批卡片/通用卡片命令在协议资产里根本不存在。

## 不要留没人用的封装

`LarkClient` 的方法如果只有自己那半个调用方在用，就是负债：会烂掉，还会被下游当成可用能力直接调。删掉比留着好。判断"没人用"要 grep 整个仓库（含 tests）和下游仓库。

## 真账号验证

- 凭证默认在 `~/.larkx/credentials.json`；真账号验证用同机其它项目里的凭证文件，`LARKX_HOME=<目录>` 指过去即可。**不要把 cookie 写进仓库。**
- 测写操作前先确认有账号在跑；`LarkAuth.validate()` 返回 (ok, msg)。
- 涉及别人账号的副作用（加协作者、@别人）用完要清理：加了就用 `members/delete` 移掉，建的文档用 `explorer/obj/delete` 删掉。
- 单测不要连真实账号：用 monkeypatch 换 `larkx.space.requests.get/post`（见 `tests/test_space.py`）。

## 约束与要求

- **改动前先在 LarkX 里跑通，再让 connector 对接**。connector 依赖的是 git master，所以改完要：LarkX 提交并推 master → 在 connector 里 `uv lock --upgrade-package larkx && uv sync`。
- 只做文字消息及其中的 AT 节点；图片、文件、表情之外的能力不要自行扩展。
- 不要引入 gRPC、消息队列、数据库；本库是纯客户端。
- 保持单租户假设；不要伪造 open_id/union_id/通讯录 user_id。
- 改动要小：新协议能力放进本库，下游只做适配；不要为"以后可能用"预留抽象。
- 不要破坏既有行为和测试；新逻辑（分支、循环、解析）至少留一个测试。
- 内部调试日志按调用方要求**不脱敏**，但日志别进仓库。

## 开发约定

- Python 3.11，`uv` 管理依赖；`uv run pytest -q` 必须全绿。
- 协议接口先在真账号上验证，再写单测（单测用假响应，不打网络）。
- 新增协议能力时：更新 `space.py` 顶部的"已用真实账号验证的能力"清单 + 写清走不通的路。
- 提交信息用简短英文 Conventional Commits。
