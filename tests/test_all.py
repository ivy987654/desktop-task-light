import contextlib
import io
import json
import os
import shutil
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import q5he_hid as hid  # noqa: E402
import statuslight as sl  # noqa: E402
from fake_fw import FakeFirmware, FakeTransport, FirmwareViolation  # noqa: E402

TOP = list(range(17))
SPACE, PKEY = 92, 46
LAYOUT = {"top": TOP, "space": SPACE, "proxy": PKEY, "count": 101}


def cfg(**over):
    c = sl.deep_merge(sl.DEFAULT_CONFIG, over)
    c["codex"]["process_check"] = False
    return c


class TmpDirs(object):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.codex = os.path.join(self.tmp, "codex")
        os.makedirs(self.codex)
        for name, sub in (("RUN_DIR", "run"), ("STATE_DIR", "state"), ("LOG_DIR", "logs")):
            setattr(sl, name, os.path.join(self.tmp, sub))
        sl.SPOOL = os.path.join(sl.RUN_DIR, "events.jsonl")
        sl.WAKE_SOCK = os.path.join(sl.RUN_DIR, "wake.sock")
        sl.LOCK_FILE = os.path.join(sl.RUN_DIR, "daemon.lock")
        sl.STATE_FILE = os.path.join(sl.STATE_DIR, "state.json")
        sl.ORIGINAL_FILE = os.path.join(sl.STATE_DIR, "original_lighting.json")
        sl.CONFIG_FILE = os.path.join(self.tmp, "config.json")
        sl.CODEX_HOME = self.codex
        sl.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def frame_at(e, now, region=True):
    colors, r1, r1c, nxt = e.frame(now, LAYOUT, region)
    return colors, r1, r1c, nxt


# ----------------------------------------------------------------------
class TestProtocol(unittest.TestCase):
    def setUp(self):
        self.fw = FakeFirmware()
        self.t = FakeTransport(self.fw)
        self.kb = hid.Keyboard(self.t, 0x0B50)

    def test_info(self):
        self.assertEqual(self.kb.via_protocol(), 12)
        self.assertTrue(self.kb.firmware_version().startswith("v1.2.1"))
        self.assertTrue(self.kb.features()["keychron_rgb"])
        self.assertEqual(self.kb.rgb_protocol(), 1)
        self.assertEqual(self.kb.get_led_count(), 101)
        m = self.kb.led_map()
        self.assertEqual(m, hid.STATIC_LED_MAP[0x0B50])
        lay = sl.build_layout(m, cfg(), 101)
        self.assertEqual({k: lay[k] for k in LAYOUT}, LAYOUT)
        self.assertEqual(self.kb.mixed_info(), {"layers": 2, "effects_per_layer": 5})

    def test_colors_roundtrip_and_chunking(self):
        cols = [(i % 256, (i * 7) % 256, 255) for i in range(101)]
        self.kb.set_colors(0, cols)
        self.assertEqual(self.kb.get_colors(0, 101), cols)
        self.assertEqual(self.fw.per_key[100], cols[100])

    def test_bounds_and_forbidden(self):
        with self.assertRaises(hid.ForbiddenCommand):
            self.kb.cmd(0xA8, 0x0A, 95, 9)                  # 95+9 > 101
        with self.assertRaises(hid.ForbiddenCommand):
            self.kb.cmd(0xA8, 0x02)                          # RGB_SAVE
        with self.assertRaises(hid.ForbiddenCommand):
            self.kb.cmd(0x09, 0x03)                          # VIA save
        with self.assertRaises(hid.ForbiddenCommand):
            self.kb.cmd(0x0B)                                # bootloader
        with self.assertRaises(hid.ForbiddenCommand):
            self.kb.cmd(0xA7, 0x11)                          # factory reset
        with self.assertRaises(hid.ForbiddenCommand):
            self.kb.cmd(0xA8, 0x0F, 2, 0, 1)                 # region 2
        with self.assertRaises(hid.ForbiddenCommand):
            self.kb.set_effect(25)
        self.assertEqual(self.t.sent, 0)

    def test_regions_effect_lists(self):
        self.kb.set_regions(0, [0] * 101)
        self.kb.set_regions(90, [1, 0, 1])
        self.assertEqual(self.kb.get_regions(88, 5), [0, 0, 1, 0, 1])
        items = [{"effect": 2, "hue": 191, "sat": 255, "speed": 110, "time": 0}]
        self.kb.set_effect_list(1, items)
        got = self.kb.get_effect_list(1)
        self.assertEqual(got[0], items[0])
        self.assertEqual(got[1]["effect"], 0)

    def test_notifications_vs_foreign(self):
        self.t.inject(b"\xA3\x00\x01")             # 层切换通知
        self.t.inject(b"\xA7\x0D\x00\x01\x7F")      # 回报率通知
        self.kb.drain()
        self.assertEqual(self.kb.foreign_count, 0)
        self.assertEqual(self.kb.notify_count, 2)
        self.t.inject(b"\xA9\x01\x02")             # Launcher 的模拟量查询回复
        self.kb.get_effect()                        # 正常请求中混入外来回复也能正确匹配
        self.assertEqual(self.kb.foreign_count, 1)

    def test_unsupported(self):
        fw = FakeFirmware(rgb_supported=False)
        kb = hid.Keyboard(FakeTransport(fw), 0x0B50)
        with self.assertRaises(hid.UnsupportedCommand):
            kb.rgb_protocol()

    def test_descriptor_parser(self):
        raw = bytes.fromhex("0660FF0961A101096215002600FF95207508810209631500" "2600FF952075089102C0")
        p = hid.parse_report_descriptor(raw)
        self.assertEqual(p, [{"usage_page": "0xFF60", "usage": "0x61",
                              "reports": [{"report_id": 0, "input_bytes": 32, "output_bytes": 32,
                                           "feature_bytes": 0}]}])
        kbd = bytes.fromhex("05010906A101850105071900" "29FF150025019508750181029501750881019505750105081901290591029501"
                            "7503910395067508150026FF000507190029FF8100C0")
        p = hid.parse_report_descriptor(kbd)
        self.assertEqual(p[0]["usage_page"], "0x0001")
        self.assertEqual(p[0]["usage"], "0x06")
        self.assertEqual(p[0]["reports"][0]["report_id"], 1)


# ----------------------------------------------------------------------
class TestEngine(unittest.TestCase):
    def setUp(self):
        self.e = sl.Engine(cfg(tasks={"thinking_style": "comet"}))
        self.t0 = 1000000.0

    def ev(self, now, **kw):
        kw.setdefault("ts", now)
        self.e.handle(kw, now)

    def test_idle_white_and_schedule(self):
        e = self.e
        e.schedule = {"active": ["a", "b"], "paused": ["c"]}
        colors, r1, r1c, _ = frame_at(e, self.t0)
        self.assertEqual(colors[0], (170, 255))
        self.assertEqual(colors[1], (170, 255))
        self.assertNotIn(2, colors)
        self.assertEqual(r1, set())

    def test_thinking_complete_lifecycle(self):
        e, t = self.e, self.t0
        self.ev(t, type="turn_start", session="s1", turn="u1", origin="hook")
        colors, _, _, nxt = frame_at(e, t)
        area = [colors.get(i) for i in TOP]
        self.assertTrue(all(c is not None and c[0] == 43 for c in area))       # 全部是黄色系
        self.assertEqual(sum(1 for c in area if c == (43, 255)), 1)           # 一个彗星头
        self.assertLessEqual(nxt - t, 0.21)
        c2, _, _, _ = frame_at(e, t + 0.3)
        self.assertNotEqual(area, [c2.get(i) for i in TOP])                   # 在移动
        self.ev(t + 3, type="turn_complete", session="s1", turn="u1")
        colors, _, _, nxt = frame_at(e, t + 3.1)
        self.assertEqual([colors[i] for i in TOP], [(85, 255)] * 17)
        e.tick(t + 7.9)
        self.assertEqual(len(e.tasks), 1)
        e.tick(t + 8.01)
        self.assertEqual(len(e.tasks), 0)
        colors, _, _, _ = frame_at(e, t + 8.1)
        self.assertEqual(colors, {})
        # 重复的完成事件（rollout 晚到）不产生孤立事件
        self.ev(t + 9, type="turn_complete", session="s1", turn="u1", origin="rollout")
        self.assertEqual(e.orphans, [])

    def test_parallel_split_and_orphan(self):
        e, t = self.e, self.t0
        e.schedule = {"active": ["x"], "paused": []}
        self.ev(t, type="turn_start", session="s1", turn="a")
        self.ev(t + 1, type="turn_start", session="s2", turn="b")
        self.ev(t + 2, type="turn_start", session="s3", turn="c")
        self.assertEqual(sl.split_even(TOP[1:], 3), [TOP[1:7], TOP[7:12], TOP[12:17]])
        self.ev(t + 3, type="turn_complete", session="s2", turn="b")
        colors, _, _, _ = frame_at(e, t + 3.05)
        self.assertEqual([colors[i] for i in TOP[7:12]], [(85, 255)] * 5)
        self.assertEqual(colors[0], (170, 255))
        self.ev(t + 3.1, type="turn_complete", session="zz", turn="nostart")
        colors, _, _, _ = frame_at(e, t + 3.2)
        self.assertEqual(colors[16], (85, 255))                              # 最右侧一个绿键
        self.assertNotEqual(colors[15], (85, 255))

    def test_interrupt_flash(self):
        e, t = self.e, self.t0
        self.ev(t, type="turn_start", session="s1", turn="a")
        self.ev(t + 1, type="turn_abort", session="s1", turn="a", reason="interrupted")
        self.assertEqual(e.tasks["turn:s1:a"]["state"], "interrupted")
        on, _, _, nxt = frame_at(e, t + 1.1)
        off, _, _, _ = frame_at(e, t + 1.3)
        self.assertEqual(on[5], (0, 255))
        self.assertEqual(off.get(5, (0, 0)), (0, 0))
        self.assertAlmostEqual(nxt, t + 1.25, places=3)
        e.tick(t + 6.01)
        self.assertEqual(e.tasks, {})

    def test_wait_breathing_and_pause(self):
        e, t = self.e, self.t0
        self.ev(t, type="turn_start", session="s1", turn="a", origin="hook")
        self.ev(t + 1, type="wait", session="s1", turn="a", kind="approval", tool="shell")
        colors, r1, r1c, _ = frame_at(e, t + 1.1)
        self.assertEqual(r1, {SPACE})
        self.assertEqual(r1c, (hid.EFFECT_BREATHING, 191, 255))
        self.assertEqual(set(colors[i] for i in TOP), {(43, 255)})          # 任务暂停跑马
        self.ev(t + 2, type="wait_clear", session="s1")
        colors, r1, r1c, _ = frame_at(e, t + 2.1)
        self.assertEqual(r1, set())
        # 没有区域呼吸时用颜色↔白色呼吸兜底
        self.ev(t + 3, type="wait", session="s1", kind="question")
        colors, r1, _, nxt = frame_at(e, t + 3.0, region=False)
        self.assertEqual(r1, set())
        self.assertEqual(colors[SPACE][0], 191)
        self.assertLess(nxt - (t + 3.0), 0.1)

    def test_proxy_states(self):
        e, t = self.e, self.t0
        e.proxy_result(t, True, "ok")
        colors, _, _, _ = frame_at(e, t)
        self.assertNotIn(PKEY, colors)
        e.proxy_result(t + 10, False, "timeout")
        colors, _, _, nxt = frame_at(e, t + 11)
        self.assertEqual(colors[PKEY], (21, 255))
        colors, r1, r1c, _ = frame_at(e, t + 71)                            # 60 秒后视为持续断开
        self.assertEqual(r1, {PKEY})
        self.assertEqual(r1c, (hid.EFFECT_BREATHING, 0, 255))
        self.ev(t + 72, type="wait", session="s", kind="approval")           # 区域 1 优先给空格
        colors, r1, r1c, _ = frame_at(e, t + 72)
        self.assertEqual(r1, {SPACE})
        self.assertEqual(colors[PKEY][0], 0)
        e.proxy_result(t + 80, True, "ok")
        colors, _, _, _ = frame_at(e, t + 81)
        self.assertEqual(colors[PKEY], (85, 255))
        colors, _, _, _ = frame_at(e, t + 101)
        self.assertNotIn(PKEY, colors)

    def test_subagents_and_session_end(self):
        e, t = self.e, self.t0
        self.ev(t, type="turn_start", session="s1", turn="a")
        self.ev(t + 1, type="agent_start", session="s1", turn="a", agent="ag1")
        self.ev(t + 1, type="agent_start", session="s1", turn="a", agent="ag2")
        self.assertEqual(len(e.visible_tasks()), 3)
        self.ev(t + 2, type="agent_stop", agent="ag1")
        self.assertEqual(e.tasks["agent:ag1"]["state"], "done")
        self.ev(t + 3, type="session_end", session="s1")
        self.assertEqual(e.tasks["turn:s1:a"]["state"], "interrupted")

    def test_new_turn_replaces_and_stale(self):
        e, t = self.e, self.t0
        self.ev(t, type="turn_start", session="s1", turn="a")
        self.ev(t + 5, type="turn_start", session="s1", turn="b")
        self.assertEqual(list(e.tasks), ["turn:s1:b"])
        e.tick(t + 5 + 45 * 60 + 1)
        self.assertEqual(e.tasks, {})
        self.ev(t + 2000, type="activity", session="s1", turn="b", create=True)   # 已失联的回合不复活
        self.assertEqual(e.tasks, {})

    def test_process_exit(self):
        e, t = self.e, self.t0
        e.codex_pids = {100, 200}
        self.ev(t, type="turn_start", session="s1", turn="a")
        e.check_processes({100, 300}, t + 1)
        self.assertEqual(e.tasks["turn:s1:a"]["state"], "thinking")
        e.check_processes({300}, t + 2)
        self.assertEqual(e.tasks["turn:s1:a"]["state"], "interrupted")

    def test_json_roundtrip(self):
        e, t = self.e, self.t0
        self.ev(t, type="turn_start", session="s1", turn="a")
        self.ev(t, type="wait", session="s1", kind="approval")
        e2 = sl.Engine(cfg())
        e2.from_json(json.loads(json.dumps(e.to_json())))
        self.assertEqual(e2.tasks, e.tasks)
        self.assertEqual(e2.waits, e.waits)


# ----------------------------------------------------------------------
class TestDaemon(TmpDirs, unittest.TestCase):
    def setUp(self):
        TmpDirs.setUp(self)
        self.fw = FakeFirmware()
        self.transports = []

        def fake_open(pids=None):
            t = FakeTransport(self.fw)
            self.transports.append(t)
            return hid.Keyboard(t, 0x0B50, info={"SerialNumber": "SN1", "Transport": "USB", "VersionNumber": 0x0121})

        self._orig_open = hid.open_keyboard
        hid.open_keyboard = fake_open
        self.d = sl.Daemon(cfg())
        self.d.wait = lambda timeout: None

    def tearDown(self):
        hid.open_keyboard = self._orig_open
        TmpDirs.tearDown(self)

    def connect(self, now=None):
        now = now or time.time()
        self.d.ensure_device(now)
        return now

    def test_takeover_and_restore(self):
        fw = self.fw
        fw.per_key_type = 1
        now = self.connect()
        self.assertTrue(self.d.owned)
        self.assertEqual(fw.mode, 24)
        self.assertEqual(fw.per_key_type, 0)
        self.assertEqual(fw.effect_list[0][0][0], 23)
        self.assertEqual(fw.effect_list[1][0][0], 2)
        self.assertEqual(set(fw.display(i) for i in range(101)), {("solid", 0, 0)})   # 全白
        orig = json.load(open(sl.ORIGINAL_FILE))["0B50:SN1"]
        self.assertEqual(orig["lighting"]["effect"], 20)
        self.assertEqual(fw.eeprom_writes, 0)
        # 停止时恢复
        self.d.restore_original()
        self.assertEqual(fw.mode, 20)
        self.assertEqual(fw.per_key_type, 1)
        self.assertEqual(fw.per_key[0], (0, 255, 255))
        self.assertEqual(fw.effect_list[0][0], [5, 0, 255, 127, 5000])

    def test_render_events_to_firmware(self):
        fw = self.fw
        now = self.connect()
        e = self.d.engine
        e.handle({"type": "turn_start", "session": "s1", "turn": "a", "ts": now, "origin": "hook"}, now)
        e.handle({"type": "wait", "session": "s1", "kind": "approval", "ts": now}, now)
        self.d.render(now)
        self.assertEqual(fw.display(SPACE), ("breath", 191, 255))
        self.assertEqual(fw.display(0), ("solid", 43, 255))      # 进度条已填充部分（等待中：条纹停止）
        self.assertEqual(fw.display(50), ("solid", 0, 0))        # 未填充部分：白色
        e.handle({"type": "wait_clear", "session": "s1", "ts": now + 1}, now + 1)
        e.handle({"type": "turn_complete", "session": "s1", "turn": "a", "ts": now + 1}, now + 1)
        self.d.render(now + 1)
        self.assertEqual(fw.display(SPACE), ("solid", 85, 255))  # 空格没有状态时跟随所在列
        self.assertEqual([fw.display(i) for i in TOP], [("solid", 85, 255)] * 17)
        sent = self.transports[-1].sent
        self.d.render(now + 1.5)                                   # 无变化不写
        self.assertEqual(self.transports[-1].sent, sent)

    def test_progress_region_stripes_on_firmware(self):
        fw = self.fw
        now = self.connect()
        e = self.d.engine
        e.handle({"type": "turn_start", "session": "s1", "turn": "a", "ts": now - 900, "origin": "hook"}, now)
        self.d.render(now)
        self.assertEqual(fw.effect_list[1][0][:3], [1, 43, 110])  # 区域 1 = 纯色浅黄
        cols = sl.build_layout(hid.STATIC_LED_MAP[0x0B50], self.d.cfg, 101)["cells"]
        shown = set(fw.display(l) for ci in range(0, 18) for l in cols[ci])
        self.assertEqual(shown, {("solid", 43, 255), ("solid", 43, 110)})
        self.assertEqual(set(fw.display(l) for l in cols[18]), {("solid", 0, 0)})
        tr = self.transports[-1]
        counts = []
        for k in range(1, 6):
            before = tr.sent
            self.d.render(now + 0.2 * k)
            counts.append(tr.sent - before)
        self.assertTrue(all(1 <= c <= 4 for c in counts), counts)   # 高光每次移动只发 ≤4 个区域包
        self.assertEqual(fw.eeprom_writes, 0)
        # 有人等待批准：区域 1 改回呼吸，高光改用逐键颜色，画面不中断
        e.handle({"type": "turn_start", "session": "s2", "turn": "b", "ts": now, "origin": "hook"}, now + 1.1)
        e.handle({"type": "wait", "session": "s2", "kind": "approval", "ts": now + 1.1}, now + 1.1)
        self.d.render(now + 1.2)
        self.assertEqual(fw.effect_list[1][0][:3], [2, 191, 255])
        self.assertEqual(fw.display(SPACE), ("breath", 191, 255))
        self.assertIn(("solid", 43, 110), set(fw.display(l) for l in range(101)))

    def test_user_changes_effect_then_resumes(self):
        fw = self.fw
        now = self.connect()
        fw.mode = 7                                               # 用户按 Fn 切换了灯效
        self.d.health_check(now + 11)
        self.assertFalse(self.d.owned)
        self.assertEqual(self.d.opted_out["kind"], "effect")
        sent = self.transports[-1].sent
        self.d.render(now + 12) if self.d.owned else None
        self.d.health_check(now + 22)
        self.assertEqual(fw.mode, 7)                              # 没有抢回来
        self.assertEqual(self.transports[-1].sent, sent + 1)       # 只读了一次灯效
        fw.mode = 23                                              # 用户切回 Per Key RGB
        self.d.health_check(now + 33)
        self.assertTrue(self.d.owned)
        self.assertEqual(fw.mode, 24)
        self.assertIsNone(self.d.opted_out)

    def test_foreign_traffic_yields(self):
        now = self.connect()
        t = self.transports[-1]
        t.inject(b"\xA3\x00\x01")
        self.d.loop_once()
        self.assertFalse(self.d.yielding)
        t.inject(b"\x04\x00\x01\x02\x00\x29")                     # Launcher 读键位的回复
        self.d.loop_once()
        self.assertTrue(self.d.yielding)
        sent = t.sent
        self.d.engine.handle({"type": "turn_start", "session": "s", "turn": "x", "ts": time.time()}, time.time())
        self.d.loop_once()
        self.assertEqual(t.sent, sent)                            # 让出期间不写
        self.d.kb.foreign_ts = time.time() - 61
        self.d.loop_once()
        self.assertFalse(self.d.yielding)
        self.assertGreater(t.sent, sent)                          # 安静后重新同步

    def test_disconnect_reconnect(self):
        now = self.connect()
        self.transports[-1].fail_after = 0
        self.d.engine.handle({"type": "turn_start", "session": "s", "turn": "x", "ts": now}, now)
        self.d.loop_once()
        self.assertIsNone(self.d.kb)
        self.fw.mode = 20                                          # 重新上电后回到 EEPROM 灯效
        self.d.next_connect = 0
        self.d.loop_once()
        self.assertTrue(self.d.owned)
        self.assertEqual(self.fw.mode, 24)
        self.assertEqual(len(self.transports), 2)

    def test_pause_resume_control(self):
        now = self.connect()
        sl.append_event({"event": "_control", "action": "pause"})
        self.d.read_spool(now)
        self.assertEqual(self.fw.mode, 20)
        self.assertFalse(self.d.owned)
        self.d.health_check(now + 20)
        self.assertEqual(self.fw.mode, 20)
        sl.append_event({"event": "_control", "action": "resume"})
        self.d.read_spool(now + 21)
        self.d.health_check(now + 22)
        self.assertTrue(self.d.owned)

    def test_spool_hook_records_and_rotation(self):
        self.connect()
        for i in range(3):
            sl.append_event({"event": "UserPromptSubmit", "session_id": "s%d" % i, "turn_id": "t", "transcript_path": "x"})
        self.d.read_spool(time.time())
        self.assertEqual(len(self.d.engine.tasks), 3)
        self.d.spool_off = 2 * 1024 * 1024
        self.d.read_spool(time.time())
        self.assertTrue(os.path.exists(sl.SPOOL + ".old"))
        sl.append_event({"event": "Stop", "session_id": "s0", "turn_id": "t", "transcript_path": "x"})
        self.d.read_spool(time.time())
        self.assertEqual(self.d.engine.tasks["turn:s0:t"]["state"], "done")

    def test_state_persistence_restart(self):
        now = self.connect()
        sl.append_event({"event": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1", "transcript_path": "x"})
        self.d.read_spool(now)
        self.d.save_state(now)
        d2 = sl.Daemon(cfg())
        d2.load_state()
        self.assertIn("turn:s1:t1", d2.engine.tasks)
        sl.append_event({"event": "Stop", "session_id": "s1", "turn_id": "t1", "transcript_path": "x"})
        d2.read_spool(time.time())                                 # 从上次的偏移继续读，不重复
        self.assertEqual(d2.engine.tasks["turn:s1:t1"]["state"], "done")
        self.assertEqual(d2.stats["events"], 1)

    def test_unsupported_firmware(self):
        self.fw.rgb_supported = False
        now = self.connect()
        self.assertFalse(self.d.owned)
        self.assertIn("升级固件", self.d.unsupported)
        self.d.loop_once()
        self.assertEqual(self.fw.mode, 20)


# ----------------------------------------------------------------------
class TestReviewFixes(TestDaemon):
    """独立审查发现的问题的回归测试。"""

    def test_lights_off_while_owned_fixes_eeprom_mode(self):
        fw = self.fw
        now = self.connect()
        self.assertEqual(fw.mode, 24)
        fw.key_toggle()                                   # 用户关灯：固件写入 EEPROM (24, 关)
        self.assertEqual((fw.ee_mode, fw.ee_enable), (24, False))
        self.d.health_check(now + 11)
        self.assertFalse(self.d.owned)
        self.assertEqual(self.d.opted_out["kind"], "off")
        self.assertEqual((fw.mode, fw.enable), (20, False))   # 内存模式已改回原灯效，仍保持关灯
        fw.key_toggle()                                   # 用户开灯：固件保存的是原灯效 20
        self.assertEqual((fw.ee_mode, fw.ee_enable), (20, True))
        self.d.health_check(now + 15)
        self.assertTrue(self.d.owned)
        self.assertEqual(fw.mode, 24)
        orig = json.load(open(sl.ORIGINAL_FILE))["0B50:SN1"]
        self.assertEqual(orig["lighting"]["effect"], 20)

    def test_start_with_lights_off_does_not_turn_on(self):
        fw = self.fw
        fw.enable = False
        now = self.connect()
        self.assertFalse(self.d.owned)
        self.assertFalse(fw.enable)
        self.assertEqual(self.d.opted_out["kind"], "off")
        fw.key_toggle()
        self.d.health_check(now + 4)
        self.assertTrue(self.d.owned)
        self.assertEqual(json.load(open(sl.ORIGINAL_FILE))["0B50:SN1"]["lighting"]["effect"], 20)

    def test_original_not_stale_after_pause_edit_resume(self):
        fw = self.fw
        fw.mode = fw.ee_mode = 23                         # 用户本来就用逐键灯光
        fw.per_key[0] = (10, 200, 255)
        now = self.connect()
        self.d.control({"action": "pause"}, now)
        self.assertEqual((fw.mode, fw.per_key[0]), (23, (10, 200, 255)))
        fw.per_key[0] = (170, 255, 255)                   # 暂停期间在 Launcher 改了颜色
        self.d.control({"action": "resume"}, now + 1)
        self.d.health_check(now + 2)
        self.assertTrue(self.d.owned)
        self.d.control({"action": "pause"}, now + 3)
        self.assertEqual(fw.per_key[0], (170, 255, 255))  # 还原的是新颜色

    def test_takeover_failure_does_not_pollute_original(self):
        fw = self.fw

        def fake_open_failing(pids=None):
            t = FakeTransport(fw)
            t.fail_after = 40                             # 接管进行到一半断开
            self.transports.append(t)
            return hid.Keyboard(t, 0x0B50, info={"SerialNumber": "SN1"})

        hid.open_keyboard = fake_open_failing
        now = self.connect()
        self.assertIsNone(self.d.kb)
        rec = json.load(open(sl.ORIGINAL_FILE))["0B50:SN1"]
        self.assertTrue(rec["active"])
        self.assertEqual(rec["lighting"]["effect"], 20)
        hid.open_keyboard = lambda pids=None: hid.Keyboard(FakeTransport(fw), 0x0B50, info={"SerialNumber": "SN1"})
        self.d.next_connect = 0
        self.d.ensure_device(now + 2)
        self.assertTrue(self.d.owned)
        rec = json.load(open(sl.ORIGINAL_FILE))["0B50:SN1"]
        self.assertEqual(rec["lighting"]["effect"], 20)
        self.assertEqual(rec["per_key_type"], 0)

    def test_effect_change_restores_hidden_parts(self):
        fw = self.fw
        now = self.connect()
        fw.key_next_effect(7)                              # 用户 Fn 切到灯效 7
        self.d.health_check(now + 11)
        self.assertEqual(self.d.opted_out["kind"], "effect")
        self.assertEqual(fw.mode, 7)
        self.assertEqual(fw.per_key[1], (43, 255, 255))   # 逐键颜色还原为用户原来的
        self.assertEqual(fw.effect_list[0][0], [5, 0, 255, 127, 5000])
        self.assertFalse(json.load(open(sl.ORIGINAL_FILE))["0B50:SN1"]["active"])

    def test_resync_after_foreign_traffic(self):
        fw = self.fw
        now = self.connect()
        t = self.transports[-1]
        t.inject(b"\xA8\x0E\x00")
        self.d.loop_once()
        self.assertTrue(self.d.yielding)
        fw.effect_list[1][0] = [5, 0, 255, 127, 0]         # Launcher 改了区域 1 灯效（仍是 Mix RGB）
        fw.per_key_type = 1
        self.d.kb.foreign_ts = time.time() - 61
        self.d.loop_once()
        self.assertEqual(fw.effect_list[1][0][0], 2)
        self.assertEqual(fw.per_key_type, 0)

    def test_pause_hid_error_retries_restore(self):
        fw = self.fw
        now = self.connect()
        self.transports[-1].fail_after = 0
        sl.append_event({"event": "_control", "action": "pause"})
        self.d.read_spool(now)
        self.assertIsNone(self.d.kb)
        self.assertTrue(self.d.restore_pending)
        self.assertEqual(fw.mode, 24)
        self.d.next_connect = 0
        self.d.ensure_device(now + 2)
        self.assertEqual(fw.mode, 20)
        self.assertFalse(self.d.restore_pending)
        self.assertFalse(self.d.owned)

    def test_config_typo_falls_back(self):
        with open(sl.CONFIG_FILE, "w") as f:
            json.dump({"colors": {"thinking": "yellow", "done": [85, 300]}, "timing": {"chase_step": 0},
                       "layout": {"space_key": [5]}, "tasks": {"thinking_style": "zigzag"}}, f)
        c = sl.load_config()
        self.assertEqual(c["colors"]["thinking"], [43, 255])
        self.assertEqual(c["colors"]["done"], [85, 255])
        self.assertEqual(c["timing"]["chase_step"], 0.2)
        self.assertEqual(c["layout"]["space_key"], [5, 6])
        self.assertEqual(c["tasks"]["thinking_style"], "progress")

    def test_spool_rotation_keeps_late_writer(self):
        self.connect()
        sl.append_event({"event": "UserPromptSubmit", "session_id": "s1", "turn_id": "t", "transcript_path": "x"})
        fd = os.open(sl.SPOOL, os.O_WRONLY | os.O_APPEND)   # 一个刚打开旧文件的 hook 进程
        self.d.read_spool(time.time())
        self.d.spool_off = 2 * 1024 * 1024
        self.d.read_spool(time.time())
        os.write(fd, (json.dumps({"event": "Stop", "session_id": "s1", "turn_id": "t", "transcript_path": "x",
                                  "ts": time.time()}) + "\n").encode())
        os.close(fd)
        self.d.read_spool(time.time())
        self.assertEqual(self.d.engine.tasks["turn:s1:t"]["state"], "done")

    def test_unsupported_probe_writes_json(self):
        self.fw.rgb_supported = False
        orig_enum = hid.enumerate_interfaces
        hid.enumerate_interfaces = lambda vid=0x3434: []
        try:
            rc = sl.cmd_probe(None)
        finally:
            hid.enumerate_interfaces = orig_enum
        self.assertEqual(rc, 2)
        p = json.load(open(os.path.join(sl.STATE_DIR, "probe.json")))
        self.assertFalse(p["keyboard"]["supported"])


class TestEngineReviewFixes(unittest.TestCase):
    def setUp(self):
        self.e = sl.Engine(cfg())
        self.t = 2000000.0

    def ev(self, now, **kw):
        kw.setdefault("ts", now)
        self.e.handle(kw, now)

    def test_long_wait_not_stale(self):
        t = self.t
        self.ev(t, type="turn_start", session="s", turn="a")
        self.ev(t + 1, type="wait", session="s", kind="approval")
        self.e.tick(t + 60 * 60)
        self.assertIn("turn:s:a", self.e.tasks)
        self.ev(t + 3600, type="wait_clear", session="s")
        self.ev(t + 3601, type="turn_complete", session="s", turn="a")
        self.assertEqual(self.e.tasks["turn:s:a"]["state"], "done")

    def test_stale_then_complete_shows_orphan(self):
        t = self.t
        self.ev(t, type="turn_start", session="s", turn="a")
        self.e.tick(t + 46 * 60)
        self.assertEqual(self.e.tasks, {})
        self.ev(t + 47 * 60, type="activity", session="s", turn="a", create=True)
        self.assertEqual(self.e.tasks, {})
        self.ev(t + 48 * 60, type="turn_complete", session="s", turn="a")
        self.assertEqual(len(self.e.orphans), 1)

    def test_replaced_turn_late_abort_no_orphan(self):
        t = self.t
        self.ev(t, type="turn_start", session="s", turn="a")
        self.ev(t + 1, type="turn_start", session="s", turn="b")
        self.ev(t + 2, type="turn_abort", session="s", turn="a", reason="interrupted", origin="rollout")
        self.assertEqual(self.e.orphans, [])
        self.ev(t + 2, type="activity", session="s", turn="a", create=True)
        self.assertEqual(list(self.e.tasks), ["turn:s:b"])

    def test_codex_exit_clears_waits(self):
        t = self.t
        self.e.codex_pids = {1}
        self.ev(t, type="turn_start", session="s", turn="a")
        self.ev(t, type="wait", session="s", kind="approval")
        self.ev(t, type="wait", session="sub", kind="approval")
        self.e.check_processes(set(), t + 5)
        self.assertEqual(self.e.waits, {})


# ----------------------------------------------------------------------
COLS = sl.build_layout(hid.STATIC_LED_MAP[0x0B50], cfg(), 101)


class TestColumnsProgress(unittest.TestCase):
    """整个键盘按列作为进度条。"""

    def setUp(self):
        self.e = sl.Engine(cfg())
        self.t = 3000000.0
        self.cells = COLS["cells"]

    def ev(self, now, **kw):
        kw.setdefault("ts", now)
        self.e.handle(kw, now)

    def colcolor(self, colors, ci):
        vals = set(colors.get(led, (0, 0)) for led in self.cells[ci])
        self.assertEqual(len(vals), 1, "第 %d 列颜色不一致：%s" % (ci, vals))
        return vals.pop()

    @staticmethod
    def shown(fr):
        """键盘上实际显示的颜色：区域 1 的灯显示区域 1 灯效的颜色，其余显示逐键颜色。"""
        colors, r1, r1c = fr[0], fr[1], fr[2]
        return {led: ((r1c[1], r1c[2]) if led in r1 else colors.get(led, (0, 0))) for led in range(101)}

    def showncol(self, fr, ci):
        sh = self.shown(fr)
        vals = set(sh[led] for led in self.cells[ci])
        self.assertEqual(len(vals), 1, "第 %d 列颜色不一致：%s" % (ci, vals))
        return vals.pop()

    def test_layout_columns(self):
        self.assertEqual(COLS["mode"], "columns")
        self.assertEqual(len(self.cells), 19)
        self.assertEqual(self.cells[0], [0, 17, 36, 55, 72, 89])
        self.assertEqual(self.cells[-1], [35, 54, 88])
        self.assertEqual(sorted(l for c in self.cells for l in c), list(range(101)))   # 每颗灯恰好属于一列
        self.assertIn(92, self.cells[6])
        self.assertIn(46, self.cells[10])
        top = sl.build_layout(hid.STATIC_LED_MAP[0x0B50], cfg(layout={"mode": "top_row"}), 101)
        self.assertEqual(top["cells"], [[i] for i in range(17)])

    def test_progress_fill_and_stripes(self):
        t = self.t
        self.e.schedule = {"active": ["a", "b"], "paused": []}
        self.ev(t, type="turn_start", session="s", turn="a")
        colors, _, _, nxt = self.e.frame(t, COLS, True)
        self.assertEqual(self.colcolor(colors, 0), (170, 255))       # 计划任务：最左侧两列蓝
        self.assertEqual(self.colcolor(colors, 1), (170, 255))
        self.assertIn(self.colcolor(colors, 2), ((43, 255), (43, 110)))   # 刚开始：只填 1 列
        self.assertEqual(self.colcolor(colors, 3), (0, 0))                # 未填充：白色
        self.assertLessEqual(nxt - t, 0.21)
        colors, _, _, _ = self.e.frame(t + 120, COLS, True)          # 120 秒：约 63%
        filled = [ci for ci in range(2, 19) if self.colcolor(colors, ci) != (0, 0)]
        self.assertEqual(filled, list(range(2, 13)))
        a = self.e.frame(t + 120.0, COLS, True)
        b = self.e.frame(t + 120.2, COLS, True)
        self.assertNotEqual([self.showncol(a, ci) for ci in range(2, 13)],
                            [self.showncol(b, ci) for ci in range(2, 13)])     # 条纹在移动
        colors, _, _, _ = self.e.frame(t + 3600, COLS, True)
        self.assertEqual(self.colcolor(colors, 18), (0, 0))         # 最后一列留给“完成”

    def test_paused_freezes_and_status_override(self):
        t = self.t
        self.ev(t, type="turn_start", session="s", turn="a")
        self.ev(t + 60, type="wait", session="s", kind="approval")
        a, r1, r1c, nxt = self.e.frame(t + 60.0, COLS, True)
        b, _, _, _ = self.e.frame(t + 60.2, COLS, True)
        self.assertEqual(a, b)                                       # 等待批准：条纹停止
        self.assertEqual(r1, {92})
        self.assertEqual(a[92], (191, 255))                          # 空格单独显示紫色呼吸
        self.assertGreaterEqual(nxt - (t + 60), 1.0)                 # 暂停时不用高频刷新
        self.e.proxy_override = ("brief", t + 1e6)
        a, _, _, _ = self.e.frame(t + 61, COLS, True)
        self.assertEqual(a[46], (21, 255))                           # P 键单独显示代理状态
        others = set(a[l] for l in self.cells[10] if l != 46)
        self.assertEqual(len(others), 1)

    def test_parallel_done_failed_orphan(self):
        t = self.t
        self.ev(t, type="turn_start", session="s1", turn="a")
        self.ev(t, type="turn_start", session="s2", turn="b")
        self.ev(t + 5, type="turn_complete", session="s1", turn="a")
        self.ev(t + 5, type="turn_abort", session="s2", turn="b", reason="interrupted")
        colors, _, _, _ = self.e.frame(t + 5.1, COLS, True)
        self.assertEqual([self.colcolor(colors, ci) for ci in range(0, 10)], [(85, 255)] * 10)
        self.assertEqual([self.colcolor(colors, ci) for ci in range(10, 19)], [(0, 255)] * 9)
        self.assertEqual(colors[92], (85, 255))                      # 空格跟随所在列
        self.ev(t + 6, type="turn_complete", session="zz", turn="q")
        self.e.tick(t + 10)
        colors, _, _, _ = self.e.frame(t + 10, COLS, True)
        self.assertEqual(self.colcolor(colors, 18), (85, 255))       # 孤立完成：最右侧一列
        self.assertEqual(self.colcolor(colors, 17), (0, 0))

    def test_region_stripes(self):
        t = self.t
        self.e.schedule = {"active": ["a", "b"], "paused": []}
        self.ev(t, type="turn_start", session="s", turn="a")
        a = self.e.frame(t + 120.0, COLS, True)
        colors, r1, r1c, _ = a
        self.assertEqual(r1c, (hid.EFFECT_SOLID, 43, 110))                # 区域 1 = 纯色浅黄高光
        filled = set(l for ci in range(2, 13) for l in self.cells[ci])
        self.assertTrue(r1 and r1 <= filled)
        self.assertEqual(set(colors[l] for l in filled), {(43, 255)})       # 逐键颜色全是黄色，移动高光不用改逐键颜色
        stripes = [ci for ci in range(2, 13) if self.showncol(a, ci) == (43, 110)]
        self.assertEqual(len(stripes), 4)                                  # 每 3 列一条
        b = self.e.frame(t + 120.2, COLS, True)
        self.assertEqual(a[0], b[0])                                       # 逐键颜色不变
        self.assertNotEqual(a[1], b[1])                                    # 只改区域归属
        changed = sorted(set(a[1]) ^ set(b[1]))
        self.assertLessEqual(len(sl.runs(changed, list(range(101)), 28)), 4)   # 每帧最多 4 个区域包
        # 关闭该方式 / 没有 Mix RGB：退回逐键颜色画高光
        for e2, region in ((sl.Engine(cfg(tasks={"region_stripes": False})), True), (self.e, False)):
            e2.handle({"type": "turn_start", "session": "s", "turn": "a", "ts": t}, t)
            e2.schedule = self.e.schedule
            c = e2.frame(t + 120.0, COLS, region)
            self.assertEqual((c[1], c[2]), (set(), None))
            self.assertEqual([self.colcolor(c[0], ci) for ci in range(2, 13)],
                             [self.showncol(a, ci) for ci in range(2, 13)])  # 看起来完全一样

    def test_region_priority_breath_and_status_keys(self):
        t = self.t
        self.ev(t, type="turn_start", session="s1", turn="a")
        self.ev(t, type="turn_start", session="s2", turn="b")
        self.ev(t + 100, type="wait", session="s2", kind="approval")
        colors, r1, r1c, _ = self.e.frame(t + 120.0, COLS, True)
        self.assertEqual((r1, r1c), ({92}, (hid.EFFECT_BREATHING, 191, 255)))   # 呼吸优先占用区域 1
        self.assertIn((43, 110), set(colors.values()))                           # 高光退回逐键颜色
        self.ev(t + 121, type="wait_clear", session="s2")
        self.e.proxy_override = ("brief", t + 1e6)
        seen = False
        for k in range(6):
            fr = self.e.frame(t + 122 + 0.2 * k, COLS, True)
            self.assertNotIn(46, fr[1])                                     # P 键显示代理状态，不放进区域 1
            self.assertEqual(self.shown(fr)[46], (21, 255))
            seen = seen or bool(set(self.cells[10]) & fr[1])
        self.assertTrue(seen)                                               # P 键所在列的其他灯照常有高光

    def test_runs_greedy_minimal_packets(self):
        changed = sorted(l for ci in range(2, 13) for l in self.cells[ci])
        vals = list(range(101))
        rs = sl.runs(changed, vals, 9)
        covered = set(i for st, v in rs for i in range(st, st + len(v)))
        self.assertTrue(set(changed) <= covered)
        self.assertTrue(all(len(v) <= 9 for _, v in rs))
        self.assertLessEqual(len(rs), 12)


class TestReport(TmpDirs, unittest.TestCase):
    def test_report_picks_q5he_raw_interface(self):
        def iface(pid, product):
            return {"ProductID": pid, "Product": product, "Transport": "USB", "PrimaryUsagePage": 0xFF60,
                    "PrimaryUsage": 0x61, "MaxInputReportSize": 32, "MaxOutputReportSize": 32,
                    "MaxFeatureReportSize": 0, "VersionNumber": 0x130, "VendorID": 0x3434,
                    "ReportDescriptorParsed": [{"usage_page": "0xFF60", "usage": "0x61", "reports": [
                        {"report_id": 0, "input_bytes": 32, "output_bytes": 32, "feature_bytes": 0}]}]}
        sl.save_json(os.path.join(sl.STATE_DIR, "probe.json"),
                     {"interfaces": [iface(0x0B50, "Keychron Q5 HE"), iface(0x0800, "Keychron Q0 Max")]})
        old_here = sl.HERE
        sl.HERE = self.tmp
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                sl.cmd_report(None)
        finally:
            sl.HERE = old_here
        text = open(os.path.join(self.tmp, "VERIFICATION.md"), encoding="utf-8").read()
        self.assertIn("原始 HID 接口：VID 0x3434 PID 0x0B50", text)


class TestBackgroundAndSleep(TestDaemon):
    def test_ignores_background_sessions(self):
        now = self.connect()
        for rec in ({"event": "UserPromptSubmit", "session_id": "bg1", "turn_id": "x", "cwd": "/"},
                    {"event": "UserPromptSubmit", "session_id": "mem", "turn_id": "y", "transcript_path": "/t.jsonl",
                     "cwd": os.path.join(sl.CODEX_HOME, "memories")},
                    {"event": "SessionEnd", "session_id": "mem"},
                    {"event": "UserPromptSubmit", "session_id": "real", "turn_id": "z", "transcript_path": "/r.jsonl",
                     "cwd": "/Users/example/Documents/p"}):
            sl.append_event(rec)
        self.d.cfg["codex"]["ignore_cwd_prefixes"] = [sl.CODEX_HOME]
        self.d.read_spool(now)
        self.assertEqual(list(self.d.engine.tasks), ["turn:real:z"])
        self.assertEqual(self.d.stats.get("ignored_events"), 3)

    def test_tracked_session_not_ignored_when_field_missing(self):
        now = self.connect()
        sl.append_event({"event": "UserPromptSubmit", "session_id": "real", "turn_id": "z",
                         "transcript_path": "/r.jsonl", "cwd": "/Users/example/Documents/p"})
        sl.append_event({"event": "PermissionRequest", "session_id": "real", "turn_id": "z", "tool_name": "shell",
                         "cwd": "/Users/example/Documents/p"})                     # 假设某个事件缺少 transcript_path
        self.d.read_spool(now)
        self.assertIn("real", self.d.engine.waits)
        self.assertNotIn("real", self.d.ignored)
        sl.append_event({"event": "Stop", "session_id": "real", "turn_id": "z", "transcript_path": "/r.jsonl"})
        self.d.read_spool(now + 1)
        self.assertEqual(self.d.engine.tasks["turn:real:z"]["state"], "done")

    def test_wake_grace(self):
        now = self.connect()
        self.d.clock_offset = time.time() - time.monotonic() - 600      # 模拟刚睡醒 10 分钟
        self.d.engine.proxy_result(now - 700, False, "before sleep")
        self.d.prober = sl.Prober(lambda: self.d.cfg["proxy"], lambda: None)
        self.d.prober.results.append((now, False, "dark wake"))
        self.d.loop_once()
        self.assertGreater(self.d.wake_grace_until, time.time())
        self.assertEqual(self.d.engine.proxy["detail"], "before sleep")   # 唤醒期间的失败被丢弃
        self.assertGreaterEqual(self.d.engine.proxy["first_fail"], now)   # 断开计时重新开始
        self.transports[-1].fail_after = 0
        self.d.disconnect("等待回包超时")
        self.assertEqual(self.d.stats.get("sleep_errors"), 1)
        first = self.d.next_connect - time.time()
        self.d.disconnect("等待回包超时")
        self.assertGreater(self.d.next_connect - time.time(), first)     # 连续失败逐步拉长间隔


# ----------------------------------------------------------------------
class TestRollouts(TmpDirs, unittest.TestCase):
    def mkdb(self, rows):
        db = os.path.join(self.codex, "state_5.sqlite")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, "
                    "archived INTEGER DEFAULT 0, updated_at INTEGER, updated_at_ms INTEGER)")
        for r in rows:
            con.execute("INSERT OR REPLACE INTO threads(id, rollout_path, source, updated_at, updated_at_ms) "
                        "VALUES (?,?,?,?,?)", r)
        con.commit()
        con.close()

    def line(self, typ, turn, **kw):
        p = {"type": typ, "turn_id": turn}
        p.update(kw)
        return json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                           "type": "event_msg", "payload": p}) + "\n"

    def test_db_query_cached_until_db_changes(self):
        now = time.time()
        self.mkdb([("th-a", "/nonexistent-a.jsonl", "vscode", int(now), int(now * 1000))])
        w = sl.RolloutWatcher(self.codex, now)
        calls = []
        real = sl.sqlite3.connect

        def counting(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        sl.sqlite3.connect = counting
        try:
            self.assertEqual([r[0] for r in w._db_rows(6)], ["th-a"])
            self.assertEqual([r[0] for r in w._db_rows(6)], ["th-a"])
            self.assertEqual(len(calls), 1)                      # 库没变化：不重复查询
            time.sleep(0.01)
            sl.sqlite3.connect = real
            self.mkdb([("th-b", "/nonexistent-b.jsonl", "vscode", int(now) + 1, int(now * 1000) + 1000)])
            sl.sqlite3.connect = counting
            self.assertEqual([r[0] for r in w._db_rows(6)], ["th-b", "th-a"])
            self.assertEqual(len(calls), 2)                      # 库有写入：重新查询
        finally:
            sl.sqlite3.connect = real

    def test_recovery_and_tail(self):
        path = os.path.join(self.codex, "rollout-a.jsonl")
        with open(path, "w") as f:
            f.write(self.line("task_started", "t0") + self.line("task_complete", "t0") + self.line("task_started", "t1"))
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "error": "x"}}) + "\n")
        now = time.time()
        self.mkdb([("th-a", path, "vscode", int(now), int(now * 1000)),
                   ("th-sub", path + ".sub", '{"subagent":{"x":1}}', int(now), int(now * 1000))])
        w = sl.RolloutWatcher(self.codex, now - 100)
        evs = []
        w.poll(now, [], evs.append, True, True)
        self.assertEqual([(e["type"], e["turn"], e["origin"]) for e in evs], [("turn_start", "t1", "recovered")])
        self.assertEqual(w.thread_kind("th-sub"), "subagent")
        evs.clear()
        with open(path, "a") as f:
            f.write(self.line("turn_aborted", "t1", reason="interrupted") + self.line("task_started", "t2"))
            f.write('{"type":"event_msg","payload":{"type":"task_comp')              # 写了一半的行
        w.poll(now + 2, [], evs.append, False, True)
        self.assertEqual([e["type"] for e in evs], ["activity", "turn_abort", "turn_start"])
        evs.clear()
        with open(path, "a") as f:
            f.write('lete","turn_id":"t2"}}\n')
        w.poll(now + 4, [], evs.append, False, True)
        self.assertEqual([e["type"] for e in evs], ["activity", "turn_complete"])
        self.assertEqual(evs[1]["turn"], "t2")

    def test_new_file_after_start_read_from_beginning(self):
        now = time.time()
        w = sl.RolloutWatcher(self.codex, now - 5)
        path = os.path.join(self.codex, "rollout-new.jsonl")
        with open(path, "w") as f:
            f.write(self.line("task_started", "n1") + self.line("task_complete", "n1"))
        self.mkdb([("th-n", path, "cli", int(now), int(now * 1000))])
        evs = []
        w.poll(now, [], evs.append, False, True)
        self.assertEqual([e["type"] for e in evs], ["activity", "turn_start", "turn_complete"])

    def test_automations(self):
        for i, st in enumerate(("ACTIVE", "PAUSED", "ACTIVE")):
            d = os.path.join(self.codex, "automations", "automation-%d" % i)
            os.makedirs(d)
            with open(os.path.join(d, "automation.toml"), "w") as f:
                f.write('version = 1\nid = "automation-%d"\nname = "任务%d"\nstatus = "%s"\nprompt = "status = \\"PAUSED\\""\n'
                        % (i, i, st))
        autos = sl.read_automations(self.codex)
        self.assertEqual([a["status"] for a in autos], ["ACTIVE", "PAUSED", "ACTIVE"])
        d = sl.Daemon(cfg())
        d.poll_schedule()
        self.assertEqual(d.engine.schedule["active"], ["任务0", "任务2"])


# ----------------------------------------------------------------------
class TestHookRelay(TmpDirs, unittest.TestCase):
    def test_relay(self):
        shutil.copy(os.path.join(ROOT, "hook_relay.py"), self.tmp)
        payload = {"session_id": "s1", "turn_id": "t1", "hook_event_name": "UserPromptSubmit", "prompt": "SECRET",
                   "transcript_path": "/x/rollout.jsonl", "cwd": "/Users/example", "model": "gpt"}
        t0 = time.time()
        p = subprocess.run([sys.executable, "-S", "-E", os.path.join(self.tmp, "hook_relay.py"), "UserPromptSubmit"],
                           input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=10)
        dt = time.time() - t0
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        self.assertEqual(p.stderr, b"")
        rec = json.loads(open(os.path.join(self.tmp, "run", "events.jsonl")).read())
        self.assertEqual(rec["session_id"], "s1")
        self.assertNotIn("prompt", rec)
        self.assertNotIn("SECRET", json.dumps(rec))
        self.assertLess(dt, 2.0)
        # 非法输入也安静退出
        p = subprocess.run([sys.executable, "-S", "-E", os.path.join(self.tmp, "hook_relay.py"), "Stop"],
                           input=b"not json", stdout=subprocess.PIPE, timeout=10)
        self.assertEqual((p.returncode, p.stdout), (0, b""))


# ----------------------------------------------------------------------
def _selfsigned(tmp):
    key, crt = os.path.join(tmp, "k.pem"), os.path.join(tmp, "c.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                    "-days", "1", "-subj", "/CN=localhost"], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    return key, crt


class TestProxyProbe(TmpDirs, unittest.TestCase):
    def setUp(self):
        TmpDirs.setUp(self)
        key, crt = _selfsigned(self.tmp)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        self.tls = socket.socket()
        self.tls.bind(("127.0.0.1", 0))
        self.tls.listen(5)
        self.tls_port = self.tls.getsockname()[1]

        def tls_srv():
            while True:
                try:
                    c, _ = self.tls.accept()
                except OSError:
                    return
                try:
                    s = ctx.wrap_socket(c, server_side=True)
                    s.recv(1024)
                    s.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                    s.close()
                except Exception:
                    c.close()

        threading.Thread(target=tls_srv, daemon=True).start()
        self.connects = []

        def pipe(a, b):
            try:
                while True:
                    d = a.recv(4096)
                    if not d:
                        break
                    b.sendall(d)
            except OSError:
                pass
            finally:
                for x in (a, b):
                    try:
                        x.close()
                    except OSError:
                        pass

        self.http = socket.socket()
        self.http.bind(("127.0.0.1", 0))
        self.http.listen(5)

        def http_proxy():
            while True:
                try:
                    c, _ = self.http.accept()
                except OSError:
                    return
                head = b""
                while b"\r\n\r\n" not in head:
                    head += c.recv(1024)
                self.connects.append(head.split(b"\r\n")[0])
                up = socket.create_connection(("127.0.0.1", self.tls_port))
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                threading.Thread(target=pipe, args=(c, up), daemon=True).start()
                threading.Thread(target=pipe, args=(up, c), daemon=True).start()

        self.socks = socket.socket()
        self.socks.bind(("127.0.0.1", 0))
        self.socks.listen(5)

        def socks_proxy():
            while True:
                try:
                    c, _ = self.socks.accept()
                except OSError:
                    return
                c.recv(3)
                c.sendall(b"\x05\x00")
                h = c.recv(5)
                name = c.recv(h[4])
                port = struct.unpack(">H", c.recv(2))[0]
                self.connects.append(b"SOCKS " + name + b":" + str(port).encode())
                up = socket.create_connection(("127.0.0.1", self.tls_port))
                c.sendall(b"\x05\x00\x00\x03\x09localhost\x00\x00")
                threading.Thread(target=pipe, args=(c, up), daemon=True).start()
                threading.Thread(target=pipe, args=(up, c), daemon=True).start()

        threading.Thread(target=http_proxy, daemon=True).start()
        threading.Thread(target=socks_proxy, daemon=True).start()

    def tearDown(self):
        for s in (self.tls, self.http, self.socks):
            s.close()
        TmpDirs.tearDown(self)

    def test_http_connect_and_socks5(self):
        pc = dict(sl.DEFAULT_CONFIG["proxy"], targets=["https://example.test/cdn-cgi/trace"], timeout=3)
        pc["proxy_url"] = "http://127.0.0.1:%d" % self.http.getsockname()[1]
        ok, detail = sl.probe_network(pc)
        self.assertTrue(ok, detail)                         # 自签证书：TLS 已到达 = 网络可达
        self.assertIn(b"CONNECT example.test:443", self.connects[0])
        pc["proxy_url"] = "socks5://127.0.0.1:%d" % self.socks.getsockname()[1]
        ok, detail = sl.probe_network(pc)
        self.assertTrue(ok, detail)
        self.assertEqual(self.connects[1], b"SOCKS example.test:443")
        pc["proxy_url"] = "http://127.0.0.1:1"               # 代理软件没开
        ok, detail = sl.probe_network(pc)
        self.assertFalse(ok)
        self.assertIn("ConnectionRefusedError", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
