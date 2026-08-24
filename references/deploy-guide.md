# 飞书 ↔ mimo 桥接 部署指南与踩坑记录

> 2026-08-24 实测成功（本机）。本文件沉淀部署顺序、验证方法、全部踩坑。

## 0. 前置依赖（一次性）

| 组件 | 说明 | 检查 |
|---|---|---|
| lark-cli | npm 全局安装，App 绑定飞书机器人 | `lark-cli auth status --json` → `identities.bot.status == ready` |
| @mimo-ai/cli | npm 全局，mimo 命令行 | `node <MIMO_BIN> --version` |
| Python 3.11 | 桥接脚本运行环境 | `E:\Python311\python.exe`（按实际） |

飞书机器人需要 scope：`im:message.p2p_msg:readonly`（收私聊事件）、`im:message.group_msg`（收群消息）、`im:message:send_as_bot`（发消息）、reactions 相关（表情）。授权时 `--domain all` 一次给全。

## 1. 部署顺序（严格按序）

1. **复制脚本 + 改 CONFIG**（`SKILL.md` 快速开始第 1-2 步）
2. **组件级验证**（见 §2）—— 任一步失败别往下走
3. **启动桥接**：前台 `python` 跑一次，确认 `consume ready, listening...`
4. **端到端验证**：飞书私聊发消息 → 看日志 `>>` / `<<` 成对出现
5. **注册计划任务**（§3）常驻，然后停手动实例

## 2. 组件级验证

### 2.1 事件接收（最关键，先验证这个）

```powershell
node "C:\Program Files\nodejs\node.exe" ... # 或直接:
node "E:\npm-global\node_modules\@larksuite\cli\scripts\run.js" event consume im.message.receive_v1 --as bot --max-events 1
```

- stderr 出现 `[event] ready event_key=im.message.receive_v1` = WebSocket 已连
- 此时飞书给机器人发条消息，stdout 应吐出 1 条 NDJSON 事件
- `--max-events 1` 收到一条后自动退出（验证用）；常驻用 `--max-events 0`

### 2.2 mimo headless

```powershell
node "C:\Program Files\nodejs\node_modules\@mimo-ai\cli\bin\mimo" run --format json --yolo --dir <ws> "你好"
```

- stdout 是 JSONL（`{"type":"text","part":{"text":"..."}}`），exit 0 = 成功
- 完成信号 = 进程 exit code 0（流内没有"完成"事件）

### 2.3 发送消息

```powershell
node <run.js> im +messages-send --as bot --chat-id oc_xxx --msg-type text --content '{"text":"hi"}'
```

## 3. 计划任务常驻（Windows）

```powershell
$action   = New-ScheduledTaskAction -Execute "E:\Python311\pythonw.exe" -Argument "<脚本>" -WorkingDirectory "<ws>"
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "<用户名>"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "mimo飞书桥接" -Action $action -Trigger $trigger -Settings $settings -Force
```

关键点：
- **pythonw.exe**（无控制台）而非 python.exe —— 否则登录时弹黑窗
- `ExecutionTimeLimit (New-TimeSpan -Hours 0)` = 无限时长（默认 3 天会杀常驻进程）
- `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries` —— 笔记本电池供电默认禁止任务
- `RestartCount 3 + RestartInterval 1分钟` —— 失败自动重启
- `-Force` 覆盖重名任务
- 用 `Export-ScheduledTask` 看 XML 确认设置真的生效（`Get-ScheduledTask.Settings` 某些字段显示为空）

## 4. 排障速查

| 症状 | 原因 | 修复 |
|---|---|---|
| consume 启动即退，日志无 ready | **pythonw 下 stdin 立即 EOF**（无界 consume 依赖 stdin 不 EOF） | `subprocess.Popen(..., stdin=subprocess.PIPE)` |
| 回复时命令窗口闪烁 | 子进程无 CREATE_NO_WINDOW，Windows 弹控制台 | 所有 subprocess 加 `creationflags=subprocess.CREATE_NO_WINDOW` |
| 发送报 `230001 invalid message content` | `--content` 传了完整消息对象 | `--content` 只传 `{"text":"..."}` + `--msg-type text` |
| 报错 `230002` | 机器人不在目标群 | 用户身份拉机器人入群（`im chat.members create --member-id-type app_id`） |
| 乱码日志 | PowerShell Get-Content 按 GBK 读 UTF-8 | Python `open(path, encoding='utf-8')` |
| PowerShell 管道破坏 JSON | `lark-cli --json \| python` 管道被改写 | Python subprocess 直接调 node.exe+run.js |
| 计划任务 LastTaskResult 2147942402 | 任务环境 PATH 不完整，裸命令找不到 | 全部绝对路径 |
| 重复消息 | 飞书可能重投 | message_id 去重（脚本内置 seen.json） |
| 别人 @ 机器人也响应？ | 不该发生——白名单检查在群聊判断**之前** | 确认 ALLOWED_USERS 正确；日志看 `IGNORE sender=...` |

## 5. 关键 API 备忘

| 能力 | 命令 |
|---|---|
| 收事件 | `event consume im.message.receive_v1 --as bot`（--max-events 0 无界） |
| 发文本 | `im +messages-send --as bot --chat-id oc_xxx --msg-type text --content '{"text":"..."}'` |
| 引用回复 | `im +messages-reply --as bot --message-id om_xxx --msg-type text --content '{"text":"..."}'` |
| 加表情 | `im reactions create --as bot --message-id om_xxx --data '{"reaction_type":{"emoji_type":"Typing"}}'` |
| 删表情 | `im reactions delete --as bot --params '{"message_id":"om_xxx","reaction_id":"<从 create 响应拿>"}'` |
| 查 p2p 会话 | `im +chat-list --types=p2p --as user` |
| 查自己 open_id | `im +chat-list --types=p2p --as user`（找和机器人的会话 sender）或通讯录查询 |

要点：
- reaction 的 emoji_type 是**大小写敏感枚举**（`Typing` 是官方的"输入中"表情；删除需要 create 返回的 reaction_id，所以 create 响应要留存）
- 发消息 `--content` 传**纯 content JSON**，不是完整消息对象
- 引用回复 = 线程回复，群里上下文清晰；第一块回复引用原消息，后续分块普通发送

## 6. 本次落地清单（2026-08-24，可作验收对照）

- [x] 白名单（仅本人 open_id）+ 群聊@才响应 + sender_type 过滤
- [x] 每 chat 独立 mimo session，sessions.json 持久化（多轮续聊实测生效）
- [x] message_id 幂等去重（seen.json）
- [x] `/reset` 命令
- [x] 2000 字符分块 + 引用回复
- [x] Typing reaction 指示（完成移除）
- [x] 崩溃自愈（3s 重启）+ 单实例锁
- [x] 计划任务 AtLogOn 常驻 + 电池允许 + 无限时长 + 失败重启
- [x] pythonw 无窗口 + CREATE_NO_WINDOW（无闪烁）
- [x] 群聊别人 @ 不响应（白名单拦截，日志可证）
