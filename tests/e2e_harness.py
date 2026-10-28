"""端到端：用模拟固件替换 IOKit，运行真实的 Daemon.run() 主循环。"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import q5he_hid as hid  # noqa: E402
import statuslight as sl  # noqa: E402
from fake_fw import FakeFirmware, FakeTransport  # noqa: E402

out_path = sys.argv[1]
fw = FakeFirmware()
lock = threading.Lock()


class LockedTransport(FakeTransport):
    def send(self, data):
        with lock:
            FakeTransport.send(self, data)


def fake_open(pids=None):
    return hid.Keyboard(LockedTransport(fw), 0x0B50, info={"SerialNumber": "E2E"})


hid.open_keyboard = fake_open


def dumper():
    while True:
        with lock:
            snap = {"mode": fw.mode, "display": [fw.display(i) for i in range(fw.n)], "eeprom": fw.eeprom_writes,
                    "t": time.time()}
        tmp = out_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, out_path)
        time.sleep(float(os.environ.get("DUMP_INTERVAL", "0.05")))


threading.Thread(target=dumper, daemon=True).start()
sl.setup_logging(to_stderr=False)
cfg = sl.load_config()
cfg["codex"]["process_check"] = False
cfg["proxy"]["enabled"] = False
d = sl.Daemon(cfg)
rc = d.run()
with lock:
    snap = {"mode": fw.mode, "display": [fw.display(i) for i in range(fw.n)], "eeprom": fw.eeprom_writes,
            "final": True}
with open(out_path, "w") as f:
    json.dump(snap, f)
sys.exit(rc)
