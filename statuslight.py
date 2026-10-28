#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex → Keychron Q5 HE 状态灯：后台服务 + 命令行工具（仅 Python 标准库，兼容 Python 3.8+）。

灯语（默认：整个键盘按列从左到右作为一条进度条，每列一格）：
  全部按键          白色常亮（常态）
  最左侧若干列      蓝色 = 已启用的计划任务（每个一列，暂停的不显示）；其余列为任务区
  任务区            并行任务平均分区，每个任务一条进度条：
                    黄色从左往右逐列填充 + 向右移动的跑马条纹 = 思考中（等待批准时条纹停止）
                    绿色整段常亮 5 秒 = 完成；红色整段闪烁 5 秒 = 中断/失败
                    没有开始记录的完成事件只占最右侧一列
  空格              紫色呼吸 = 等待批准或回答
  P 键              白色 = 代理正常；橙色 = 短暂断开；红色呼吸 = 持续断开；绿色 = 刚恢复

命令：
  run                         后台服务（launchd 调用）
  status                      查看服务、键盘、任务、代理状态
  probe                       只读探测 HID 接口与协议，结果写 state/probe.json
  selftest [--cycles N]       只改 RAM 的灯光自检 + 写入/回读稳定性测试，结果写 state/selftest.json
  demo                        依次演示全部灯效（服务需在运行）
  pause | resume | restore    暂停接管（并恢复原灯效）/ 恢复接管 / 同 pause
  emit EVENT [JSON]           手动注入一个 hook 事件（调试）
"""
from __future__ import print_function

import argparse
import calendar
import fcntl
import glob
import http.client
import json
import logging
import logging.handlers
import math
import os
import random
import re
import resource
import select
import signal
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import q5he_hid as hid  # noqa: E402

VERSION = "1.2.0"
CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
RUN_DIR = os.path.join(HERE, "run")
STATE_DIR = os.path.join(HERE, "state")
LOG_DIR = os.path.join(HERE, "logs")
SPOOL = os.path.join(RUN_DIR, "events.jsonl")
WAKE_SOCK = os.path.join(RUN_DIR, "wake.sock")
LOCK_FILE = os.path.join(RUN_DIR, "daemon.lock")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
ORIGINAL_FILE = os.path.join(STATE_DIR, "original_lighting.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
UA = "q5he-statuslight/%s" % VERSION

DEFAULT_CONFIG = {
    "colors": {                       # QMK HSV（色相/饱和度 0–255）；亮度跟随键盘全局亮度
        "base": [0, 0],               # 白
        "thinking": [43, 255],        # 黄
        "thinking_mid": [43, 175],
        "thinking_tail": [43, 130],
        "thinking_bg": [43, 85],      # 任务分区底色（淡黄，comet/chase 风格用）
        "thinking_stripe": [43, 110], # 进度条里向右移动的浅黄色跑马高光
        "done": [85, 255],            # 绿
        "failed": [0, 255],           # 红
        "schedule": [170, 255],       # 蓝
        "waiting": [191, 255],        # 紫
        "proxy_brief": [21, 255],     # 橙
        "proxy_down": [0, 255],       # 红（呼吸）
        "proxy_recovered": [85, 255], # 绿
    },
    "timing": {
        "done_seconds": 5.0,
        "failed_seconds": 5.0,
        "orphan_seconds": 5.0,
        "chase_step": 0.2,
        "progress_tau_seconds": 120,  # 进度条填充速度：运行这么多秒约填满 63%
        "flash_period": 0.5,
        "breath_period": 2.4,
        "stale_minutes": 45,
        "health_check_seconds": 10,
        "full_refresh_minutes": 10,
        "foreign_yield_seconds": 60,
        "wait_expire_hours": 12,
    },
    "layout": {
        "mode": "columns",            # columns = 整个键盘按列（左→右每列一格）；top_row = 只用顶排
        "top_row": 0,                 # 顶排 = 矩阵第 0 行（Esc、F1–F12、Del、F13–F15）
        "space_key": [5, 6],          # [矩阵行, 列]
        "proxy_key": [2, 10],         # P 键
        "schedule_max_keys": 8,       # 计划任务最多占几格（列模式下为列）
    },
    "tasks": {
        "show_subagents": True,
        "thinking_style": "progress", # progress（进度条+跑马条纹）| comet | chase
        "region_stripes": True,       # 进度条高光用 Mix RGB 区域 1（纯色）来画：移动时只改区域归属，发包约为逐键方式的 1/3
        "question_tools": ["request_user_input"],
    },
    "breathing": {"mode": "auto", "speed": 110},   # auto = Mix RGB 区域 1 真呼吸；saturation = 颜色↔白色
    "proxy": {
        "enabled": True,
        "targets": ["https://chatgpt.com/cdn-cgi/trace", "https://api.openai.com/v1/models"],
        "proxy_url": "",              # 空 = 跟随 macOS 系统代理；也可填 http://127.0.0.1:7890 / socks5://127.0.0.1:7891
        "interval_ok": 20,
        "interval_fail": 5,
        "timeout": 6,
        "persistent_after": 60,
        "recovered_seconds": 20,
    },
    "codex": {"watch_rollouts": True, "rollout_window_hours": 6, "process_check": True,
              "ignore_ephemeral": True,             # 忽略没有会话记录的后台会话（Codex 的记忆整理、应用列表等）
              "ignore_cwd_prefixes": ["~/.codex"]},  # 工作目录在这些路径下的会话也忽略
    "device": {"pids": [0x0B50, 0x0B51, 0x0B52]},
}

log = logging.getLogger("q5he")


# ======================================================================
# 工具函数
# ======================================================================
def setup_logging(to_stderr):
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "statuslight.log"),
                                              maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def ensure_dirs():
    for d in (RUN_DIR, STATE_DIR, LOG_DIR):
        os.makedirs(d, mode=0o700, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_POSITIVE = ("timing.", "proxy.interval_ok", "proxy.interval_fail", "proxy.timeout", "proxy.persistent_after",
             "proxy.recovered_seconds", "codex.rollout_window_hours")


def _valid(path, v, dv):
    def is_num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    if isinstance(dv, bool):
        return isinstance(v, bool)
    if is_num(dv):
        if not is_num(v) or v < 0 or v != v:
            return False
        if any(path.startswith(p) for p in _POSITIVE) and v <= 0:
            return False
        if path == "breathing.speed" and v > 255:
            return False
        if path == "layout.top_row" and v > 5:
            return False
        return True
    if isinstance(dv, str):
        return isinstance(v, str)
    if isinstance(dv, list):
        if not isinstance(v, list):
            return False
        if path.startswith("colors."):
            return len(v) == 2 and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in v)
        if path in ("layout.space_key", "layout.proxy_key"):
            return len(v) == 2 and all(isinstance(x, int) and 0 <= x < 32 for x in v)
        if path == "device.pids":
            return len(v) > 0 and all(isinstance(x, int) and 0 < x < 0x10000 for x in v)
        return all(isinstance(x, str) for x in v)
    return True


def sanitize_config(cfg, defaults=None, path=""):
    """逐项校验配置，类型或取值不对的项退回默认值（避免一个笔误让服务反复出错）。"""
    defaults = DEFAULT_CONFIG if defaults is None else defaults
    out = {}
    for k, dv in defaults.items():
        p = "%s.%s" % (path, k) if path else k
        v = cfg.get(k, dv) if isinstance(cfg, dict) else dv
        if isinstance(dv, dict):
            out[k] = sanitize_config(v if isinstance(v, dict) else {}, dv, p)
            continue
        if not _valid(p, v, dv):
            log.warning("配置项 %s 的值 %r 无效，使用默认值 %r", p, v, dv)
            v = dv
        out[k] = v
    if path == "":
        st = out["tasks"].get("thinking_style")
        if st not in ("progress", "comet", "chase"):
            out["tasks"]["thinking_style"] = "progress"
        if out["layout"].get("mode") not in ("columns", "top_row"):
            out["layout"]["mode"] = "columns"
        if out["breathing"].get("mode") not in ("auto", "saturation"):
            out["breathing"]["mode"] = "auto"
    return out


def load_config():
    user = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                user = json.load(f)
        except Exception as e:
            log.warning("config.json 解析失败，沿用默认配置：%s", e)
            user = {}
    return sanitize_config(deep_merge(DEFAULT_CONFIG, user if isinstance(user, dict) else {}))


_TS_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?Z")


def parse_iso(ts):
    m = _TS_RE.match(ts or "")
    if not m:
        return None
    sec = calendar.timegm(tuple(int(m.group(i)) for i in range(1, 7)) + (0, 0, 0))
    return sec + (float(m.group(7)) if m.group(7) else 0.0)


def split_even(items, k):
    n = len(items)
    out, pos = [], 0
    for i in range(k):
        size = n // k + (1 if i < n % k else 0)
        out.append(items[pos:pos + size])
        pos += size
    return out


def runs(indices, values, maxlen, gap=None):
    """把需要更新的下标合并成连续区段，每段不超过 maxlen。
    gap 默认 maxlen-1：用定长窗口贪心覆盖，发包数最少（未变化的灯顺带重发，不额外占包）。"""
    if gap is None:
        gap = maxlen - 1
    out = []
    start = end = None
    for i in sorted(indices):
        if start is None:
            start = end = i
        elif i - end <= gap + 1 and i - start < maxlen:
            end = i
        else:
            out.append((start, values[start:end + 1]))
            start = end = i
    if start is not None:
        out.append((start, values[start:end + 1]))
    return out


def codex_pids():
    """当前运行中的 codex 进程（Codex App 的 app-server 与 CLI 都叫 codex）。失败返回 None。"""
    try:
        out = subprocess.run(["/bin/ps", "-axo", "pid=,comm="], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=3).stdout
    except Exception:
        return None
    pids = set()
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1]).lower() == "codex":
            try:
                pids.add(int(parts[0]))
            except ValueError:
                pass
    return pids


def read_automations(codex_home):
    out = []
    for path in sorted(glob.glob(os.path.join(codex_home, "automations", "*", "automation.toml"))):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue

        def g(key):
            m = re.search(r'^%s\s*=\s*"((?:[^"\\]|\\.)*)"' % key, text, re.M)
            return m.group(1) if m else None

        out.append({"id": g("id") or os.path.basename(os.path.dirname(path)), "name": g("name") or "",
                    "status": (g("status") or "").upper(), "kind": g("kind") or ""})
    return out


# ======================================================================
# 状态机（纯逻辑，可离线测试）
# ======================================================================
TERMINAL = ("done", "failed", "interrupted")


def tkey(session, turn):
    return "turn:%s:%s" % (session or "?", turn or "?")


def akey(agent):
    return "agent:%s" % agent


class Engine(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.tasks = {}
        self.waits = {}           # session -> {"kind", "tool", "ts"}
        self.orphans = []         # [{"kind": "done"|"failed", "ts", "until"}]
        self.terminal = {}        # key -> ts（去重：已结束的回合）
        self.stale = {}           # key -> ts（失联移除的回合：不再由“活动”复活，但完成时仍可显示）
        self.proxy = {"state": "unknown", "first_fail": None, "recovered_until": 0.0,
                      "last_ok": None, "last_fail": None, "detail": ""}
        self.proxy_override = None
        self.schedule = {"active": [], "paused": []}
        self.codex_pids = None
        self.dirty = True

    # ---------- 持久化 ----------
    def to_json(self):
        return {"tasks": list(self.tasks.values()), "waits": self.waits, "orphans": self.orphans,
                "terminal": self.terminal, "stale": self.stale, "proxy": self.proxy, "schedule": self.schedule}

    def from_json(self, d):
        d = d or {}
        self.tasks = {t["key"]: t for t in d.get("tasks", []) if isinstance(t, dict) and "key" in t}
        self.waits = d.get("waits") or {}
        self.orphans = d.get("orphans") or []
        self.terminal = d.get("terminal") or {}
        self.stale = d.get("stale") or {}
        p = d.get("proxy")
        if isinstance(p, dict):
            self.proxy.update(p)
        self.schedule = d.get("schedule") or self.schedule

    # ---------- 事件 ----------
    def _new_task(self, key, kind, session, turn, agent, ts, origin, rollout=None):
        t = {"key": key, "kind": kind, "session": session, "turn": turn, "agent": agent, "state": "thinking",
             "started": ts, "updated": ts, "ended": None, "origin": origin, "rollout": rollout,
             "pids": sorted(self.codex_pids) if self.codex_pids else None}
        self.tasks[key] = t
        log.info("任务开始 %s（来源 %s）", key, origin)
        return t

    def _find_turn(self, session, turn):
        if turn:
            return self.tasks.get(tkey(session, turn))
        c = [t for t in self.tasks.values()
             if t["session"] == session and t["kind"] == "turn" and t["state"] == "thinking"]
        return max(c, key=lambda t: t["started"]) if c else None

    def _orphan(self, kind, ts):
        secs = float(self.cfg["timing"]["orphan_seconds"])
        self.orphans.append({"kind": kind, "ts": ts, "until": ts + secs})
        self.orphans = self.orphans[-5:]
        log.info("无开始记录的%s事件 → 最右侧键", "完成" if kind == "done" else "中断/失败")

    def _finish(self, t, final, ts, reason=None):
        t["state"] = final
        t["ended"] = ts
        t["reason"] = reason
        log.info("任务%s %s%s", {"done": "完成", "failed": "失败", "interrupted": "中断"}[final], t["key"],
                 "（%s）" % reason if reason else "")

    def handle(self, ev, now):
        typ = ev.get("type")
        ts = float(ev.get("ts") or now)
        s = ev.get("session")
        turn = ev.get("turn")
        stale = float(self.cfg["timing"]["stale_minutes"]) * 60
        self.dirty = True

        if typ == "turn_start":
            if not s or not turn:
                return
            key = tkey(s, turn)
            if key in self.terminal or now - ts > stale:
                return
            for t in list(self.tasks.values()):     # 同一会话同时只有一个主回合
                if t["kind"] == "turn" and t["session"] == s and t["state"] == "thinking" and t["key"] != key:
                    log.info("会话 %s 开始新回合，丢弃未收到结束事件的旧回合 %s", s, t["key"])
                    del self.tasks[t["key"]]
                    self.terminal[t["key"]] = ts      # 迟到的结束/活动事件不再产生孤立灯或幽灵分区
            t = self.tasks.get(key)
            if t is None:
                self._new_task(key, "turn", s, turn, None, ts, ev.get("origin", "hook"), ev.get("rollout"))
            else:
                t["updated"] = max(t["updated"], ts)
                if ev.get("rollout"):
                    t["rollout"] = ev["rollout"]
            if ev.get("origin") == "hook":
                self.waits.pop(s, None)

        elif typ == "activity":
            t = self._find_turn(s, turn)
            if t is None and ev.get("create") and s and turn and tkey(s, turn) not in self.terminal \
                    and tkey(s, turn) not in self.stale and now - ts < stale:
                t = self._new_task(tkey(s, turn), "turn", s, turn, None, ts, ev.get("origin", "activity"),
                                   ev.get("rollout"))
            if t is not None:
                t["updated"] = max(t["updated"], ts)
            for a in self.tasks.values():         # 子代理会话的活动
                if a["kind"] == "agent" and a["agent"] == s:
                    a["updated"] = max(a["updated"], ts)

        elif typ == "wait":
            if s:
                self.waits[s] = {"kind": ev.get("kind", "approval"), "tool": ev.get("tool"), "ts": ts,
                                 "pids": sorted(self.codex_pids) if self.codex_pids else None}
                log.info("等待用户%s（会话 %s）", "回答" if ev.get("kind") == "question" else "批准", s)

        elif typ == "wait_clear":
            if s in self.waits and ts >= self.waits[s].get("ts", 0):
                del self.waits[s]

        elif typ in ("turn_complete", "turn_abort"):
            ok = typ == "turn_complete"
            reason = ev.get("reason")
            if ok:
                final = "done"
            elif reason in ("interrupted", "replaced", "codex-exited", "session_end"):
                final = "interrupted"
            else:
                final = "failed"
            if s:
                self.waits.pop(s, None)
            t = self._find_turn(s, turn)
            key = tkey(s, turn) if turn else (t["key"] if t else None)
            if key and key in self.terminal:
                return
            if t is not None and t["state"] == "thinking":
                self._finish(t, final, ts, reason)
            elif t is None:
                self._orphan("done" if ok else "failed", ts)
            if key:
                self.terminal[key] = ts

        elif typ == "agent_start":
            if not self.cfg["tasks"].get("show_subagents", True) or not ev.get("agent"):
                return
            key = akey(ev["agent"])
            if key in self.terminal or key in self.tasks or now - ts > stale:
                return
            t = self._new_task(key, "agent", s, turn, ev["agent"], ts, "hook")
            t["agent_type"] = ev.get("agent_type")

        elif typ == "agent_stop":
            if not self.cfg["tasks"].get("show_subagents", True) or not ev.get("agent"):
                return
            key = akey(ev["agent"])
            if key in self.terminal:
                return
            t = self.tasks.get(key)
            if t is not None and t["state"] == "thinking":
                self._finish(t, "done", ts)
            elif t is None:
                self._orphan("done", ts)
            self.terminal[key] = ts

        elif typ == "session_end":
            self.waits.pop(s, None)
            for t in list(self.tasks.values()):
                if t["session"] == s and t["state"] == "thinking":
                    self._finish(t, "interrupted", ts, "session_end")
                    self.terminal[t["key"]] = ts

    def check_processes(self, pids, now, silent=False):
        """Codex 进程消失（退出/崩溃/重启）后，仍在“思考中”的任务视为中断。"""
        self.codex_pids = pids
        if pids is None:
            return
        for t in list(self.tasks.values()):
            if t["state"] != "thinking" or t.get("session", "").startswith("claude:"):
                continue
            gone = (len(pids) == 0) or (t.get("pids") and not (set(t["pids"]) & pids))
            if gone:
                if silent:
                    log.info("启动时发现任务 %s 的 Codex 进程已不在，静默移除", t["key"])
                    del self.tasks[t["key"]]
                else:
                    self._finish(t, "interrupted", now, "codex-exited")
                self.terminal[t["key"]] = now
                self.waits.pop(t["session"], None)
                self.dirty = True
        for s, w in list(self.waits.items()):        # Codex 退出后不会再有人批准
            if s.startswith("claude:"):
                continue
            if len(pids) == 0 or (w.get("pids") and not (set(w["pids"]) & pids)):
                log.info("Codex 进程已退出，清除会话 %s 的等待状态", s)
                del self.waits[s]
                self.dirty = True

    def proxy_result(self, ts, ok, detail):
        pc = self.cfg["proxy"]
        p = self.proxy
        prev = p["state"]
        if ok:
            if prev in ("brief", "down"):
                p["recovered_until"] = ts + float(pc["recovered_seconds"])
                log.info("代理恢复：%s", detail)
            p["state"] = "ok"
            p["first_fail"] = None
            p["last_ok"] = ts
        else:
            if p["first_fail"] is None:
                p["first_fail"] = ts
                log.warning("代理/网络断开：%s", detail)
            p["last_fail"] = ts
            p["state"] = "down" if ts - p["first_fail"] >= float(pc["persistent_after"]) else "brief"
            if prev == "brief" and p["state"] == "down":
                log.warning("代理持续断开超过 %ss", pc["persistent_after"])
        p["detail"] = detail
        self.dirty = True

    def proxy_view(self, now):
        if self.proxy_override and self.proxy_override[1] > now:
            return self.proxy_override[0]
        p = self.proxy
        st = p["state"]
        if st == "brief" and p["first_fail"] and now - p["first_fail"] >= float(self.cfg["proxy"]["persistent_after"]):
            st = "down"
        if st == "ok" and now < (p.get("recovered_until") or 0):
            return "recovered"
        return st

    def tick(self, now):
        tm = self.cfg["timing"]
        stale = float(tm["stale_minutes"]) * 60
        for key, t in list(self.tasks.items()):
            if t["state"] == "done" and now >= t["ended"] + float(tm["done_seconds"]):
                del self.tasks[key]
                self.dirty = True
            elif t["state"] in ("failed", "interrupted") and now >= t["ended"] + float(tm["failed_seconds"]):
                del self.tasks[key]
                self.dirty = True
            elif t["state"] == "thinking" and now - t["updated"] > stale:
                if t["session"] in self.waits or (t["kind"] == "agent" and t["agent"] in self.waits):
                    continue                          # 正在等你批准/回答：不算失联
                log.info("任务 %s 超过 %s 分钟没有任何活动，视为失联并移除", key, tm["stale_minutes"])
                del self.tasks[key]
                self.stale[key] = now
                self.dirty = True
        n = len(self.orphans)
        self.orphans = [o for o in self.orphans if o["until"] > now]
        if len(self.orphans) != n:
            self.dirty = True
        wexp = float(tm["wait_expire_hours"]) * 3600
        for s in [s for s, w in self.waits.items() if now - w.get("ts", now) > wexp]:
            del self.waits[s]
        for k in [k for k, ts in self.terminal.items() if now - ts > 3600]:
            del self.terminal[k]
        for k in [k for k, ts in self.stale.items() if now - ts > 6 * 3600]:
            del self.stale[k]
        if self.proxy_override and self.proxy_override[1] <= now:
            self.proxy_override = None

    # ---------- 画面 ----------
    def visible_tasks(self):
        return sorted(self.tasks.values(), key=lambda t: (t["started"], t["key"]))

    def frame(self, now, layout, region_breath):
        """返回 (colors{led:(h,s)}, 区域 1 的灯, 区域 1 灯效 (effect, h, s) 或 None, 下一次需要重画的时间)。"""
        cfg = self.cfg
        c = cfg["colors"]
        tm = cfg["timing"]

        def col(name):
            return (int(c[name][0]), int(c[name][1]))

        base = col("base")
        colors = {}
        nxt = [now + 3600.0]

        def due(t):
            if t > now:
                nxt.append(t)

        # 显示单元：columns 模式下每一格是键盘的一整列（从左到右）；top_row 模式下每一格是顶排的一个键
        cells = [list(c) for c in (layout.get("cells") or [[v] for v in (layout.get("top") or [])]) if c]
        stripes = set()                             # 进度条里浅黄高光所在的灯
        step = float(tm["chase_step"])
        half = float(tm["flash_period"]) / 2.0

        def paint(cell, hs):
            for led in cell:
                colors[led] = hs

        def flash_on(since):
            k = int(max(0.0, now - since) / half)
            due(since + (k + 1) * half)
            return k % 2 == 0

        # 计划任务：从最左侧开始，每个已启用的计划任务一格蓝色（至少给任务区留下约三分之一）
        n_sched = min(len(self.schedule.get("active") or []), int(cfg["layout"]["schedule_max_keys"]),
                      max(0, len(cells) - max(1, len(cells) // 3)))
        for cell in cells[:n_sched]:
            paint(cell, col("schedule"))
        area = cells[n_sched:]

        # 任务区：并行任务平均分区
        vis = self.visible_tasks()
        k = min(len(vis), len(area))
        if k:
            phase = int(now / step)
            style = cfg["tasks"].get("thinking_style", "progress")
            tau = float(tm.get("progress_tau_seconds", 120))
            for t, seg in zip(vis[:k], split_even(area, k)):
                st = t["state"]
                w = len(seg)
                if st == "thinking":
                    paused = (t["session"] in self.waits) or (t["kind"] == "agent" and t["agent"] in self.waits)
                    if style == "progress":
                        # 进度条：按已运行时间从左往右逐格填充（渐近，最后一格留给“完成”），
                        # 已填充部分里有向右移动的跑马条纹；等待批准时条纹停止
                        frac = 1.0 - math.exp(-max(0.0, now - t["started"]) / tau)
                        n = 1 if w <= 1 else max(1, min(w - 1, 1 + int(frac * (w - 1))))
                        for j, cell in enumerate(seg):
                            if j >= n:
                                paint(cell, base)              # 未填充部分保持白色：进度边界一眼可见
                            elif not paused and (j - phase) % 3 == 0:
                                paint(cell, col("thinking_stripe"))
                                stripes.update(cell)
                            else:
                                paint(cell, col("thinking"))
                        if w == 1 and not paused:
                            paint(seg[0], col("thinking" if phase % 2 == 0 else "thinking_stripe"))
                            stripes.update(seg[0])
                        due((phase + 1) * step if not paused else now + 2.0)
                        continue
                    if paused:
                        for cell in seg:
                            paint(cell, col("thinking"))
                        continue
                    due((phase + 1) * step)
                    for j, cell in enumerate(seg):
                        if style == "chase":
                            m = (j - phase) % 3
                            paint(cell, col(("thinking", "thinking_tail", "thinking_bg")[m]))
                        elif w == 1:
                            paint(cell, col("thinking" if phase % 2 == 0 else "thinking_bg"))
                        else:
                            d = (phase - j) % w
                            paint(cell, col({0: "thinking", 1: "thinking_mid", 2: "thinking_tail"}.get(d, "thinking_bg")))
                elif st == "done":
                    due(t["ended"] + float(tm["done_seconds"]))
                    for cell in seg:
                        paint(cell, col("done"))
                else:
                    due(t["ended"] + float(tm["failed_seconds"]))
                    on = flash_on(t["ended"])
                    for cell in seg:
                        paint(cell, col("failed") if on else base)

        # 没有开始记录的完成事件：只占最右侧一格
        if self.orphans and cells:
            o = self.orphans[-1]
            due(o["until"])
            if o["kind"] == "done":
                paint(cells[-1], col("done"))
            else:
                paint(cells[-1], col("failed") if flash_on(o["ts"]) else base)

        # 呼吸请求：空格（等待批准/回答）、P 键（代理持续断开）
        breath = []
        if self.waits and layout.get("space") is not None:
            breath.append((layout["space"], col("waiting")))
        pv = self.proxy_view(now)
        pk = layout.get("proxy")
        if pk is not None and cfg["proxy"].get("enabled", True):
            if pv == "brief":
                colors[pk] = col("proxy_brief")
                if self.proxy["first_fail"]:
                    due(self.proxy["first_fail"] + float(cfg["proxy"]["persistent_after"]))
            elif pv == "down":
                breath.append((pk, col("proxy_down")))
            elif pv == "recovered":
                colors[pk] = col("proxy_recovered")
                due(self.proxy.get("recovered_until") or now)
            if self.proxy_override:
                due(self.proxy_override[1])

        # 区域 1（Mix RGB）：有呼吸需求时给呼吸键用；否则用纯色画进度条高光，高光移动时只需改区域归属
        r1, r1c = set(), None
        stripe = col("thinking_stripe")
        if region_breath and not breath and stripes and cfg["tasks"].get("region_stripes", True):
            fill = col("thinking")
            for led in stripes:
                if colors.get(led) == stripe:           # 状态键等被覆盖成别的颜色的灯不放进区域 1
                    r1.add(led)
                    colors[led] = fill                  # 逐键颜色保持黄色：离开区域 1 时直接显示填充色
            if r1:
                r1c = (hid.EFFECT_SOLID, stripe[0], stripe[1])
        period = float(tm["breath_period"])
        for led, hs in breath:
            fx = (hid.EFFECT_BREATHING, hs[0], hs[1])
            if region_breath and r1c in (None, fx):
                r1.add(led)
                r1c = fx
                colors[led] = hs
            else:                                   # 兜底：颜色在饱和色与白色之间呼吸
                x = 0.5 - 0.5 * math.cos(2 * math.pi * (now % period) / period)
                lvl = int(round(x * 8)) / 8.0
                colors[led] = (hs[0], int(hs[1] * lvl))
                due(now + period / 32.0)
        return colors, r1, r1c, min(nxt)


# ======================================================================
# Codex 会话记录（rollout）监视：补齐 hook 之外的开始/完成/中断/错误，并用于重启恢复
# ======================================================================
class RolloutWatcher(object):
    def __init__(self, codex_home, started_at):
        self.home = codex_home
        self.files = {}           # path -> {"off", "tid", "buf"}
        self.kinds = {}           # thread id -> "subagent" | "main"
        self.started_at = started_at
        self._db_cache = None     # (库文件签名, 查询时间, 结果)

    def to_json(self):
        # 不保存写了一半的行：偏移退回到该行开头，重启后整行重读
        return {p: {"off": f["off"] - len(f["buf"]), "tid": f["tid"]} for p, f in self.files.items()}

    def from_json(self, d):
        for p, f in (d or {}).items():
            self.files[p] = {"off": int(f.get("off", 0)), "tid": f.get("tid"), "buf": b""}

    def _db_rows(self, window_hours):
        db = os.path.join(self.home, "state_5.sqlite")
        if not os.path.exists(db):
            cands = sorted(glob.glob(os.path.join(self.home, "state_*.sqlite")))
            if not cands:
                return []
            db = cands[-1]
        # 线程库（主文件与 WAL）的大小和修改时间都没变时复用上次结果：空闲时不必每 2 秒扫一遍库
        sig = [db]
        for p in (db, db + "-wal"):
            try:
                st = os.stat(p)
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
        sig = tuple(sig)
        now = time.time()
        if self._db_cache and self._db_cache[0] == sig and 0 <= now - self._db_cache[1] < 60:
            return self._db_cache[2]
        since_ms = int((now - window_hours * 3600) * 1000)
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=0.3)
        try:
            rows = con.execute(
                "SELECT id, rollout_path, source FROM threads WHERE archived = 0 AND "
                "coalesce(updated_at_ms, updated_at * 1000, 0) > ? ORDER BY coalesce(updated_at_ms, updated_at * 1000) "
                "DESC LIMIT 60", (since_ms,)).fetchall()
        finally:
            con.close()
        self._db_cache = (sig, now, rows)
        return rows

    def thread_kind(self, tid):
        return self.kinds.get(tid)

    @staticmethod
    def _parse_line(line):
        if b'"event_msg"' not in line:
            return None
        if not (b"task_started" in line or b"task_complete" in line or b"turn_aborted" in line
                or b'"error"' in line):
            return None
        try:
            o = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            return None
        p = o.get("payload") or {}
        if o.get("type") != "event_msg" or not isinstance(p, dict):
            return None
        return p.get("type"), p.get("turn_id"), p.get("reason"), parse_iso(o.get("timestamp"))

    def _scan_open_turn(self, path, size):
        """读取文件尾部，判断最后一个回合是否仍未结束。返回 turn_id 或 None。"""
        try:
            with open(path, "rb") as f:
                start = max(0, size - 512 * 1024)
                f.seek(start)
                data = f.read(size - start)
        except Exception:
            return None
        if start:
            data = data.split(b"\n", 1)[-1]
        state = {}
        last = None
        for line in data.split(b"\n"):
            r = self._parse_line(line)
            if not r:
                continue
            typ, turn, _, _ = r
            if typ == "task_started" and turn:
                state[turn] = "open"
                last = turn
            elif typ in ("task_complete", "turn_aborted") and turn:
                state[turn] = "closed"
        return last if last and state.get(last) == "open" else None

    def poll(self, now, extra_paths, emit, recovering, codex_alive, window_hours=6):
        try:
            rows = self._db_rows(window_hours)
        except Exception as e:
            rows = []
            log.debug("读取 Codex 线程库失败：%s", e)
        cands = {}
        for tid, path, source in rows:
            kind = "subagent" if (source or "").startswith('{"subagent"') else "main"
            self.kinds[tid] = kind
            if kind == "main" and path:
                cands[path] = tid
        for path, tid in extra_paths:
            if path and path not in cands:
                cands[path] = tid
        for path, tid in cands.items():
            try:
                st = os.stat(path)
            except OSError:
                continue
            f = self.files.get(path)
            if f is None:
                birth = getattr(st, "st_birthtime", st.st_ctime)
                if not recovering and birth >= self.started_at:
                    self.files[path] = {"off": 0, "tid": tid, "buf": b""}       # 新会话：从头读
                    f = self.files[path]
                else:
                    self.files[path] = {"off": st.st_size, "tid": tid, "buf": b""}
                    turn = self._scan_open_turn(path, st.st_size)
                    if turn and codex_alive and now - st.st_mtime < 600:
                        emit({"type": "turn_start", "session": tid, "turn": turn, "ts": st.st_mtime,
                              "origin": "recovered", "rollout": path})
                    continue
            if st.st_size < f["off"]:
                f["off"] = st.st_size
                f["buf"] = b""
                continue
            if st.st_size == f["off"]:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(f["off"])
                    data = fh.read(min(st.st_size - f["off"], 8 * 1024 * 1024))
            except Exception:
                continue
            f["off"] += len(data)
            buf = f["buf"] + data
            lines = buf.split(b"\n")
            f["buf"] = lines.pop() if len(lines[-1]) < 4 * 1024 * 1024 else b""
            emit({"type": "activity", "session": tid, "turn": None, "ts": now, "origin": "rollout"})
            for line in lines:
                r = self._parse_line(line)
                if not r:
                    continue
                typ, turn, reason, ts = r
                ts = ts or now
                if typ == "task_started":
                    emit({"type": "turn_start", "session": tid, "turn": turn, "ts": ts, "origin": "rollout",
                          "rollout": path})
                elif typ == "task_complete":
                    emit({"type": "turn_complete", "session": tid, "turn": turn, "ts": ts, "origin": "rollout"})
                elif typ == "turn_aborted":
                    emit({"type": "turn_abort", "session": tid, "turn": turn, "ts": ts, "origin": "rollout",
                          "reason": reason or "aborted"})
                elif typ == "error":
                    emit({"type": "turn_abort", "session": tid, "turn": turn, "ts": ts, "origin": "rollout",
                          "reason": "error"})
        # 控制内存：只保留最近候选与正在跟踪的文件
        if len(self.files) > 200:
            keep = set(cands)
            self.files = {p: f for p, f in self.files.items() if p in keep}


# ======================================================================
# 代理 / 网络探测（独立线程）
# ======================================================================
def macos_system_proxy():
    try:
        out = subprocess.run(["/usr/sbin/scutil", "--proxy"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=3).stdout
    except Exception:
        return None
    kv = dict(re.findall(r"^\s*(\w+)\s*:\s*(\S+)\s*$", out, re.M))
    for kind, en, host, port, dport in (("http", "HTTPSEnable", "HTTPSProxy", "HTTPSPort", 443),
                                         ("http", "HTTPEnable", "HTTPProxy", "HTTPPort", 80),
                                         ("socks5", "SOCKSEnable", "SOCKSProxy", "SOCKSPort", 1080)):
        if kv.get(en) == "1" and kv.get(host):
            try:
                return (kind, kv[host], int(kv.get(port, dport)))
            except ValueError:
                continue
    return None


def parse_proxy_url(url):
    u = urllib.parse.urlsplit(url)
    scheme = (u.scheme or "http").lower()
    kind = "socks5" if scheme.startswith("socks") else "http"
    return (kind, u.hostname, u.port or (1080 if kind == "socks5" else 8080))


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        b = sock.recv(n - len(buf))
        if not b:
            raise OSError("连接被关闭")
        buf += b
    return buf


_SSL_CTX = [None, 0.0]


def _ssl_context():
    """复用 TLS 上下文：每次新建都要重新解析系统根证书（较耗 CPU）；每 6 小时重建一次以跟上证书更新。"""
    now = time.monotonic()
    if _SSL_CTX[0] is None or now - _SSL_CTX[1] > 6 * 3600:
        _SSL_CTX[0] = ssl.create_default_context()
        _SSL_CTX[1] = now
    return _SSL_CTX[0]


def https_probe(url, route, timeout):
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or 443
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    ctx = _ssl_context()
    if route is None or route[0] == "http":
        if route:
            conn = http.client.HTTPSConnection(route[1], route[2], timeout=timeout, context=ctx)
            conn.set_tunnel(host, port)
        else:
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        try:
            conn.request("GET", path, headers={"User-Agent": UA, "Connection": "close"})
            r = conn.getresponse()
            r.read(256)
            return r.status
        finally:
            conn.close()
    sock = socket.create_connection((route[1], route[2]), timeout=timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise OSError("SOCKS5 握手失败")
        hb = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
        rep = _recv_exact(sock, 4)
        if rep[1] != 0:
            raise OSError("SOCKS5 CONNECT 失败（%d）" % rep[1])
        atyp = rep[3]
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 4:
            _recv_exact(sock, 16)
        else:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        _recv_exact(sock, 2)
        tls = ctx.wrap_socket(sock, server_hostname=host)
        tls.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\nConnection: close\r\n\r\n"
                     % (path, host, UA)).encode("ascii"))
        line = tls.recv(64).split(b"\r\n", 1)[0].decode("ascii", "replace")
        return int(line.split()[1])
    finally:
        sock.close()


def probe_network(pc):
    route = parse_proxy_url(pc["proxy_url"]) if pc.get("proxy_url") else macos_system_proxy()
    via = "直连" if route is None else "%s://%s:%s" % route
    last = "无探测目标"
    for url in pc.get("targets") or []:
        try:
            status = https_probe(url, route, float(pc.get("timeout", 6)))
            return True, "%s HTTP %s（%s）" % (urllib.parse.urlsplit(url).hostname, status, via)
        except ssl.SSLCertVerificationError as e:
            return True, "%s TLS 已到达但证书校验失败（%s）：%s" % (urllib.parse.urlsplit(url).hostname, via, e)
        except Exception as e:
            last = "%s：%s（%s）" % (urllib.parse.urlsplit(url).hostname, e.__class__.__name__, via)
    return False, last


class Prober(threading.Thread):
    def __init__(self, cfg_getter, notify):
        threading.Thread.__init__(self, name="proxy-prober")
        self.daemon = True
        self.cfg = cfg_getter
        self.notify = notify
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.results = []

    def run(self):
        while not self.stop_ev.is_set():
            pc = self.cfg()
            if not pc.get("enabled", True):
                self.stop_ev.wait(10)
                continue
            try:
                ok, detail = probe_network(pc)
            except Exception as e:
                ok, detail = False, "探测异常：%s" % e
            with self.lock:
                self.results.append((time.time(), ok, detail))
            self.notify()
            self.stop_ev.wait(float(pc["interval_ok"] if ok else pc["interval_fail"]))

    def take(self):
        with self.lock:
            out, self.results = self.results, []
        return out


# ======================================================================
# 后台服务
# ======================================================================
def build_layout(led_map, cfg, led_count):
    L = cfg["layout"]

    def ok(v):
        return v is not None and v != 0xFF and 0 <= v < led_count

    top = [v for v in led_map[int(L.get("top_row", 0))] if ok(v)]

    def at(rc):
        try:
            v = led_map[int(rc[0])][int(rc[1])]
        except Exception:
            return None
        return v if ok(v) else None

    # 键盘矩阵的列即物理上从左到右的列（Q5 HE 共 19 列，每列 3–6 颗灯）
    ncols = max(len(r) for r in led_map) if led_map else 0
    columns = []
    for c in range(ncols):
        col = [r[c] for r in led_map if c < len(r) and ok(r[c])]
        if col:
            columns.append(col)
    mode = L.get("mode", "columns")
    cells = columns if mode == "columns" else [[v] for v in top]
    return {"mode": mode, "cells": cells, "top": top, "space": at(L["space_key"]), "proxy": at(L["proxy_key"]),
            "count": led_count}


def hook_events(rec, cfg):
    """把 hook 记录转换成状态机事件。"""
    ev = rec.get("event") or rec.get("hook_event_name") or ""
    base = {"session": rec.get("session_id"), "turn": rec.get("turn_id"), "ts": rec.get("ts"),
            "origin": "hook", "rollout": rec.get("transcript_path")}
    tool = rec.get("tool_name") or ""
    qtools = cfg["tasks"].get("question_tools") or []

    def e(typ, **kw):
        d = dict(base)
        d["type"] = typ
        d.update(kw)
        return d

    if ev == "UserPromptSubmit":
        return [e("turn_start")]
    if ev == "PreToolUse":
        if tool in qtools:
            return [e("wait", kind="question", tool=tool)]
        return [e("activity", create=True)]
    if ev == "PermissionRequest":
        return [e("wait", kind="approval", tool=tool)]
    if ev == "PostToolUse":
        return [e("wait_clear"), e("activity", create=True)]
    if ev == "Stop":
        return [e("turn_complete")]
    if ev == "Interrupt":
        return [e("turn_abort", reason="interrupted")]
    if ev == "SubagentStart":
        return [e("agent_start", agent=rec.get("agent_id"), agent_type=rec.get("agent_type"))]
    if ev == "SubagentStop":
        return [e("agent_stop", agent=rec.get("agent_id"))]
    if ev == "SessionEnd":
        return [e("session_end")]
    return []


class Daemon(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = Engine(cfg)
        self.kb = None
        self.layout = None
        self.devinfo = {}
        self.owned = False
        self.mode_effect = None
        self.mixed_ok = False
        self.breath_region_ok = False
        self.unsupported = None
        self.sent_colors = {}
        self.sent_regions = {}
        self.sent_r1 = None
        self.opted_out = None
        self.yielding = False
        self.restore_pending = False
        self.resync = False
        self.old_spools = []
        self.ignored = {}            # 被忽略的后台会话 -> 时间
        self.clock_offset = None     # time.time() - time.monotonic()：macOS 上 monotonic 在休眠时不走
        self.wake_grace_until = 0.0  # 刚从休眠唤醒：这段时间内网络探测结果作废、键盘通信失败不报警
        self.fail_streak = 0
        self.next_connect = 0.0
        self.next_health = 0.0
        self.next_full = 0.0
        self.connect_fail_logged = False
        self.stop_flag = False
        self.reexec = False
        self.reload_cfg = False
        self.started_at = time.time()
        self.rollouts = RolloutWatcher(CODEX_HOME, self.started_at)
        self.spool_fd = None
        self.spool_ino = None
        self.spool_off = 0
        self.spool_buf = b""
        self.saved_spool = {}
        self.timers = {"rollout": 0.0, "sched": 0.0, "proc": 0.0, "reload": 0.0}
        self.first_proc_check = True
        self.mtimes = {}
        self.last_save = 0.0
        self.prober = None
        self.wake = None
        self.rpipe = self.wpipe = None
        self.stats = {"hid_commands": 0, "errors": 0, "connects": 0, "events": 0, "frames": 0}
        self.last_error = None
        self.original = None

    # ---------- 状态文件 ----------
    def load_state(self):
        st = load_json(STATE_FILE, {})
        self.engine.from_json(st.get("engine"))
        self.opted_out = st.get("opted_out")
        self.saved_spool = st.get("spool") or {}
        self.rollouts.from_json(st.get("rollouts"))
        if self.engine.tasks:
            log.info("恢复上次保存的 %d 个任务", len(self.engine.tasks))

    def save_state(self, now=None):
        now = now or time.time()
        try:                                   # 自身资源占用（累计 CPU 秒数、峰值内存），便于核对“低占用”
            ru = resource.getrusage(resource.RUSAGE_SELF)
            self.stats["cpu_seconds"] = round(ru.ru_utime + ru.ru_stime, 3)
            self.stats["max_rss_mb"] = round(ru.ru_maxrss / (1048576.0 if sys.platform == "darwin" else 1024.0), 1)
        except Exception:
            pass
        st = {
            "version": VERSION, "updated_at": now, "pid": os.getpid(), "started_at": self.started_at,
            "engine": self.engine.to_json(), "opted_out": self.opted_out,
            "spool": {"ino": self.spool_ino, "off": self.spool_off - len(self.spool_buf)},
            "rollouts": self.rollouts.to_json(),
            "device": dict(self.devinfo, connected=self.kb is not None, owned=self.owned,
                           mode_effect=self.mode_effect, mixed=self.breath_region_ok, yielding=self.yielding,
                           unsupported=self.unsupported),
            "layout": self.layout, "stats": self.stats, "last_error": self.last_error,
        }
        try:
            save_json(STATE_FILE, st)
            self.engine.dirty = False
            self.last_save = now
        except Exception as e:
            log.warning("保存状态失败：%s", e)

    # ---------- 原始灯效 ----------
    def device_id(self):
        info = self.devinfo
        return "%04X:%s" % (info.get("pid") or 0, info.get("serial") or "")

    def _save_original(self, rec):
        allrec = load_json(ORIGINAL_FILE, {})
        allrec[self.device_id()] = rec
        save_json(ORIGINAL_FILE, allrec)

    def record_original(self):
        """记录“接管前”的灯光。记录带 active 标记：接管期间（含关灯让出）一直沿用同一份记录，
        只有在完整还原、或你主动换了灯效之后才失效，下次接管时重新记录。"""
        kb = self.kb
        prev = load_json(ORIGINAL_FILE, {}).get(self.device_id())
        light = kb.get_lighting()
        if prev and prev.get("active"):
            self.original = prev
            return prev
        if prev and light["effect"] == hid.EFFECT_MIXED and int(prev["lighting"].get("effect", 0)) != 0:
            # 键盘处于 Mix RGB 多半是之前接管时固件在开关灯时保存的，沿用上一份真实记录
            log.info("键盘当前为 Mix RGB（可能是接管期间开关灯时固件保存的），沿用之前记录的原灯效 %s",
                     prev["lighting"].get("effect"))
            prev["active"] = True
            self._save_original(prev)
            self.original = prev
            return prev
        rec = {"saved_at": time.time(), "active": True, "lighting": light, "per_key_type": kb.get_per_key_type(),
               "per_key_colors": kb.get_colors(0, kb.led_count)}
        if self.mixed_ok:
            rec["regions"] = kb.get_regions(0, kb.led_count)
            rec["effect_lists"] = [kb.get_effect_list(0), kb.get_effect_list(1)]
        self._save_original(rec)            # 先落盘再做任何写入：中途失败也不会把自己的设置当成“原灯效”
        self.original = rec
        log.info("已记录接管前的灯效：%s", light)
        return rec

    def restore_original(self, keep_off=False, mark_inactive=True, hidden_only=False):
        """还原接管前的灯光（全部只改 RAM）。
        hidden_only：只还原看不见的部分（逐键颜色/类型、混合区域与灯效列表），不动你刚选的灯效；
        keep_off：还原灯效后保持关灯（让固件下次开灯时保存的是你的原灯效，而不是 Mix RGB）。"""
        kb = self.kb
        if kb is None:
            return False
        rec = self.original or load_json(ORIGINAL_FILE, {}).get(self.device_id())
        if not rec:
            log.warning("没有接管前的灯效记录，保持当前灯效")
            return False
        light = rec["lighting"]
        steps = [("逐键类型", lambda: kb.set_per_key_type(min(4, int(rec.get("per_key_type", 0)))))]
        cols = rec.get("per_key_colors") or []
        if cols and len(cols) <= kb.led_count:
            steps.append(("逐键颜色", lambda: kb.set_colors(0, [tuple(x) for x in cols])))
        if self.mixed_ok and rec.get("regions") and rec.get("effect_lists"):
            steps.append(("混合区域", lambda: kb.set_regions(0, [min(1, int(v)) for v in rec["regions"][:kb.led_count]])))
            for r in (0, 1):
                steps.append(("混合灯效列表 %d" % r,
                              lambda r=r: kb.set_effect_list(r, [it for it in rec["effect_lists"][r]
                                                                 if int(it.get("effect", 0)) < hid.EFFECT_MIXED])))
        eff = min(hid.EFFECT_MIXED, int(light["effect"]))
        if not hidden_only:
            # 切换灯效时固件会从 EEPROM 重新载入颜色/速度，所以只需还原灯效本身
            steps.append(("灯效", lambda: kb.set_effect(eff)))
            if keep_off and eff != 0:
                steps.append(("保持关灯", lambda: kb.set_effect(0)))
        ok = True
        for name, fn in steps:
            try:
                fn()
            except hid.HidError:
                raise
            except (hid.UnsupportedCommand, hid.ForbiddenCommand) as e:
                ok = False
                log.warning("恢复%s失败：%s", name, e)
        self.owned = False
        self.invalidate()
        if mark_inactive and ok:
            rec["active"] = False
            self._save_original(rec)
        log.info("已恢复接管前的%s（原灯效 %s%s）%s", "逐键颜色与混合设置" if hidden_only else "灯光", light["effect"],
                 "，保持关灯" if keep_off else "", "" if ok else "，部分项目失败")
        return ok

    # ---------- 设备 ----------
    def invalidate(self):
        self.sent_colors = {}
        self.sent_regions = {}
        self.sent_r1 = None

    def disconnect(self, reason):
        now = time.time()
        quiet = now < self.wake_grace_until
        if self.kb is not None:
            if quiet:
                log.info("刚从休眠唤醒，键盘暂未响应（%s），稍后重连", reason)
            else:
                log.warning("键盘连接断开：%s", reason)
            self.kb.close()
        self.kb = None
        self.owned = False
        self.invalidate()
        # 连续失败（例如系统处于休眠/后台唤醒、USB 被挂起）时逐步拉长重连间隔：1.5、3、6 … 最长 30 秒
        self.fail_streak = min(self.fail_streak + 1, 6)
        self.next_connect = now + min(30.0, 1.5 * (2 ** (self.fail_streak - 1)))
        self.last_error = reason
        self.stats["sleep_errors" if quiet else "errors"] = self.stats.get("sleep_errors" if quiet else "errors", 0) + 1

    def on_wake(self, now, slept):
        log.info("系统休眠约 %d 秒后唤醒：%d 秒内暂停网络判断，键盘稍后重新同步", slept, 30)
        self.wake_grace_until = now + 30.0
        if self.prober is not None:
            self.prober.take()                         # 休眠前后的探测结果作废
        p = self.engine.proxy
        if p.get("first_fail"):
            p["first_fail"] = now                      # 断开计时从唤醒时重新算，避免唤醒瞬间直接判为“持续断开”
        self.invalidate()
        self.resync = True
        self.next_health = min(self.next_health, now + 2.0)

    def identify(self):
        kb = self.kb
        info = {"pid": kb.pid, "model": (hid.MODELS.get(kb.pid) or ("?",))[0]}
        info.update({k.lower(): v for k, v in (kb.info or {}).items()})
        info["serial"] = (kb.info or {}).get("SerialNumber") or ""
        info["via_protocol"] = kb.via_protocol()
        try:
            info["firmware"] = kb.firmware_version()
        except hid.UnsupportedCommand:
            info["firmware"] = None
        try:
            info["features"] = kb.features()
        except hid.UnsupportedCommand:
            info["features"] = None
        try:
            info["rgb_protocol"] = kb.rgb_protocol()
            n = kb.get_led_count()
            if n != kb.led_count:
                log.warning("固件报告 LED 数 %d，与型号表 %d 不同，以固件为准", n, kb.led_count)
                kb.led_count = n
            info["led_count"] = n
        except hid.UnsupportedCommand:
            self.unsupported = "固件不支持 Keychron 逐键 RGB 指令（0xA8），请在 Keychron Launcher 中升级固件"
            log.error(self.unsupported)
            self.devinfo = info
            return False
        try:
            led_map = kb.led_map()
            info["led_map_source"] = "firmware(A8 06)"
        except hid.UnsupportedCommand:
            led_map = hid.STATIC_LED_MAP.get(kb.pid) or hid.STATIC_LED_MAP[0x0B50]
            info["led_map_source"] = "static"
        try:
            mi = kb.mixed_info()
            self.mixed_ok = mi.get("layers", 0) >= 2 and mi.get("effects_per_layer", 0) >= 1
            info["mixed"] = mi
        except hid.UnsupportedCommand:
            self.mixed_ok = False
        self.layout = build_layout(led_map, self.cfg, kb.led_count)
        info["layout"] = self.layout
        self.devinfo = info
        self.unsupported = None
        log.info("键盘：%s 固件 %s VIA v%s RGB 协议 %s LED %d 顶排 %s 空格 %s P %s 混合灯效 %s",
                 info["model"], info.get("firmware"), info["via_protocol"], info.get("rgb_protocol"),
                 kb.led_count, self.layout["top"], self.layout["space"], self.layout["proxy"],
                 "支持" if self.mixed_ok else "不支持")
        return True

    def ensure_device(self, now):
        if self.kb is not None or now < self.next_connect:
            return
        try:
            self.kb = hid.open_keyboard(self.cfg["device"]["pids"])
        except Exception as e:
            if not self.connect_fail_logged:
                log.info("等待键盘连接（USB 有线）：%s", e)
                self.connect_fail_logged = True
            self.next_connect = now + 3.0
            return
        self.connect_fail_logged = False
        self.stats["connects"] += 1
        self.invalidate()
        try:
            if not self.identify():
                self.next_health = now + 60
                return
            self.next_health = 0.0
            self.health_check(now)
        except (hid.HidError, hid.ForbiddenCommand) as e:
            self.disconnect("初始化失败：%s" % e)

    def takeover(self, now):
        kb = self.kb
        self.record_original()
        if kb.get_per_key_type() != 0:
            kb.set_per_key_type(0)
        use_mixed = self.mixed_ok and self.cfg["breathing"].get("mode", "auto") != "saturation"
        speed = int(self.original["lighting"]["speed"]) if self.original else 127
        if use_mixed:
            try:
                kb.set_effect_list(0, [{"effect": hid.EFFECT_PER_KEY, "hue": 0, "sat": 0, "speed": speed, "time": 0}])
                w = self.cfg["colors"]["waiting"]
                kb.set_effect_list(1, [{"effect": hid.EFFECT_BREATHING, "hue": int(w[0]), "sat": int(w[1]),
                                        "speed": int(self.cfg["breathing"]["speed"]), "time": 0}])
                self.sent_r1 = (hid.EFFECT_BREATHING, int(w[0]), int(w[1]))
            except (hid.UnsupportedCommand, hid.ForbiddenCommand) as e:
                log.warning("混合灯效设置失败（%s），改用 Per Key RGB + 颜色呼吸", e)
                self.mixed_ok = False
                use_mixed = False
        self.breath_region_ok = use_mixed
        self.render(now, force=True)          # 先写颜色和区域，再切换灯效，避免闪一下默认颜色
        target = hid.EFFECT_MIXED if use_mixed else hid.EFFECT_PER_KEY
        kb.set_effect(target)
        eff = kb.get_effect()
        if eff != target:
            raise hid.HidError("切换灯效失败：期望 %d 实际 %d" % (target, eff))
        self.mode_effect = target
        self.owned = True
        self.next_full = now + float(self.cfg["timing"]["full_refresh_minutes"]) * 60
        log.info("已接管灯光（%s）", "Mix RGB：区域 0 逐键 + 区域 1 呼吸/进度条高光" if use_mixed else "Per Key RGB")

    def health_check(self, now):
        kb = self.kb
        interval = float(self.cfg["timing"]["health_check_seconds"])
        if self.opted_out and self.opted_out.get("kind") == "off":
            interval = min(interval, 3.0)              # 关灯让出时更快发现你重新开灯
        self.next_health = now + interval
        if self.unsupported:
            return
        eff = kb.get_effect()
        self.fail_streak = 0                           # 键盘有回应：重连间隔复位
        if self.restore_pending and self.opted_out and self.opted_out.get("kind") == "manual":
            self.restore_original()
            self.restore_pending = False
            return
        if self.owned:
            if eff == self.mode_effect:
                if self.resync:                        # 其他程序刚用过键盘：把灯效列表、逐键类型等整体重设
                    self.resync = False
                    self.takeover(now)
                return
            self.owned = False
            self.invalidate()
            self.engine.dirty = True
            if eff == 0:
                # 你关了灯（F5 调到最低 / Fn+Tab）。固件此时已把“Mix RGB + 关”写进存储；
                # 把内存中的模式改回原灯效并保持关闭，下次开灯时固件保存的就是你的原灯效。
                self.opted_out = {"kind": "off", "since": now, "reason": "键盘背光被关闭，服务让出；重新开灯后自动恢复"}
                log.info(self.opted_out["reason"])
                self.restore_original(keep_off=True, mark_inactive=False)
                return
            if eff == hid.EFFECT_PER_KEY and self.mode_effect == hid.EFFECT_MIXED:
                log.info("灯效被切到 Per Key RGB，继续由状态灯接管")
                self.takeover(now)
                return
            self.opted_out = {"kind": "effect", "since": now, "effect": eff,
                              "reason": "灯效被切换为 %d（键盘 Fn 键或 Launcher），服务让出控制；切回 Per Key RGB / Mix RGB "
                                        "或执行 resume 即恢复" % eff}
            log.info(self.opted_out["reason"])
            self.restore_original(hidden_only=True)    # 还原你的逐键颜色与混合设置，不动你刚选的灯效
            return
        if self.opted_out:
            kind = self.opted_out.get("kind")
            if kind == "off" and eff != 0:
                log.info("检测到背光重新打开（灯效 %d），恢复接管", eff)
            elif kind == "effect" and eff in (hid.EFFECT_PER_KEY, hid.EFFECT_MIXED):
                log.info("检测到灯效切回 %d，恢复接管", eff)
            else:
                return
            self.opted_out = None
            self.engine.dirty = True
        if eff == 0:
            # 背光是关着的：尊重你的选择，不去点亮；开灯后再接管
            self.opted_out = {"kind": "off", "since": now, "reason": "键盘背光处于关闭状态，开灯后自动接管"}
            log.info(self.opted_out["reason"])
            self.engine.dirty = True
            self.next_health = now + 3.0
            return
        self.takeover(now)

    def render(self, now, force=False):
        kb = self.kb
        if self.layout is None:
            return now + 1.0
        if not force and now >= self.next_full:
            force = True
            self.next_full = now + float(self.cfg["timing"]["full_refresh_minutes"]) * 60
        colors, r1, r1c, nxt = self.engine.frame(now, self.layout, self.breath_region_ok)
        base = tuple(int(x) for x in self.cfg["colors"]["base"][:2])
        n = kb.led_count
        desired = [colors.get(i, base) for i in range(n)]
        fx_change = self.breath_region_ok and r1c is not None and r1c != self.sent_r1

        def send_fx():
            kb.set_effect_list(1, [{"effect": r1c[0], "hue": r1c[1], "sat": r1c[2],
                                    "speed": int(self.cfg["breathing"]["speed"]), "time": 0}])
            self.sent_r1 = r1c

        # 切到纯色高光时先换灯效（新加入区域 1 的灯直接显示高光色）；切到呼吸时最后换（避免高光闪成呼吸色）
        if fx_change and r1c[0] == hid.EFFECT_SOLID:
            send_fx()
        changed = [i for i in range(n) if force or self.sent_colors.get(i) != desired[i]]
        for start, vals in runs(changed, desired, 9):
            kb.set_colors(start, [(h, s, 255) for h, s in vals])
            for j, v in enumerate(vals):
                self.sent_colors[start + j] = v
        rch = []
        if self.breath_region_ok:
            want = [1 if i in r1 else 0 for i in range(n)]
            rch = [i for i in range(n) if force or self.sent_regions.get(i) != want[i]]
            for start, vals in runs(rch, want, 28):
                kb.set_regions(start, vals)
                for j, v in enumerate(vals):
                    self.sent_regions[start + j] = v
        if fx_change and r1c[0] != hid.EFFECT_SOLID:
            send_fx()
        if changed or rch:
            self.stats["frames"] += 1
        self.stats["hid_commands"] = kb.tx
        return nxt

    # ---------- 事件来源 ----------
    def ignored_session(self, rec):
        """Codex App 会在后台跑一些看不见的会话（记忆整理、应用列表等），它们也会触发 hook，
        但没有会话记录文件；这些不是你的任务，不显示。"""
        cc = self.cfg["codex"]
        sid = rec.get("session_id")
        if sid and sid in self.ignored:
            return True
        reason = None
        cwd = rec.get("cwd") or ""
        for pre in cc.get("ignore_cwd_prefixes") or []:
            pre = os.path.expanduser(pre).rstrip("/")
            if pre and (cwd == pre or cwd.startswith(pre + "/")):
                reason = "工作目录 %s" % cwd
        if reason is None and cc.get("ignore_ephemeral", True) and not rec.get("transcript_path") and \
                rec.get("event") not in ("SessionEnd", "SubagentStart", "SubagentStop"):
            if sid and any(t["session"] == sid for t in self.engine.tasks.values()):
                return False        # 已经在显示的会话：个别事件缺少字段时照常处理，不把整个会话当成后台会话
            reason = "没有会话记录"
        if reason and sid:
            self.ignored[sid] = time.time()
            if len(self.ignored) > 500:
                for k in sorted(self.ignored, key=self.ignored.get)[:250]:
                    del self.ignored[k]
            log.info("忽略 Codex 后台会话 %s（%s）", sid, reason)
        return bool(reason)

    def handle_record(self, rec, now):
        self.stats["events"] += 1
        if rec.get("event") == "_control":
            self.control(rec, now)
            return
        if rec.get("agent_source") == "claude":
            for ev in hook_events(rec, self.cfg):
                self.engine.handle(ev, now)
            return
        if self.ignored_session(rec):
            self.stats["ignored_events"] = self.stats.get("ignored_events", 0) + 1
            return
        if rec.get("session_id") and self.rollouts.thread_kind(rec.get("session_id")) == "subagent":
            # 子代理会话里的普通活动只用于刷新，不创建新任务
            for ev in hook_events(rec, self.cfg):
                if ev["type"] == "activity":
                    ev["create"] = False
                self.engine.handle(ev, now)
            return
        for ev in hook_events(rec, self.cfg):
            self.engine.handle(ev, now)

    def control(self, rec, now):
        act = rec.get("action")
        log.info("收到控制命令：%s", act)
        if act in ("pause", "restore"):
            self.opted_out = {"kind": "manual", "since": now, "reason": "手动暂停（statuslight.py %s）" % act}
            if self.kb is not None and self.owned:
                try:
                    self.restore_original()
                except hid.HidError as e:
                    self.restore_pending = True          # 重连后再还原
                    self.disconnect("暂停时还原失败：%s" % e)
                finally:
                    self.owned = False
            elif self.kb is None:
                self.restore_pending = True
        elif act == "resume":
            self.opted_out = None
            self.next_health = 0.0
        elif act == "proxy_override":
            self.engine.proxy_override = (rec.get("state", "ok"), now + float(rec.get("seconds", 5)))
        elif act == "reload":
            self.reload_cfg = True
        self.engine.dirty = True

    def read_spool(self, now):
        for item in list(self.old_spools):              # 先读完轮转后的旧文件（晚到的 hook 写入）
            item[1], _ = self._consume_spool(item[0], item[1], now)
            if now >= item[2]:
                os.close(item[0])
                self.old_spools.remove(item)
        try:
            st = os.stat(SPOOL)
        except OSError:
            st = None
        if self.spool_fd is None:
            if st is None:
                return
            fd = os.open(SPOOL, os.O_RDONLY)
            fst = os.fstat(fd)
            off = 0
            if self.saved_spool.get("ino") == fst.st_ino and 0 <= int(self.saved_spool.get("off") or 0) <= fst.st_size:
                off = int(self.saved_spool["off"])
            self.saved_spool = {}
            os.lseek(fd, off, os.SEEK_SET)
            self.spool_fd, self.spool_ino, self.spool_off, self.spool_buf = fd, fst.st_ino, off, b""
        self.spool_buf, n = self._consume_spool(self.spool_fd, self.spool_buf, now)
        self.spool_off += n
        rotate = st is not None and st.st_ino != self.spool_ino
        if not rotate and st is not None and self.spool_off > 1024 * 1024 and self.spool_off >= st.st_size:
            try:
                os.rename(SPOOL, SPOOL + ".old")
                rotate = True
            except OSError:
                pass
        if rotate or st is None:
            # 旧文件再保留几秒：轮转瞬间刚打开旧文件的 hook 进程仍可能写入
            self.old_spools.append([self.spool_fd, self.spool_buf, now + 5.0])
            self.spool_fd = None
            self.spool_ino = None
            self.spool_off = 0
            self.spool_buf = b""

    def _consume_spool(self, fd, buf, now):
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        if not chunks:
            return buf, 0
        data = b"".join(chunks)
        lines = (buf + data).split(b"\n")
        rest = lines.pop()
        self._handle_lines(lines, now)
        return rest, len(data)

    def _handle_lines(self, lines, now):
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            try:
                self.handle_record(rec, now)
            except Exception:
                log.exception("处理事件失败：%r", line[:200])

    def poll_rollouts(self, now, recovering=False):
        if not self.cfg["codex"].get("watch_rollouts", True):
            return
        extra = [(t.get("rollout"), t["session"]) for t in self.engine.tasks.values()
                 if t["kind"] == "turn" and t.get("rollout")]
        alive = bool(self.engine.codex_pids) if self.engine.codex_pids is not None else True
        try:
            self.rollouts.poll(now, extra, lambda ev: self.engine.handle(ev, now), recovering, alive,
                               float(self.cfg["codex"].get("rollout_window_hours", 6)))
        except Exception:
            log.exception("监视 Codex 会话记录失败")

    def poll_schedule(self):
        autos = read_automations(CODEX_HOME)
        active = [a["name"] or a["id"] for a in autos if a["status"] == "ACTIVE"]
        paused = [a["name"] or a["id"] for a in autos if a["status"] != "ACTIVE"]
        new = {"active": active, "paused": paused}
        if new != self.engine.schedule:
            log.info("计划任务：启用 %d 个 %s，未启用 %d 个", len(active), active, len(paused))
            self.engine.schedule = new
            self.engine.dirty = True

    def poll_processes(self, now):
        if not self.cfg["codex"].get("process_check", True):
            return
        pids = codex_pids()
        self.engine.check_processes(pids, now, silent=self.first_proc_check)
        self.first_proc_check = False

    def snapshot_mtimes(self):
        out = {}
        for name in ("statuslight.py", "q5he_hid.py", "config.json"):
            try:
                out[name] = os.stat(os.path.join(HERE, name)).st_mtime
            except OSError:
                out[name] = None
        return out

    def check_reload(self):
        cur = self.snapshot_mtimes()
        code_changed = any(cur[k] != self.mtimes.get(k) for k in ("statuslight.py", "q5he_hid.py"))
        cfg_changed = cur["config.json"] != self.mtimes.get("config.json")
        if code_changed:
            # 等文件稳定（两次检查之间没有再变化）再重启，避免读到写了一半的文件
            if getattr(self, "_pending_code", None) == cur:
                if self._code_compiles():
                    log.info("检测到程序文件更新，平滑重启服务")
                    self.reexec = True
                    self.stop_flag = True
                else:
                    log.error("新程序文件无法编译，继续运行旧版本")
                    self.mtimes = cur
                self._pending_code = None
            else:
                self._pending_code = cur
            return
        self._pending_code = None
        self.mtimes = cur
        if cfg_changed or self.reload_cfg:
            self.reload_cfg = False
            self.cfg = load_config()
            self.engine.cfg = self.cfg
            if self.kb is not None and self.layout is not None:
                try:
                    led_map = self.kb.led_map()
                except Exception:
                    led_map = hid.STATIC_LED_MAP.get(self.kb.pid) or hid.STATIC_LED_MAP[0x0B50]
                self.layout = build_layout(led_map, self.cfg, self.kb.led_count)
                self.invalidate()
            log.info("已重新加载 config.json")

    def _code_compiles(self):
        for name in ("statuslight.py", "q5he_hid.py"):
            try:
                with open(os.path.join(HERE, name), "rb") as f:
                    compile(f.read(), name, "exec")
            except Exception as e:
                log.error("%s 编译失败：%s", name, e)
                return False
        return True

    # ---------- 主循环 ----------
    def poke(self):
        try:
            os.write(self.wpipe, b"x")
        except Exception:
            pass

    def wait(self, timeout):
        fds = [x for x in (self.wake, self.rpipe) if x is not None]
        try:
            r, _, _ = select.select(fds, [], [], timeout)
        except (InterruptedError, OSError):
            return
        for x in r:
            try:
                if x is self.wake:
                    while True:
                        self.wake.recv(64)
                else:
                    os.read(self.rpipe, 4096)
            except (BlockingIOError, OSError):
                pass

    def loop_once(self):
        now = time.time()
        offset = now - time.monotonic()
        if self.clock_offset is not None and offset - self.clock_offset > 5.0:
            self.on_wake(now, offset - self.clock_offset)
        self.clock_offset = offset
        self.read_spool(now)
        if self.prober is not None:
            for ts, ok, detail in self.prober.take():
                if now < self.wake_grace_until:
                    continue                           # 刚唤醒时网络还没就绪，不算断开
                self.engine.proxy_result(ts, ok, detail)
        T = self.timers
        if now >= T["proc"]:
            self.poll_processes(now)
            T["proc"] = now + 15
        if now >= T["rollout"]:
            self.poll_rollouts(now)
            T["rollout"] = now + 2
        if now >= T["sched"]:
            self.poll_schedule()
            T["sched"] = now + 10
        if now >= T["reload"]:
            self.check_reload()
            T["reload"] = now + 5
            if self.stop_flag:
                return
        self.engine.tick(now)
        self.ensure_device(now)
        nxt = now + 1.0
        if self.kb is not None:
            try:
                self.kb.drain()
                yield_s = float(self.cfg["timing"]["foreign_yield_seconds"])
                if self.kb.foreign_ts and now - self.kb.foreign_ts < yield_s:
                    if not self.yielding:
                        log.info("检测到其他程序（如 Keychron Launcher）正在与键盘通信，暂停写入 %ds", int(yield_s))
                        self.yielding = True
                        self.engine.dirty = True
                else:
                    if self.yielding:
                        log.info("其他程序已安静 %ds，重新同步灯光", int(yield_s))
                        self.yielding = False
                        self.invalidate()
                        self.resync = True
                        self.next_health = 0.0
                    if now >= self.next_health:
                        self.health_check(now)
                    if self.owned:
                        nxt = min(nxt, self.render(now))
            except (hid.HidError, hid.ForbiddenCommand) as e:
                self.disconnect(str(e))
        if self.engine.dirty and now - self.last_save >= 2.0:
            self.save_state(now)
        self.wait(max(0.01, min(nxt, now + 1.0) - time.time()))

    def acquire_lock(self):
        self.lockf = open(LOCK_FILE, "a+")
        try:
            fcntl.flock(self.lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.lockf.seek(0)
        self.lockf.truncate()
        self.lockf.write(str(os.getpid()))
        self.lockf.flush()
        return True

    def run(self):
        ensure_dirs()
        if not self.acquire_lock():
            log.error("已有一个状态灯服务在运行，本进程退出")
            return 3
        try:
            os.unlink(WAKE_SOCK)
        except OSError:
            pass
        try:
            self.wake = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.wake.bind(WAKE_SOCK)
            os.chmod(WAKE_SOCK, 0o600)
            self.wake.setblocking(False)
        except OSError as e:
            # 唤醒套接字只是为了降低延迟；失败时退回每秒轮询事件文件
            log.warning("无法创建唤醒套接字（%s），改为每秒轮询事件", e)
            try:
                self.wake.close()
            except Exception:
                pass
            self.wake = None
        self.rpipe, self.wpipe = os.pipe()
        os.set_blocking(self.rpipe, False)
        os.set_blocking(self.wpipe, False)

        def on_term(signum, frame):
            self.stop_flag = True
            self.poke()

        def on_hup(signum, frame):
            self.reload_cfg = True
            self.poke()

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
        signal.signal(signal.SIGHUP, on_hup)
        self.load_state()
        self.mtimes = self.snapshot_mtimes()
        log.info("状态灯服务启动 v%s pid=%d python=%s", VERSION, os.getpid(), sys.version.split()[0])
        self.prober = Prober(lambda: self.cfg["proxy"], self.poke)
        self.prober.start()
        now = time.time()
        self.poll_processes(now)
        self.poll_schedule()
        self.poll_rollouts(now, recovering=True)
        self.timers["rollout"] = now + 2
        self.timers["sched"] = now + 10
        self.timers["proc"] = now + 15
        self.timers["reload"] = now + 5
        while not self.stop_flag:
            try:
                self.loop_once()
            except hid.HidError as e:
                self.disconnect("通信异常：%s" % e)
            except Exception as e:
                # 非通信类异常（例如程序缺陷）：记录并稍后重试，不反复断开重连键盘
                self.last_error = "主循环异常：%s" % e
                self.stats["errors"] += 1
                log.exception("主循环异常，2 秒后继续")
                self.invalidate()
                time.sleep(2)
        self.prober.stop_ev.set()
        try:
            if self.kb is not None and not self.reexec and self.owned:
                self.restore_original()
        except Exception as e:
            log.warning("退出时恢复原灯效失败：%s", e)
        finally:
            if self.kb is not None:
                self.kb.close()
                self.kb = None
            self.save_state()
        if self.wake is not None:
            try:
                self.wake.close()
                os.unlink(WAKE_SOCK)
            except OSError:
                pass
        if self.reexec:
            log.info("重新启动服务进程")
            logging.shutdown()
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
        log.info("状态灯服务退出")
        return 0


# ======================================================================
# 命令行
# ======================================================================
def daemon_running():
    try:
        f = open(LOCK_FILE, "a+")
    except OSError:
        return None
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        pid = f.read().strip()
        f.close()
        return int(pid) if pid.isdigit() else -1
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    f.close()
    return None


def append_event(rec):
    ensure_dirs()
    rec.setdefault("ts", time.time())
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(SPOOL, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.setblocking(False)
        s.sendto(b"!", WAKE_SOCK)
    except OSError:
        pass
    finally:
        s.close()


def cmd_status(args):
    st = load_json(STATE_FILE, {})
    pid = daemon_running()
    print("服务：%s" % ("运行中 pid=%s" % pid if pid else "未运行"))
    if not st:
        print("（还没有状态文件）")
        return 0
    age = time.time() - st.get("updated_at", 0)
    dev = st.get("device") or {}
    print("状态更新于：%.0f 秒前" % age)
    if dev.get("connected"):
        print("键盘：%s 固件 %s，LED %s，%s%s" % (
            dev.get("model"), dev.get("firmware"), dev.get("led_count"),
            "已接管（%s）" % ("Mix RGB" if dev.get("mixed") else "Per Key RGB") if dev.get("owned") else "未接管",
            "，暂时让出（其他程序在通信）" if dev.get("yielding") else ""))
    else:
        print("键盘：未连接")
    if dev.get("unsupported"):
        print("  ！%s" % dev["unsupported"])
    if st.get("opted_out"):
        print("让出/暂停：%s" % st["opted_out"].get("reason"))
    eng = st.get("engine") or {}
    sched = eng.get("schedule") or {}
    print("计划任务：启用 %d %s；未启用 %d" % (len(sched.get("active") or []), sched.get("active") or [],
                                       len(sched.get("paused") or [])))
    tasks = eng.get("tasks") or []
    print("任务：%d" % len(tasks))
    for t in sorted(tasks, key=lambda x: x.get("started", 0)):
        print("  - %-11s %s（%s，开始于 %s）" % (t.get("state"), t.get("key"), t.get("origin"),
                                           time.strftime("%H:%M:%S", time.localtime(t.get("started", 0)))))
    waits = eng.get("waits") or {}
    if waits:
        print("等待用户：%s" % ", ".join("%s(%s)" % (s, w.get("kind")) for s, w in waits.items()))
    p = eng.get("proxy") or {}
    print("代理：%s  %s" % (p.get("state"), p.get("detail")))
    if st.get("last_error"):
        print("最近错误：%s" % st["last_error"])
    print("统计：%s" % st.get("stats"))
    return 0


def _open_for_cli(cfg):
    kb = hid.open_keyboard(cfg["device"]["pids"])
    return kb


def cmd_probe(args):
    cfg = load_config()
    ensure_dirs()
    out = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "host": socket.gethostname(),
           "python": sys.version.split()[0], "platform": sys.platform}
    print("== 枚举 Keychron（VID 0x3434）所有 HID 接口（只读属性，不发送数据）==")
    ifaces = hid.enumerate_interfaces()
    out["interfaces"] = []
    for it in ifaces:
        d = dict(it)
        out["interfaces"].append(d)
        print("  PID 0x%04X  %s  %s  UsagePage 0x%04X Usage 0x%02X  in/out/feature=%s/%s/%s  bcd=0x%04X  %s" % (
            d.get("ProductID") or 0, d.get("Product"), d.get("Transport"), d.get("PrimaryUsagePage") or 0,
            d.get("PrimaryUsage") or 0, d.get("MaxInputReportSize"), d.get("MaxOutputReportSize"),
            d.get("MaxFeatureReportSize"), d.get("VersionNumber") or 0,
            json.dumps(d.get("ReportDescriptorParsed"), ensure_ascii=False)))
    if daemon_running():
        print("提示：状态灯服务正在运行，探测的只读查询会让它暂时让出 %ss" % cfg["timing"]["foreign_yield_seconds"])
    print("== 打开原始 HID 接口（Usage Page 0xFF60 / Usage 0x61）做只读协议查询 ==")
    try:
        kb = _open_for_cli(cfg)
    except Exception as e:
        out["raw_hid_error"] = str(e)
        save_json(os.path.join(STATE_DIR, "probe.json"), out)
        print("  打不开：%s" % e)
        return 2
    d = Daemon(cfg)
    d.kb = kb
    try:
        okk = d.identify()
        info = dict(d.devinfo)
        for key, fn in (("kc_protocol", kb.kc_protocol), ("lighting", kb.get_lighting)):
            try:
                info[key] = fn()
            except hid.UnsupportedCommand:
                info[key] = None
        info["per_key_type"] = None
        if okk:
            try:
                info["per_key_type"] = kb.get_per_key_type()
                info["led_map"] = kb.led_map()
            except hid.UnsupportedCommand:
                pass
        info["supported"] = bool(okk)
        out["keyboard"] = info
        lay = info.get("layout") or {}
        print("  型号 %s，固件 %s，VIA v%s，Keychron 协议 %s" % (info.get("model"), info.get("firmware"),
                                                       info.get("via_protocol"), info["kc_protocol"]))
        print("  逐键 RGB 指令：%s；LED %s 颗；混合灯效：%s" % (
            "支持（协议 %s）" % info.get("rgb_protocol") if okk else "不支持", info.get("led_count"),
            info.get("mixed") or "不支持"))
        print("  顶排 LED %s；空格 LED %s；P 键 LED %s（来源 %s）" % (lay.get("top"), lay.get("space"),
                                                           lay.get("proxy"), info.get("led_map_source")))
        print("  当前灯光 %s，逐键类型 %s" % (info["lighting"], info["per_key_type"]))
        if not okk:
            print("  ！%s" % d.unsupported)
    finally:
        kb.close()
    save_json(os.path.join(STATE_DIR, "probe.json"), out)
    print("已写入 %s" % os.path.join(STATE_DIR, "probe.json"))
    return 0 if out.get("keyboard", {}).get("supported") else 2


def cmd_selftest(args):
    cfg = load_config()
    ensure_dirs()
    if daemon_running() and not args.force:
        print("状态灯服务正在运行，请先停止服务（或加 --force）")
        return 2
    kb = _open_for_cli(cfg)
    d = Daemon(cfg)
    d.kb = kb
    res = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "cycles": args.cycles, "ok": False}
    try:
        if not d.identify():
            res["error"] = d.unsupported
            print(d.unsupported)
            return 2
        res["device"] = d.devinfo
        before = kb.get_lighting()
        res["lighting_before"] = before
        d.record_original()
        lay = d.layout
        print("1) 显示测试图案（只改 RAM）：左侧蓝色列、两条黄色进度条，空格紫色呼吸，P 键橙色 …")
        e = d.engine
        e.schedule = {"active": ["test1", "test2"], "paused": []}
        now = time.time()
        e.handle({"type": "turn_start", "session": "selftest-a", "turn": "t1", "ts": now}, now)
        e.handle({"type": "turn_start", "session": "selftest-b", "turn": "t2", "ts": now}, now)
        e.handle({"type": "wait", "session": "selftest-a", "kind": "approval", "ts": now}, now)
        e.proxy_override = ("brief", now + 3600)
        d.takeover(now)
        end = time.time() + 4
        while time.time() < end:
            nxt = d.render(time.time())
            time.sleep(max(0.01, min(nxt, time.time() + 0.2) - time.time()))
        print("2) 写入/回读稳定性测试 %d 次（随机键随机颜色）…" % args.cycles)
        errors, lat = 0, []
        rnd = random.Random(42)
        for i in range(args.cycles):
            led = rnd.randrange(kb.led_count)
            h, s = rnd.randrange(256), rnd.randrange(256)
            t0 = time.time()
            kb.set_colors(led, [(h, s, 255)])
            back = kb.get_colors(led, 1)[0]
            lat.append((time.time() - t0) * 1000)
            if back[0] != h or back[1] != s:
                errors += 1
        t0 = time.time()
        for _ in range(10):
            d.invalidate()
            d.render(time.time(), force=True)
        full_ms = (time.time() - t0) * 100
        res.update({"rw_errors": errors, "rw_avg_ms": round(sum(lat) / len(lat), 2) if lat else None,
                    "rw_max_ms": round(max(lat), 2) if lat else None, "full_frame_ms": round(full_ms, 1),
                    "foreign_reports": kb.foreign_count, "notifications": kb.notify_count})
        print("   回读错误 %d / %d，平均 %.2f ms，最大 %.2f ms；整帧刷新 %.1f ms" % (
            errors, args.cycles, res["rw_avg_ms"] or 0, res["rw_max_ms"] or 0, full_ms))
        print("3) 恢复测试前的灯效 …")
        d.owned = True
        d.restore_original()
        after = kb.get_lighting()
        res["lighting_after"] = after
        orig_eff = int((d.original or {}).get("lighting", {}).get("effect", before["effect"]))
        res["restored"] = after["effect"] == orig_eff
        if orig_eff != before["effect"]:
            res["note"] = "测试前灯效为 %d，按记录恢复为接管前的原灯效 %d" % (before["effect"], orig_eff)
        res["ok"] = errors == 0 and res["restored"]
        print("   恢复%s（灯效 %s → %s）" % ("成功" if res["restored"] else "失败", before["effect"], after["effect"]))
    except Exception as e:
        res["error"] = "%s: %s" % (e.__class__.__name__, e)
        print("自检出错：%s" % res["error"])
        try:
            d.restore_original()
        except Exception:
            pass
    finally:
        kb.close()
        save_json(os.path.join(STATE_DIR, "selftest.json"), res)
    print("自检%s，结果已写入 %s" % ("通过" if res["ok"] else "未通过", os.path.join(STATE_DIR, "selftest.json")))
    return 0 if res["ok"] else 1


def cmd_demo(args):
    if not daemon_running():
        print("服务没有运行，无法演示")
        return 2

    def hook(event, **kw):
        rec = {"event": event}
        rec.update(kw)
        append_event(rec)

    # 进度条按已运行时间增长；演示时把开始时间往前推，免得只看到最左边一两列
    steps = [
        ("思考中：黄色进度条从左往右填充 + 浅黄高光向右跑（假设任务已运行 90 秒）",
         lambda: hook("UserPromptSubmit", session_id="demo-a", turn_id="a1", transcript_path="demo",
                      ts=time.time() - 90), 6),
        ("并行：第二个任务加入，键盘平均分成两条进度条（假设已运行 25 秒）",
         lambda: hook("UserPromptSubmit", session_id="demo-b", turn_id="b1", transcript_path="demo",
                      ts=time.time() - 25), 6),
        ("等待批准：空格紫色呼吸（任务 A 条纹停止）",
         lambda: hook("PermissionRequest", session_id="demo-a", turn_id="a1", tool_name="shell",
                      transcript_path="demo"), 6),
        ("批准后继续", lambda: hook("PostToolUse", session_id="demo-a", turn_id="a1", tool_name="shell",
                                 transcript_path="demo"), 2),
        ("任务 A 完成：左段整段绿色常亮 5 秒", lambda: hook("Stop", session_id="demo-a", turn_id="a1",
                                                transcript_path="demo"), 6),
        ("任务 B 中断：右段整段红色闪烁 5 秒", lambda: hook("Interrupt", session_id="demo-b", turn_id="b1",
                                                transcript_path="demo"), 6),
        ("没有开始记录的完成：最右侧一列变绿",
         lambda: hook("Stop", session_id="demo-x", turn_id="x%d" % int(time.time()), transcript_path="demo"), 6),
        ("代理短暂断开：P 键橙色",
         lambda: append_event({"event": "_control", "action": "proxy_override", "state": "brief", "seconds": 4}), 4),
        ("代理持续断开：P 键红色呼吸",
         lambda: append_event({"event": "_control", "action": "proxy_override", "state": "down", "seconds": 6}), 6),
        ("代理刚恢复：P 键绿色",
         lambda: append_event({"event": "_control", "action": "proxy_override", "state": "recovered", "seconds": 4}), 4),
    ]
    for text, fn, wait_s in steps:
        print("· %s" % text)
        fn()
        time.sleep(wait_s)
    print("演示结束。最左侧蓝色列 = 当前已启用的计划任务。")
    return 0


def cmd_report(args):
    """把 probe.json / selftest.json / state.json 整理成 VERIFICATION.md（本机验证结果）。"""
    probe = load_json(os.path.join(STATE_DIR, "probe.json"), {})
    st = load_json(os.path.join(STATE_DIR, "selftest.json"), {})
    state = load_json(STATE_FILE, {})
    kb = probe.get("keyboard") or {}
    L = ["# 本机验证结果", "",
         "生成时间：%s（主机 %s，Python %s）" % (time.strftime("%Y-%m-%d %H:%M:%S"), probe.get("host"), probe.get("python")),
         "", "## 1. 设备识别（只读探测，%s）" % probe.get("time", "未运行"), ""]
    raw = None
    pids = set(DEFAULT_CONFIG["device"]["pids"])
    for it in probe.get("interfaces") or []:          # 同时插着别的 Keychron 键盘时，只取 Q5 HE 的原始 HID 接口
        if it.get("PrimaryUsagePage") == hid.RAW_USAGE_PAGE and it.get("PrimaryUsage") == hid.RAW_USAGE:
            if raw is None or (it.get("ProductID") in pids and raw.get("ProductID") not in pids):
                raw = it
    if probe.get("interfaces"):
        L += ["| PID | 产品 | 传输 | Usage Page / Usage | 输入/输出/特性报告(字节) | bcdDevice | 报告描述符解析 |",
              "|---|---|---|---|---|---|---|"]
        for it in probe["interfaces"]:
            L.append("| 0x%04X | %s | %s | 0x%04X / 0x%02X | %s / %s / %s | 0x%04X | %s |" % (
                it.get("ProductID") or 0, it.get("Product"), it.get("Transport"), it.get("PrimaryUsagePage") or 0,
                it.get("PrimaryUsage") or 0, it.get("MaxInputReportSize"), it.get("MaxOutputReportSize"),
                it.get("MaxFeatureReportSize"), it.get("VersionNumber") or 0,
                "; ".join("%s/%s: %s" % (c["usage_page"], c["usage"], ", ".join(
                    "ID%d in%d out%d feat%d" % (r["report_id"], r["input_bytes"], r["output_bytes"], r["feature_bytes"])
                    for r in c["reports"])) for c in (it.get("ReportDescriptorParsed") or []))))
        L.append("")
    if raw:
        L.append("原始 HID 接口：VID 0x%04X PID 0x%04X，Usage Page 0x%04X / Usage 0x%02X，输入 %s 字节、输出 %s 字节%s。" % (
            raw.get("VendorID") or 0, raw.get("ProductID") or 0, raw["PrimaryUsagePage"], raw["PrimaryUsage"],
            raw.get("MaxInputReportSize"), raw.get("MaxOutputReportSize"),
            "，无 Report ID" if all(r["report_id"] == 0 for c in raw.get("ReportDescriptorParsed") or []
                                    for r in c["reports"]) else ""))
    if kb:
        L += ["", "| 项目 | 结果 |", "|---|---|",
              "| 型号 | %s |" % kb.get("model"), "| 固件版本 | %s |" % kb.get("firmware"),
              "| VIA 协议 | v%s |" % kb.get("via_protocol"), "| Keychron 协议 | %s |" % kb.get("kc_protocol"),
              "| 功能位 | %s |" % kb.get("features"), "| Keychron RGB 协议 | %s |" % kb.get("rgb_protocol"),
              "| LED 数 | %s |" % kb.get("led_count"), "| 混合灯效 | %s |" % kb.get("mixed"),
              "| 键位→LED 来源 | %s |" % kb.get("led_map_source"),
              "| 顶排 / 空格 / P 键 LED | %s / %s / %s |" % ((kb.get("layout") or {}).get("top"),
                                                          (kb.get("layout") or {}).get("space"),
                                                          (kb.get("layout") or {}).get("proxy")),
              "| 探测时灯光 | %s，逐键类型 %s |" % (kb.get("lighting"), kb.get("per_key_type"))]
    elif probe.get("raw_hid_error"):
        L.append("打开原始 HID 接口失败：%s" % probe["raw_hid_error"])
    L += ["", "## 2. 灯光控制自检（只改 RAM，%s）" % st.get("time", "未运行"), ""]
    if st:
        L += ["| 项目 | 结果 |", "|---|---|",
              "| 结论 | %s |" % ("**通过**" if st.get("ok") else "未通过：%s" % st.get("error", "")),
              "| 写入/回读次数 | %s，错误 %s |" % (st.get("cycles"), st.get("rw_errors")),
              "| 单次写+读延迟 | 平均 %s ms，最大 %s ms |" % (st.get("rw_avg_ms"), st.get("rw_max_ms")),
              "| 整帧刷新（101 键颜色 + 区域） | %s ms |" % st.get("full_frame_ms"),
              "| 测试前 → 测试后灯效 | %s → %s（恢复%s） |" % ((st.get("lighting_before") or {}).get("effect"),
                                                      (st.get("lighting_after") or {}).get("effect"),
                                                      "成功" if st.get("restored") else "失败"),
              "| 外部通信 / 固件主动上报 | %s / %s |" % (st.get("foreign_reports"), st.get("notifications"))]
    L += ["", "## 3. 服务运行状态（%s）" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state["updated_at"]))
                                       if state.get("updated_at") else "未运行"), ""]
    if state:
        dev = state.get("device") or {}
        eng = state.get("engine") or {}
        L += ["| 项目 | 结果 |", "|---|---|",
              "| 服务进程 | %s |" % ("运行中 pid %s" % daemon_running() if daemon_running() else "未运行"),
              "| 键盘 | %s，%s |" % ("已连接" if dev.get("connected") else "未连接",
                                   ("已接管（%s）" % ("Mix RGB" if dev.get("mixed") else "Per Key RGB"))
                                   if dev.get("owned") else "未接管"),
              "| 计划任务 | 启用 %s |" % ((eng.get("schedule") or {}).get("active")),
              "| 代理 | %s %s |" % ((eng.get("proxy") or {}).get("state"), (eng.get("proxy") or {}).get("detail")),
              "| 统计 | %s |" % state.get("stats")]
    path = os.path.join(HERE, "VERIFICATION.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("已写入 %s" % path)
    return 0


def cmd_control(action):
    append_event({"event": "_control", "action": action})
    print("已发送：%s%s" % (action, "" if daemon_running() else "（服务未运行，将在启动后生效）"))
    return 0


def cmd_emit(args):
    rec = json.loads(args.json) if args.json else {}
    rec["event"] = args.event
    append_event(rec)
    print("已注入 %s" % json.dumps(rec, ensure_ascii=False))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Codex → Keychron Q5 HE 状态灯")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("probe")
    p = sub.add_parser("selftest")
    p.add_argument("--cycles", type=int, default=300)
    p.add_argument("--force", action="store_true")
    sub.add_parser("demo")
    sub.add_parser("report")
    for a in ("pause", "resume", "restore"):
        sub.add_parser(a)
    p = sub.add_parser("emit")
    p.add_argument("event")
    p.add_argument("json", nargs="?")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        setup_logging(to_stderr=False)
        return Daemon(load_config()).run()
    setup_logging(to_stderr=False)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "probe":
        return cmd_probe(args)
    if args.cmd == "selftest":
        return cmd_selftest(args)
    if args.cmd == "demo":
        return cmd_demo(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd in ("pause", "resume", "restore"):
        return cmd_control(args.cmd)
    if args.cmd == "emit":
        return cmd_emit(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
