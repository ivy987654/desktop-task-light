"""Keychron Q5 HE 原始 HID 访问层（macOS IOKit + ctypes，仅用 Python 标准库）。

协议依据（只读参考，未复制代码）：
  Keychron/qmk_firmware 分支 2025q3，提交 c5d998442710（2026-09-30），GPL-2.0-or-later
    keyboards/keychron/common/keychron_raw_hid.c   0xA0–0xA3 / 0xA7 / 0xA8 分发
    keyboards/keychron/common/rgb/keychron_rgb.c   0xA8 子命令（逐键 RGB / 混合灯效）
    keyboards/keychron/common/state_notify.c       固件主动上报（0xA3 层切换等）
    quantum/via.c                                  VIA 协议 v12，channel 3 = RGB Matrix
  原始 HID 接口：Usage Page 0xFF60 / Usage 0x61，无 Report ID，输入/输出报告各 32 字节。

安全设计：
  * 命令白名单：只允许只读查询和“只改 RAM”的设置命令；VIA 保存(0x09)、EEPROM 重置(0x0A)、
    进入 bootloader(0x0B)、Keychron RGB 保存(A8 02)、指示灯配置(A8 04)、恢复出厂(A7 11)等一律拒绝。
  * 越界保护：固件的 A8 0A/09 不检查 start+count，越界会改写固件内存；这里强制 start+count <= LED 数。
  * HID 访问使用 macOS IOKit 与 Python ctypes。
"""
import ctypes as C
import ctypes.util
import struct
import sys
import time

VID = 0x3434
RAW_USAGE_PAGE = 0xFF60
RAW_USAGE = 0x61
REPORT_LEN = 32

# PID -> (型号, LED 数, 矩阵行, 矩阵列)；数据来自 q5_he/via_json 与各布局 config.h
MODELS = {
    0x0B50: ("Q5 HE ANSI Knob", 101, 6, 19),
    0x0B51: ("Q5 HE ISO Knob", 102, 6, 19),
    0x0B52: ("Q5 HE JIS Knob", 105, 6, 19),
}

# 固件源码中的矩阵->LED 表（A8 06 不可用时的兜底），0xFF = 无灯
_N = 0xFF
STATIC_LED_MAP = {
    0x0B50: [
        [0, _N, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, _N],
        [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35],
        [36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54],
        [55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, _N, 69, 70, 71, _N],
        [72, _N, 73, 74, 75, 76, 77, 78, 79, 80, 81, _N, 82, 83, 84, 85, 86, 87, 88],
        [89, 90, 91, _N, _N, _N, 92, _N, _N, 93, 94, 95, 96, 97, 98, _N, 99, 100, _N],
    ],
    0x0B51: [
        [0, _N, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, _N],
        [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35],
        [36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54],
        [55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, _N, 69, 70, 71, _N],
        [72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, _N, 83, 84, 85, 86, 87, 88, 89],
        [90, 91, 92, _N, _N, _N, 93, _N, _N, 94, 95, 96, 97, 98, 99, _N, 100, 101, _N],
    ],
    0x0B52: [
        [0, _N, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, _N],
        [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 32, 33, 34, 35, 36],
        [37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55],
        [56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 31, 70, 71, 72, _N],
        [73, _N, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90],
        [91, 92, 93, 94, _N, _N, 95, _N, 96, 97, 98, 99, 100, 101, 102, _N, 103, 104, _N],
    ],
}

# VIA RGB Matrix 灯效编号（q5_he_ansi_encoder.json）
EFFECT_OFF = 0
EFFECT_SOLID = 1
EFFECT_BREATHING = 2
EFFECT_PER_KEY = 23
EFFECT_MIXED = 24

# VIA channel 3 value id
VIA_BRIGHTNESS, VIA_EFFECT, VIA_SPEED, VIA_COLOR = 1, 2, 3, 4

# Keychron 0xA8 子命令
RGB_GET_PROTOCOL_VER = 0x01
RGB_SAVE = 0x02                 # 写 EEPROM —— 禁止
GET_INDICATORS_CONFIG = 0x03
SET_INDICATORS_CONFIG = 0x04    # 不需要，禁止
RGB_GET_LED_COUNT = 0x05
RGB_GET_LED_IDX = 0x06
PER_KEY_RGB_GET_TYPE = 0x07
PER_KEY_RGB_SET_TYPE = 0x08
PER_KEY_RGB_GET_COLOR = 0x09
PER_KEY_RGB_SET_COLOR = 0x0A
MIXED_EFFECT_RGB_GET_INFO = 0x0B
MIXED_EFFECT_RGB_GET_REGIONS = 0x0C
MIXED_EFFECT_RGB_SET_REGIONS = 0x0D
MIXED_EFFECT_RGB_GET_EFFECT_LIST = 0x0E
MIXED_EFFECT_RGB_SET_EFFECT_LIST = 0x0F

_A8_ALLOWED = {
    RGB_GET_PROTOCOL_VER, GET_INDICATORS_CONFIG, RGB_GET_LED_COUNT, RGB_GET_LED_IDX,
    PER_KEY_RGB_GET_TYPE, PER_KEY_RGB_SET_TYPE, PER_KEY_RGB_GET_COLOR, PER_KEY_RGB_SET_COLOR,
    MIXED_EFFECT_RGB_GET_INFO, MIXED_EFFECT_RGB_GET_REGIONS, MIXED_EFFECT_RGB_SET_REGIONS,
    MIXED_EFFECT_RGB_GET_EFFECT_LIST, MIXED_EFFECT_RGB_SET_EFFECT_LIST,
}


class HidError(Exception):
    """通信失败（设备断开、超时等），上层应关闭并重连。"""


class UnsupportedCommand(Exception):
    """固件不认识这条命令（回包首字节 0xFF）。"""


class ForbiddenCommand(Exception):
    """白名单外的命令，未发送。"""


def _check_allowed(req, led_count):
    """白名单 + 参数越界检查。req 为 32 字节请求。"""
    c0, c1 = req[0], req[1]
    if c0 == 0x01:                                   # VIA 协议版本（只读）
        return
    if c0 in (0x07, 0x08):                           # VIA custom set/get（RAM）
        if c1 != 0x03 or req[2] not in (VIA_BRIGHTNESS, VIA_EFFECT, VIA_SPEED, VIA_COLOR):
            raise ForbiddenCommand("VIA 0x%02X 只允许 channel 3 的亮度/灯效/速度/颜色" % c0)
        if c0 == 0x07 and req[2] == VIA_EFFECT and req[3] > EFFECT_MIXED:
            raise ForbiddenCommand("灯效编号越界: %d" % req[3])
        return
    if c0 in (0xA0, 0xA1, 0xA2):                     # Keychron 只读信息
        return
    if c0 != 0xA8 or c1 not in _A8_ALLOWED:
        raise ForbiddenCommand("命令不在白名单: %02X %02X" % (c0, c1))
    if c1 in (PER_KEY_RGB_GET_COLOR, PER_KEY_RGB_SET_COLOR):
        start, count = req[2], req[3]
        if count < 1 or count > 9 or (led_count and start + count > led_count):
            raise ForbiddenCommand("逐键颜色越界: start=%d count=%d led_count=%s" % (start, count, led_count))
    elif c1 == PER_KEY_RGB_SET_TYPE:
        if req[2] > 4:
            raise ForbiddenCommand("逐键类型越界: %d" % req[2])
    elif c1 == RGB_GET_LED_IDX:
        if req[2] > 5:
            raise ForbiddenCommand("矩阵行越界: %d" % req[2])
    elif c1 == MIXED_EFFECT_RGB_GET_REGIONS:
        start, count = req[2], req[3]
        if count < 1 or count > 29 or (led_count and start + count > led_count):
            raise ForbiddenCommand("区域读取越界")
    elif c1 == MIXED_EFFECT_RGB_SET_REGIONS:
        start, count = req[2], req[3]
        if count < 1 or count > 28 or (led_count and start + count > led_count):
            raise ForbiddenCommand("区域写入越界")
        if any(v > 1 for v in req[4:4 + count]):
            raise ForbiddenCommand("区域编号只能是 0/1")
    elif c1 in (MIXED_EFFECT_RGB_GET_EFFECT_LIST, MIXED_EFFECT_RGB_SET_EFFECT_LIST):
        region, start, count = req[2], req[3], req[4]
        # 固件检查写成了 region > EFFECT_LAYERS（差一），这里严格限制为 0/1
        if region > 1 or count < 1 or count > 3 or start + count > 5:
            raise ForbiddenCommand("混合灯效列表越界")
        if c1 == MIXED_EFFECT_RGB_SET_EFFECT_LIST:
            for i in range(count):
                if req[5 + i * 8] >= EFFECT_MIXED:
                    raise ForbiddenCommand("混合灯效里不能嵌套 Mix RGB")


def is_notification(rep):
    """固件主动上报（不是对任何请求的回复）：层切换、回报率、恢复出厂通知。"""
    if not rep:
        return True
    if rep[0] == 0xA3:
        return True
    if rep[0] == 0xA7 and len(rep) > 1 and rep[1] in (0x0D, 0x11):
        return True
    return False


def _reply_matches(req, rep):
    if len(rep) < 3:
        return False
    if rep[0] == 0xFF:                     # 未处理：VIA / Keychron 把 data[0] 改成 0xFF，data[1] 回显
        return rep[1] == req[1]
    if rep[0] != req[0]:
        return False
    if req[0] in (0x07, 0x08):
        return rep[1] == req[1] and rep[2] == req[2]
    if req[0] == 0xA8:
        if rep[1] != req[1]:
            return False
        sub = req[1]
        if sub == PER_KEY_RGB_SET_COLOR:   # 回包 data[3] 起保持请求的 count 与颜色
            n = req[3]
            return rep[3:4 + 3 * n] == req[3:4 + 3 * n]
        if sub == MIXED_EFFECT_RGB_SET_REGIONS:
            n = req[3]
            return rep[3:4 + n] == req[3:4 + n]
        if sub == MIXED_EFFECT_RGB_SET_EFFECT_LIST:
            n = req[4]
            return rep[3:5 + 8 * n] == req[3:5 + 8 * n]
        return True
    return True


class Keyboard(object):
    """Q5 HE 协议层。transport 需提供 send(bytes32) / poll(timeout)->list[bytes] / close()。"""

    def __init__(self, transport, pid=0x0B50, info=None):
        self.t = transport
        self.pid = pid
        self.info = info or {}
        model = MODELS.get(pid)
        self.led_count = model[1] if model else 0
        self.rows = model[2] if model else 6
        self.cols = model[3] if model else 19
        self.foreign_ts = 0.0          # 最近一次看到“别的程序”和键盘对话的时间
        self.foreign_count = 0
        self.notify_count = 0
        self.last_notification = None
        self.tx = 0

    # ---------- 底层 ----------
    def _classify(self, rep):
        if is_notification(rep):
            self.notify_count += 1
            self.last_notification = bytes(rep[:4])
        else:
            self.foreign_ts = time.time()
            self.foreign_count += 1

    def drain(self):
        """处理排队的输入报告（不发送任何东西）。"""
        for rep in self.t.poll(0.0):
            self._classify(rep)

    def cmd(self, *payload, **kw):
        timeout = kw.get("timeout", 0.35)
        pad = kw.get("pad", 0)
        req = bytearray([pad] * REPORT_LEN)
        req[:len(payload)] = bytes(payload)
        req = bytes(req)
        _check_allowed(req, self.led_count)
        self.t.send(req)
        self.tx += 1
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HidError("等待回包超时: %02X %02X" % (req[0], req[1]))
            for rep in self.t.poll(min(remaining, 0.05)):
                if _reply_matches(req, rep):
                    if rep[0] == 0xFF:
                        raise UnsupportedCommand("%02X %02X" % (req[0], req[1]))
                    if req[0] == 0xA8 and rep[2] != 0:
                        raise HidError("0xA8 子命令 0x%02X 执行失败" % req[1])
                    return rep
                self._classify(rep)

    # ---------- 信息 ----------
    def via_protocol(self):
        r = self.cmd(0x01)
        return (r[1] << 8) | r[2]

    def kc_protocol(self):
        r = self.cmd(0xA0)
        return {"protocol": r[1], "qmk_command_set": r[3]}

    def firmware_version(self):
        r = self.cmd(0xA1)
        raw = bytes(r[1:])
        return raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()

    def features(self):
        r = self.cmd(0xA2)
        byte0, byte1 = r[2], r[3]
        return {
            "raw": [byte0, byte1],
            "keychron_rgb": bool(byte0 & 0x80),
            "analog_matrix": bool(byte0 & 0x08),
            "bluetooth": bool(byte0 & 0x02),
            "p24g": bool(byte0 & 0x04),
            "state_notify": bool(byte0 & 0x10),
        }

    def rgb_protocol(self):
        r = self.cmd(0xA8, RGB_GET_PROTOCOL_VER)
        return r[3] | (r[4] << 8)

    def get_led_count(self):
        return self.cmd(0xA8, RGB_GET_LED_COUNT)[3]

    def led_row(self, row):
        # 掩码之后的字节填 0xFF：固件只覆盖 MATRIX_COLS 个位置，其余仍是请求值
        r = self.cmd(0xA8, RGB_GET_LED_IDX, row, 0xFF, 0xFF, 0xFF, pad=0xFF)
        return list(r[3:3 + self.cols])

    def led_map(self):
        return [self.led_row(r) for r in range(self.rows)]

    def mixed_info(self):
        r = self.cmd(0xA8, MIXED_EFFECT_RGB_GET_INFO)
        return {"layers": r[3], "effects_per_layer": r[4]}

    # ---------- VIA 整体灯光（只改 RAM） ----------
    def via_get(self, vid, n=1):
        r = self.cmd(0x08, 0x03, vid)
        return list(r[3:3 + n])

    def get_lighting(self):
        return {
            "brightness": self.via_get(VIA_BRIGHTNESS)[0],
            "effect": self.via_get(VIA_EFFECT)[0],
            "speed": self.via_get(VIA_SPEED)[0],
            "color": self.via_get(VIA_COLOR, 2),
        }

    def get_effect(self):
        return self.via_get(VIA_EFFECT)[0]

    def set_effect(self, effect):
        self.cmd(0x07, 0x03, VIA_EFFECT, effect)

    def set_speed(self, speed):
        self.cmd(0x07, 0x03, VIA_SPEED, speed)

    def set_color(self, h, s):
        self.cmd(0x07, 0x03, VIA_COLOR, h, s)

    def set_brightness(self, v):
        self.cmd(0x07, 0x03, VIA_BRIGHTNESS, v)

    # ---------- 逐键 ----------
    def get_per_key_type(self):
        return self.cmd(0xA8, PER_KEY_RGB_GET_TYPE)[3]

    def set_per_key_type(self, t):
        self.cmd(0xA8, PER_KEY_RGB_SET_TYPE, t)

    def get_colors(self, start, count):
        out = []
        while count > 0:
            n = min(9, count)
            r = self.cmd(0xA8, PER_KEY_RGB_GET_COLOR, start, n)
            for i in range(n):
                out.append((r[3 + i * 3], r[4 + i * 3], r[5 + i * 3]))
            start += n
            count -= n
        return out

    def set_colors(self, start, hsvs):
        """hsvs: [(h, s, v)...]，自动按 9 个一包拆分。"""
        i = 0
        while i < len(hsvs):
            chunk = hsvs[i:i + 9]
            payload = [0xA8, PER_KEY_RGB_SET_COLOR, start + i, len(chunk)]
            for h, s, v in chunk:
                payload += [h & 0xFF, s & 0xFF, v & 0xFF]
            self.cmd(*payload)
            i += len(chunk)

    # ---------- 混合灯效 ----------
    def get_regions(self, start=0, count=None):
        if count is None:
            count = self.led_count - start
        out = []
        while count > 0:
            n = min(28, count)
            r = self.cmd(0xA8, MIXED_EFFECT_RGB_GET_REGIONS, start, n)
            out += list(r[3:3 + n])
            start += n
            count -= n
        return out

    def set_regions(self, start, values):
        i = 0
        while i < len(values):
            chunk = values[i:i + 28]
            self.cmd(0xA8, MIXED_EFFECT_RGB_SET_REGIONS, start + i, len(chunk), *chunk)
            i += len(chunk)

    def get_effect_list(self, region):
        out = []
        for start, n in ((0, 3), (3, 2)):
            r = self.cmd(0xA8, MIXED_EFFECT_RGB_GET_EFFECT_LIST, region, start, n)
            for i in range(n):
                o = 3 + i * 8
                out.append({"effect": r[o], "hue": r[o + 1], "sat": r[o + 2], "speed": r[o + 3],
                            "time": struct.unpack("<I", bytes(r[o + 4:o + 8]))[0]})
        return out

    def set_effect_list(self, region, items):
        items = list(items) + [{"effect": 0, "hue": 0, "sat": 0, "speed": 0, "time": 0}] * 5
        items = items[:5]
        for start, n in ((0, 3), (3, 2)):
            payload = [0xA8, MIXED_EFFECT_RGB_SET_EFFECT_LIST, region, start, n]
            for it in items[start:start + n]:
                payload += [it["effect"] & 0xFF, it["hue"] & 0xFF, it["sat"] & 0xFF, it["speed"] & 0xFF]
                payload += list(struct.pack("<I", int(it.get("time", 0)) & 0xFFFFFFFF))
            self.cmd(*payload)

    def close(self):
        try:
            self.t.close()
        except Exception:
            pass


# ======================================================================
# HID 报告描述符解析（用于探测报告：Usage Page / Usage / Report ID / 报告长度）
# ======================================================================
def parse_report_descriptor(desc):
    desc = bytes(desc or b"")
    i = 0
    usage_page = 0
    report_id = 0
    report_size = 0
    report_count = 0
    gstack = []
    locals_usages = []
    depth = 0
    collections = []
    cur = None
    while i < len(desc):
        b = desc[i]
        if b == 0xFE:                         # long item
            if i + 2 >= len(desc):
                break
            i += 3 + desc[i + 1]
            continue
        size = (0, 1, 2, 4)[b & 3]
        typ = (b >> 2) & 3
        tag = (b >> 4) & 0xF
        data = desc[i + 1:i + 1 + size]
        val = int.from_bytes(data, "little") if data else 0
        i += 1 + size
        if typ == 1:                          # global
            if tag == 0:
                usage_page = val
            elif tag == 7:
                report_size = val
            elif tag == 8:
                report_id = val
            elif tag == 9:
                report_count = val
            elif tag == 0xA:
                gstack.append((usage_page, report_id, report_size, report_count))
            elif tag == 0xB and gstack:
                usage_page, report_id, report_size, report_count = gstack.pop()
        elif typ == 2:                        # local
            if tag == 0:
                if size == 4:
                    locals_usages.append(val)
                else:
                    locals_usages.append((usage_page << 16) | val)
        elif typ == 0:                        # main
            if tag == 0xA:                    # collection
                if depth == 0:
                    u = locals_usages[0] if locals_usages else 0
                    cur = {"usage_page": u >> 16, "usage": u & 0xFFFF, "reports": {}}
                    collections.append(cur)
                depth += 1
            elif tag == 0xC:
                depth = max(0, depth - 1)
            elif tag in (8, 9, 0xB) and cur is not None:
                kind = {8: "input_bits", 9: "output_bits", 0xB: "feature_bits"}[tag]
                rep = cur["reports"].setdefault(report_id, {"input_bits": 0, "output_bits": 0, "feature_bits": 0})
                rep[kind] += report_size * report_count
            locals_usages = []
    out = []
    for c in collections:
        reports = []
        for rid, r in sorted(c["reports"].items()):
            reports.append({"report_id": rid,
                            "input_bytes": (r["input_bits"] + 7) // 8,
                            "output_bytes": (r["output_bits"] + 7) // 8,
                            "feature_bytes": (r["feature_bits"] + 7) // 8})
        out.append({"usage_page": "0x%04X" % c["usage_page"], "usage": "0x%02X" % c["usage"], "reports": reports})
    return out


# ======================================================================
# macOS IOKit 传输层
# ======================================================================
_iok = None


class _IOKit(object):
    def __init__(self):
        if sys.platform != "darwin":
            raise OSError("IOKit 只在 macOS 上可用")
        cf = C.CDLL(ctypes.util.find_library("CoreFoundation"))
        io = C.CDLL(ctypes.util.find_library("IOKit"))
        vp = C.c_void_p
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, C.c_char_p, C.c_uint32]
        cf.CFNumberCreate.restype = vp
        cf.CFNumberCreate.argtypes = [vp, C.c_long, vp]          # CFNumberType 是 CFIndex（64 位）
        cf.CFDictionaryCreateMutable.restype = vp
        cf.CFDictionaryCreateMutable.argtypes = [vp, C.c_long, vp, vp]
        cf.CFDictionarySetValue.argtypes = [vp, vp, vp]
        cf.CFSetGetCount.restype = C.c_long
        cf.CFSetGetCount.argtypes = [vp]
        cf.CFSetGetValues.argtypes = [vp, vp]
        cf.CFRunLoopGetCurrent.restype = vp
        cf.CFRunLoopRunInMode.restype = C.c_int32
        cf.CFRunLoopRunInMode.argtypes = [vp, C.c_double, C.c_bool]
        cf.CFRelease.argtypes = [vp]
        cf.CFRetain.restype = vp
        cf.CFRetain.argtypes = [vp]
        cf.CFGetTypeID.restype = C.c_ulong
        cf.CFGetTypeID.argtypes = [vp]
        for fn in ("CFNumberGetTypeID", "CFStringGetTypeID", "CFDataGetTypeID", "CFArrayGetTypeID",
                   "CFDictionaryGetTypeID", "CFBooleanGetTypeID"):
            getattr(cf, fn).restype = C.c_ulong
            getattr(cf, fn).argtypes = []
        cf.CFNumberGetValue.restype = C.c_bool
        cf.CFNumberGetValue.argtypes = [vp, C.c_long, vp]
        cf.CFStringGetCString.restype = C.c_bool
        cf.CFStringGetCString.argtypes = [vp, C.c_char_p, C.c_long, C.c_uint32]
        cf.CFDataGetLength.restype = C.c_long
        cf.CFDataGetLength.argtypes = [vp]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFArrayGetCount.restype = C.c_long
        cf.CFArrayGetCount.argtypes = [vp]
        cf.CFArrayGetValueAtIndex.restype = vp
        cf.CFArrayGetValueAtIndex.argtypes = [vp, C.c_long]
        cf.CFDictionaryGetCount.restype = C.c_long
        cf.CFDictionaryGetCount.argtypes = [vp]
        cf.CFDictionaryGetKeysAndValues.argtypes = [vp, vp, vp]
        cf.CFBooleanGetValue.restype = C.c_bool
        cf.CFBooleanGetValue.argtypes = [vp]
        io.IOHIDManagerCreate.restype = vp
        io.IOHIDManagerCreate.argtypes = [vp, C.c_uint32]
        io.IOHIDManagerSetDeviceMatching.argtypes = [vp, vp]
        io.IOHIDManagerCopyDevices.restype = vp
        io.IOHIDManagerCopyDevices.argtypes = [vp]
        io.IOHIDDeviceOpen.restype = C.c_int32
        io.IOHIDDeviceOpen.argtypes = [vp, C.c_uint32]
        io.IOHIDDeviceClose.restype = C.c_int32
        io.IOHIDDeviceClose.argtypes = [vp, C.c_uint32]
        io.IOHIDDeviceSetReport.restype = C.c_int32
        io.IOHIDDeviceSetReport.argtypes = [vp, C.c_int, C.c_long, vp, C.c_long]
        io.IOHIDDeviceScheduleWithRunLoop.argtypes = [vp, vp, vp]
        io.IOHIDDeviceUnscheduleFromRunLoop.argtypes = [vp, vp, vp]
        io.IOHIDDeviceGetProperty.restype = vp
        io.IOHIDDeviceGetProperty.argtypes = [vp, vp]
        self.CB = C.CFUNCTYPE(None, vp, C.c_int32, vp, C.c_int, C.c_uint32, C.POINTER(C.c_uint8), C.c_long)
        io.IOHIDDeviceRegisterInputReportCallback.argtypes = [vp, vp, C.c_long, self.CB, vp]
        self.cf, self.io = cf, io
        self.mode = C.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
        self.kcb = C.addressof(C.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        self.vcb = C.addressof(C.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        self.tid = {n: getattr(cf, "CF%sGetTypeID" % n)() for n in
                    ("Number", "String", "Data", "Array", "Dictionary", "Boolean")}
        self._strs = {}

    def cfstr(self, s):
        # 属性名常量缓存，进程内复用，不释放
        p = self._strs.get(s)
        if p is None:
            p = self.cf.CFStringCreateWithCString(None, s.encode("utf-8"), 0x08000100)
            self._strs[s] = p
        return p

    def to_py(self, ref, depth=0):
        if not ref or depth > 4:
            return None
        cf, t = self.cf, self.cf.CFGetTypeID(ref)
        if t == self.tid["Number"]:
            v = C.c_int64()
            cf.CFNumberGetValue(ref, 4, C.byref(v))     # kCFNumberSInt64Type
            return v.value
        if t == self.tid["Boolean"]:
            return bool(cf.CFBooleanGetValue(ref))
        if t == self.tid["String"]:
            buf = C.create_string_buffer(1024)
            if cf.CFStringGetCString(ref, buf, 1024, 0x08000100):
                return buf.value.decode("utf-8", "replace")
            return None
        if t == self.tid["Data"]:
            n = cf.CFDataGetLength(ref)
            p = cf.CFDataGetBytePtr(ref)
            return C.string_at(p, n) if p and n > 0 else b""
        if t == self.tid["Array"]:
            return [self.to_py(cf.CFArrayGetValueAtIndex(ref, i), depth + 1) for i in range(cf.CFArrayGetCount(ref))]
        if t == self.tid["Dictionary"]:
            n = cf.CFDictionaryGetCount(ref)
            keys = (C.c_void_p * n)()
            vals = (C.c_void_p * n)()
            cf.CFDictionaryGetKeysAndValues(ref, keys, vals)
            return {str(self.to_py(keys[i], depth + 1)): self.to_py(vals[i], depth + 1) for i in range(n)}
        return None

    def prop(self, dev, key):
        return self.to_py(self.io.IOHIDDeviceGetProperty(dev, self.cfstr(key)))

    def copy_devices(self, match):
        """返回 (manager 句柄, [device...])；调用方用 release_mgr 释放句柄。设备引用已 CFRetain。"""
        cf, io = self.cf, self.io
        mgr = io.IOHIDManagerCreate(None, 0)
        d = cf.CFDictionaryCreateMutable(None, 0, self.kcb, self.vcb)
        nums = []
        for k, v in match.items():
            n = C.c_int32(v)
            num = cf.CFNumberCreate(None, 3, C.byref(n))         # kCFNumberSInt32Type
            cf.CFDictionarySetValue(d, self.cfstr(k), num)
            nums.append(num)
        io.IOHIDManagerSetDeviceMatching(mgr, d)
        for num in nums:
            cf.CFRelease(num)                                    # 已被字典持有
        devs = io.IOHIDManagerCopyDevices(mgr)
        out = []
        if devs:
            cnt = cf.CFSetGetCount(devs)
            arr = (C.c_void_p * cnt)()
            cf.CFSetGetValues(devs, arr)
            for i in range(cnt):
                out.append(cf.CFRetain(arr[i]))
            cf.CFRelease(devs)
        return (mgr, d), out

    def release(self, ref):
        if ref:
            self.cf.CFRelease(ref)

    def release_mgr(self, handle):
        if handle:
            mgr, d = handle
            self.release(mgr)                                     # 匹配字典与管理器同生命周期
            self.release(d)


def iokit():
    global _iok
    if _iok is None:
        _iok = _IOKit()
    return _iok


DEVICE_PROPS = ("Transport", "VendorID", "ProductID", "VersionNumber", "Manufacturer", "Product",
                "SerialNumber", "PrimaryUsagePage", "PrimaryUsage", "DeviceUsagePairs", "MaxInputReportSize",
                "MaxOutputReportSize", "MaxFeatureReportSize", "LocationID", "ReportInterval", "CountryCode")


def enumerate_interfaces(vid=VID):
    """列出该厂商所有 HID 接口及属性（不打开设备，不发送任何数据）。"""
    k = iokit()
    mgr, devs = k.copy_devices({"VendorID": vid})
    out = []
    try:
        for d in devs:
            info = {}
            for key in DEVICE_PROPS:
                info[key] = k.prop(d, key)
            desc = k.prop(d, "ReportDescriptor")
            if isinstance(desc, (bytes, bytearray)):
                info["ReportDescriptorHex"] = bytes(desc).hex()
                info["ReportDescriptorParsed"] = parse_report_descriptor(desc)
            out.append(info)
    finally:
        for d in devs:
            k.release(d)
        k.release_mgr(mgr)
    return out


class IOKitTransport(object):
    """打开 Q5 HE 的原始 HID 接口（非独占，与 Keychron Launcher 可共存）。"""

    def __init__(self, pids, vid=VID):
        k = iokit()
        self.k = k
        self.mgr = None
        self.dev = None
        self.pid = None
        self.props = {}
        self._inbox = []
        last_err = None
        for pid in pids:
            mgr, devs = k.copy_devices({"VendorID": vid, "ProductID": pid,
                                        "PrimaryUsagePage": RAW_USAGE_PAGE, "PrimaryUsage": RAW_USAGE})
            if not devs:
                k.release_mgr(mgr)
                continue
            dev = devs[0]
            for extra in devs[1:]:
                k.release(extra)
            rc = k.io.IOHIDDeviceOpen(dev, 0)
            if rc != 0:
                last_err = "IOHIDDeviceOpen 失败 0x%08X%s" % (
                    rc & 0xFFFFFFFF, "（可能需要在 系统设置→隐私与安全性→输入监控 中允许 python3）"
                    if (rc & 0xFFFFFFFF) == 0xE00002E2 else "")
                k.release(dev)
                k.release_mgr(mgr)
                continue
            self.mgr, self.dev, self.pid = mgr, dev, pid
            break
        if self.dev is None:
            raise HidError(last_err or "未找到 Q5 HE（USB 有线连接的原始 HID 接口）")
        for key in ("Transport", "VersionNumber", "Product", "SerialNumber", "LocationID"):
            self.props[key] = k.prop(self.dev, key)
        self._buf = (C.c_uint8 * REPORT_LEN)()
        self._loop = k.cf.CFRunLoopGetCurrent()

        def on_report(ctx, result, sender, rtype, rid, report, length):
            try:
                self._inbox.append(bytes(report[:length]))
            except Exception:
                pass

        self._cb = k.CB(on_report)
        k.io.IOHIDDeviceRegisterInputReportCallback(self.dev, self._buf, REPORT_LEN, self._cb, None)
        k.io.IOHIDDeviceScheduleWithRunLoop(self.dev, self._loop, self.k.mode)

    def send(self, data):
        if self.dev is None:
            raise HidError("设备已关闭")
        buf = (C.c_uint8 * REPORT_LEN)(*data[:REPORT_LEN])
        rc = self.k.io.IOHIDDeviceSetReport(self.dev, 1, 0, buf, REPORT_LEN)   # kIOHIDReportTypeOutput
        if rc != 0:
            raise HidError("IOHIDDeviceSetReport 失败 0x%08X" % (rc & 0xFFFFFFFF))

    def poll(self, timeout):
        end = time.monotonic() + max(0.0, timeout)
        while True:
            # returnAfterSourceHandled=True：收到报告就返回
            rc = self.k.cf.CFRunLoopRunInMode(
                self.k.mode, 0.0 if timeout <= 0 else min(0.02, max(0.0, end - time.monotonic())), True)
            if self._inbox or time.monotonic() >= end:
                break
            if rc == 1:                       # kCFRunLoopRunFinished：没有可运行的源，避免空转
                time.sleep(0.005)
        out, self._inbox = self._inbox, []
        return out

    def close(self):
        if self.dev is not None:
            try:
                self.k.io.IOHIDDeviceUnscheduleFromRunLoop(self.dev, self._loop, self.k.mode)
                self.k.io.IOHIDDeviceClose(self.dev, 0)
            finally:
                self.k.release(self.dev)
                self.k.release_mgr(self.mgr)
                self.dev = None
                self.mgr = None


def open_keyboard(pids=None):
    pids = list(pids or MODELS.keys())
    t = IOKitTransport(pids)
    kb = Keyboard(t, t.pid, info=dict(t.props))
    return kb
