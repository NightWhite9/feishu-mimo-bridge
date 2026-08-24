# -*- coding: utf-8 -*-
"""
飞书 -> mimo 桥接服务（常驻进程, 带自愈）

流程:
  1. 监听 im.message.receive_v1 事件 (bot 身份, WebSocket 长连)
  2. 过滤: 仅处理白名单用户 (sender open_id) 的消息;
     p2p 私聊全部处理, 群聊仅处理 @了机器人的消息
  3. 调用 mimo run --format json --yolo --session <sid> --dir <workspace> <内容>
  4. 提取回复文本, 引用原消息分块发回
  5. 多轮续聊: chat_id -> mimo session id 映射 (持久化到 sessions.json)

改进 (参照 OpenClaw feishu 通道):
  - 崩溃自愈: consume 子进程异常退出/断连后自动重启
  - message_id 幂等去重: 防飞书重试/抖动导致重复执行
  - /reset 命令: 清空该 chat 的 mimo session (新对话)
  - 长回复分块: CHUNK_LIMIT 字符/条多块发送, 替代截断
  - typing 反应: 消息上加 Typing 表情, 处理完移除 (替代 [处理中] 文本)
  - 回复引用: +messages-reply 引用原消息, 群里上下文清晰
  - 单实例锁: 重复启动直接退出 (配合计划任务安全)

用法:  python bridge_feishu_mimo.py   (前台调试)
       pythonw bridge_feishu_mimo.py  (无窗口常驻, 配合计划任务)
日志:  bridge.log (同目录)

=== 复用说明 (把本脚本复制到其他 mimo agent 时必改 CONFIG 区) ===
  1. BOT_APP_ID: 你的飞书机器人 app_id (cli_ 前缀)
  2. ALLOWED_USERS: 允许对话的用户 open_id 集合
     (mimo 跑 --yolo 全权限, 必须白名单! 查自己 open_id:
      lark-cli im +chat-list --types=p2p --as user)
  3. NODE / LARK_RUNJS / MIMO_BIN: 本机绝对路径 (npm 全局目录按实际安装位置)
  4. WORKSPACE: mimo 工作目录 (放 agent 配置/记忆的地方)
  依赖: lark-cli (npm 全局) + @mimo-ai/cli (npm 全局)
========================================================================
"""

import json
import msvcrt
import os
import subprocess
import sys
import time

# ===================== CONFIG (复用前必改) =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = BASE_DIR  # mimo 工作目录
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
SEEN_FILE = os.path.join(BASE_DIR, "seen.json")
LOG_FILE = os.path.join(BASE_DIR, "bridge.log")
LOCK_FILE = os.path.join(BASE_DIR, "bridge.lock")

NODE = r"C:\Program Files\nodejs\node.exe"
LARK_RUNJS = r"E:\npm-global\node_modules\@larksuite\cli\scripts\run.js"
MIMO_BIN = r"C:\Program Files\nodejs\node_modules\@mimo-ai\cli\bin\mimo"

BOT_APP_ID = "cli_xxxxxxxxxxxxxxxx"  # 你的飞书机器人 app_id (飞书开放平台获取)
# 白名单: 只响应这些 open_id 的消息 (安全: --yolo 全权限, 不能对所有人开放)
ALLOWED_USERS = {"ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}  # 你的 open_id (查法见 deploy-guide.md)
# ===============================================================

MIMO_TIMEOUT = 300  # 秒, 单次 mimo 任务上限
CHUNK_LIMIT = 2000  # 飞书单条文本安全上限, 超出分块发送
SEEN_MAX = 100  # 幂等去重保留的 message_id 条数
RESTART_DELAY = 3  # 秒, consume 崩溃后重启间隔
TYPING_EMOJI = "Typing"


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw 无控制台时静默


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def acquire_lock():
    """单实例锁: 已运行则返回 None, 否则返回 fd (退出时自动释放)"""
    fh = open(LOCK_FILE, "a+")
    fh.seek(0)
    if fh.read() == "":
        fh.write("\x00")
        fh.flush()
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except OSError:
        fh.close()
        return None


def lark(args, timeout=60):
    """调用 lark-cli (node.exe + run.js, 绕开 .cmd PATH 问题)"""
    return subprocess.run(
        [NODE, LARK_RUNJS] + args,
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,  # 防弹出控制台窗口
    )


def react(message_id):
    """消息上加 Typing 表情; 返回 reaction_id 或 None (失败静默)"""
    try:
        r = lark(["im", "reactions", "create", "--as", "bot", "--message-id", message_id,
                  "--data", json.dumps({"reaction_type": {"emoji_type": TYPING_EMOJI}})])
        if r.returncode != 0:
            return None
        return json.loads(r.stdout).get("data", {}).get("reaction_id")
    except Exception:
        return None


def unreact(message_id, reaction_id):
    """移除 Typing 表情; 失败静默"""
    if not reaction_id:
        return
    try:
        lark(["im", "reactions", "delete", "--as", "bot",
              "--params", json.dumps({"message_id": message_id, "reaction_id": reaction_id})],
             timeout=30)
    except Exception:
        pass


def send_text(chat_id, text):
    r = lark(["im", "+messages-send", "--as", "bot", "--chat-id", chat_id,
              "--msg-type", "text", "--content", json.dumps({"text": text}, ensure_ascii=False)])
    return r.returncode == 0


def reply_text(message_id, text):
    """引用原消息回复"""
    r = lark(["im", "+messages-reply", "--as", "bot", "--message-id", message_id,
              "--msg-type", "text", "--content", json.dumps({"text": text}, ensure_ascii=False)])
    return r.returncode == 0


def send_chunks(chat_id, message_id, text):
    """分块发送; 第一块引用原消息, 后续块普通发送"""
    chunks = [text[i:i + CHUNK_LIMIT] for i in range(0, len(text), CHUNK_LIMIT)]
    if not chunks:
        chunks = ["(空回复)"]
    if message_id:
        ok = reply_text(message_id, chunks[0])
        if not ok:
            send_text(chat_id, chunks[0])
    else:
        ok = send_text(chat_id, chunks[0])
    for c in chunks[1:]:
        send_text(chat_id, c)
    return ok


def run_mimo(content, session_id):
    """调用 mimo headless, 返回 (回复文本, session_id, 错误)"""
    cmd = [NODE, MIMO_BIN, "run", "--format", "json", "--yolo", "--dir", WORKSPACE]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(content)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=MIMO_TIMEOUT,
                           creationflags=subprocess.CREATE_NO_WINDOW)  # 防弹出控制台窗口
    except subprocess.TimeoutExpired:
        return None, session_id, "mimo 处理超时(>%ds)" % MIMO_TIMEOUT

    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[-500:]
        return None, session_id, "mimo 出错(code=%s): %s" % (r.returncode, err)

    # 解析 JSONL, 取最后一个 text 事件作为最终回复
    reply, new_sid = None, session_id
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "text":
            reply = ev.get("part", {}).get("text", "")
        if ev.get("sessionID"):
            new_sid = ev["sessionID"]
    return reply, new_sid, None


def handle_event(ev, sessions, seen):
    """处理一条消息事件; 返回是否已消费"""
    message_id = ev.get("message_id") or ""
    if message_id and message_id in seen:
        log("DUP skip msg=%s" % message_id)
        return True

    sender_type = ev.get("sender_type")
    if sender_type != "user":
        return False  # 忽略机器人/系统消息, 防死循环

    sender_id = ev.get("sender_id", "")
    if sender_id not in ALLOWED_USERS:
        mentions = [m.get("id") for m in (ev.get("mentions") or [])]
        log("IGNORE sender=%s chat_type=%s mentioned=%s (不在白名单)" % (
            sender_id, ev.get("chat_type", ""), mentions))
        return True

    chat_id = ev.get("chat_id", "")
    chat_type = ev.get("chat_type", "")
    content = (ev.get("content") or "").strip()
    if not content:
        return True

    # 群聊: 必须 @ 了机器人 (p2p 不需要)
    if chat_type == "group":
        mentions = ev.get("mentions") or []
        mentioned = any(
            m.get("id") == BOT_APP_ID or BOT_APP_ID in str(m.get("id", ""))
            for m in mentions
        )
        if not mentioned:
            return True  # 没 @ 机器人, 不响应

    # 清理 @ 占位符, 让 mimo 看到干净指令
    for m in ev.get("mentions") or []:
        content = content.replace(m.get("key", ""), "").strip()
    content = content.lstrip("@").strip()

    # 幂等去重登记 (处理前登记, 防处理期间重试)
    if message_id:
        seen.add(message_id)
        save_json(SEEN_FILE, list(seen)[-SEEN_MAX:])

    key = chat_id
    session_id = sessions.get(key, {}).get("sid")

    # /reset 命令: 清空该 chat 的会话
    if content in ("/reset", "/new"):
        if key in sessions:
            del sessions[key]
            save_json(SESSIONS_FILE, sessions)
        send_chunks(chat_id, message_id, "已重置会话, 开始新对话。")
        log("RESET chat=%s" % chat_id)
        return True

    log(">> chat=%s type=%s msg=%s" % (chat_id, chat_type, content[:200]))

    # typing 反应替代 [处理中] 文本
    reaction_id = react(message_id) if message_id else None

    reply, new_sid, err = run_mimo(content, session_id)
    if reaction_id:
        unreact(message_id, reaction_id)

    if err:
        log("mimo ERROR: %s" % err)
        send_chunks(chat_id, message_id, "出错: %s" % err[:500])
        return True

    if not reply:
        reply = "(mimo 无文本回复)"

    # 保存会话
    sessions[key] = {"sid": new_sid, "last": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_json(SESSIONS_FILE, sessions)

    ok = send_chunks(chat_id, message_id, reply)
    log("<< replied=%s len=%d chunks=%d (to %s)" % (
        ok, len(reply), max(1, (len(reply) + CHUNK_LIMIT - 1) // CHUNK_LIMIT), chat_id))
    return True


def wait_ready(proc, timeout=30):
    """等待 consume 的 ready 标记 (stderr)"""
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            time.sleep(0.2)
            continue
        buf += line
        if "ready event_key" in line:
            return True
        if "error" in line.lower() and "envelope" in line.lower():
            log("consume error: %s" % line.strip())
            return False
    log("stderr tail: %s" % buf[-800:])
    return False


def run_consume_loop(sessions, seen):
    """单次 consume 生命周期; 进程退出/EOF 即返回, 由外层重启"""
    cmd = [NODE, LARK_RUNJS, "event", "consume", "im.message.receive_v1",
           "--as", "bot", "--max-events", "0"]
    # stdin=PIPE: 无界 consume 依赖 stdin 不 EOF; pythonw 无控制台时继承的 stdin 立即 EOF 会导致 consume 退出
    # CREATE_NO_WINDOW: 防弹出控制台窗口
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW)

    if not wait_ready(proc):
        log("FATAL: consume not ready, killing")
        proc.kill()
        return

    log("consume ready, listening...")
    for raw in proc.stdout:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            handle_event(ev, sessions, seen)
        except Exception as e:
            log("handle_event EXC: %r" % e)

    # stdout EOF = consume 进程退出
    code = proc.poll()
    log("consume exited (code=%s), will restart in %ds" % (code, RESTART_DELAY))
    try:
        proc.kill()
    except Exception:
        pass


def main():
    lock = acquire_lock()
    if lock is None:
        print("another bridge instance already running, exit")
        return

    log("=== bridge started, workspace=%s ===" % WORKSPACE)
    sessions = load_json(SESSIONS_FILE, {})
    seen = set(load_json(SEEN_FILE, []))

    while True:
        try:
            run_consume_loop(sessions, seen)
        except Exception as e:
            log("consume loop crashed: %r, restart in %ds" % (e, RESTART_DELAY))
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
