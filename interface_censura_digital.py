#!/usr/bin/env python3
"""
Sistema de Censura Digital - Interface Gráfica v2.1
Monitor visual com semáforos, VU meter, streaming RTMP/Icecast e autostart.
"""

import json
import math
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path

# Gravação em processo separado: se travar (crash nativo), a janela continua aberta
USE_WORKER_RECORDING = True
WORKER_STATUS_FILE = "censura_status.json"
WORKER_STOP_FILE = "censura_stop.flag"
WORKER_RTMP_CMD_FILE = "stream_rtmp_cmd.json"
WORKER_ICECAST_CMD_FILE = "stream_icecast_cmd.json"
WORKER_SCRIPT = "recorder_worker.py"

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

LOGO_FILENAME = "aleceplay.png"

# Gravador/Processor/StreamManager carregados sob demanda (evita travar no Windows)
CensuraDigital = None
AudioProcessor = None
StreamManager = None

try:
    from tkcalendar import Calendar
    CALENDAR_AVAILABLE = True
except ImportError:
    CALENDAR_AVAILABLE = False

MAX_LOG_LINES = 1000
MONITOR_REFRESH_MS = 150


# ── Custom widgets ────────────────────────────────────────────────


class SemaphoreWidget(ttk.Frame):
    """Indicador circular estilo semáforo (usa composição para compatibilidade Tcl/Tk 9)."""

    _PALETTE = {
        "off":    ("#555555", "#333333"),
        "red":    ("#FF2222", "#4D0000"),
        "green":  ("#22CC22", "#004D00"),
        "yellow": ("#FFCC00", "#4D3D00"),
    }

    def __init__(self, parent, size=64):
        super().__init__(parent)
        self._c = tk.Canvas(self, width=size + 10, height=size + 10, highlightthickness=0)
        self._c.pack()
        pad = 5
        self._glow = self._c.create_oval(
            pad, pad, size + pad, size + pad,
            outline="#666666", width=2, fill="#222222",
        )
        inset = 8
        self._light = self._c.create_oval(
            pad + inset, pad + inset, size + pad - inset, size + pad - inset,
            outline="", fill="#555555",
        )
        self._state = "off"

    def set_state(self, state: str):
        if state == self._state:
            return
        self._state = state
        fill, glow_bg = self._PALETTE.get(state, self._PALETTE["off"])
        self._c.itemconfig(self._light, fill=fill)
        self._c.itemconfig(self._glow, fill=glow_bg if state != "off" else "#222222")


class VUMeterWidget(ttk.Frame):
    """Barra horizontal dBFS com decay balístico, ataque rápido e peak hold."""

    DB_FLOOR = -60.0
    DB_YELLOW = -12.0
    DB_RED = -3.0

    ATTACK_COEFF = 0.7
    RELEASE_COEFF = 0.12
    PEAK_HOLD_S = 1.5
    ANIM_MS = 30

    def __init__(self, parent, bar_width=350, bar_height=28):
        super().__init__(parent)
        self._bw = bar_width
        self._bh = bar_height
        self._c = tk.Canvas(self, width=bar_width, height=bar_height, highlightthickness=0, bg="#1a1a1a")
        self._c.pack()

        usable = bar_width - 4
        yellow_x = 2 + int(usable * (-self.DB_FLOOR + self.DB_YELLOW) / -self.DB_FLOOR)
        red_x = 2 + int(usable * (-self.DB_FLOOR + self.DB_RED) / -self.DB_FLOOR)

        self._c.create_rectangle(2, 2, yellow_x, bar_height - 2, outline="", fill="#0a3a0a")
        self._c.create_rectangle(yellow_x, 2, red_x, bar_height - 2, outline="", fill="#3a3a0a")
        self._c.create_rectangle(red_x, 2, bar_width - 2, bar_height - 2, outline="", fill="#3a0a0a")
        self._c.create_rectangle(1, 1, bar_width - 1, bar_height - 1, outline="#444444")

        self._bar = self._c.create_rectangle(2, 2, 2, bar_height - 2, outline="", fill="#22CC22")
        self._peak_line = self._c.create_line(2, 2, 2, bar_height - 2, fill="#FF8800", width=2, state="hidden")

        for db_mark in [-48, -36, -24, -18, -12, -6, -3, 0]:
            mx = 2 + int(usable * (db_mark - self.DB_FLOOR) / -self.DB_FLOOR)
            self._c.create_line(mx, 1, mx, 5, fill="#888888")
            self._c.create_line(mx, bar_height - 5, mx, bar_height - 1, fill="#888888")

        self._target_db = self.DB_FLOOR
        self._display_db = self.DB_FLOOR
        self._peak_db = self.DB_FLOOR
        self._peak_ts = 0.0
        self._animating = False

    def set_db(self, db: float):
        """Define nível alvo — animação interna suaviza a transição."""
        db = max(self.DB_FLOOR, min(0.0, db))
        self._target_db = db
        if db > self._peak_db:
            self._peak_db = db
            self._peak_ts = time.time()
        if not self._animating:
            self._animating = True
            self._animate()

    def _animate(self):
        if self._target_db > self._display_db:
            self._display_db += (self._target_db - self._display_db) * self.ATTACK_COEFF
        else:
            self._display_db += (self._target_db - self._display_db) * self.RELEASE_COEFF

        if self._display_db < self.DB_FLOOR + 0.3:
            self._display_db = self.DB_FLOOR

        now = time.time()
        if now - self._peak_ts > self.PEAK_HOLD_S:
            self._peak_db += (self.DB_FLOOR - self._peak_db) * 0.08
            if self._peak_db < self.DB_FLOOR + 0.3:
                self._peak_db = self.DB_FLOOR

        self._draw()

        still_moving = (
            abs(self._display_db - self._target_db) > 0.2
            or self._peak_db > self.DB_FLOOR + 0.3
        )
        if still_moving:
            self.after(self.ANIM_MS, self._animate)
        else:
            self._animating = False

    def _draw(self):
        db = self._display_db
        frac = (db - self.DB_FLOOR) / -self.DB_FLOOR
        bar_x = 2 + int(frac * (self._bw - 4))

        if db < self.DB_YELLOW:
            color = "#22CC22"
        elif db < self.DB_RED:
            color = "#CCCC00"
        else:
            color = "#FF2222"

        self._c.coords(self._bar, 2, 2, bar_x, self._bh - 2)
        self._c.itemconfig(self._bar, fill=color)

        if self._peak_db > self.DB_FLOOR + 0.3:
            peak_frac = (self._peak_db - self.DB_FLOOR) / -self.DB_FLOOR
            peak_x = 2 + int(peak_frac * (self._bw - 4))
            self._c.coords(self._peak_line, peak_x, 2, peak_x, self._bh - 2)
            self._c.itemconfig(self._peak_line, state="normal")
        else:
            self._c.itemconfig(self._peak_line, state="hidden")


# ── Processor window (unchanged) ─────────────────────────────────


class ProcessorWindow(tk.Toplevel):
    def __init__(self, parent, processor):
        super().__init__(parent)
        self.transient(parent)
        self.title("Processador de Gravações")
        self.geometry("540x520")
        self.minsize(540, 520)
        self.processor = processor

        frame = ttk.Frame(self, padding="10")
        frame.pack(expand=True, fill="both")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        date_frame = ttk.LabelFrame(frame, text="1. Selecione a Data", padding="10")
        date_frame.pack(fill="x", pady=5)
        if CALENDAR_AVAILABLE:
            self.cal = Calendar(date_frame, selectmode="day", date_pattern="yyyy-mm-dd")
            self.cal.pack(pady=10)
        else:
            ttk.Label(date_frame, text="Data (AAAA-MM-DD):").pack(side="left", padx=5)
            self.date_entry = ttk.Entry(date_frame)
            self.date_entry.pack(side="left", expand=True, fill="x")
            self.date_entry.insert(0, datetime.now().strftime("%Y-%m-%d"))

        action_frame = ttk.LabelFrame(frame, text="2. Inicie o Processamento", padding="10")
        action_frame.pack(fill="x", pady=5)
        self.keep_mp3_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(action_frame, text="Manter arquivos MP3 após compactar", variable=self.keep_mp3_var).pack(anchor="w")
        self.process_btn = ttk.Button(action_frame, text="Processar Gravações da Data Selecionada", command=self.run_process)
        self.process_btn.pack(fill="x", pady=10)

        cut_frame = ttk.LabelFrame(frame, text="3. Extrair Intervalo (Trecho)", padding="10")
        cut_frame.pack(fill="x", pady=5)
        ttk.Label(cut_frame, text="Início (AAAA-MM-DD HH:MM:SS):").grid(row=0, column=0, sticky="w", pady=2)
        self.cut_start = ttk.Entry(cut_frame)
        self.cut_start.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(cut_frame, text="Fim (AAAA-MM-DD HH:MM:SS):").grid(row=1, column=0, sticky="w", pady=2)
        self.cut_end = ttk.Entry(cut_frame)
        self.cut_end.grid(row=1, column=1, sticky="ew", padx=5)
        cut_frame.columnconfigure(1, weight=1)
        self.cut_btn = ttk.Button(cut_frame, text="Extrair Intervalo", command=self.run_cut)
        self.cut_btn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)

        log_frame = ttk.LabelFrame(frame, text="Progresso", padding="10")
        log_frame.pack(expand=True, fill="both", pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state="disabled", height=10)
        self.log_text.pack(expand=True, fill="both")

    def log_message(self, message):
        try:
            self.log_text.config(state="normal")
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state="disabled")
        except tk.TclError:
            pass

    def run_process(self):
        self.log_text.config(state="normal")
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state="disabled")
        self.process_btn.config(state="disabled")
        target_date = self.cal.get_date() if CALENDAR_AVAILABLE else self.date_entry.get()
        callback = lambda msg: self.after(0, self.log_message, msg)
        self.processor.run_processing(target_date, self.keep_mp3_var.get(), progress_callback=callback)
        self.after(3000, lambda: self.process_btn.config(state="normal"))

    def run_cut(self):
        self.process_btn.config(state="disabled")
        self.cut_btn.config(state="disabled")
        start, end = self.cut_start.get().strip(), self.cut_end.get().strip()
        callback = lambda msg: self.after(0, self.log_message, msg)

        def done(msg):
            self.after(0, self.log_message, msg or "Intervalo processado.")
            self.after(0, lambda: self.process_btn.config(state="normal"))
            self.after(0, lambda: self.cut_btn.config(state="normal"))

        def task():
            try:
                result = self.processor.extract_interval(start, end, progress_callback=callback, blocking=True)
                done(f"Trecho salvo em: {result}" if result else "Falha ao extrair o trecho.")
            except Exception as e:
                done(f"Erro: {e}")

        threading.Thread(target=task, daemon=True).start()

    def on_closing(self):
        self.processor.stop()
        self.destroy()


# ── Main interface ────────────────────────────────────────────────


class CensuraDigitalInterface:
    def __init__(self, root):
        self.root = root
        self.root.title("Alece Play - Sistema de Censura Digital v2.2")
        self.root.geometry("660x720")
        self.root.minsize(660, 680)

        self._logo_img = None
        self._load_logo()

        self.censura = None
        self.processor = None
        self.stream_manager = None
        self._stream_error = False
        self.audio_devices = []
        self.status_poller = None
        self._monitor_poller = None
        self._worker_proc = None
        self._last_worker_rtmp_msg = ""
        self._last_worker_ice_msg = ""
        self._cached_worker_data = None

        # Carregamento direto no mesmo processo (como no censura-digital funcional)
        load_frame = tk.Frame(self.root, bg="#1a1a2e")
        load_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        ttk.Label(load_frame, text="Iniciando Sistema de Censura Digital...", font=("Arial", 14)).pack(expand=True, pady=50)
        self.root.update()
        try:
            from gravador_censura_digital import CensuraDigital as CD
            from processador_audio import AudioProcessor as AP
            from stream_manager import StreamManager as SM
            self.censura = CD()
            self.processor = AP()
            self.stream_manager = SM(self.censura.config, logger=self.censura.logger)
            self.censura.set_stream_manager(self.stream_manager)
            self.censura.set_alert_callback(self._on_watchdog_alert)
            self.censura.set_recording_failed_callback(self._on_recording_failed)
            self.stream_manager.set_status_callback(self._on_stream_status)
        except Exception as e:
            load_frame.destroy()
            messagebox.showerror("Erro ao iniciar", str(e))
            self.root.destroy()
            return
        load_frame.destroy()
        self.setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.refresh_devices_list()
        self._start_monitor_loop()
        autostart = self.censura.config.get("interface", {}).get("autostart_recording", False)
        if autostart:
            self.root.after(800, self.start_recording)

    # ── Dependency error screen ────────────────────────────────

    def _show_dependency_error(self, error_msg):
        import platform
        is_win = platform.system() == "Windows"

        frame = ttk.Frame(self.root, padding="30")
        frame.pack(expand=True, fill="both")

        ttk.Label(frame, text="Dependência não encontrada", font=("Arial", 14, "bold")).pack(pady=(0, 15))
        ttk.Label(frame, text=f"Erro: {error_msg}", wraplength=500, foreground="red").pack(pady=(0, 15))

        if "pyaudio" in error_msg.lower() or "sounddevice" in error_msg.lower() or "audio" in error_msg.lower():
            if is_win:
                instructions = (
                    "No Windows, use sounddevice (mais estável que PyAudio):\n\n"
                    "  pip install sounddevice\n\n"
                    "Ou tente PyAudio:\n\n"
                    "  pip install pyaudio"
                )
            else:
                instructions = (
                    "Instale as dependências:\n\n"
                    "  brew install portaudio\n"
                    "  pip install pyaudio numpy\n"
                )
        else:
            instructions = (
                "Instale todas as dependências:\n\n"
                "  pip install -r requirements.txt\n"
            )

        text = tk.Text(frame, wrap=tk.WORD, height=12, width=60, font=("Courier", 11))
        text.pack(pady=10, fill="x")
        text.insert("1.0", instructions)
        text.config(state="disabled")

        ttk.Button(frame, text="Fechar", command=self.root.destroy).pack(pady=10)

    # ── Logo ─────────────────────────────────────────────────────

    def _load_logo(self):
        if not PIL_AVAILABLE:
            return
        if getattr(sys, "frozen", False):
            base_dir = Path(sys._MEIPASS)
        else:
            base_dir = Path(__file__).resolve().parent
        logo_path = base_dir / LOGO_FILENAME
        if not logo_path.exists():
            return
        try:
            img = Image.open(logo_path).convert("RGBA")

            # Window icon: pad to square then resize for crisp rendering
            sq = max(img.width, img.height)
            icon_base = Image.new("RGBA", (sq, sq), (0, 0, 0, 0))
            offset_x = (sq - img.width) // 2
            offset_y = (sq - img.height) // 2
            icon_base.paste(img, (offset_x, offset_y))

            import platform
            if platform.system() == "Windows":
                import tempfile
                ico_path = Path(tempfile.gettempdir()) / "aleceplay.ico"
                sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
                ico_images = [icon_base.resize(s, Image.LANCZOS) for s in sizes]
                ico_images[0].save(str(ico_path), format="ICO", sizes=sizes, append_images=ico_images[1:])
                self.root.iconbitmap(str(ico_path))
            else:
                self._icon_imgs = []
                for sz in (256, 128, 64, 32):
                    resized = icon_base.resize((sz, sz), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(resized)
                    self._icon_imgs.append(photo)
                self.root.iconphoto(True, *self._icon_imgs)

            # Logo for Monitor tab
            target_h = 80
            ratio = target_h / img.height
            target_w = int(img.width * ratio)
            self._logo_img = ImageTk.PhotoImage(img.resize((target_w, target_h), Image.LANCZOS))
        except Exception:
            pass

    # ── UI Setup ──────────────────────────────────────────────────

    def setup_ui(self):
        notebook = ttk.Notebook(self.root, padding="10")
        notebook.pack(expand=True, fill="both")

        tab_monitor = ttk.Frame(notebook)
        tab_rec = ttk.Frame(notebook)
        tab_stream = ttk.Frame(notebook)
        tab_config = ttk.Frame(notebook)

        notebook.add(tab_monitor, text=" Monitor ")
        notebook.add(tab_rec, text=" Gravação ")
        notebook.add(tab_stream, text=" Streaming ")
        notebook.add(tab_config, text=" Configurações ")

        self.notebook = notebook
        self._create_monitor_tab(tab_monitor)
        self._create_recording_tab(tab_rec)
        self._create_streaming_tab(tab_stream)
        self._create_config_tab(tab_config)

    # ── Monitor tab ───────────────────────────────────────────────

    def _create_monitor_tab(self, parent):
        frame = ttk.Frame(parent, padding="15")
        frame.pack(expand=True, fill="both")

        # Logo header
        if self._logo_img:
            logo_label = ttk.Label(frame, image=self._logo_img, anchor="center")
            logo_label.pack(pady=(0, 8))

        # VU meter
        vu_frame = ttk.LabelFrame(frame, text="Nível de Áudio", padding="10")
        vu_frame.pack(fill="x", pady=(0, 10))

        vu_inner = ttk.Frame(vu_frame)
        vu_inner.pack(fill="x")

        self.vu_meter = VUMeterWidget(vu_inner, bar_width=420, bar_height=28)
        self.vu_meter.pack(side="left", padx=(0, 10))

        self.vu_label = ttk.Label(vu_inner, text="--- dB", width=12, anchor="w")
        self.vu_label.pack(side="left")

        # Semaphore cards row
        cards_frame = ttk.Frame(frame)
        cards_frame.pack(fill="x", pady=5)
        cards_frame.columnconfigure((0, 1, 2), weight=1)

        # Card: Gravação
        rec_card = ttk.LabelFrame(cards_frame, text="Gravação", padding="10")
        rec_card.grid(row=0, column=0, padx=5, sticky="nsew")
        self.rec_semaphore = SemaphoreWidget(rec_card, size=64)
        self.rec_semaphore.pack(pady=(5, 8))
        self.rec_card_status = ttk.Label(rec_card, text="INATIVO", anchor="center", font=("Arial", 10, "bold"))
        self.rec_card_status.pack()
        self.rec_card_detail = ttk.Label(rec_card, text="--", anchor="center", font=("Arial", 9))
        self.rec_card_detail.pack(pady=(2, 0))
        rec_btn_f = ttk.Frame(rec_card)
        rec_btn_f.pack(pady=(6, 0), fill="x")
        self.mon_rec_start = ttk.Button(rec_btn_f, text="Iniciar", command=self.start_recording)
        self.mon_rec_start.pack(side="left", expand=True, fill="x", padx=1)
        self.mon_rec_stop = ttk.Button(rec_btn_f, text="Parar", command=self.stop_recording, state="disabled")
        self.mon_rec_stop.pack(side="left", expand=True, fill="x", padx=1)

        # Card: RTMP
        rtmp_card = ttk.LabelFrame(cards_frame, text="RTMP", padding="10")
        rtmp_card.grid(row=0, column=1, padx=5, sticky="nsew")
        self.rtmp_semaphore = SemaphoreWidget(rtmp_card, size=64)
        self.rtmp_semaphore.pack(pady=(5, 8))
        self.rtmp_card_status = ttk.Label(rtmp_card, text="INATIVO", anchor="center", font=("Arial", 10, "bold"))
        self.rtmp_card_status.pack()
        self.rtmp_card_detail = ttk.Label(rtmp_card, text="--", anchor="center", font=("Arial", 9))
        self.rtmp_card_detail.pack(pady=(2, 0))
        rtmp_btn_f = ttk.Frame(rtmp_card)
        rtmp_btn_f.pack(pady=(6, 0), fill="x")
        self.mon_rtmp_start = ttk.Button(rtmp_btn_f, text="Iniciar", command=self.start_rtmp)
        self.mon_rtmp_start.pack(side="left", expand=True, fill="x", padx=1)
        self.mon_rtmp_stop = ttk.Button(rtmp_btn_f, text="Parar", command=self.stop_rtmp, state="disabled")
        self.mon_rtmp_stop.pack(side="left", expand=True, fill="x", padx=1)

        # Card: Icecast
        ice_card = ttk.LabelFrame(cards_frame, text="Icecast", padding="10")
        ice_card.grid(row=0, column=2, padx=5, sticky="nsew")
        self.ice_semaphore = SemaphoreWidget(ice_card, size=64)
        self.ice_semaphore.pack(pady=(5, 8))
        self.ice_card_status = ttk.Label(ice_card, text="INATIVO", anchor="center", font=("Arial", 10, "bold"))
        self.ice_card_status.pack()
        self.ice_card_detail = ttk.Label(ice_card, text="--", anchor="center", font=("Arial", 9))
        self.ice_card_detail.pack(pady=(2, 0))
        ice_btn_f = ttk.Frame(ice_card)
        ice_btn_f.pack(pady=(6, 0), fill="x")
        self.mon_ice_start = ttk.Button(ice_btn_f, text="Iniciar", command=self.start_icecast)
        self.mon_ice_start.pack(side="left", expand=True, fill="x", padx=1)
        self.mon_ice_stop = ttk.Button(ice_btn_f, text="Parar", command=self.stop_icecast, state="disabled")
        self.mon_ice_stop.pack(side="left", expand=True, fill="x", padx=1)

        # Streaming metrics panel
        metrics_frame = ttk.LabelFrame(frame, text="Métricas de Transmissão", padding="8")
        metrics_frame.pack(fill="x", pady=(8, 0))
        metrics_frame.columnconfigure((0, 1), weight=1)

        rtmp_m = ttk.Frame(metrics_frame)
        rtmp_m.grid(row=0, column=0, padx=5, sticky="nsew")
        ttk.Label(rtmp_m, text="RTMP", font=("Arial", 9, "bold")).pack(anchor="w")
        self.rtmp_metrics_var = tk.StringVar(value="--")
        ttk.Label(rtmp_m, textvariable=self.rtmp_metrics_var, font=("Consolas", 8), justify=tk.LEFT).pack(anchor="w")

        ice_m = ttk.Frame(metrics_frame)
        ice_m.grid(row=0, column=1, padx=5, sticky="nsew")
        ttk.Label(ice_m, text="Icecast", font=("Arial", 9, "bold")).pack(anchor="w")
        self.ice_metrics_var = tk.StringVar(value="--")
        ttk.Label(ice_m, textvariable=self.ice_metrics_var, font=("Consolas", 8), justify=tk.LEFT).pack(anchor="w")

        # Quality bar (RTMP)
        qbar_frame = ttk.Frame(metrics_frame)
        qbar_frame.grid(row=1, column=0, columnspan=2, padx=5, pady=(4, 0), sticky="ew")
        ttk.Label(qbar_frame, text="Qualidade:", font=("Arial", 8)).pack(side="left")
        self._quality_canvas = tk.Canvas(qbar_frame, width=200, height=12, highlightthickness=0, bg="#1a1a1a")
        self._quality_canvas.pack(side="left", padx=5)
        self._quality_bar = self._quality_canvas.create_rectangle(1, 1, 1, 11, outline="", fill="#22CC22")
        self._quality_canvas.create_rectangle(0, 0, 200, 12, outline="#444444")
        self.quality_label = ttk.Label(qbar_frame, text="--", font=("Arial", 8), width=8)
        self.quality_label.pack(side="left")

        # Autostart checkbox
        auto_frame = ttk.Frame(frame)
        auto_frame.pack(fill="x", pady=(8, 3))
        self.autostart_var = tk.BooleanVar(
            value=self.censura.config.get("interface", {}).get("autostart_recording", False)
        )
        ttk.Checkbutton(
            auto_frame,
            text="Iniciar gravação automaticamente ao abrir o programa",
            variable=self.autostart_var,
            command=self._save_autostart,
        ).pack(anchor="w")

        # Alert bar
        alert_frame = ttk.LabelFrame(frame, text="Alertas", padding="5")
        alert_frame.pack(fill="x", pady=(5, 0))
        self.alert_var = tk.StringVar(value="Nenhum alerta")
        ttk.Label(alert_frame, textvariable=self.alert_var, wraplength=550).pack(fill="x")

        ttk.Label(frame, text="Alece Play  |  Desenvolvido por Rodrigo Lima", font=("Arial", 8)).pack(side="bottom", pady=3)

    def _save_autostart(self):
        if "interface" not in self.censura.config:
            self.censura.config["interface"] = {}
        self.censura.config["interface"]["autostart_recording"] = self.autostart_var.get()
        try:
            self.censura.save_config()
        except Exception:
            pass

    # ── Monitor update loop ───────────────────────────────────────

    def _start_monitor_loop(self):
        self._update_monitor()

    def _apply_worker_data(self, status, stream_st, wd):
        """Mescla dados do worker no status/stream_st local."""
        status = {
            **status,
            "is_recording": wd.get("is_recording", False),
            "chunk_counter": wd.get("chunk_counter", 0),
            "current_chunk_start": wd.get("current_chunk_start"),
            "current_level": wd.get("current_level", 0.0),
            "stall_count": wd.get("stall_count", 0),
        }
        stream_st = {
            **stream_st,
            "rtmp_active": wd.get("rtmp_active", False),
            "icecast_active": wd.get("icecast_active", False),
            "rtmp_status": wd.get("rtmp_status", "Inativo"),
            "icecast_status": wd.get("icecast_status", "Inativo"),
            "rtmp_metrics": wd.get("rtmp_metrics", {}),
            "icecast_metrics": wd.get("icecast_metrics", {}),
        }
        return status, stream_st

    def _update_monitor(self):
        status = self.censura.get_status()
        stream_st = self.stream_manager.get_status()
        if USE_WORKER_RECORDING and self._worker_proc is not None:
            if os.path.isfile(WORKER_STATUS_FILE):
                try:
                    with open(WORKER_STATUS_FILE, "r", encoding="utf-8") as f:
                        self._cached_worker_data = json.load(f)
                except Exception:
                    pass
            if self._cached_worker_data is not None:
                status, stream_st = self._apply_worker_data(
                    status, stream_st, self._cached_worker_data,
                )

        # Route streaming status changes to alert bar (worker mode)
        for proto, key, attr in (
            ("RTMP", "rtmp_status", "_last_worker_rtmp_msg"),
            ("Icecast", "icecast_status", "_last_worker_ice_msg"),
        ):
            msg = stream_st.get(key, "")
            prev = getattr(self, attr, "")
            if msg and msg != prev:
                setattr(self, attr, msg)
                if msg != "Inativo" and any(kw in msg.lower() for kw in self._ALERT_KEYWORDS):
                    ts = time.strftime('%H:%M:%S')
                    self.alert_var.set(f"[{ts}] {proto}: {msg}")

        is_rec = status["is_recording"]
        stalls = status.get("stall_count", 0)

        # VU meter (escala dBFS logarítmica)
        level = status.get("current_level", 0.0) if is_rec else 0.0
        if is_rec and level > 0:
            db = 20 * math.log10(max(level, 1e-10))
            db = max(-60.0, db)
            self.vu_meter.set_db(db)
            self.vu_label.config(text=f"{db:+.1f} dBFS")
        else:
            self.vu_meter.set_db(-60.0)
            self.vu_label.config(text="--- dBFS")

        # Recording semaphore
        if is_rec and stalls == 0:
            self.rec_semaphore.set_state("red")
            self.rec_card_status.config(text="GRAVANDO")
        elif is_rec and stalls > 0:
            self.rec_semaphore.set_state("yellow")
            self.rec_card_status.config(text="ALERTA")
        else:
            self.rec_semaphore.set_state("off")
            self.rec_card_status.config(text="INATIVO")

        if is_rec and status.get("current_chunk_start"):
            start_time = datetime.fromisoformat(status["current_chunk_start"])
            elapsed = datetime.now() - start_time
            mins, secs = divmod(int(elapsed.total_seconds()), 60)
            self.rec_card_detail.config(text=f"Chunk #{status['chunk_counter']}  {mins:02d}:{secs:02d}")
        else:
            self.rec_card_detail.config(text="--")

        # RTMP semaphore
        rtmp_active = stream_st.get("rtmp_active", False)
        rtmp_m = stream_st.get("rtmp_metrics", {})
        if rtmp_active:
            rtmp_connected = rtmp_m.get("connected", False)
            if rtmp_connected:
                self.rtmp_semaphore.set_state("red")
                self.rtmp_card_status.config(text="ON AIR")
                self.rtmp_card_detail.config(
                    text=f"{rtmp_m.get('target_bitrate_kbps', 0)} kbps"
                )
            else:
                self.rtmp_semaphore.set_state("yellow")
                self.rtmp_card_status.config(text="CONECTANDO")
                self.rtmp_card_detail.config(text="Aguardando servidor...")
        elif self._stream_error:
            self.rtmp_semaphore.set_state("yellow")
            self.rtmp_card_status.config(text="ERRO")
            self.rtmp_card_detail.config(text=rtmp_m.get("last_error", "")[:30])
        elif is_rec:
            self.rtmp_semaphore.set_state("green")
            self.rtmp_card_status.config(text="PRONTO")
            self.rtmp_card_detail.config(text="Gravação ativa")
        else:
            self.rtmp_semaphore.set_state("off")
            self.rtmp_card_status.config(text="INATIVO")
            self.rtmp_card_detail.config(text="--")

        # Icecast semaphore
        ice_active = stream_st.get("icecast_active", False)
        ice_m = stream_st.get("icecast_metrics", {})
        if ice_active:
            ice_connected = ice_m.get("connected", False)
            if ice_connected:
                self.ice_semaphore.set_state("red")
                self.ice_card_status.config(text="ON AIR")
                self.ice_card_detail.config(
                    text=f"{ice_m.get('target_bitrate_kbps', 0)} kbps"
                )
            else:
                self.ice_semaphore.set_state("yellow")
                self.ice_card_status.config(text="CONECTANDO")
                self.ice_card_detail.config(text="Aguardando servidor...")
        elif self._stream_error:
            self.ice_semaphore.set_state("yellow")
            self.ice_card_status.config(text="ERRO")
            self.ice_card_detail.config(text=ice_m.get("last_error", "")[:30])
        elif is_rec:
            self.ice_semaphore.set_state("green")
            self.ice_card_status.config(text="PRONTO")
            self.ice_card_detail.config(text="Gravação ativa")
        else:
            self.ice_semaphore.set_state("off")
            self.ice_card_status.config(text="INATIVO")
            self.ice_card_detail.config(text="--")

        # Streaming metrics panel
        self._update_metrics_display(rtmp_active, rtmp_m, ice_active, ice_m)

        # Sync monitor tab buttons
        self.mon_rec_start.config(state="disabled" if is_rec else "normal")
        self.mon_rec_stop.config(state="normal" if is_rec else "disabled")
        self.mon_rtmp_start.config(state="normal" if (is_rec and not rtmp_active) else "disabled")
        self.mon_rtmp_stop.config(state="normal" if rtmp_active else "disabled")
        self.mon_ice_start.config(state="normal" if (is_rec and not ice_active) else "disabled")
        self.mon_ice_stop.config(state="normal" if ice_active else "disabled")

        self._monitor_poller = self.root.after(MONITOR_REFRESH_MS, self._update_monitor)

    def _format_metrics(self, m: dict, active: bool) -> str:
        if not active or not m:
            return "--"
        conn = "SIM" if m.get("connected", False) else "NÃO"
        uptime = int(m.get("uptime_seconds", 0))
        um, us = divmod(uptime, 60)
        uh, um = divmod(um, 60)
        uptime_str = f"{uh:02d}:{um:02d}:{us:02d}" if uh > 0 else f"{um:02d}:{us:02d}"
        qsize = m.get("queue_size", 0)
        qmax = m.get("queue_max", 2000)
        qpct = (qsize / qmax * 100) if qmax > 0 else 0
        target = m.get("target_bitrate_kbps", 0)
        pcm = m.get("pcm_feed_kbps", 0)
        last_disc = m.get("last_disconnect_ts", 0)
        if last_disc > 0:
            disc_str = datetime.fromtimestamp(last_disc).strftime("%H:%M:%S")
        else:
            disc_str = "—"
        return (
            f"Conexão: {conn} | Bitrate: {target} kbps\n"
            f"Feed PCM: {pcm:.0f} kbps | "
            f"Qualidade: {m.get('quality_score', 1) * 100:.1f}%\n"
            f"Enviados: {m.get('frames_sent', 0)} | "
            f"Drops: {m.get('frames_dropped', 0)}\n"
            f"Queue: {qsize}/{qmax} ({qpct:.0f}%) | "
            f"Pico: {m.get('queue_peak', 0)}\n"
            f"Reconexões: {m.get('reconnect_count', 0)} | "
            f"Último drop: {disc_str}\n"
            f"Uptime: {uptime_str}"
        )

    def _update_metrics_display(self, rtmp_active, rtmp_m, ice_active, ice_m):
        self.rtmp_metrics_var.set(self._format_metrics(rtmp_m, rtmp_active))
        self.ice_metrics_var.set(self._format_metrics(ice_m, ice_active))

        best_quality = 1.0
        any_stream = False
        for active, m in ((rtmp_active, rtmp_m), (ice_active, ice_m)):
            if active and m:
                any_stream = True
                q = m.get("quality_score", 1.0)
                if q < best_quality:
                    best_quality = q

        if any_stream:
            bar_width = 198
            bar_x = max(1, int(best_quality * bar_width))
            if best_quality >= 0.98:
                color = "#22CC22"
            elif best_quality >= 0.95:
                color = "#CCCC00"
            else:
                color = "#FF2222"
            self._quality_canvas.coords(self._quality_bar, 1, 1, bar_x, 11)
            self._quality_canvas.itemconfig(self._quality_bar, fill=color)
            self.quality_label.config(text=f"{best_quality * 100:.1f}%")
        else:
            self._quality_canvas.coords(self._quality_bar, 1, 1, 1, 11)
            self.quality_label.config(text="--")

    # ── Recording tab ─────────────────────────────────────────────

    def _create_recording_tab(self, parent):
        frame = ttk.Frame(parent, padding="15")
        frame.pack(expand=True, fill="both")

        ttk.Button(frame, text="Processar Gravações Antigas...", command=self.open_processor_window).pack(fill="x", pady=(0, 15))

        control_frame = ttk.LabelFrame(frame, text="Controle de Gravação", padding="10")
        control_frame.pack(fill="x", pady=5)
        self.status_var = tk.StringVar(value="Pronto para iniciar")
        ttk.Label(control_frame, textvariable=self.status_var, wraplength=450, justify=tk.CENTER).grid(row=0, column=0, columnspan=2, pady=10)
        self.start_btn = ttk.Button(control_frame, text="Iniciar Gravação", command=self.start_recording)
        self.start_btn.grid(row=1, column=0, padx=5, pady=5, sticky="ew")
        self.stop_btn = ttk.Button(control_frame, text="Parar Gravação", command=self.stop_recording, state="disabled")
        self.stop_btn.grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        control_frame.columnconfigure((0, 1), weight=1)

        monitor_frame = ttk.LabelFrame(frame, text="Monitor de Áudio", padding="10")
        monitor_frame.pack(fill="x", pady=10)
        self.monitor_var = tk.BooleanVar()
        ttk.Checkbutton(monitor_frame, text="Ouvir o que está sendo gravado", variable=self.monitor_var, command=self.toggle_monitoring).grid(row=0, column=0, columnspan=2, sticky="w")
        self.volume_var = tk.DoubleVar(value=1.0)
        ttk.Label(monitor_frame, text="Volume:").grid(row=1, column=0, pady=10, sticky="w")
        self.volume_slider = ttk.Scale(monitor_frame, from_=0.0, to=1.5, orient=tk.HORIZONTAL, variable=self.volume_var, command=self.set_volume)
        self.volume_slider.grid(row=1, column=1, pady=10, sticky="ew")
        monitor_frame.columnconfigure(1, weight=1)

        health_frame = ttk.LabelFrame(frame, text="Saúde da Gravação", padding="10")
        health_frame.pack(fill="x", pady=5)
        self.health_var = tk.StringVar(value="--")
        ttk.Label(health_frame, textvariable=self.health_var, wraplength=450).pack(fill="x")

        self.toggle_monitoring()

    # ── Streaming tab ─────────────────────────────────────────────

    def _create_streaming_tab(self, parent):
        frame = ttk.Frame(parent, padding="15")
        frame.pack(expand=True, fill="both")

        # RTMP
        rtmp_frame = ttk.LabelFrame(frame, text="RTMP (YouTube/Facebook/Genérico)", padding="10")
        rtmp_frame.pack(fill="x", pady=5)
        ttk.Label(rtmp_frame, text="URL RTMP:").grid(row=0, column=0, sticky="w", pady=2)
        rtmp_cfg = self.censura.config.get("streaming", {}).get("rtmp", {})
        self.rtmp_url_var = tk.StringVar(value=rtmp_cfg.get("url", ""))
        ttk.Entry(rtmp_frame, textvariable=self.rtmp_url_var, width=50).grid(row=0, column=1, columnspan=2, sticky="ew", padx=5)
        ttk.Label(rtmp_frame, text="Bitrate (kbps):").grid(row=1, column=0, sticky="w", pady=2)
        self.rtmp_bitrate_var = tk.IntVar(value=rtmp_cfg.get("audio_bitrate_kbps", 128))
        ttk.Spinbox(rtmp_frame, from_=64, to=320, textvariable=self.rtmp_bitrate_var, width=8).grid(row=1, column=1, sticky="w", padx=5)
        self.rtmp_status_var = tk.StringVar(value="Inativo")
        ttk.Label(rtmp_frame, text="Status:").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Label(rtmp_frame, textvariable=self.rtmp_status_var).grid(row=2, column=1, columnspan=2, sticky="w", padx=5)
        btn_rtmp = ttk.Frame(rtmp_frame)
        btn_rtmp.grid(row=3, column=0, columnspan=3, pady=8, sticky="ew")
        self.rtmp_start_btn = ttk.Button(btn_rtmp, text="Iniciar RTMP", command=self.start_rtmp)
        self.rtmp_start_btn.pack(side="left", padx=5, expand=True, fill="x")
        self.rtmp_stop_btn = ttk.Button(btn_rtmp, text="Parar RTMP", command=self.stop_rtmp, state="disabled")
        self.rtmp_stop_btn.pack(side="left", padx=5, expand=True, fill="x")
        self.rtmp_autostart_var = tk.BooleanVar(value=rtmp_cfg.get("enabled", False))
        ttk.Checkbutton(rtmp_frame, text="Iniciar automaticamente com a gravação", variable=self.rtmp_autostart_var).grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        rtmp_frame.columnconfigure(1, weight=1)

        # Icecast
        ice_frame = ttk.LabelFrame(frame, text="Icecast (Rádio Internet)", padding="10")
        ice_frame.pack(fill="x", pady=10)
        ice_cfg = self.censura.config.get("streaming", {}).get("icecast", {})
        ttk.Label(ice_frame, text="Host:").grid(row=0, column=0, sticky="w", pady=2)
        self.ice_host_var = tk.StringVar(value=ice_cfg.get("host", "localhost"))
        ttk.Entry(ice_frame, textvariable=self.ice_host_var, width=25).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(ice_frame, text="Porta:").grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.ice_port_var = tk.IntVar(value=ice_cfg.get("port", 8000))
        ttk.Spinbox(ice_frame, from_=1, to=65535, textvariable=self.ice_port_var, width=7).grid(row=0, column=3, sticky="w", padx=5)
        ttk.Label(ice_frame, text="Mount:").grid(row=1, column=0, sticky="w", pady=2)
        self.ice_mount_var = tk.StringVar(value=ice_cfg.get("mount", "/live"))
        ttk.Entry(ice_frame, textvariable=self.ice_mount_var, width=20).grid(row=1, column=1, sticky="ew", padx=5)
        ttk.Label(ice_frame, text="Senha:").grid(row=1, column=2, sticky="w", padx=(10, 0))
        self.ice_pass_var = tk.StringVar(value=ice_cfg.get("source_password", ""))
        ttk.Entry(ice_frame, textvariable=self.ice_pass_var, show="*", width=15).grid(row=1, column=3, sticky="ew", padx=5)
        ttk.Label(ice_frame, text="Bitrate (kbps):").grid(row=2, column=0, sticky="w", pady=2)
        self.ice_bitrate_var = tk.IntVar(value=ice_cfg.get("audio_bitrate_kbps", 128))
        ttk.Spinbox(ice_frame, from_=64, to=320, textvariable=self.ice_bitrate_var, width=8).grid(row=2, column=1, sticky="w", padx=5)
        self.ice_status_var = tk.StringVar(value="Inativo")
        ttk.Label(ice_frame, text="Status:").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Label(ice_frame, textvariable=self.ice_status_var).grid(row=3, column=1, columnspan=3, sticky="w", padx=5)
        btn_ice = ttk.Frame(ice_frame)
        btn_ice.grid(row=4, column=0, columnspan=4, pady=8, sticky="ew")
        self.ice_start_btn = ttk.Button(btn_ice, text="Iniciar Icecast", command=self.start_icecast)
        self.ice_start_btn.pack(side="left", padx=5, expand=True, fill="x")
        self.ice_stop_btn = ttk.Button(btn_ice, text="Parar Icecast", command=self.stop_icecast, state="disabled")
        self.ice_stop_btn.pack(side="left", padx=5, expand=True, fill="x")
        self.ice_autostart_var = tk.BooleanVar(value=ice_cfg.get("enabled", False))
        ttk.Checkbutton(ice_frame, text="Iniciar automaticamente com a gravação", variable=self.ice_autostart_var).grid(row=5, column=0, columnspan=4, sticky="w", pady=(2, 0))
        ice_frame.columnconfigure(1, weight=1)
        ice_frame.columnconfigure(3, weight=1)

        ttk.Button(frame, text="Salvar Configurações de Streaming", command=self.save_streaming_config).pack(fill="x", pady=10)

    # ── Config tab ────────────────────────────────────────────────

    def _create_config_tab(self, parent):
        frame = ttk.Frame(parent, padding="15")
        frame.pack(expand=True, fill="both")

        device_frame = ttk.LabelFrame(frame, text="Dispositivo de Gravação", padding="10")
        device_frame.pack(fill="x", pady=5)
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(device_frame, textvariable=self.device_var, state="readonly", width=50)
        self.device_combo.grid(row=0, column=0, sticky="ew", pady=5)
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_select)
        ttk.Button(device_frame, text="Atualizar", command=self.refresh_devices_list).grid(row=0, column=1, padx=10)
        self.device_details_var = tk.StringVar(value="Selecione um dispositivo para ver os detalhes.")
        ttk.Label(device_frame, textvariable=self.device_details_var, wraplength=450, justify=tk.LEFT).grid(row=1, column=0, columnspan=2, pady=5, sticky="w")
        device_frame.columnconfigure(0, weight=1)

        dir_frame = ttk.LabelFrame(frame, text="Diretório de Saída", padding="10")
        dir_frame.pack(fill="x", pady=10)
        self.output_dir_var = tk.StringVar(value=self.censura.config["recording"]["output_directory"])
        ttk.Label(dir_frame, text="Salvar gravações em:").pack(anchor="w")
        ttk.Entry(dir_frame, textvariable=self.output_dir_var, state="readonly").pack(side="left", fill="x", expand=True, pady=5)
        ttk.Button(dir_frame, text="Procurar...", command=self.browse_directory).pack(side="left", padx=10)

        ttk.Button(frame, text="Aplicar e Salvar Configurações", command=self.apply_and_save_settings).pack(fill="x", pady=20)

    # ── Streaming controls ────────────────────────────────────────

    def _is_recording_active(self):
        if USE_WORKER_RECORDING and self._worker_proc is not None:
            return True
        return self.censura is not None and self.censura.is_recording

    def start_rtmp(self):
        if not self._is_recording_active():
            messagebox.showwarning("Gravação Inativa", "Inicie a gravação antes de iniciar o streaming RTMP.")
            return
        self._stream_error = False
        url = self.rtmp_url_var.get().strip()
        bitrate = self.rtmp_bitrate_var.get()
        if USE_WORKER_RECORDING and self._worker_proc is not None:
            try:
                with open(WORKER_RTMP_CMD_FILE, "w", encoding="utf-8") as f:
                    json.dump({"action": "start", "url": url, "bitrate": bitrate}, f)
                self.rtmp_start_btn.config(state="disabled")
                self.rtmp_stop_btn.config(state="normal")
            except Exception as e:
                messagebox.showerror("Erro", str(e))
        elif self.stream_manager.start_rtmp(url=url, bitrate=bitrate):
            self.rtmp_start_btn.config(state="disabled")
            self.rtmp_stop_btn.config(state="normal")

    def stop_rtmp(self):
        if USE_WORKER_RECORDING and self._worker_proc is not None:
            try:
                with open(WORKER_RTMP_CMD_FILE, "w", encoding="utf-8") as f:
                    json.dump({"action": "stop"}, f)
                self.rtmp_start_btn.config(state="normal")
                self.rtmp_stop_btn.config(state="disabled")
            except Exception:
                pass
        else:
            self.stream_manager.stop_rtmp()
            self.rtmp_start_btn.config(state="normal")
            self.rtmp_stop_btn.config(state="disabled")
            self.rtmp_status_var.set("Inativo")

    def start_icecast(self):
        if not self._is_recording_active():
            messagebox.showwarning("Gravação Inativa", "Inicie a gravação antes de iniciar o streaming Icecast.")
            return
        self._stream_error = False
        host = self.ice_host_var.get().strip()
        port = self.ice_port_var.get()
        mount = self.ice_mount_var.get().strip()
        password = self.ice_pass_var.get()
        bitrate = self.ice_bitrate_var.get()
        if USE_WORKER_RECORDING and self._worker_proc is not None:
            try:
                with open(WORKER_ICECAST_CMD_FILE, "w", encoding="utf-8") as f:
                    json.dump({"action": "start", "host": host, "port": port, "mount": mount, "password": password, "bitrate": bitrate}, f)
                self.ice_start_btn.config(state="disabled")
                self.ice_stop_btn.config(state="normal")
            except Exception as e:
                messagebox.showerror("Erro", str(e))
        elif self.stream_manager.start_icecast(host=host, port=port, mount=mount, password=password, bitrate=bitrate):
            self.ice_start_btn.config(state="disabled")
            self.ice_stop_btn.config(state="normal")

    def stop_icecast(self):
        if USE_WORKER_RECORDING and self._worker_proc is not None:
            try:
                with open(WORKER_ICECAST_CMD_FILE, "w", encoding="utf-8") as f:
                    json.dump({"action": "stop"}, f)
                self.ice_start_btn.config(state="normal")
                self.ice_stop_btn.config(state="disabled")
            except Exception:
                pass
        else:
            self.stream_manager.stop_icecast()
            self.ice_start_btn.config(state="normal")
            self.ice_stop_btn.config(state="disabled")
            self.ice_status_var.set("Inativo")

    def save_streaming_config(self):
        if "streaming" not in self.censura.config:
            self.censura.config["streaming"] = {}
        self.censura.config["streaming"]["rtmp"] = {
            "enabled": self.rtmp_autostart_var.get(),
            "url": self.rtmp_url_var.get().strip(),
            "audio_bitrate_kbps": self.rtmp_bitrate_var.get(),
        }
        self.censura.config["streaming"]["icecast"] = {
            "enabled": self.ice_autostart_var.get(),
            "host": self.ice_host_var.get().strip(),
            "port": self.ice_port_var.get(),
            "mount": self.ice_mount_var.get().strip(),
            "source_password": self.ice_pass_var.get(),
            "audio_bitrate_kbps": self.ice_bitrate_var.get(),
        }
        try:
            self.censura.save_config()
            self.stream_manager.reload_config(self.censura.config)
            messagebox.showinfo("Sucesso", "Configurações de streaming salvas!")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao salvar: {e}")

    # ── Watchdog / stream status callbacks ────────────────────────

    def _on_watchdog_alert(self, message):
        self.root.after(0, self._show_alert, message)

    def _on_recording_failed(self, message):
        """Chamado quando falha ao abrir stream (ex: timeout). Atualiza UI na main thread."""
        self.root.after(0, self._handle_recording_failed, message)

    def _handle_recording_failed(self, message):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.health_var.set("Falha ao iniciar gravação")
        for child in self.root.winfo_children():
            if isinstance(child, ttk.Notebook):
                tabs = child.tabs()
                if len(tabs) > 3:
                    child.tab(tabs[3], state="normal")
        messagebox.showerror("Erro de Gravação", message)

    def _show_alert(self, message):
        self.health_var.set(f"[{time.strftime('%H:%M:%S')}] {message}")
        self.alert_var.set(f"[{time.strftime('%H:%M:%S')}] {message}")

    def _on_stream_status(self, protocol, message):
        self.root.after(0, self._update_stream_status, protocol, message)

    _ALERT_KEYWORDS = (
        "erro", "encerrou", "reconexão", "tentativa", "ciclo",
        "falha", "conexão estabelecida", "conectando",
    )

    def _update_stream_status(self, protocol, message):
        is_error = "erro" in message.lower() or "encerrou" in message.lower() or "não encontrado" in message.lower()
        if is_error:
            self._stream_error = True

        lower = message.lower()
        if any(kw in lower for kw in self._ALERT_KEYWORDS):
            ts = time.strftime('%H:%M:%S')
            self.alert_var.set(f"[{ts}] {protocol.upper()}: {message}")

        if protocol == "rtmp":
            self.rtmp_status_var.set(message)
            if not self.stream_manager._rtmp_active:
                self.rtmp_start_btn.config(state="normal")
                self.rtmp_stop_btn.config(state="disabled")
        elif protocol == "icecast":
            self.ice_status_var.set(message)
            if not self.stream_manager._icecast_active:
                self.ice_start_btn.config(state="normal")
                self.ice_stop_btn.config(state="disabled")

    # ── Processor / daily ─────────────────────────────────────────

    def open_processor_window(self):
        self.processor.reload_config()
        processor_win = ProcessorWindow(self.root, self.processor)
        processor_win.grab_set()

    def _schedule_daily_processing(self, run_at_minutes_after_midnight: int = 5):
        try:
            now = datetime.now()
            tomorrow = now.date() + timedelta(days=1)
            run_time = datetime.combine(tomorrow, datetime.min.time()) + timedelta(minutes=run_at_minutes_after_midnight)
            delay_seconds = max(1, int((run_time - now).total_seconds()))
        except Exception:
            delay_seconds = 60

        def run_job():
            try:
                target_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

                def worker():
                    try:
                        self.processor.reload_config()
                        self.processor.run_processing(target_date, keep_mp3=True, progress_callback=None, blocking=True)
                    finally:
                        try:
                            self.processor.cleanup_old_wavs(keep_days=1)
                        except Exception:
                            pass

                threading.Thread(target=worker, daemon=True).start()
            finally:
                self._schedule_daily_processing(run_at_minutes_after_midnight)

        self.root.after(delay_seconds * 1000, run_job)

    # ── Device / config ───────────────────────────────────────────

    def refresh_devices_list(self):
        input_devices = [dev for dev in self.censura.get_audio_devices() if dev["maxInputChannels"] > 0]
        default_entry = {
            "index": None,
            "name": "Padrão (recomendado se a gravação travar)",
            "maxInputChannels": 1,
            "maxOutputChannels": 0,
            "defaultSampleRate": 44100,
        }
        self.audio_devices = [default_entry] + input_devices
        self.device_combo["values"] = [
            dev["name"] if dev["index"] is None else f"{dev['index']}: {dev['name']}"
            for dev in self.audio_devices
        ]
        current_idx = self.censura.config["audio"].get("device_index")
        for i, dev in enumerate(self.audio_devices):
            if dev["index"] == current_idx:
                self.device_combo.current(i)
                self.on_device_select(None)
                break
        else:
            if not self.audio_devices:
                self.device_details_var.set("Nenhum dispositivo de entrada encontrado.")
            elif current_idx is not None:
                self.device_combo.current(0)
                self.on_device_select(None)

    def on_device_select(self, event):
        selected_idx = self.device_combo.current()
        if selected_idx < 0:
            return
        device = self.audio_devices[selected_idx]
        if device.get("index") is None:
            details = "Usa o dispositivo de entrada padrão do Windows. Use esta opção se a gravação travar ou fechar o programa."
        else:
            details = (
                f"Canais de Entrada: {device['maxInputChannels']} | "
                f"Canais de Saída: {device['maxOutputChannels']} | "
                f"Taxa Padrão: {int(device['defaultSampleRate'])} Hz"
            )
        self.device_details_var.set(details)

    def browse_directory(self):
        directory = filedialog.askdirectory(initialdir=self.output_dir_var.get(), title="Selecione o diretório para salvar as gravações")
        if directory:
            self.output_dir_var.set(directory)

    def apply_and_save_settings(self):
        selected_idx = self.device_combo.current()
        if selected_idx < 0:
            messagebox.showwarning("Nenhuma Seleção", "Por favor, selecione um dispositivo da lista.")
            return
        device = self.audio_devices[selected_idx]
        self.censura.config["audio"]["device_index"] = device["index"]
        self.censura.config["audio"]["channels"] = device.get("maxInputChannels") or 1
        self.censura.config["recording"]["output_directory"] = self.output_dir_var.get()
        try:
            self.censura.save_config()
            messagebox.showinfo("Sucesso", "Configurações salvas!")
        except Exception as e:
            messagebox.showerror("Erro ao Salvar", f"Não foi possível salvar: {e}")

    # ── Recording controls ────────────────────────────────────────

    def start_recording(self):
        """Defer para próxima iteração do event loop para evitar travar a UI."""
        self.root.after(0, self._do_start_recording)

    def _do_start_recording(self):
        try:
            if USE_WORKER_RECORDING:
                self._do_start_recording_worker()
            else:
                self._do_start_recording_inprocess()
        except Exception as e:
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            messagebox.showerror("Erro ao iniciar gravação", str(e))

    def _do_start_recording_worker(self):
        if self._worker_proc is not None:
            return
        for f in (WORKER_STOP_FILE, WORKER_RTMP_CMD_FILE, WORKER_ICECAST_CMD_FILE):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        worker_path = os.path.join(os.getcwd(), WORKER_SCRIPT)
        if not os.path.isfile(worker_path):
            messagebox.showerror("Erro", f"Arquivo {WORKER_SCRIPT} não encontrado.")
            return
        cmd = [sys.executable, WORKER_SCRIPT, "--config", self.censura.config_file]
        if self.monitor_var.get():
            cmd.append("--monitor")
        try:
            worker_log = os.path.join(os.getcwd(), "worker_stderr.log")
            self._worker_stderr_file = open(worker_log, "w", encoding="utf-8")
            self._worker_proc = subprocess.Popen(
                cmd, cwd=os.getcwd(), stdout=subprocess.DEVNULL, stderr=self._worker_stderr_file,
            )
        except Exception as e:
            messagebox.showerror("Erro ao iniciar gravador", str(e))
            return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.health_var.set("Gravação em andamento")
        for child in self.root.winfo_children():
            if isinstance(child, ttk.Notebook):
                tabs = child.tabs()
                if len(tabs) > 3:
                    child.tab(tabs[3], state="disabled")
        self._worker_status_poller()

    def _worker_status_poller(self):
        if self._worker_proc is None:
            return
        ret = self._worker_proc.poll()
        if ret is not None:
            self._worker_proc = None
            self._cached_worker_data = None
            self._close_worker_stderr()
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.health_var.set("Gravação parada")
            self.rtmp_start_btn.config(state="normal")
            self.rtmp_stop_btn.config(state="disabled")
            self.rtmp_status_var.set("Inativo")
            self.ice_start_btn.config(state="normal")
            self.ice_stop_btn.config(state="disabled")
            for child in self.root.winfo_children():
                if isinstance(child, ttk.Notebook):
                    tabs = child.tabs()
                    if len(tabs) > 3:
                        child.tab(tabs[3], state="normal")
            if ret != 0:
                stderr_hint = ""
                try:
                    log_path = os.path.join(os.getcwd(), "worker_stderr.log")
                    if os.path.isfile(log_path):
                        with open(log_path, "r", encoding="utf-8") as f:
                            stderr_hint = f.read().strip()[:500]
                except Exception:
                    pass
                msg = (
                    f"O gravador encerrou com código {ret}.\n"
                    "A janela continua aberta.\n\n"
                    "Tente:\n"
                    "1) Selecionar 'Padrão' em Configurações → Dispositivo\n"
                    "2) Fechar outros programas de áudio\n"
                    "3) Atualizar driver do microfone"
                )
                if stderr_hint:
                    msg += f"\n\nDetalhe do worker:\n{stderr_hint}"
                messagebox.showerror("Gravação encerrada", msg)
            return
        try:
            if os.path.isfile(WORKER_STATUS_FILE):
                with open(WORKER_STATUS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("is_recording") and data.get("current_chunk_start"):
                    start_time = datetime.fromisoformat(data["current_chunk_start"])
                    elapsed = datetime.now() - start_time
                    m, s = divmod(elapsed.total_seconds(), 60)
                    self.status_var.set(f"Gravando chunk #{data.get('chunk_counter', 0)}...\nTempo: {int(m):02d}:{int(s):02d}")
                self.rtmp_status_var.set(data.get("rtmp_status", "Inativo"))
                self.ice_status_var.set(data.get("icecast_status", "Inativo"))
                rtmp_active = data.get("rtmp_active", False)
                icecast_active = data.get("icecast_active", False)
                self.rtmp_start_btn.config(state="disabled" if rtmp_active else "normal")
                self.rtmp_stop_btn.config(state="normal" if rtmp_active else "disabled")
                self.ice_start_btn.config(state="disabled" if icecast_active else "normal")
                self.ice_stop_btn.config(state="normal" if icecast_active else "disabled")
        except Exception:
            pass
        if self._worker_proc is not None:
            self.status_poller = self.root.after(1000, self._worker_status_poller)

    def _autostart_streams(self):
        """Auto-inicia RTMP e/ou Icecast se configurado como automático."""
        if not self._is_recording_active():
            return
        streaming_cfg = self.censura.config.get("streaming", {})
        rtmp_cfg = streaming_cfg.get("rtmp", {})
        if rtmp_cfg.get("enabled") and not self.stream_manager._rtmp_active:
            self.start_rtmp()
        ice_cfg = streaming_cfg.get("icecast", {})
        if ice_cfg.get("enabled") and not self.stream_manager._icecast_active:
            self.start_icecast()

    def _do_start_recording_inprocess(self):
        if self.censura.start_recording(enable_monitoring=self.monitor_var.get()):
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.health_var.set("Gravação em andamento")
            for child in self.root.winfo_children():
                if isinstance(child, ttk.Notebook):
                    tabs = child.tabs()
                    if len(tabs) > 3:
                        child.tab(tabs[3], state="disabled")
            self.update_status()
            self.root.after(2000, self._autostart_streams)
        else:
            messagebox.showerror(
                "Erro de Gravação",
                "Não foi possível iniciar a gravação.\nVerifique se o dispositivo está correto e não está em uso.",
            )

    def stop_recording(self):
        self.stream_manager.stop_all()
        self._stream_error = False
        self.rtmp_start_btn.config(state="normal")
        self.rtmp_stop_btn.config(state="disabled")
        self.rtmp_status_var.set("Inativo")
        self.ice_start_btn.config(state="normal")
        self.ice_stop_btn.config(state="disabled")
        self.ice_status_var.set("Inativo")

        if USE_WORKER_RECORDING and self._worker_proc is not None:
            self.stop_btn.config(state="disabled")
            self.health_var.set("Parando gravação...")
            try:
                open(WORKER_STOP_FILE, "w").close()
            except Exception:
                pass

            def _wait_worker():
                proc = self._worker_proc
                if proc is None:
                    return
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                self.root.after(0, self._on_worker_stopped)

            threading.Thread(target=_wait_worker, daemon=True).start()
            return

        if self.censura.stop_recording():
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.health_var.set("Gravação parada")
            for child in self.root.winfo_children():
                if isinstance(child, ttk.Notebook):
                    tabs = child.tabs()
                    if len(tabs) > 3:
                        child.tab(tabs[3], state="normal")
            if self.status_poller:
                self.root.after_cancel(self.status_poller)
        else:
            messagebox.showwarning("Aviso", "A gravação já estava parada.")

    def _on_worker_stopped(self):
        """Callback na main thread após o worker parar."""
        self._worker_proc = None
        self._cached_worker_data = None
        self._close_worker_stderr()
        if self.status_poller:
            self.root.after_cancel(self.status_poller)
            self.status_poller = None
        try:
            if os.path.exists(WORKER_STOP_FILE):
                os.remove(WORKER_STOP_FILE)
        except Exception:
            pass
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.health_var.set("Gravação parada")
        for child in self.root.winfo_children():
            if isinstance(child, ttk.Notebook):
                tabs = child.tabs()
                if len(tabs) > 3:
                    child.tab(tabs[3], state="normal")

    def _close_worker_stderr(self):
        f = getattr(self, "_worker_stderr_file", None)
        if f:
            try:
                f.close()
            except Exception:
                pass
            self._worker_stderr_file = None

    def update_status(self):
        try:
            if not self.censura.is_recording:
                return
            status = self.censura.get_status()
            if status.get("current_chunk_start"):
                start_time = datetime.fromisoformat(status["current_chunk_start"])
                elapsed = datetime.now() - start_time
                minutes, seconds = divmod(elapsed.total_seconds(), 60)
                msg = f"Gravando chunk #{status['chunk_counter']}...\nTempo no chunk: {int(minutes):02d}:{int(seconds):02d}"
                stalls = status.get("stall_count", 0)
                if stalls > 0:
                    msg += f"  |  Stalls: {stalls}"
                stream_st = self.stream_manager.get_status()
                parts = []
                if stream_st.get("rtmp_active"):
                    parts.append("RTMP")
                if stream_st.get("icecast_active"):
                    parts.append("Icecast")
                if parts:
                    msg += f"\nStreaming: {', '.join(parts)}"
                self.status_var.set(msg)
        except Exception:
            pass
        if self.censura.is_recording:
            self.status_poller = self.root.after(1000, self.update_status)

    def toggle_monitoring(self):
        self.volume_slider.config(state="normal" if self.monitor_var.get() else "disabled")
        self.censura.is_monitoring = self.monitor_var.get()

    def set_volume(self, value):
        self.censura.set_monitor_volume(float(value))

    def on_closing(self):
        if self._monitor_poller:
            self.root.after_cancel(self._monitor_poller)
        recording_active = (USE_WORKER_RECORDING and self._worker_proc is not None) or (self.censura and self.censura.is_recording)
        if recording_active:
            answer = messagebox.askyesnocancel(
                "Sair",
                "A gravação está em andamento.\nDeseja parar e sair?\nSim: parar e sair\nNão: manter gravando em segundo plano",
            )
            if answer is True:
                self.stream_manager.stop_all()
                if USE_WORKER_RECORDING and self._worker_proc is not None:
                    try:
                        open(WORKER_STOP_FILE, "w").close()
                    except Exception:
                        pass
                    proc = self._worker_proc
                    self._worker_proc = None
                    self._close_worker_stderr()

                    def _kill_and_destroy():
                        if proc:
                            try:
                                proc.wait(timeout=5)
                            except Exception:
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                        try:
                            if os.path.exists(WORKER_STOP_FILE):
                                os.remove(WORKER_STOP_FILE)
                        except Exception:
                            pass
                        self.root.after(0, self.root.destroy)

                    threading.Thread(target=_kill_and_destroy, daemon=True).start()
                    return
                elif self.censura:
                    self.censura.stop_recording()
                self.root.destroy()
            elif answer is False:
                self._close_worker_stderr()
                self.root.destroy()
            else:
                return
        else:
            self.stream_manager.stop_all()
            self._close_worker_stderr()
            self.root.destroy()


def main():
    root = tk.Tk()
    app = CensuraDigitalInterface(root)
    root.mainloop()


if __name__ == "__main__":
    main()
