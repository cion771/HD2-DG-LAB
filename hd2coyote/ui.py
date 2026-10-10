"""Tkinter 界面：连接手机 App、接收游戏状态、实时调强度、急停。

界面只做三件事：改配置（立刻生效）、看状态、点急停。
所有实际输出都在 Engine / SafetyGuard 里，界面不直接碰设备。
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk

from .config import AppConfig
from .engine import Engine
from .safety import HotkeyWatcher

RULE_LABELS = {
    "damage": "受伤",
    "limb_injury": "肢体损伤",
    "death": "阵亡",
    "low_health": "低血量",
}


# --------------------------------------------------------------------- 主窗口
class MainWindow:
    def __init__(self, cfg: AppConfig, config_path: Path) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.engine = Engine(cfg)
        self.events: deque[str] = deque(maxlen=200)
        self.engine.on_event = lambda ev: self.events.append(_describe(ev))
        self._hotkeys: HotkeyWatcher | None = None

        self.root = tk.Tk()
        self.root.title("绝地潜兵 2 × 郊狼 DG-LAB")
        self.root.minsize(940, 560)
        self._build_top()
        self._build_body()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._poll)

    # ------------------------------------------------------------ 顶部：设备
    def _build_top(self) -> None:
        frame = ttk.LabelFrame(self.root, text="① 连接手机 App")
        frame.pack(fill="x", padx=8, pady=6)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=6, pady=4)

        ttk.Label(row, text="状态来源：游戏桥 / HTTP 事件源").pack(side="left")
        row2 = ttk.Frame(frame)
        row2.pack(fill="x", padx=6, pady=4)
        ttk.Label(row2, text="模式").pack(side="left")
        self.var_kind = tk.StringVar(value=self.cfg.device.kind)
        combo = ttk.Combobox(row2, textvariable=self.var_kind, width=8, state="readonly",
                             values=["socket", "mock"])
        combo.pack(side="left", padx=4)

        ttk.Label(row2, text="端口").pack(side="left")
        self.var_port = tk.StringVar(value=str(self.cfg.device.port))
        ttk.Entry(row2, textvariable=self.var_port, width=6).pack(side="left", padx=4)

        ttk.Button(row2, text="启动连接", command=self._start_device).pack(side="left", padx=4)
        ttk.Button(row2, text="断开", command=self._stop_device).pack(side="left", padx=4)
        self.lbl_device = ttk.Label(row2, text="未连接")
        self.lbl_device.pack(side="left", padx=10)

        qr_row = ttk.Frame(frame)
        qr_row.pack(fill="x", padx=6, pady=4)
        self.qr_canvas = tk.Canvas(qr_row, width=180, height=180, bg="#111", highlightthickness=0)
        self.qr_canvas.pack(side="left")
        info = ttk.Frame(qr_row)
        info.pack(side="left", fill="both", expand=True, padx=8)
        ttk.Label(info, text="用 DG-LAB App 扫描左侧二维码（Socket 被控）").pack(anchor="w")
        self.var_url = tk.StringVar(value="")
        entry = ttk.Entry(info, textvariable=self.var_url)
        entry.pack(fill="x", pady=4)
        ttk.Button(info, text="复制地址", command=self._copy_url).pack(anchor="w")
        ttk.Label(info, text="手机与电脑需在同一局域网；App 内可随时调整通道强度上限。",
                  foreground="#666").pack(anchor="w", pady=4)

    # ------------------------------------------------------------ 主体
    def _build_body(self) -> None:
        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.LabelFrame(body, text="② 检测与输出")
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right = ttk.LabelFrame(body, text="③ 强度（可随时调整，立即生效）")
        right.pack(side="left", fill="both", expand=True)

        # --- 左：检测
        row = ttk.Frame(left)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Button(row, text="开始检测", command=self._start_detect).pack(side="left", padx=3)
        ttk.Button(row, text="停止检测", command=self._stop).pack(side="left", padx=3)
        ttk.Button(row, text="急停 (F12)", command=lambda: self.engine.trip("急停按钮")).pack(
            side="left", padx=3)
        ttk.Button(row, text="重新武装", command=self._arm).pack(side="left", padx=3)

        self.lbl_hp = ttk.Label(left, text="HP: --", font=("Consolas", 12))
        self.lbl_hp.pack(anchor="w", padx=8)
        self.lbl_injury = ttk.Label(left, text="损伤: --", font=("Consolas", 10))
        self.lbl_injury.pack(anchor="w", padx=8)
        self.lbl_out = ttk.Label(left, text="输出: A=0 B=0", font=("Consolas", 10))
        self.lbl_out.pack(anchor="w", padx=8)

        ttk.Label(left, text="事件日志").pack(anchor="w", padx=8, pady=(6, 0))
        self.txt_events = tk.Text(left, height=12, width=48, state="disabled", wrap="none")
        self.txt_events.pack(fill="both", expand=True, padx=8, pady=6)

        # --- 右：强度滑块
        self._rule_widgets: dict[str, tuple[tk.BooleanVar, tk.DoubleVar]] = {}
        for key, rule in self.cfg.rules.items():
            box = ttk.Frame(right)
            box.pack(fill="x", padx=6, pady=3)
            var_on = tk.BooleanVar(value=rule.enabled)
            var_val = tk.DoubleVar(value=rule.base_pct)
            ttk.Checkbutton(box, text=RULE_LABELS.get(key, key), variable=var_on,
                            command=lambda k=key, v=var_on: self._set_rule_enabled(k, v)
                            ).pack(side="left")
            ttk.Scale(box, from_=0, to=60, variable=var_val, length=140,
                      command=lambda _v, k=key, v=var_val: self._set_rule_pct(k, v)
                      ).pack(side="left", padx=6)
            lbl = ttk.Label(box, text=f"{rule.base_pct:.0f}%", width=5)
            lbl.pack(side="left")
            self._rule_widgets[key] = (var_on, var_val)
            setattr(self, f"_lbl_rule_{key}", lbl)

        sep = ttk.Separator(right)
        sep.pack(fill="x", pady=6)

        self.var_master = tk.DoubleVar(value=self.cfg.safety.master_multiplier)
        self.var_maxpct = tk.DoubleVar(value=self.cfg.safety.max_pct)
        self.var_maxabs = tk.DoubleVar(value=self.cfg.safety.max_absolute)
        self._slider(right, "总倍率", self.var_master, 0.0, 2.0, self._set_master, "%.2f")
        self._slider(right, "单次强度上限 %", self.var_maxpct, 1.0, 60.0, self._set_maxpct, "%.0f")
        self._slider(right, "绝对强度上限", self.var_maxabs, 1.0, 120.0, self._set_maxabs, "%.0f")

        ttk.Label(right, text="（绝对上限 = App 里的强度数值，官方范围 0~200）",
                  foreground="#666").pack(anchor="w", padx=8)

        sep2 = ttk.Separator(right)
        sep2.pack(fill="x", pady=8)
        cal = ttk.LabelFrame(right, text="④ 测试与保存")
        cal.pack(fill="x", padx=6, pady=4)
        ttk.Button(cal, text="测试脉冲 5%", command=self._test).grid(row=2, column=0, padx=4, pady=4)
        ttk.Button(cal, text="保存配置", command=self._save).grid(row=2, column=1, padx=4)
        self.lbl_notice = ttk.Label(cal, text="", foreground="#0a0")
        self.lbl_notice.grid(row=3, column=0, columnspan=2, sticky="w", padx=6)

    def _slider(self, parent, label, var, lo, hi, on_change, fmt) -> None:
        box = ttk.Frame(parent)
        box.pack(fill="x", padx=6, pady=2)
        ttk.Label(box, text=label, width=14).pack(side="left")
        ttk.Scale(box, from_=lo, to=hi, variable=var, length=150,
                  command=lambda _v: on_change()).pack(side="left", padx=6)
        lbl = ttk.Label(box, text=fmt % var.get(), width=6)
        lbl.pack(side="left")
        var.trace_add("write", lambda *_: lbl.config(text=fmt % var.get()))

    # ------------------------------------------------------------ 动作
    def _start_device(self) -> None:
        self.cfg.device.kind = self.var_kind.get()
        try:
            self.cfg.device.port = int(self.var_port.get())
        except ValueError:
            messagebox.showerror("端口错误", "端口必须是数字")
            return
        try:
            self.engine.stop()
        except Exception:
            pass
        self.engine = Engine(self.cfg)
        self.engine.on_event = lambda ev: self.events.append(_describe(ev))
        self.engine.device.start()
        payload = getattr(self.engine.device, "qr_payload", "")
        self.var_url.set(getattr(self.engine.device, "ws_url", "(mock 模式无需连接)"))
        self._draw_qr(payload)

    def _stop_device(self) -> None:
        self.engine.stop()

    def _start_detect(self) -> None:
        try:
            self.engine.device.start()
        except Exception:
            pass
        self.engine.start()

    def _stop(self) -> None:
        self.engine.stop()

    def _arm(self) -> None:
        self.engine.safety.reset_session()
        self.engine.arm()

    def _copy_url(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.var_url.get())

    def _draw_qr(self, payload: str) -> None:
        self.qr_canvas.delete("all")
        if not payload:
            return
        try:
            import qrcode

            qr = qrcode.QRCode(border=1, box_size=1)
            qr.add_data(payload)
            qr.make(fit=True)
            matrix = qr.get_matrix()
        except Exception:
            self.qr_canvas.create_text(90, 90, text="未安装 qrcode\n请手输地址", fill="#eee")
            return
        n = len(matrix)
        cell = max(1, 176 // n)
        off = (180 - cell * n) // 2
        for y, row in enumerate(matrix):
            for x, val in enumerate(row):
                if val:
                    self.qr_canvas.create_rectangle(
                        off + x * cell, off + y * cell,
                        off + (x + 1) * cell, off + (y + 1) * cell,
                        fill="#fff", outline="")

    def _set_rule_enabled(self, key: str, var: tk.BooleanVar) -> None:
        self.cfg.rules[key].enabled = bool(var.get())

    def _set_rule_pct(self, key: str, var: tk.DoubleVar) -> None:
        self.cfg.rules[key].base_pct = float(var.get())
        getattr(self, f"_lbl_rule_{key}").config(text=f"{var.get():.0f}%")

    def _set_master(self) -> None:
        self.cfg.safety.master_multiplier = float(self.var_master.get())

    def _set_maxpct(self) -> None:
        self.cfg.safety.max_pct = float(self.var_maxpct.get())

    def _set_maxabs(self) -> None:
        self.cfg.safety.max_absolute = int(self.var_maxabs.get())

    def _test(self) -> None:
        if not self.engine.device.connected and self.cfg.device.kind != "mock":
            messagebox.showwarning("未连接", "请先在手机上扫码连接，或用 mock 模式")
            return
        self._countdown(3)

    def _countdown(self, n: int) -> None:
        if n <= 0:
            self.engine.test_pulse(pct=5.0, ms=700)
            self.lbl_notice.config(text="已发送测试脉冲（5%）")
            return
        self.lbl_notice.config(text=f"{n} 秒后输出测试脉冲……（点急停可中止）")
        self.root.after(1000, lambda: self._countdown(n - 1))

    def _save(self, silent: bool = False) -> None:
        try:
            self.cfg.save(self.config_path)
            if not silent:
                self.lbl_notice.config(text=f"已保存到 {self.config_path}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    # ------------------------------------------------------------ 循环
    def _poll(self) -> None:
        st = self.engine.status
        hp = "--" if st.hp is None else f"{st.hp * 100:5.1f}%"
        self.lbl_hp.config(text=f"HP: {hp}")
        self.lbl_injury.config(text=st.hook_detail or "等待游戏内 Lua 桥上报……")
        flag = "运行中" if st.running else "已停止"
        armed = "已武装" if self.engine.safety.armed else f"已静音({self.engine.safety.mute_reason})"
        self.lbl_out.config(
            text=f"输出: A={st.output_a} B={st.output_b}  ({st.pct:.0f}%)  {flag}  {armed}")
        conn = "已连接" if self.engine.device.connected else "未连接"
        self.lbl_device.config(
            text=f"{conn}  ·  {self.engine.safety.session_seconds:.0f}s 累计输出  ·  来源={st.source}")

        if self.events:
            self.txt_events.config(state="normal")
            while self.events:
                self.txt_events.insert("end", self.events.popleft() + "\n")
            self.txt_events.see("end")
            self.txt_events.config(state="disabled")
        self.root.after(200, self._poll)

    def _on_close(self) -> None:
        try:
            self.engine.stop()
        finally:
            if self._hotkeys is not None:
                self._hotkeys.stop()
            self.root.destroy()

    def run(self) -> None:
        self._hotkeys = HotkeyWatcher({
            self.cfg.safety.emergency_key.upper(): lambda: self.engine.trip("F12 急停"),
            self.cfg.safety.pause_key.upper(): self._toggle_detect,
        })
        self._hotkeys.start()
        self.lbl_notice.config(text="提示：先「启动连接」，再用 App 扫码；然后启动检测")
        self.root.mainloop()

    def _toggle_detect(self) -> None:
        if self.engine.status.running:
            self.engine.stop()
        else:
            self._start_detect()


def _describe(event) -> str:
    import time as _t

    from .engine import _fmt_event

    return f"{_t.strftime('%H:%M:%S')}  {_fmt_event(event)}"


# --------------------------------------------------------------------- 入口
def run_ui(cfg: AppConfig, config_path: Path) -> None:
    MainWindow(cfg, config_path).run()
