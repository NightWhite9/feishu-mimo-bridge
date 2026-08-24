---
name: feishu-mimo-bridge
description: 飞书↔mimo 桥接（Feishu chatbot bridge to mimo agent）。把飞书机器人变成 mimo agent 的聊天入口：用户私聊或群里@机器人，桥接脚本监听 im.message.receive_v1 事件，调用 mimo run headless 处理并回复（支持多轮续聊）。当用户要把某个 mimo agent 接入飞书聊天、给机器人配 AI 对话能力、或在飞书上和智能伙伴聊天时使用。触发词：飞书桥接、mimo接飞书、飞书智能助手、让机器人和mimo对话、接入飞书、bot接入mimo。
license: MIT
compatibility: Windows + Python 3.11 + lark-cli + mimo(@mimo-ai/cli)
metadata:
  version: 1.0.0
  author: mayifan
  created: 2026-08-24
  verified: true
  verification_date: 2026-08-24
---

# 飞书 ↔ mimo 桥接

把飞书机器人变成 mimo agent 的聊天入口。架构：

```
飞书用户 (私聊 / 群里@机器人)
   │  im.message.receive_v1 事件 (WebSocket, --as bot)
   ▼
bridge_feishu_mimo.py  (常驻进程, 白名单+去重+崩溃自愈)
   │  mimo run --format json --yolo --session <sid> --dir <ws> <内容>
   ▼
mimo agent  (headless, 多轮记忆按 chat 隔离)
   │  im +messages-reply 引用回复 (分块 ≤2000字符/条)
   ▼
飞书用户
```

## 快速开始

1. 复制 `scripts/bridge_feishu_mimo.py` 到目标机器/工作目录
2. 改文件顶部 `CONFIG` 区（唯一必改的地方）：
   - `BOT_APP_ID` — 你的飞书机器人 app_id（`cli_` 前缀）
   - `ALLOWED_USERS` — 允许对话的用户 open_id 集合（**mimo 跑 --yolo 全权限，必须白名单**）
   - `NODE` / `LARK_RUNJS` / `MIMO_BIN` — 本机绝对路径（见下）
   - `WORKSPACE` — mimo 工作目录
3. 组件级验证（任一步失败别往下走）：
   - `node <LARK_RUNJS> event consume im.message.receive_v1 --as bot --max-events 1` — 应出现 `ready event_key` 后收到自己发的测试消息
   - `node <MIMO_BIN> run --format json --yolo --dir <ws> "你好"` — 应输出 JSONL 且 exit 0
4. 启动桥接：`pythonw scripts/bridge_feishu_mimo.py`（无窗口）或前台 `python` 调试
5. 飞书上给机器人发消息实测 → 看 `bridge.log` 出现 `>> chat=...` 和 `<< replied=...`

## 必备本机路径（绕开 PATH 坑）

计划任务/无控制台环境下 PATH 不完整，所有子进程调用必须用绝对路径：

| 组件 | 路径公式 |
|---|---|
| lark-cli | `C:\Program Files\nodejs\node.exe` + `E:\npm-global\node_modules\@larksuite\cli\scripts\run.js`（npm 全局目录按实际） |
| mimo | `C:\Program Files\nodejs\node.exe` + `C:\Program Files\nodejs\node_modules\@mimo-ai\cli\bin\mimo` |

> 用 node.exe + JS 入口直接调，**不要**用 `.cmd`/`.bat` 包装（PATH 依赖 + 控制台窗口）。

## 内置能力

- **白名单**：仅 `ALLOWED_USERS` 响应；群聊必须 @ 机器人（p2p 不用）；`sender_type != user` 直接忽略（防 bot 死循环）
- **多轮续聊**：chat_id → mimo session 映射，持久化 `sessions.json`（每 chat 独立会话）
- **message_id 幂等去重**：`seen.json` 最近 100 条，防飞书重投重复执行
- **/reset 命令**：飞书里发 `/reset` 清空该 chat 的 mimo 记忆
- **长回复分块**：≤2000 字符/条（飞书 text 安全上限），第一块引用原消息
- **typing 指示**：处理中在消息上加 `Typing` 表情 reaction，完成移除（替代"[处理中]"刷消息）
- **崩溃自愈**：consume 断连/退出 3 秒自动重启；外层 while True 守护
- **单实例锁**：msvcrt 文件锁，重复启动自动退出

## 常驻方式（Windows）

计划任务（推荐，登录触发）：

```powershell
$action   = New-ScheduledTaskAction -Execute "E:\Python311\pythonw.exe" -Argument "<脚本绝对路径>" -WorkingDirectory "<工作目录>"
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "<用户名>"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "mimo飞书桥接" -Action $action -Trigger $trigger -Settings $settings -Force
```

`ExecutionTimeLimit = 0`（无限时长防杀）、`RestartOnFailure`（失败重启）、`IgnoreNew`（配合脚本内单实例锁）。

## 验证

- 端到端：飞书私聊发消息 → 消息上出现 Typing 表情 → 回复引用原消息 → Typing 消失
- 日志：`bridge.log`（UTF-8；PowerShell `Get-Content` 读会乱码，用 Python `open(..., encoding='utf-8')`）
- 群聊不响应：非白名单用户 @ 机器人 → 日志 `IGNORE sender=... (不在白名单)`，无任何回复

## 调试命令

```powershell
# 看日志尾部（UTF-8）
python -c "import io; print(''.join(io.open(r'<ws>\bridge.log', encoding='utf-8').readlines()[-10:]))"
# 停/启
Stop-ScheduledTask -TaskName "mimo飞书桥接"
Start-ScheduledTask -TaskName "mimo飞书桥接"
```

## 已知限制

- 只处理 text 消息；图片/文件/语音等非文本消息忽略（mimo headless 无媒体输入）
- 无流式输出（mimo 输出是批处理 JSONL，无增量事件）
- 回复超 2000 字符分多条发送，无拼接（每条都是独立消息）
