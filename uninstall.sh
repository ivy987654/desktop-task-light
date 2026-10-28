#!/bin/bash
# 卸载 Codex → Q5 HE 状态灯：停止服务（服务退出时自动恢复原灯效）、移除 LaunchAgent 与 hooks。
# 程序文件、日志与状态保留在本目录，可手动删除。
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.q5he-statuslight"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
export CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
PY=/usr/bin/python3
[ -x "${PY}" ] || PY="$(command -v python3)"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null && echo "已停止服务（原灯效已恢复）"
rm -f "${PLIST}" && echo "已删除 ${PLIST}"
"${PY}" "${DIR}/install_hooks.py" remove
echo "卸载完成。如需彻底删除：rm -rf \"${DIR}\""
