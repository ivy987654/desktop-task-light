"""在 ~/.codex/hooks.json 中加入 / 移除 Q5 HE 状态灯的 hook（只动带有 q5he-statuslight 标记的条目）。

用法：python3 install_hooks.py install <python 路径>
      python3 install_hooks.py remove
修改前会把原文件备份为 hooks.json.bak-q5he-<时间>。新增的 hook 需要在 Codex 里用 /hooks 信任一次后才会运行。
"""
import json
import os
import shlex
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
HOOKS = os.path.join(CODEX_HOME, "hooks.json")
MARK = "--q5he-statuslight"                       # 固定标记参数（不依赖目录名），hook_relay.py 忽略它
LEGACY_MARK = "q5he-statuslight/hook_relay.py"

EVENTS = [
    ("UserPromptSubmit", None, 3),
    ("PreToolUse", "^request_user_input$", 3),
    ("PermissionRequest", None, 3),
    ("PostToolUse", None, 3),
    ("Stop", None, 3),
    ("Interrupt", None, 2),
    ("SubagentStart", None, 3),
    ("SubagentStop", None, 3),
    ("SessionEnd", None, 2),
]


def load():
    if not os.path.exists(HOOKS):
        return {}
    with open(HOOKS, encoding="utf-8") as f:
        return json.load(f)


def strip(doc):
    hooks = doc.get("hooks") or {}
    for ev in list(hooks):
        groups = []
        for g in hooks[ev] or []:
            hs = [h for h in (g.get("hooks") or [])
                  if MARK not in (h.get("command") or "") and LEGACY_MARK not in (h.get("command") or "")]
            if hs:
                g = dict(g, hooks=hs)
                groups.append(g)
            elif not (g.get("hooks") or []):
                groups.append(g)
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    doc["hooks"] = hooks
    return doc


def save(doc):
    if os.path.exists(HOOKS):
        bak = "%s.bak-q5he-%s" % (HOOKS, time.strftime("%Y%m%d-%H%M%S"))
        with open(HOOKS, "rb") as f, open(bak, "wb") as g:
            g.write(f.read())
        print("已备份原 hooks.json → %s" % bak)
    os.makedirs(os.path.dirname(HOOKS), exist_ok=True)
    tmp = HOOKS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, HOOKS)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "remove"):
        print(__doc__)
        return 2
    doc = strip(load())
    if sys.argv[1] == "install":
        py = sys.argv[2] if len(sys.argv) > 2 else "/usr/bin/python3"
        relay = os.path.join(HERE, "hook_relay.py")
        for ev, matcher, timeout in EVENTS:
            group = {"hooks": [{"type": "command",
                                "command": "%s -S -E %s %s %s" % (shlex.quote(py), shlex.quote(relay), ev, MARK),
                                "timeout": timeout}]}
            if matcher:
                group["matcher"] = matcher
            doc["hooks"].setdefault(ev, []).append(group)
        save(doc)
        print("已在 %s 写入 %d 个状态灯 hook（%s）" % (HOOKS, len(EVENTS), ", ".join(e[0] for e in EVENTS)))
    else:
        if not doc["hooks"]:
            doc.pop("hooks")
        if not os.path.exists(HOOKS):
            print("没有 %s，无需移除" % HOOKS)
            return 0
        save(doc)
        if not doc:                                # 只剩空对象：说明文件是安装时新建的，直接删除
            os.remove(HOOKS)
            print("已删除只含状态灯 hook 的 %s（备份保留）" % HOOKS)
        else:
            print("已从 %s 移除状态灯 hook" % HOOKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
