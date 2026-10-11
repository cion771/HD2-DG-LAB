"""Separate bridge reachability from state freshness; no hardware or game access."""
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hd2coyote.config import AppConfig
from hd2coyote.hook import HookSource

ROOT = Path(__file__).resolve().parents[1]

class TestPresence(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig()
        self.cfg.hook.port = 0
        self.src = HookSource(self.cfg)
        self.src.start()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addr = self.src._sock.getsockname()

    def tearDown(self):
        self.sock.close()
        self.src.stop()

    def send(self, packet):
        raw = packet if isinstance(packet, bytes) else json.dumps(packet).encode()
        bad_before = self.src.state.bad_packets
        self.sock.sendto(raw, self.addr)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with self.src._lock:
                ready = bool(self.src._queue) or self.src.state.bad_packets > bad_before
            if ready:
                break
            time.sleep(.01)
        return self.src.poll()

    def test_heartbeat_discovers_bridge_without_state(self):
        self.assertEqual(self.send({'v':1, 'ev':'heartbeat', 'mode':'menu', 'ver':'test',
                                   'detail':'not reading player memory'}), [])
        self.assertTrue(self.src.connected)
        self.assertFalse(self.src.alive)
        self.assertIsNone(self.src.state.hp)
        self.assertIn('桥在线', self.src.describe())
        self.assertEqual(self.src.state.mode, 'menu')

    def test_heartbeat_does_not_keep_stale_hp_alive(self):
        self.send({'v':1, 'ev':'state', 'hp':100, 'hp_max':100})
        self.assertTrue(self.src.alive)
        self.src.state.received = time.monotonic() - 10
        self.send({'v':1, 'ev':'heartbeat', 'mode':'live', 'detail':'player unavailable'})
        self.assertTrue(self.src.connected)
        self.assertFalse(self.src.alive)

    def test_malformed_packets_do_not_claim_connection(self):
        for packet in (b'bad json', {'v':'broken', 'ev':'state'},
                       {'v':1, 'ev':'state', 'hp':1, 'hp_max':0},
                       {'v':1, 'ev':'state', 'hp':float('nan')},
                       {'v':1, 'ev':'state', 'hp':1, 'limbs':3},
                       {'v':1, 'ev':'unknown'}):
            self.send(packet)
            self.assertFalse(self.src.connected)
            self.assertFalse(self.src.alive)
        self.send({'v':1, 'ev':'heartbeat', 'mode':'recon'})
        self.assertTrue(self.src.connected)

    def test_bye_disconnects_both_channels(self):
        self.send({'v':1, 'ev':'state', 'hp':100})
        self.send({'v':1, 'ev':'bye'})
        self.assertFalse(self.src.connected)
        self.assertFalse(self.src.alive)

    def test_queued_state_keeps_arrival_time(self):
        self.src._queue.append({'packet':{'v':1,'ev':'state','hp':100},
                                'received':time.monotonic()-10})
        self.src.poll()
        self.assertFalse(self.src.alive)

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None

@unittest.skipIf(LuaRuntime is None, 'requires LuaJIT test dependency')
class TestLuaHeartbeat(unittest.TestCase):
    def test_menu_and_failed_recon_heartbeat_without_memory_probe(self):
        for mode in ('menu', 'recon', 'live'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, {'HD2COYOTE_DIR':tmp}):
                    Path(tmp, 'bridge_config.lua').write_text(
                        "return {config={mode='" + mode + "', frame_hooks={'update'}}}", encoding='utf-8')
                    rt = LuaRuntime()
                    rt.execute((ROOT/'tests/lua/fake_game.lua').read_text(encoding='utf-8'))
                    rt.execute('update = function() end')
                    rt.execute((ROOT/'lua/hd2_coyote_bridge.lua').read_text(encoding='utf-8'))
                    reads = rt.eval('FAKE_PEEK_CALLS')
                    queries = rt.eval('FAKE_VQ_CALLS')
                    # Simulate starting the controller after startup hello was lost.
                    rt.execute('FAKE_SENT = {}; for i=1,4 do update() end')
                    messages = [json.loads(v) for v in rt.globals().FAKE_SENT.values()]
                    beats = [p for p in messages if p['ev']=='heartbeat']
                    self.assertGreaterEqual(len(beats), 2)
                    self.assertEqual(beats[-1]['mode'], mode)
                    self.assertFalse(any(p['ev']=='state' for p in messages))
                    self.assertEqual(rt.eval('FAKE_PEEK_CALLS'), reads)
                    self.assertEqual(rt.eval('FAKE_VQ_CALLS'), queries)
                    self.assertTrue(beats[-1]['detail'])

if __name__ == '__main__':
    unittest.main()
