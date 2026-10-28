"""Q5 HE 固件行为模拟（按 Keychron/qmk_firmware 2025q3 源码逐条实现），用于离线测试。"""
import struct
import time

import q5he_hid as hid

ALLOWED_FIRST = {0x01, 0x07, 0x08, 0xA0, 0xA1, 0xA2, 0xA8}


class FirmwareViolation(AssertionError):
    pass


class FakeFirmware(object):
    def __init__(self, led_count=101, mode=20, hsv=(16, 255, 255), speed=127, fw="v1.2.1 2026-09-30-14:36:41",
                 rgb_supported=True):
        self.n = led_count
        self.enable = True
        self.mode = mode
        self.hsv = list(hsv)
        self.speed = speed
        self.per_key_type = 0
        # 默认逐键颜色（固件 default_per_key_led：红/黄/蓝）
        self.per_key = [(0, 255, 255)] + [(43, 255, 255)] * 16 + [(170, 255, 255)] * (led_count - 17)
        self.regions = [0] * led_count
        self.effect_list = [[[0, 0, 0, 0, 0] for _ in range(5)] for _ in range(2)]
        self.effect_list[0][0] = [5, 0, 255, 127, 5000]
        self.effect_list[1][0] = [2, 0, 255, 127, 5000]
        self.fw = fw
        self.rgb_supported = rgb_supported
        self.eeprom_writes = 0          # 只统计“主机命令”导致的 EEPROM 写入（必须为 0）
        self.log = []
        self.led_map = [list(r) for r in hid.STATIC_LED_MAP[0x0B50]]
        # EEPROM 中的 rgb_config（开机值 / 切换灯效时重新载入颜色与速度）
        self.ee_mode, self.ee_enable = mode, True
        self.ee_hsv, self.ee_speed = list(hsv), speed

    def _set_mode(self, mode):
        if mode != self.mode:
            # rgb_task_render：灯效变化时从 EEPROM 重新载入 hsv 与 speed
            self.hsv = list(self.ee_hsv)
            self.speed = self.ee_speed
        self.mode = mode

    # ---- 用户在键盘上的操作（固件自己写 EEPROM，这是 QMK 行为） ----
    def key_toggle(self):
        """Fn+Tab / F5 调到最低 / 关灯时按 F6：rgb_matrix_toggle() 把 (RAM 模式, 开关) 写入 EEPROM。"""
        self.enable = not self.enable
        self.ee_mode, self.ee_enable = self.mode, self.enable

    def key_next_effect(self, mode):
        self._set_mode(mode)
        self.ee_mode, self.ee_enable = mode, True

    def power_cycle(self):
        self.mode, self.enable = self.ee_mode, self.ee_enable
        self.hsv, self.speed = list(self.ee_hsv), self.ee_speed
        self.regions = [0] * self.n
        self.per_key_type = 0

    # 处理一条 32 字节请求，返回回复（就地修改的 data）
    def handle(self, req):
        data = bytearray(req)
        self.log.append(bytes(req[:6]))
        c0 = data[0]
        if c0 not in ALLOWED_FIRST:
            raise FirmwareViolation("收到白名单外命令 %02X" % c0)
        if c0 == 0x09:
            self.eeprom_writes += 1
        if c0 == 0x01:
            data[1], data[2] = 0x00, 0x0C
        elif c0 in (0x07, 0x08):
            if data[1] != 3:
                data[0] = 0xFF
                return bytes(data)
            vid = data[2]
            if c0 == 0x07:
                if vid == 1:
                    self.hsv[2] = data[3]
                elif vid == 2:
                    if data[3] == 0:
                        self.enable = False
                    else:
                        self.enable = True
                        self._set_mode(data[3])
                elif vid == 3:
                    self.speed = data[3]
                elif vid == 4:
                    self.hsv[0], self.hsv[1] = data[3], data[4]
            else:
                if vid == 1:
                    data[3] = self.hsv[2]
                elif vid == 2:
                    data[3] = self.mode if self.enable else 0
                elif vid == 3:
                    data[3] = self.speed
                elif vid == 4:
                    data[3], data[4] = self.hsv[0], self.hsv[1]
        elif c0 == 0xA0:
            data[1], data[2], data[3] = 2, 0, 2
        elif c0 == 0xA1:
            s = self.fw.encode()
            data[1:1 + len(s)] = s
            data[1 + len(s)] = 0
        elif c0 == 0xA2:
            data[1], data[2], data[3] = 0, 0x01 | 0x02 | 0x04 | 0x08 | 0x10 | (0x80 if self.rgb_supported else 0), 0x03
        elif c0 == 0xA8:
            if not self.rgb_supported:
                data[0] = 0xFF
                return bytes(data)
            sub = data[1]
            d = data[2:]
            ok = True
            if sub == 0x01:
                d[1], d[2] = 0x01, 0x00
            elif sub == 0x02:
                self.eeprom_writes += 1
                raise FirmwareViolation("收到 RGB_SAVE（会写 EEPROM）")
            elif sub == 0x05:
                d[1] = self.n
            elif sub == 0x06:
                row = d[0]
                if row > 6:
                    ok = False
                else:
                    mask = d[1] | (d[2] << 8) | (d[3] << 16)
                    for c in range(19):
                        v = 0xFF
                        if mask & (1 << c) and row < 6:
                            v = self.led_map[row][c]
                        d[1 + c] = v
            elif sub == 0x07:
                d[1] = self.per_key_type
            elif sub == 0x08:
                if d[0] >= 5:
                    ok = False
                else:
                    self.per_key_type = d[0]
            elif sub == 0x09:
                start, count = d[0], d[1]
                if count > 9:
                    ok = False
                else:
                    if start + count > self.n:
                        raise FirmwareViolation("越界读取 per_key_led")
                    for i in range(count):
                        h, s, v = self.per_key[start + i]
                        d[1 + i * 3], d[2 + i * 3], d[3 + i * 3] = h, s, v
            elif sub == 0x0A:
                start, count = d[0], d[1]
                if count > 9:
                    ok = False
                else:
                    if start + count > self.n:
                        raise FirmwareViolation("越界写入 per_key_led（会破坏固件内存）")
                    for i in range(count):
                        self.per_key[start + i] = (d[2 + i * 3], d[3 + i * 3], d[4 + i * 3])
            elif sub == 0x0B:
                d[1], d[2] = 2, 5
            elif sub == 0x0C:
                start, count = d[0], d[1]
                if count > 29 or start + count > self.n:
                    ok = False
                else:
                    d[1:1 + count] = bytes(self.regions[start:start + count])
            elif sub == 0x0D:
                start, count = d[0], d[1]
                if count > 28 or start + count > self.n or any(d[2 + i] >= 2 for i in range(count)):
                    ok = False
                else:
                    for i in range(count):
                        self.regions[start + i] = d[2 + i]
            elif sub == 0x0E:
                region, start, count = d[0], d[1], d[2]
                if count > 3 or region > 2 or start + count > 5:
                    ok = False
                else:
                    for i in range(count):
                        e = self.effect_list[region][start + i]
                        o = 1 + i * 8
                        d[o], d[o + 1], d[o + 2], d[o + 3] = e[0], e[1], e[2], e[3]
                        d[o + 4:o + 8] = struct.pack("<I", e[4])
            elif sub == 0x0F:
                region, start, count = d[0], d[1], d[2]
                if count > 3 or region > 2 or start + count > 5:
                    ok = False
                else:
                    if region == 2:
                        raise FirmwareViolation("region=2 越界写 effect_list")
                    for i in range(count):
                        if d[3 + i * 8] >= 24:
                            ok = False
                    if ok:
                        for i in range(count):
                            o = 3 + i * 8
                            self.effect_list[region][start + i] = [d[o], d[o + 1], d[o + 2], d[o + 3],
                                                                   struct.unpack("<I", bytes(d[o + 4:o + 8]))[0]]
            else:
                data[0] = 0xFF
                return bytes(data[:2] + d)
            data = data[:2] + d
            data[2] = 0 if ok else 1
        return bytes(data)

    # 当前每个 LED 实际显示的颜色（h, s, 亮度系数 'solid' / 'breath'）
    def display(self, led):
        if not self.enable or self.mode == 0:
            return ("off",)
        if self.mode == 23:
            h, s, _ = self.per_key[led]
            return ("solid", h, s) if self.per_key_type == 0 else ("perkey-type%d" % self.per_key_type, h, s)
        if self.mode == 24:
            region = self.regions[led]
            e = self.effect_list[region][0]
            if e[0] == 23:
                h, s, _ = self.per_key[led]
                return ("solid", h, s)
            if e[0] == 1:
                return ("solid", e[1], e[2])
            if e[0] == 2:
                return ("breath", e[1], e[2])
            return ("effect%d" % e[0],)
        return ("effect%d" % self.mode,)


class FakeTransport(object):
    def __init__(self, fw):
        self.fw = fw
        self.queue = []
        self.closed = False
        self.fail_after = None
        self.sent = 0

    def send(self, data):
        if self.closed:
            raise hid.HidError("closed")
        if self.fail_after is not None:
            if self.fail_after <= 0:
                raise hid.HidError("IOHIDDeviceSetReport 失败 0xE00002D8")
            self.fail_after -= 1
        assert len(data) == 32
        self.sent += 1
        self.queue.append(self.fw.handle(data))

    def poll(self, timeout):
        out, self.queue = self.queue, []
        return out

    def inject(self, rep):
        self.queue.append(bytes(rep) + b"\x00" * (32 - len(rep)))

    def close(self):
        self.closed = True
