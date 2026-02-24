#!/usr/bin/env python3
"""
Sistema de Censura Digital - Interface Gráfica v2.1
Monitor visual com semáforos, VU meter, streaming RTMP/Icecast e autostart.
"""

import math
import os
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path

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
    """Barra horizontal de nível de áudio em escala dBFS (-60 a 0 dB)."""

    DB_FLOOR = -60.0
    DB_YELLOW = -12.0
    DB_RED = -3.0

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

        for db_mark in [-48, -36, -24, -18, -12, -6, -3, 0]:
            mx = 2 + int(usable * (db_mark - self.DB_FLOOR) / -self.DB_FLOOR)
            self._c.create_line(mx, 1, mx, 5, fill="#888888")
            self._c.create_line(mx, bar_height - 5, mx, bar_height - 1, fill="#888888")

        self._db = self.DB_FLOOR

    def set_db(self, db: float):
        """Atualiza a barra com valor em dBFS."""
        db = max(self.DB_FLOOR, min(0.0, db))
        self._db = db
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
        self.root.title("Alece Play - Sistema de Censura Digital v2.1")
        self.root.geometry("640x580")
        self.root.minsize(640, 580)

        self._logo_img = None
        self._load_logo()

        self.censura = None
        self.processor = None
        self.stream_manager = None
        self._stream_error = False
        self.audio_devices = []
        self.status_poller = None
        self._monitor_poller = None

        self._load_overlay = None
        self._load_label = None
        self._load_timeout_id = None
        self._load_done = threading.Event()
        self._load_result = None

        self._show_loading_overlay()
        threading.Thread(target=self._load_backend, daemon=True).start()
        self._load_timeout_id = self.root.after(30000, self._on_load_timeout)

    def _show_loading_overlay(self):
        self._load_overlay = tk.Frame(self.root, bg="#1a1a2e", cursor="watch")
        self._load_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        ttk.Label(self._load_overlay, text="Carregando módulos de áudio...", font=("Arial", 14)).pack(expand=True, pady=50)
        ttk.Label(self._load_overlay, text="(No Windows, isso pode levar alguns segundos)", font=("Arial", 10)).pack(pady=5)

    def _hide_loading_overlay(self):
        if self._load_overlay:
            self._load_overlay.destroy()
            self._load_overlay = None
        if self._load_timeout_id:
            self.root.after_cancel(self._load_timeout_id)
            self._load_timeout_id = None

    def _load_backend(self):
        try:
            from gravador_censura_digital import CensuraDigital as CD
            from processador_audio import AudioProcessor as AP
            from stream_manager import StreamManager as SM
            censura = CD()
            processor = AP()
            stream_manager = SM(censura.config, logger=censura.logger)
            self._load_result = (censura, processor, stream_manager)
        except Exception as e:
            self._load_result = e
        finally:
            self._load_done.set()
            self.root.after(0, self._on_backend_loaded)

    def _on_backend_loaded(self):
        if not self._load_done.is_set():
            return
        self._hide_loading_overlay()
        if isinstance(self._load_result, Exception):
            self._show_dependency_error(str(self._load_result))
            return
        self.censura, self.processor, self.stream_manager = self._load_result
        self.censura.set_stream_manager(self.stream_manager)
        self.censura.set_alert_callback(self._on_watchdog_alert)
        self.stream_manager.set_status_callback(self._on_stream_status)
        self.setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.refresh_devices_list()
        self._start_monitor_loop()
        autostart = self.censura.config.get("interface", {}).get("autostart_recording", False)
        if autostart:
            self.root.after(800, self.start_recording)

    def _on_load_timeout(self):
        self._load_timeout_id = None
        if self._load_done.is_set():
            return
        self._hide_loading_overlay()
        self._show_dependency_error(
            "Timeout ao carregar. No Windows, PyAudio/numpy podem travar.\n"
            "Tente: pip install pyaudio numpy\nOu use Python 3.11 em vez de 3.13."
        )

    # ── Dependency error screen ────────────────────────────────

    def _show_dependency_error(self, error_msg):
        import platform
        is_win = platform.system() == "Windows"

        frame = ttk.Frame(self.root, padding="30")
        frame.pack(expand=True, fill="both")

        ttk.Label(frame, text="Dependência não encontrada", font=("Arial", 14, "bold")).pack(pady=(0, 15))
        ttk.Label(frame, text=f"Erro: {error_msg}", wraplength=500, foreground="red").pack(pady=(0, 15))

        if "pyaudio" in error_msg.lower():
            if is_win:
                instructions = (
                    "No Windows, instale o PyAudio com:\n\n"
                    "  pip install pyaudio\n\n"
                    "Se falhar, instale o Build Tools do Visual Studio ou use:\n\n"
                    "  pip install pipwin\n"
                    "  pipwin install pyaudio\n\n"
                    "Certifique-se de ter Python 3.10+ instalado do python.org"
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

        # Card: RTMP
        rtmp_card = ttk.LabelFrame(cards_frame, text="RTMP", padding="10")
        rtmp_card.grid(row=0, column=1, padx=5, sticky="nsew")
        self.rtmp_semaphore = SemaphoreWidget(rtmp_card, size=64)
        self.rtmp_semaphore.pack(pady=(5, 8))
        self.rtmp_card_status = ttk.Label(rtmp_card, text="INATIVO", anchor="center", font=("Arial", 10, "bold"))
        self.rtmp_card_status.pack()
        self.rtmp_card_detail = ttk.Label(rtmp_card, text="--", anchor="center", font=("Arial", 9))
        self.rtmp_card_detail.pack(pady=(2, 0))

        # Card: Icecast
        ice_card = ttk.LabelFrame(cards_frame, text="Icecast", padding="10")
        ice_card.grid(row=0, column=2, padx=5, sticky="nsew")
        self.ice_semaphore = SemaphoreWidget(ice_card, size=64)
        self.ice_semaphore.pack(pady=(5, 8))
        self.ice_card_status = ttk.Label(ice_card, text="INATIVO", anchor="center", font=("Arial", 10, "bold"))
        self.ice_card_status.pack()
        self.ice_card_detail = ttk.Label(ice_card, text="--", anchor="center", font=("Arial", 9))
        self.ice_card_detail.pack(pady=(2, 0))

        # Autostart checkbox
        auto_frame = ttk.Frame(frame)
        auto_frame.pack(fill="x", pady=(15, 5))
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
        alert_frame.pack(fill="x", pady=(10, 0))
        self.alert_var = tk.StringVar(value="Nenhum alerta")
        ttk.Label(alert_frame, textvariable=self.alert_var, wraplength=550).pack(fill="x")

        ttk.Label(frame, text="Alece Play  |  Desenvolvido por Rodrigo Lima", font=("Arial", 8)).pack(side="bottom", pady=5)

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

    def _update_monitor(self):
        status = self.censura.get_status()
        stream_st = self.stream_manager.get_status()
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

        # RTMP semaphore: red=ON AIR, green=pronto, yellow=erro, off=inativo
        rtmp_active = stream_st.get("rtmp_active", False)
        if rtmp_active:
            self.rtmp_semaphore.set_state("red")
            self.rtmp_card_status.config(text="ON AIR")
            self.rtmp_card_detail.config(text=self.rtmp_status_var.get()[:30])
        elif self._stream_error:
            self.rtmp_semaphore.set_state("yellow")
            self.rtmp_card_status.config(text="ERRO")
        elif is_rec:
            self.rtmp_semaphore.set_state("green")
            self.rtmp_card_status.config(text="PRONTO")
            self.rtmp_card_detail.config(text="Gravação ativa")
        else:
            self.rtmp_semaphore.set_state("off")
            self.rtmp_card_status.config(text="INATIVO")
            self.rtmp_card_detail.config(text="--")

        # Icecast semaphore: red=ON AIR, green=pronto, yellow=erro, off=inativo
        ice_active = stream_st.get("icecast_active", False)
        if ice_active:
            self.ice_semaphore.set_state("red")
            self.ice_card_status.config(text="ON AIR")
            self.ice_card_detail.config(text=self.ice_status_var.get()[:30])
        elif self._stream_error:
            self.ice_semaphore.set_state("yellow")
            self.ice_card_status.config(text="ERRO")
        elif is_rec:
            self.ice_semaphore.set_state("green")
            self.ice_card_status.config(text="PRONTO")
            self.ice_card_detail.config(text="Gravação ativa")
        else:
            self.ice_semaphore.set_state("off")
            self.ice_card_status.config(text="INATIVO")
            self.ice_card_detail.config(text="--")

        self._monitor_poller = self.root.after(MONITOR_REFRESH_MS, self._update_monitor)

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

    def start_rtmp(self):
        if not self.censura.is_recording:
            messagebox.showwarning("Gravação Inativa", "Inicie a gravação antes de iniciar o streaming RTMP.")
            return
        self._stream_error = False
        url = self.rtmp_url_var.get().strip()
        bitrate = self.rtmp_bitrate_var.get()
        if self.stream_manager.start_rtmp(url=url, bitrate=bitrate):
            self.rtmp_start_btn.config(state="disabled")
            self.rtmp_stop_btn.config(state="normal")

    def stop_rtmp(self):
        self.stream_manager.stop_rtmp()
        self.rtmp_start_btn.config(state="normal")
        self.rtmp_stop_btn.config(state="disabled")
        self.rtmp_status_var.set("Inativo")

    def start_icecast(self):
        if not self.censura.is_recording:
            messagebox.showwarning("Gravação Inativa", "Inicie a gravação antes de iniciar o streaming Icecast.")
            return
        self._stream_error = False
        if self.stream_manager.start_icecast(
            host=self.ice_host_var.get().strip(),
            port=self.ice_port_var.get(),
            mount=self.ice_mount_var.get().strip(),
            password=self.ice_pass_var.get(),
            bitrate=self.ice_bitrate_var.get(),
        ):
            self.ice_start_btn.config(state="disabled")
            self.ice_stop_btn.config(state="normal")

    def stop_icecast(self):
        self.stream_manager.stop_icecast()
        self.ice_start_btn.config(state="normal")
        self.ice_stop_btn.config(state="disabled")
        self.ice_status_var.set("Inativo")

    def save_streaming_config(self):
        if "streaming" not in self.censura.config:
            self.censura.config["streaming"] = {}
        self.censura.config["streaming"]["rtmp"] = {
            "enabled": False,
            "url": self.rtmp_url_var.get().strip(),
            "audio_bitrate_kbps": self.rtmp_bitrate_var.get(),
        }
        self.censura.config["streaming"]["icecast"] = {
            "enabled": False,
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

    def _show_alert(self, message):
        self.health_var.set(f"[{time.strftime('%H:%M:%S')}] {message}")
        self.alert_var.set(f"[{time.strftime('%H:%M:%S')}] {message}")

    def _on_stream_status(self, protocol, message):
        self.root.after(0, self._update_stream_status, protocol, message)

    def _update_stream_status(self, protocol, message):
        is_error = "erro" in message.lower() or "encerrou" in message.lower() or "não encontrado" in message.lower()
        if is_error:
            self._stream_error = True

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
        self.audio_devices = [dev for dev in self.censura.get_audio_devices() if dev["maxInputChannels"] > 0]
        self.device_combo["values"] = [f"{dev['index']}: {dev['name']}" for dev in self.audio_devices]
        current_idx = self.censura.config["audio"]["device_index"]
        for i, dev in enumerate(self.audio_devices):
            if dev["index"] == current_idx:
                self.device_combo.current(i)
                self.on_device_select(None)
                break

    def on_device_select(self, event):
        selected_idx = self.device_combo.current()
        if selected_idx < 0:
            return
        device = self.audio_devices[selected_idx]
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
        self.censura.config["audio"]["channels"] = device["maxInputChannels"]
        self.censura.config["recording"]["output_directory"] = self.output_dir_var.get()
        try:
            self.censura.save_config()
            messagebox.showinfo("Sucesso", "Configurações salvas!")
        except Exception as e:
            messagebox.showerror("Erro ao Salvar", f"Não foi possível salvar: {e}")

    # ── Recording controls ────────────────────────────────────────

    def start_recording(self):
        if self.censura.start_recording(enable_monitoring=self.monitor_var.get()):
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.health_var.set("Gravação em andamento")
            # Disable config tab (now index 3)
            for child in self.root.winfo_children():
                if isinstance(child, ttk.Notebook):
                    config_tab_id = child.tabs()[3]
                    child.tab(config_tab_id, state="disabled")
            self.update_status()
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

        if self.censura.stop_recording():
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.health_var.set("Gravação parada")
            for child in self.root.winfo_children():
                if isinstance(child, ttk.Notebook):
                    config_tab_id = child.tabs()[3]
                    child.tab(config_tab_id, state="normal")
            if self.status_poller:
                self.root.after_cancel(self.status_poller)
        else:
            messagebox.showwarning("Aviso", "A gravação já estava parada.")

    def update_status(self):
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
        self.status_poller = self.root.after(1000, self.update_status)

    def toggle_monitoring(self):
        self.volume_slider.config(state="normal" if self.monitor_var.get() else "disabled")
        self.censura.is_monitoring = self.monitor_var.get()

    def set_volume(self, value):
        self.censura.set_monitor_volume(float(value))

    def on_closing(self):
        if self._monitor_poller:
            self.root.after_cancel(self._monitor_poller)
        if self.censura.is_recording:
            answer = messagebox.askyesnocancel(
                "Sair",
                "A gravação está em andamento.\nDeseja parar e sair?\nSim: parar e sair\nNão: manter gravando em segundo plano",
            )
            if answer is True:
                self.stream_manager.stop_all()
                self.censura.stop_recording()
                self.root.destroy()
            elif answer is False:
                self.root.destroy()
            else:
                return
        else:
            self.stream_manager.stop_all()
            self.root.destroy()


def main():
    root = tk.Tk()
    app = CensuraDigitalInterface(root)
    root.mainloop()


if __name__ == "__main__":
    main()
