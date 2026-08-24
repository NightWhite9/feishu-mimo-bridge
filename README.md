# feishu-mimo-bridge

把飞书机器人变成 mimo agent 的聊天入口：飞书私聊 / 群里 @机器人 → 桥接脚本监听 `im.message.receive_v1` 事件 → 调用 `mimo run`（headless）处理并回复，支持多轮续聊。

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

## 功能特性

- **白名单准入**：仅配置的 open_id 可对话；群聊必须 @机器人；机器人/系统消息自动忽略（防死循环）
- **多轮续聊**：chat_id → mimo session 映射，持久化 `sessions.json`，每 chat 独立会话
- **message_id 幂等去重**：`seen.json` 记录最近 100 条，防飞书重投重复执行
- **`/reset` 命令**：飞书里发送即清空该会话的 mimo 记忆
- **长回复分块**：≤2000 字符/条，第一块引用原消息，不截断
- **typing 指示**：处理中在消息上加 `Typing` 表情 reaction，完成自动移除
- **崩溃自愈**：consume 断连/退出 3 秒自动重启，外层守护循环
- **单实例锁**：msvcrt 文件锁，重复启动自动退出
- **无窗口常驻**：pythonw + `CREATE_NO_WINDOW`，无命令窗口闪烁

## 快速开始

1. 复制 `scripts/bridge_feishu_mimo.py` 到目标机器，改文件顶部 `CONFIG` 区（唯一必改处）：
   - `BOT_APP_ID` — 飞书机器人 app_id（`cli_` 前缀）
   - `ALLOWED_USERS` — 允许对话的用户 open_id 集合（**mimo 跑 --yolo 全权限，必须白名单**）
   - `NODE` / `LARK_RUNJS` / `MIMO_BIN` — 本机绝对路径（绕开 PATH 坑）
2. 组件级验证（任一步失败别往下走）：
   - `node <LARK_RUNJS> event consume im.message.receive_v1 --as bot --max-events 1` → 出现 `ready event_key` 并收到自己发的测试消息
   - `node <MIMO_BIN> run --format json --yolo --dir <ws> "你好"` → 输出 JSONL 且 exit 0
3. 启动桥接：`pythonw bridge_feishu_mimo.py`（无窗口）或 `python`（前台调试）
4. 飞书给机器人发消息实测，看 `bridge.log` 出现 `>> chat=...` 和 `<< replied=...`

## 常驻（Windows 计划任务）

```powershell
$action   = New-ScheduledTaskAction -Execute "E:\Python311\pythonw.exe" -Argument "<脚本绝对路径>" -WorkingDirectory "<工作目录>"
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "<用户名>"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "mimo飞书桥接" -Action $action -Trigger $trigger -Settings $settings -Force
```

要点：`pythonw.exe`（无控制台）、`ExecutionTimeLimit = 0`（无限时长防杀）、电池允许、失败重启。

## 目录结构

```
├── SKILL.md                        # agent skill 入口（描述/触发词/给 agent 的指令）
├── scripts/
│   └── bridge_feishu_mimo.py       # 桥接脚本（参数化，CONFIG 区集中标注复用必改）
└── references/
    └── deploy-guide.md             # 部署顺序 + 组件级验证 + 排障速查 + API 备忘
```

## 依赖

- Windows + Python 3.11
- [lark-cli](https://www.npmjs.com/package/@larksuite/cli)（npm 全局，绑定飞书机器人，需 `im:message.p2p_msg:readonly` 等 scope）
- `@mimo-ai/cli`（npm 全局）

## 已知限制

- 只处理 text 消息；图片/文件/语音等非文本消息忽略（mimo headless 无媒体输入）
- 无流式输出（mimo 输出是批处理 JSONL，无增量事件）
- 超长回复分多条发送，无拼接（每条都是独立消息）

## License

MIT
