"""Codex hook → 状态灯服务 的转发器。

由 ~/.codex/hooks.json 调用：python3 -S -E hook_relay.py <EventName>，事件 JSON 从 stdin 读入。
设计约束：
  * 绝不向 stdout 输出（对 UserPromptSubmit/SessionStart 等事件，stdout 会被当成额外上下文注入 Codex）；
  * 永远 exit 0，任何异常都吞掉，不影响 Codex；
  * 只记录元数据（会话/回合/工具名等），不记录提示词、回复内容、工具参数与输出；
  * 只做一次追加写 + 一个本地数据报唤醒，通常 < 30 ms。
"""
import json
import os
import select
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(HERE, "run")
SPOOL = os.path.join(RUN_DIR, "events.jsonl")
WAKE_SOCK = os.path.join(RUN_DIR, "wake.sock")
MAX_SPOOL = 5 * 1024 * 1024
KEEP = ("session_id", "turn_id", "hook_event_name", "transcript_path", "tool_name", "tool_use_id",
        "agent_id", "agent_type", "source", "reason", "cwd", "permission_mode", "stop_hook_active", "model")


def read_stdin(limit=4 * 1024 * 1024, wait=0.5):
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return b""
        fd = sys.stdin.fileno()
        chunks, total, end = [], 0, time.time() + wait
        while total < limit:
            r, _, _ = select.select([fd], [], [], max(0.0, end - time.time()))
            if not r:
                break
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
            total += len(b)
        return b"".join(chunks)
    except Exception:
        return b""


def main():
    ts = time.time()
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(read_stdin() or b"{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    rec = {"ts": ts, "event": event or payload.get("hook_event_name") or ""}
    for k in KEEP:
        v = payload.get(k)
        if isinstance(v, (str, int, float, bool)) and v is not None:
            rec[k] = v if not isinstance(v, str) else v[:512]
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
        fd = os.open(SPOOL, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size > MAX_SPOOL:      # 服务长期未运行：丢弃旧积压，避免无限增长
                os.close(fd)
                try:
                    os.replace(SPOOL, SPOOL + ".old")
                except OSError:
                    pass
                fd = os.open(SPOOL, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.setblocking(False)
            s.sendto(b"!", WAKE_SOCK)
        finally:
            s.close()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    os._exit(0)
