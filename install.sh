#!/bin/bash
# Codex → Keychron Q5 HE 状态灯：安装脚本（可重复运行）
#   1. 只读探测键盘 HID 接口与协议
#   2. 灯光自检 + 稳定性测试（只改键盘 RAM，不写 EEPROM），不通过则不安装
#   3. 安装 LaunchAgent（开机/登录自动启动、崩溃自动重启）
#   4. 在 ~/.codex/hooks.json 加入 hook（需在 Codex 里 /hooks 信任一次）
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.q5he-statuslight"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
export CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
GUI="gui/$(id -u)"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ "$(uname)" = "Darwin" ] || { echo "只支持 macOS"; exit 1; }

PY=""
if xcode-select -p >/dev/null 2>&1 && [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; fi
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -z "${PY}" ] && [ -x "${c}" ]; then PY="${c}"; fi
done
if [ -z "${PY}" ]; then
  echo "没有可用的 python3。请先运行：xcode-select --install"
  exit 1
fi
"${PY}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "需要 Python 3.8 及以上：${PY}"; exit 1; }
echo "使用 Python：${PY}（$("${PY}" -c 'import sys; print(sys.version.split()[0])')）"

mkdir -p "${DIR}/run" "${DIR}/state" "${DIR}/logs"
chmod 700 "${DIR}/run" "${DIR}/state" "${DIR}/logs"

if launchctl print "${GUI}/${LABEL}" >/dev/null 2>&1; then
  bold "停止已在运行的旧版服务（会自动恢复原灯效）"
  launchctl bootout "${GUI}/${LABEL}" 2>/dev/null || true
  sleep 1
fi

bold "① 探测键盘（只读，不改任何设置）"
"${PY}" "${DIR}/statuslight.py" probe
rc=$?
if [ ${rc} -ne 0 ]; then
  echo
  echo "探测没有通过（${rc}）。请确认 Q5 HE 用 USB 线直连这台 Mac，然后重新运行本脚本。"
  echo "详情见 ${DIR}/state/probe.json"
  exit 2
fi

bold "② 灯光自检（只改键盘 RAM，不写 EEPROM；约 10 秒，键盘会变色闪烁）"
"${PY}" "${DIR}/statuslight.py" selftest --cycles 300
rc=$?
if [ ${rc} -ne 0 ]; then
  echo
  echo "自检没有通过，服务未安装。详情见 ${DIR}/state/selftest.json 与 ${DIR}/logs/statuslight.log"
  exit 3
fi

bold "③ 安装后台服务（LaunchAgent：${LABEL}）"
mkdir -p "${HOME}/Library/LaunchAgents"
cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${DIR}/statuslight.py</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>${DIR}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>5</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CODEX_HOME</key><string>${CODEX_HOME}</string>
    <key>PYTHONDONTWRITEBYTECODE</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>${DIR}/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>${DIR}/logs/launchd.err.log</string>
</dict>
</plist>
EOF
plutil -lint "${PLIST}" >/dev/null || { echo "plist 校验失败"; exit 4; }
launchctl bootstrap "${GUI}" "${PLIST}" 2>/dev/null || true
launchctl enable "${GUI}/${LABEL}"
launchctl kickstart -k "${GUI}/${LABEL}"

bold "④ 写入 Codex hooks（${CODEX_HOME}/hooks.json）"
"${PY}" "${DIR}/install_hooks.py" install "${PY}" || { echo "写入 hooks.json 失败"; exit 5; }

sleep 3
bold "当前状态"
"${PY}" "${DIR}/statuslight.py" status
"${PY}" "${DIR}/statuslight.py" report >/dev/null && echo "本机验证结果已写入 ${DIR}/VERIFICATION.md"

cat <<EOF

安装完成。还差最后一步：
  在 Codex 里输入 /hooks，把 9 个带 q5he-statuslight/hook_relay.py 的 hook 设为信任（Trust）。
  （Codex App 若没有自动加载，重启一次 Codex。没信任前，服务仍会从会话记录里识别开始/完成/中断，
   但“等待批准”的空格紫色呼吸需要 hook。）

常用命令：
  ${PY} ${DIR}/statuslight.py status     查看状态
  ${PY} ${DIR}/statuslight.py demo       演示全部灯效
  ${PY} ${DIR}/statuslight.py pause      暂停并恢复原灯效（resume 恢复）
  bash ${DIR}/uninstall.sh             卸载
日志：${DIR}/logs/statuslight.log
EOF
