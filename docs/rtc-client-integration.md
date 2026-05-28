# RTC 客户端集成指南

> 面向 **Bot 端（Electron）** 与 **Web 端（用户浏览器）** 的开发文档，描述如何基于 `/rtc/*` HTTP API 接入 TRTC（Tencent Real-Time Communication）会话。
>
> 服务端规约的权威来源是 `openspec/specs/rtc-bot-matchmaker/spec.md`（中文，Spec-Driven）。本文档是面向客户端开发者的**操作性**文档，会用代码片段和时序图说明"该做什么、不该做什么"。

---

## 1. 系统总览

```
┌─────────────┐  HTTPS  ┌──────────────────────┐         ┌─────────────────────┐
│   Web 端    │◀──────▶│   k0work-openapi     │  webhook│   TRTC Cloud        │
│ (浏览器)    │         │   (Cloudflare Worker)│◀────────│   (腾讯云 RTC)      │
└──────┬──────┘         └─────────┬────────────┘         └──────────▲──────────┘
       │                          │  Cron 1 min                     │
       │                          │  REST API                       │
       │                          ▼                                 │
       │                       ┌─────┐                              │
       │                       │  D1 │                              │
       │                       └─────┘                              │
       │                                                            │
       │                                                            │
       │  TRTC SDK (WebRTC)                                          │
       └────────────────────────────────────────────────────────────▶│
                                                                    │
┌─────────────┐                                                     │
│  Bot 端     │  TRTC SDK (Electron WebRTC)                         │
│ (Electron)  │─────────────────────────────────────────────────────▶│
└──────┬──────┘                                                     │
       │  HTTPS heartbeat 每 2s                                       │
       └────────────────────────────────────────────────────────────▶
```

**职责划分**：

| 组件 | 职责 |
|---|---|
| **Bot 端** | 持有固定 `botUserId`；每 2s 心跳；当心跳响应里出现 `assignment` 就调 TRTC SDK `enterRoom`；看到 `IDLE` 就 `exitRoom` |
| **Web 端** | 调 `GET /rtc/bots/:botUserId` 看状态；调 `POST .../connect` 抢占 → 拿 `userSig` → TRTC SDK `enterRoom`；通话结束自然退出（无需调用任何 HTTP） |
| **k0work-openapi** | 状态撮合（CAS 抢占、重连软鉴权、签发 userSig、维护 `rtc_bots` 表）；接收 TRTC webhook 推动状态机；Cron 每分钟兜底清理 |
| **TRTC Cloud** | 真实音视频通道；状态权威源；进/出房推 webhook；提供 REST API 供 cron 反查 |

> ⚠️ **状态权威不在本服务**。`rtc_bots` 表仅是 TRTC 状态的**镜像**——所以"我以为我在通话中但服务端说 IDLE"时，**以服务端为准**，立即 `exitRoom`。

---

## 2. 状态机

每个 `botUserId` 在 D1 中有一行，三种状态：

```
                  POST /connect                   webhook 103 (bot enter)
       ┌────────────  CAS  ─────────────▶┐   ┌────────────────────────────────┐
       │                                 │   │                                │
       ▼                                 │   ▼                                │
   ┌───────┐  webhook 102 / cron     ┌────────┐  webhook 104 (bot exit)   ┌──────┐
   │ IDLE  │◀────────────────────────│RESERVED│◀──────────────────────────│ BUSY │
   └───────┘  reservation_deadline   └────────┘                            └──────┘
       ▲      过期 (lazy on heartbeat)    │   ▲                              │
       │                                  │   │ webhook 102 / cron            │
       │                                  │   └──────────────────────────────┘
       │      heartbeat 超时 → row 删除   │
       └──────────────────────────────────┘
```

| 状态 | 含义 | 关键字段 |
|---|---|---|
| **IDLE** | 空闲，可被抢占 | `room_id` / `user_id` / `user_sig` 全 NULL |
| **RESERVED** | 已被某 Web 用户预定，等待 Bot 进房 | 上述字段填好；`reservation_deadline` 倒计时 30s |
| **BUSY** | Bot 已进房，通话进行中 | `call_started_at` 写入；`reservation_deadline` 清空 |

**关键不变量**：
- `RESERVED` 与 `BUSY` 的 `roomId` / `userId` / `displayName` **保持不变**——网络抖动 BUSY→RESERVED→BUSY 来回切，房间号不变。
- `RESERVED` 状态下 `reservation_deadline` 过期会被**惰性回收**（Bot 下次心跳时检测到并 reset），这是为什么 Web 端**不要假设** `connect` 成功后 30s 内一定能进房。
- Bot 心跳超时（默认 30s 无心跳）会被**直接删除行**——下次任何请求看到的是 `404 / 7404`，不是 `IDLE`。

---

## 3. API 规约（公开端点 3 个）

> **Base URL**：`https://<your-worker>.workers.dev` 或自定义域。  
> **Content-Type**：所有 POST 请求需 `application/json`。  
> **时间字段**：epoch milliseconds（13 位整数）。

### 通用响应外形

成功：

```json
{ "success": true, "result": { ... } }
```

失败：

```json
{ "success": false, "errors": [{ "code": 7xxx, "message": "..." }] }
```

`success: false` 时 HTTP 状态与 code 一对一映射（`code=7404 → HTTP 404`，等等）。**永远不要**用 `success` 字段以外的方式判断成败。

### 3.1 `GET /rtc/bots/:botUserId` — 读取 Bot 状态

| 谁调用 | Web 端（也欢迎 Bot 自检） |
|---|---|
| 路径参数 | `botUserId` 必须匹配 `^bot_[a-zA-Z0-9_]{1,32}$` |
| 请求体 | 无 |

**成功响应**（HTTP 200）：

```json
{
  "success": true,
  "result": {
    "botUserId": "bot_dev1",
    "status": "IDLE",
    "lastHeartbeatAt": 1715000000000
  }
}
```

**错误**：

| HTTP | code | 场景 |
|---|---|---|
| 400 | 7400 | `botUserId` 不匹配正则 |
| 404 | 7404 | Bot 从未注册，或心跳超时已被删除 |
| 503 | 7503 | 服务端 secret 缺失 |

### 3.2 `POST /rtc/bots/:botUserId/connect` — 抢占 / 同名重连

| 谁调用 | Web 端 |
|---|---|
| 路径参数 | `botUserId`（同上） |
| 请求体 | `{ "userName": "<显示名>" }` — 1–32 字符（首尾空白会 trim），不允许控制字符（`\x00-\x1F`、`\x7F`） |

**成功响应**（HTTP 200）：

```json
{
  "success": true,
  "result": {
    "status": "RESERVED",
    "sdkAppId": 1400000000,
    "roomId": "room_a1b2c3d4",
    "userId": "user_3e9c1f2b",
    "userSig": "eJw1jktrAj...",
    "expiresAt": 1715003600000,
    "reservationDeadline": 1715000030000
  }
}
```

**字段说明**：

| 字段 | 含义 |
|---|---|
| `status` | `RESERVED`（首次抢占）或 `BUSY`（同名重连，bot 已进房） |
| `sdkAppId` | TRTC SDK App ID，传给 TRTC SDK 的 `enterRoom` |
| `roomId` | 服务端生成的房间号；同名重连返回原值 |
| `userId` | 服务端为本次会话生成的 user 端 TRTC userId（**不是** botUserId） |
| `userSig` | TRTC userSig 签名；TTL 默认 1 小时（`USERSIG_TTL_SEC`） |
| `expiresAt` | userSig 过期时间（epoch ms）。早于这个时间必须重新 `connect` |
| `reservationDeadline` | RESERVED 倒计时截止；BUSY 状态下重连返回时为 `null`（已被 webhook 103 清空） |

**错误**：

| HTTP | code | 场景 | 客户端动作 |
|---|---|---|---|
| 400 | 7400 | `botUserId` / `userName` 校验失败 | 修参数后重试 |
| 404 | 7404 | Bot 不存在（从未上线）或已被惰性删除 | UI 提示"机器人不在线"，不要轮询重试 |
| 409 | 7409 | `BOT_BUSY` — 被另一个用户占用，或同名重连时距 `reservation_deadline` 不足 10s | 退避后再试，或换 bot |
| 410 | 7410 | `BOT_OFFLINE` — 行还在但 30s 没心跳，本次调用同时把行删了 | UI 提示"机器人离线"；不要立刻重试，等 bot 重新上线 |
| 503 | 7503 | 服务端 secret 缺失 | 联系运维 |

**幂等性**：用**同一 userName** 重复 POST 是幂等的——首次成功后再调返回相同 `roomId` / `userId`，`userSig` 在缓存到期时会 just-in-time 重签。同 userName 重连**不会**重置 `reservation_deadline`。换 userName 重连按"另一个用户"对待，会返回 409。

### 3.3 `POST /rtc/bots/:botUserId/heartbeat` — Bot 心跳

| 谁调用 | Bot 端 |
|---|---|
| 路径参数 | `botUserId`（同上） |
| 请求体 | 任意 JSON（服务端不消费）；按惯例发 `{}` |

**成功响应**（HTTP 200）：

```json
{
  "success": true,
  "result": {
    "status": "IDLE",
    "assignment": null,
    "serverTime": 1715000000000
  }
}
```

当 `status === "RESERVED"` 时 `assignment` 非空：

```json
{
  "success": true,
  "result": {
    "status": "RESERVED",
    "assignment": {
      "sdkAppId": 1400000000,
      "roomId": "room_a1b2c3d4",
      "userId": "bot_dev1",
      "userSig": "eJw1jktrAj...",
      "displayName": "小王",
      "reservedAt": 1715000000000
    },
    "serverTime": 1715000010000
  }
}
```

**`assignment` 字段**：
- `userId` 是 **bot 自己的 botUserId**（与 `connect` 响应里的 `userId` 不同——那个是 user 的 user_id）
- `userSig` 是签给 bot 自己的（用 `userId = botUserId` 签名）
- `displayName` 是抢占方的显示名（可在通话 UI 中展示）
- `reservedAt` 是 RESERVED 状态进入时间

**`status === "BUSY"` 时 `assignment === null`**——因为 Bot 已经在房间里了，不需要服务端再下发 enterRoom 指令。Bot 应**自检**自身 TRTC 状态：如果还在房间内继续待着；如果已退出（网络抖动）需立即重连。

**`status === "IDLE"` 时 `assignment === null`**——Bot 应主动 `trtc.exitRoom()`（如果之前在房内）。

**首次心跳 = 注册**：Bot 第一次调 `/heartbeat` 时数据库里没有这一行，服务端会 `INSERT ... ON CONFLICT DO UPDATE`，等价于"自动注册 + 心跳"。所以**没有"注册"端点**。

**错误**：

| HTTP | code | 场景 |
|---|---|---|
| 400 | 7400 | `botUserId` 不匹配正则 |
| 503 | 7503 | 服务端 secret 缺失 |

> Bot 端心跳**不会**返回 404/410——失活检测是其他端点的事。Bot 的心跳即使在"前一刻被删除"的情况也会通过 UPSERT 重新建一行 IDLE。

### 3.4 `POST /rtc/webhook` — TRTC 服务端推送（**不要从客户端调用**）

由 TRTC 云服务器主动 POST，HMAC 签名校验。客户端**永不**调用此端点。从 OpenAPI 文档隐藏（`x-ignore: true`）。详见 `openspec/specs/rtc-bot-matchmaker/spec.md` 的 webhook section。

---

## 4. Bot 端开发指南（Electron）

### 4.1 必备依赖

- TRTC Electron SDK：`trtc-electron-sdk`（[腾讯云文档](https://cloud.tencent.com/document/product/647/35119)）
- HTTP 客户端：`fetch` 或 axios

### 4.2 启动流程

```ts
// 1. 启动时持久化 botUserId（首次随机生成）
import { app } from "electron";
import * as fs from "node:fs/promises";
import * as path from "node:path";

const ID_FILE = path.join(app.getPath("userData"), "bot-id.txt");

async function loadOrCreateBotId(): Promise<string> {
  try {
    return (await fs.readFile(ID_FILE, "utf8")).trim();
  } catch {
    const id = `bot_${[...crypto.getRandomValues(new Uint8Array(4))]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("")}`;
    await fs.writeFile(ID_FILE, id);
    return id;
  }
}

const botUserId = await loadOrCreateBotId();
```

> **关键**：`botUserId` 必须在重启后保持不变。把它当作设备指纹来管理。

### 4.3 主循环：心跳 + 状态响应

```ts
import TRTCCloud, { TRTCAppScene, TRTCRoleType } from "trtc-electron-sdk";

const API_BASE = "https://your-worker.workers.dev";
const HEARTBEAT_INTERVAL = 2000;            // 2 秒
const FAILED_HEARTBEAT_THRESHOLD = 5;       // 5 次连续失败 → 离线 UI

const trtc = TRTCCloud.getTRTCShareInstance();

let inRoom = false;            // bot 自身是否在 TRTC 房内
let currentRoomId: string | null = null;
let consecutiveFailures = 0;

async function heartbeat() {
  try {
    const res = await fetch(`${API_BASE}/rtc/bots/${botUserId}/heartbeat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const json = await res.json();
    if (!json.success) throw new Error(`HB failed: ${json.errors?.[0]?.code}`);
    consecutiveFailures = 0;

    const { status, assignment } = json.result;

    if (status === "RESERVED" && assignment) {
      // 服务端告知有人预定了 + 给了 enterRoom 凭据
      if (!inRoom || currentRoomId !== assignment.roomId) {
        await enterRoom(assignment);
      }
    } else if (status === "BUSY") {
      // 已在通话中。assignment 为 null。自检：如果本地认为自己不在房（网络抖动），
      // 主动重新 enterRoom。重新 enterRoom 需要凭据 → 没有 assignment 怎么办？
      // 答：服务端在 BUSY 时不下发 assignment 是因为它假设 bot 还在房内。
      // 网络抖动重入场景由 webhook 104 → 状态推回 RESERVED → 下次心跳就能拿到 assignment。
      // 所以这里 bot 只需要"待在房里"。如果 SDK 报告自己已离开但状态还是 BUSY，
      // 等下一秒（webhook 104 处理后）会变成 RESERVED 拿到新 assignment。
    } else if (status === "IDLE") {
      // 通话已结束（或被 dismiss）。退房。
      if (inRoom) {
        trtc.exitRoom();
        inRoom = false;
        currentRoomId = null;
      }
    }
  } catch (err) {
    consecutiveFailures++;
    if (consecutiveFailures >= FAILED_HEARTBEAT_THRESHOLD) {
      notifyOffline();
    }
  }
}

async function enterRoom(assignment: {
  sdkAppId: number;
  roomId: string;
  userId: string;
  userSig: string;
  displayName: string;
}) {
  trtc.enterRoom(
    {
      sdkAppId: assignment.sdkAppId,
      userId: assignment.userId,
      userSig: assignment.userSig,
      strRoomId: assignment.roomId,
      role: TRTCRoleType.TRTCRoleAnchor,
    },
    TRTCAppScene.TRTCAppSceneVideoCall,
  );
  inRoom = true;
  currentRoomId = assignment.roomId;
  // displayName 可显示在 bot UI："正在与 ${displayName} 通话"
}

setInterval(heartbeat, HEARTBEAT_INTERVAL);
heartbeat();  // 立即触发首次（= 注册）
```

### 4.4 关键不变量（Bot 端必须遵守）

| 不变量 | 为什么 |
|---|---|
| **每 2s 心跳一次**（允许 ±200ms 抖动） | 服务端 `HEARTBEAT_TIMEOUT_MS=30000`，连错 14 次以上才算超时；2s 给重试留足缓冲。频率过低会被误判离线 |
| **`status === "IDLE"` 必须主动 `exitRoom`** | 服务端不会发"挂断"指令；Bot 自己感知 IDLE 后退房 |
| **`status === "RESERVED"` 必须自检并 `enterRoom`** | `enterRoom` 是幂等的；服务端不会显式触发 |
| **`status === "BUSY"` 不要做任何动作** | 待在房里。一旦 webhook 把状态推回 RESERVED，下次心跳会带 assignment |
| **不要存 userSig** | 心跳响应每次都给最新的（缓存或重签）；不要复用过期的 |

### 4.5 异常恢复

| 场景 | Bot 行为 |
|---|---|
| Bot 进程崩溃于 IDLE | 自动重启后重新心跳 → 服务端会重新 INSERT 一行 IDLE |
| Bot 进程崩溃于 BUSY | 心跳停 → 服务端 30s 后删行；TRTC 自身 keep-alive 30~90s 把 bot 踢出房（webhook 104）；通话由 cron 兜底清理 |
| Bot 网络抖动 | 心跳失败计数 ≥5 → UI 提示离线；恢复后自动重新走主循环 |
| TRTC SDK `enterRoom` 失败 | 等下次心跳重试（assignment 还会下发，因为状态仍 RESERVED） |
| 拿到的 userSig 已过期（极端时序） | TRTC SDK 会报错；下次心跳服务端会重签后发新 sig |

---

## 5. Web 端开发指南（浏览器）

### 5.1 必备依赖

- TRTC Web SDK：[`trtc-sdk-v5`](https://web.sdk.qcloud.com/trtc/webrtc/v5/doc/zh-cn/) 或更新版本
- HTTP 客户端：`fetch`

### 5.2 入场流程

```ts
import TRTC from "trtc-sdk-v5";

const API_BASE = "https://your-worker.workers.dev";

async function joinBot(botUserId: string, userName: string) {
  // 1. 抢占
  const res = await fetch(`${API_BASE}/rtc/bots/${botUserId}/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ userName }),
  });
  const json = await res.json();

  if (!json.success) {
    handleConnectError(json.errors[0]);
    return;
  }

  const { sdkAppId, roomId, userId, userSig, expiresAt, reservationDeadline } =
    json.result;

  // 2. 进入 TRTC 房间
  const trtc = TRTC.create();
  await trtc.enterRoom({
    strRoomId: roomId,         // 字符串 roomId 必须用 strRoomId 字段
    sdkAppId,
    userId,
    userSig,
    scene: "rtc",              // 通话场景
  });

  // 3. 启动音视频采集
  await trtc.startLocalAudio();
  await trtc.startLocalVideo({ view: "local-video" });

  // 4. 订阅远端（bot）流
  trtc.on(TRTC.EVENT.REMOTE_VIDEO_AVAILABLE, ({ userId, streamType }) => {
    trtc.startRemoteVideo({ userId, streamType, view: "remote-video" });
  });

  return { trtc, roomId, expiresAt, reservationDeadline };
}

function handleConnectError(err: { code: number; message: string }) {
  switch (err.code) {
    case 7404:
      alert("机器人不在线，请稍后再试");
      break;
    case 7409:
      alert("机器人正忙，请换一个或稍后重试");
      break;
    case 7410:
      alert("机器人离线，请等待重新上线");
      break;
    case 7503:
      alert("服务端配置异常，请联系运维");
      break;
    default:
      alert(`连接失败：${err.message}`);
  }
}
```

### 5.3 通话中

通话期间 Web 端**不需要**调任何 HTTP 接口——TRTC SDK 自己维持长连。

可选：每隔 5–10s 调 `GET /rtc/bots/:botUserId` 显示状态指示灯（"已连接 / 重连中 / 已断开"），但这只是 UI 增强，**不是**必需的。

### 5.4 用户主动结束通话

```ts
async function leaveCall(trtc: TRTC) {
  await trtc.exitRoom();
  trtc.destroy();
}
```

> Web 端**不需要**调任何 HTTP 接口。Bot 在用户离开后会通过下面任一路径回到 IDLE：
> - 用户调用 `exitRoom` → TRTC webhook 104 (UserId !== botUserId) → 仅更新 `last_event_time` → cron 1 分钟内反查 `MemberCount=1` → DismissRoom → 行 reset IDLE
> - 用户直接关页 → TRTC keep-alive 30~90s 后踢出 → 同上

### 5.5 重连（同名）

如果用户因为网络抖动断开 TRTC，可以重新调一次 `connect`，**用同一个 userName**：

```ts
async function reconnect(botUserId: string, userName: string) {
  // 重新拿 userSig（旧的可能还有效，但保险起见拉新的）
  const json = await (await fetch(`${API_BASE}/rtc/bots/${botUserId}/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ userName }),
  })).json();
  if (!json.success) return handleConnectError(json.errors[0]);

  // 服务端会返回**同一个 roomId**（这是同名重连软鉴权的关键）
  const trtc = TRTC.create();
  await trtc.enterRoom({
    strRoomId: json.result.roomId,
    sdkAppId: json.result.sdkAppId,
    userId: json.result.userId,
    userSig: json.result.userSig,
    scene: "rtc",
  });
  return trtc;
}
```

> ⚠️ **userName 必须与首次抢占完全相同**，否则会被识别为"另一个用户"，返回 409。可在 sessionStorage 里持久化首次的 userName。

### 5.6 关键不变量（Web 端必须遵守）

| 不变量 | 为什么 |
|---|---|
| **不要轮询 `/connect`** | 抢占失败（409 / 410）时退避或换 bot；不要立即重试，会拒绝服务级别地阻塞别人 |
| **重连必须用同 userName** | 服务端的"同名重连软鉴权"是判定是否是同一个用户的唯一依据 |
| **`reservationDeadline` 是参考值** | 服务端 30s 内 bot 没进房会被惰性回收，UI 上可显示倒计时但不要做硬阻断（应让 TRTC SDK 报错引导重试） |
| **userSig 可能在通话中过期** | TTL 默认 1 小时；超长通话需处理 SDK 的 sig 过期事件，重新调 `connect` 拿新 sig |

---

## 6. 错误码完整列表

| HTTP | code | 业务含义 | Web/Bot 处理建议 |
|---|---|---|---|
| 400 | 7400 | 入参格式错（regex / 长度 / 控制字符） | 检查并修正请求 |
| 401 | 7401 | webhook 验签失败（仅 webhook 路径） | 客户端不会遇到 |
| 404 | 7404 | Bot 未注册或已被回收 | UI 提示"不在线"；不要重试 |
| 409 | 7409 | `BOT_BUSY` 或 reconnect 窗口太短 | 退避 5–10s 重试，或换 bot |
| 410 | 7410 | `BOT_OFFLINE`（行存在但心跳超时） | UI 提示"离线"，不要重试 |
| 429 | 7429 | 上游限流（cron 路径，客户端少见） | 退避重试 |
| 500 | 7000 | 服务端内部错误兜底 | 上报日志后重试 |
| 502 | 7502 | TRTC REST API 不可用 | 退避重试 |
| 503 | 7503 | 服务端 secret 缺失 | 联系运维；不要重试 |

---

## 7. CORS

`/rtc/bots/*` 端点（Web 端调用的三个）走 `hono/cors` 白名单，origin 来自服务端环境变量 `CORS_ORIGIN`（逗号分隔）。允许的方法：`GET`、`POST`、`OPTIONS`。允许的 header：`Content-Type`。

如果你的前端域名不在白名单里，浏览器会预检失败。联系运维在 Cloudflare Dashboard 把你的 origin 加到 `CORS_ORIGIN`。

`/rtc/webhook` 不挂 CORS（TRTC 服务端调用，不走浏览器）。

---

## 8. 故障恢复矩阵（用于 QA / 集成测试）

| 故障场景 | 谁先感知 | 恢复路径 | 收敛时间 |
|---|---|---|---|
| Bot 进程崩溃于 IDLE | 服务端心跳超时 | 行被惰性删除（其他端点首次访问时） | ≤ 30s |
| Bot 进程崩溃于 BUSY | TRTC keep-alive | webhook 104 → RESERVED；行被删除 → cron DismissRoom | 30~90s + 1 分钟 cron |
| 用户关页于 RESERVED | Bot 心跳的惰性清理 | `now > reservation_deadline` → 行 reset IDLE | ≤ 30s |
| 用户关页于 BUSY | TRTC keep-alive | webhook 104 → cron 反查 `MemberCount=1` → DismissRoom | 30~90s + 1 分钟 cron |
| Bot 网络抖动 30s 内重连 | TRTC | webhook 104 → RESERVED；30s 内 bot 心跳 → 拿到 assignment → enterRoom → webhook 103 → BUSY | 即时 |
| Bot 离线 >30s | 心跳超时 | 行删除；cron 反查 `MemberCount<=1` → DismissRoom | 30s + 1 分钟 cron |
| User 网络抖动 30s 内重连 | 用户主动 reconnect | `POST /connect` 同名 → 返回原 roomId + 新 userSig → 重 enterRoom → webhook 103（不变状态） | 即时 |
| Worker 部署 / 回滚 | 无影响 | D1 状态保留；正在通话由 TRTC 自身维持 | 0 |

---

## 9. FAQ

**Q1：抢占之后多久必须 enterRoom？**  
A：服务端 RESERVED 倒计时是 `RECONNECT_WINDOW_MS`（默认 30s）。超过就被 Bot 心跳惰性回收。但 Bot 进房本身只要在 30s 内就行——Bot 心跳是 2s 一次，所以正常情况下 ≤ 2s 就会进房并切到 BUSY。

**Q2：怎么知道 Bot 进房成功？**  
A：Web 端等 TRTC SDK 的 `REMOTE_USER_ENTER` 事件（参数里的 `userId === botUserId`）。或者轮询 `GET /rtc/bots/:botUserId`，看到 `status === "BUSY"` 也行（但 SDK 事件更快）。

**Q3：同时多个 Web 用户抢同一个 Bot 会发生什么？**  
A：CAS 抢占只会有一个赢家，其他全部 409。**不要在前端写"再试一次"循环**——失败就显示"机器人忙"，让用户决定下一步。

**Q4：通话超过 1 小时会怎样？**  
A：默认 `USERSIG_TTL_SEC=3600`，userSig 过期会被 TRTC SDK 报错。处理 sig-expired 事件 → 重新调 `connect` 拿新 sig → SDK 自动续约（具体 API 参考 TRTC SDK 文档）。或者运维侧把 `USERSIG_TTL_SEC` 调大。

**Q5：Web 端怎么"挂断"通话？**  
A：调 `trtc.exitRoom()`，**不需要**任何 HTTP。Bot 会在 cron 1 分钟内（或 webhook 102/104 触发时）回到 IDLE。

**Q6：如何切换到另一个 Bot？**  
A：Web 端：(1) `await trtc.exitRoom()` 退出当前房；(2) 用新的 `botUserId` 走 `connect` → `enterRoom` 流程。原来的 bot 会按上述故障恢复机制自动回到 IDLE。

**Q7：同一个浏览器 tab 能同时连两个 Bot 吗？**  
A：技术上 TRTC SDK 可以创建多个实例，但**业务上不推荐**。每个 Bot 一个 TRTC 房间，多个房间同时音视频会显著增加网络/CPU 负担。

**Q8：webhook 是必需的吗？**  
A：是。没有 webhook 服务端不知道 bot 实际何时进/出房，状态机会卡在 RESERVED。运维需要在 TRTC 控制台 → 回调配置里把 webhook URL 指向 `https://<your-worker>/rtc/webhook`，并设置 HMAC key（同 `TRTC_WEBHOOK_KEY`）。

---

## 10. 集成 checklist

### Bot 端

- [ ] 持久化 `botUserId`，重启后保持不变
- [ ] 每 2s 调一次 `/heartbeat`
- [ ] `status === "RESERVED"` && `assignment != null`：调 `trtc.enterRoom(assignment)`
- [ ] `status === "IDLE"`：调 `trtc.exitRoom()` 释放本地资源
- [ ] `status === "BUSY"`：什么都不做（已在房里）
- [ ] 心跳连错 5 次：UI 提示离线，不要 spam 重试
- [ ] 不缓存 userSig（每次心跳都用最新的）

### Web 端

- [ ] 把目标 `botUserId` 通过 URL / props 传入页面
- [ ] 调 `connect` 时把 `userName` 持久化到 sessionStorage（重连用）
- [ ] 处理 7404 / 7409 / 7410 → 三种不同 UI 提示
- [ ] `enterRoom` 用 `connect` 返回的 `sdkAppId` / `roomId` / `userId` / `userSig`
- [ ] 处理 SDK 的 sig-expired 事件 → 重新调 `connect`
- [ ] 用户离开页面：`exitRoom` + `destroy`，不需要 HTTP
- [ ] 确认前端域名已加入服务端 `CORS_ORIGIN`

---

## 附：服务端环境变量（运维参考）

| 变量 | 类型 | 默认 | 客户端是否影响 |
|---|---|---|---|
| `TRTC_SDK_APP_ID` | Dashboard plaintext | — | Web/Bot 在 SDK 调用时使用此值（来自 `connect` / heartbeat 响应） |
| `TRTC_SDK_SECRET_KEY` | Secret | — | 只服务端用（签 userSig） |
| `TRTC_WEBHOOK_KEY` | Dashboard plaintext | — | 只服务端用（验 webhook） |
| `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY` | plaintext / Secret | — | 只服务端用（cron 调 TRTC REST） |
| `CORS_ORIGIN` | Dashboard plaintext | — | Web 端 origin 必须在此白名单 |
| `HEARTBEAT_TIMEOUT_MS` | wrangler vars | 30000 | Bot 心跳超时阈值，影响 4.4 中的"≥5 次失败"判定 |
| `IDLE_ROOM_TIMEOUT_MS` | wrangler vars | 30000 | cron 反查窗口，影响故障恢复时间 |
| `RECONNECT_WINDOW_MS` | wrangler vars | 30000 | RESERVED 倒计时 = `reservation_deadline` 中的这个数字 |
| `USERSIG_TTL_SEC` | wrangler vars | 3600 | userSig 有效期；通话超过这个时长需重连刷 sig |

详见 `AGENTS.md` 的 "Env / Bindings" 段。
