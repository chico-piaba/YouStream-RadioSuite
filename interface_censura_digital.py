#!/usr/bin/env python3
"""
Sistema de Censura Digital - Interface Gráfica v2.0
Inclui gravação, processamento, streaming RTMP/Icecast e monitoramento com watchdog.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path

from gravador_censura_digital import CensuraDigital
from processador_audio import AudioProcessor
from stream_manager import StreamManager

try:
    from tkcalendar import Calendar
    CALENDAR_AVAILABLE = True
except ImportError:
    CALENDAR_AVAILABLE = False

MAX_LOG_LINES = 1000


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
        ttk.Checkbutton(
            action_frame,
            text="Manter arquivos MP3 após compactar",
            variable=self.keep_mp3_var,
        ).pack(anchor="w")

        self.process_btn = ttk.Button(
            action_frame,
            text="Processar Gravações da Data Selecionada",
            command=self.run_process,
        )
        self.process_btn.pack(fill="x", pady=10)

        cut_frame = ttk.LabelFrame(
            frame, text="3. Extrair Intervalo (Trecho)", padding="10"
        )
        cut_frame.pack(fill="x", pady=5)
        ttk.Label(cut_frame, text="Início (AAAA-MM-DD HH:MM:SS):").grid(
            row=0, column=0, sticky="w", pady=2
        )
        self.cut_start = ttk.Entry(cut_frame)
        self.cut_start.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(cut_frame, text="Fim (AAAA-MM-DD HH:MM:SS):").grid(
            row=1, column=0, sticky="w", pady=2
        )
        self.cut_end = ttk.Entry(cut_frame)
        self.cut_end.grid(row=1, column=1, sticky="ew", padx=5)
        cut_frame.columnconfigure(1, weight=1)
        self.cut_btn = ttk.Button(
            cut_frame, text="Extrair Intervalo", command=self.run_cut
        )
        self.cut_btn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)

        log_frame = ttk.LabelFrame(frame, text="Progresso", padding="10")
        log_frame.pack(expand=True, fill="both", pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, state="disabled", height=10
        )
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

        if CALENDAR_AVAILABLE:
            target_date = self.cal.get_date()
        else:
            target_date = self.date_entry.get()

        callback = lambda msg: self.after(0, self.log_message, msg)
        self.processor.run_processing(
            target_date, self.keep_mp3_var.get(), progress_callback=callback
        )
        self.after(3000, lambda: self.process_btn.config(state="normal"))

    def run_cut(self):
        self.process_btn.config(state="disabled")
        self.cut_btn.config(state="disabled")
        start = self.cut_start.get().strip()
        end = self.cut_end.get().strip()
        callback = lambda msg: self.after(0, self.log_message, msg)

        def done(msg):
            self.after(0, self.log_message, msg or "Intervalo processado.")
            self.after(0, lambda: self.process_btn.config(state="normal"))
            self.after(0, lambda: self.cut_btn.config(state="normal"))

        def task():
            try:
                result = self.processor.extract_interval(
                    start, end, progress_callback=callback, blocking=True
                )
                if result:
                    done(f"Trecho salvo em: {result}")
                else:
                    done("Falha ao extrair o trecho.")
            except Exception as e:
                done(f"Erro: {e}")

        threading.Thread(target=task, daemon=True).start()

    def on_closing(self):
        self.processor.stop()
        self.destroy()


class CensuraDigitalInterface:
    def __init__(self, root):
        self.root = root
        self.root.title("Sistema de Censura Digital v2.0")
        self.root.geometry("600x520")
        self.root.minsize(600, 520)

        try:
            self.censura = CensuraDigital()
            self.processor = AudioProcessor()
            self.stream_manager = StreamManager(
                self.censura.config, logger=self.censura.logger
            )
        except Exception as e:
            messagebox.showerror("Erro Crítico na Inicialização", f"{e}")
            self.root.destroy()
            return

        self.censura.set_stream_manager(self.stream_manager)
        self.censura.set_alert_callback(self._on_watchdog_alert)
        self.stream_manager.set_status_callback(self._on_stream_status)

        self.audio_devices = []
        self.status_poller = None
        self.setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.refresh_devices_list()
        self._schedule_daily_processing()

    # ── UI Setup ──────────────────────────────────────────────────

    def setup_ui(self):
        notebook = ttk.Notebook(self.root, padding="10")
        notebook.pack(expand=True, fill="both")

        tab_rec = ttk.Frame(notebook)
        tab_stream = ttk.Frame(notebook)
        tab_config = ttk.Frame(notebook)

        notebook.add(tab_rec, text=" Gravação ")
        notebook.add(tab_stream, text=" Streaming ")
        notebook.add(tab_config, text=" Configurações ")

        self.notebook = notebook
        self._create_recording_tab(tab_rec)
        self._create_streaming_tab(tab_stream)
        self._create_config_tab(tab_config)

    def _create_recording_tab(self, parent):
        frame = ttk.Frame(parent, padding="15")
        frame.pack(expand=True, fill="both")

        ttk.Button(
            frame,
            text="Processar Gravações Antigas...",
            command=self.open_processor_window,
        ).pack(fill="x", pady=(0, 15))

        control_frame = ttk.LabelFrame(frame, text="Controle de Gravação", padding="10")
        control_frame.pack(fill="x", pady=5)
        self.status_var = tk.StringVar(value="Pronto para iniciar")
        ttk.Label(
            control_frame,
            textvariable=self.status_var,
            wraplength=450,
            justify=tk.CENTER,
        ).grid(row=0, column=0, columnspan=2, pady=10)
        self.start_btn = ttk.Button(
            control_frame, text="Iniciar Gravação", command=self.start_recording
        )
        self.start_btn.grid(row=1, column=0, padx=5, pady=5, sticky="ew")
        self.stop_btn = ttk.Button(
            control_frame,
            text="Parar Gravação",
            command=self.stop_recording,
            state="disabled",
        )
        self.stop_btn.grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        control_frame.columnconfigure((0, 1), weight=1)

        monitor_frame = ttk.LabelFrame(frame, text="Monitor de Áudio", padding="10")
        monitor_frame.pack(fill="x", pady=10)
        self.monitor_var = tk.BooleanVar()
        ttk.Checkbutton(
            monitor_frame,
            text="Ouvir o que está sendo gravado",
            variable=self.monitor_var,
            command=self.toggle_monitoring,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.volume_var = tk.DoubleVar(value=1.0)
        ttk.Label(monitor_frame, text="Volume:").grid(row=1, column=0, pady=10, sticky="w")
        self.volume_slider = ttk.Scale(
            monitor_frame,
            from_=0.0,
            to=1.5,
            orient=tk.HORIZONTAL,
            variable=self.volume_var,
            command=self.set_volume,
        )
        self.volume_slider.grid(row=1, column=1, pady=10, sticky="ew")
        monitor_frame.columnconfigure(1, weight=1)

        # Health / alert indicator
        health_frame = ttk.LabelFrame(frame, text="Saúde da Gravação", padding="10")
        health_frame.pack(fill="x", pady=5)
        self.health_var = tk.StringVar(value="--")
        ttk.Label(health_frame, textvariable=self.health_var, wraplength=450).pack(
            fill="x"
        )

        ttk.Label(frame, text="Desenvolvido por Rodrigo Lima", font=("Arial", 8)).pack(
            side="bottom", pady=10
        )

        self.toggle_monitoring()

    def _create_streaming_tab(self, parent):
        frame = ttk.Frame(parent, padding="15")
        frame.pack(expand=True, fill="both")

        # ── RTMP ──
        rtmp_frame = ttk.LabelFrame(frame, text="RTMP (YouTube/Facebook/Genérico)", padding="10")
        rtmp_frame.pack(fill="x", pady=5)

        ttk.Label(rtmp_frame, text="URL RTMP:").grid(row=0, column=0, sticky="w", pady=2)
        rtmp_cfg = self.censura.config.get("streaming", {}).get("rtmp", {})
        self.rtmp_url_var = tk.StringVar(value=rtmp_cfg.get("url", ""))
        ttk.Entry(rtmp_frame, textvariable=self.rtmp_url_var, width=50).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=5
        )

        ttk.Label(rtmp_frame, text="Bitrate (kbps):").grid(row=1, column=0, sticky="w", pady=2)
        self.rtmp_bitrate_var = tk.IntVar(value=rtmp_cfg.get("audio_bitrate_kbps", 128))
        ttk.Spinbox(
            rtmp_frame, from_=64, to=320, textvariable=self.rtmp_bitrate_var, width=8
        ).grid(row=1, column=1, sticky="w", padx=5)

        self.rtmp_status_var = tk.StringVar(value="Inativo")
        ttk.Label(rtmp_frame, text="Status:").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Label(rtmp_frame, textvariable=self.rtmp_status_var).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=5
        )

        btn_frame_rtmp = ttk.Frame(rtmp_frame)
        btn_frame_rtmp.grid(row=3, column=0, columnspan=3, pady=8, sticky="ew")
        self.rtmp_start_btn = ttk.Button(
            btn_frame_rtmp, text="Iniciar RTMP", command=self.start_rtmp
        )
        self.rtmp_start_btn.pack(side="left", padx=5, expand=True, fill="x")
        self.rtmp_stop_btn = ttk.Button(
            btn_frame_rtmp, text="Parar RTMP", command=self.stop_rtmp, state="disabled"
        )
        self.rtmp_stop_btn.pack(side="left", padx=5, expand=True, fill="x")

        rtmp_frame.columnconfigure(1, weight=1)

        # ── Icecast ──
        ice_frame = ttk.LabelFrame(frame, text="Icecast (Rádio Internet)", padding="10")
        ice_frame.pack(fill="x", pady=10)

        ice_cfg = self.censura.config.get("streaming", {}).get("icecast", {})

        ttk.Label(ice_frame, text="Host:").grid(row=0, column=0, sticky="w", pady=2)
        self.ice_host_var = tk.StringVar(value=ice_cfg.get("host", "localhost"))
        ttk.Entry(ice_frame, textvariable=self.ice_host_var, width=25).grid(
            row=0, column=1, sticky="ew", padx=5
        )

        ttk.Label(ice_frame, text="Porta:").grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.ice_port_var = tk.IntVar(value=ice_cfg.get("port", 8000))
        ttk.Spinbox(
            ice_frame, from_=1, to=65535, textvariable=self.ice_port_var, width=7
        ).grid(row=0, column=3, sticky="w", padx=5)

        ttk.Label(ice_frame, text="Mount:").grid(row=1, column=0, sticky="w", pady=2)
        self.ice_mount_var = tk.StringVar(value=ice_cfg.get("mount", "/live"))
        ttk.Entry(ice_frame, textvariable=self.ice_mount_var, width=20).grid(
            row=1, column=1, sticky="ew", padx=5
        )

        ttk.Label(ice_frame, text="Senha:").grid(row=1, column=2, sticky="w", padx=(10, 0))
        self.ice_pass_var = tk.StringVar(value=ice_cfg.get("source_password", ""))
        ttk.Entry(ice_frame, textvariable=self.ice_pass_var, show="*", width=15).grid(
            row=1, column=3, sticky="ew", padx=5
        )

        ttk.Label(ice_frame, text="Bitrate (kbps):").grid(row=2, column=0, sticky="w", pady=2)
        self.ice_bitrate_var = tk.IntVar(value=ice_cfg.get("audio_bitrate_kbps", 128))
        ttk.Spinbox(
            ice_frame, from_=64, to=320, textvariable=self.ice_bitrate_var, width=8
        ).grid(row=2, column=1, sticky="w", padx=5)

        self.ice_status_var = tk.StringVar(value="Inativo")
        ttk.Label(ice_frame, text="Status:").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Label(ice_frame, textvariable=self.ice_status_var).grid(
            row=3, column=1, columnspan=3, sticky="w", padx=5
        )

        btn_frame_ice = ttk.Frame(ice_frame)
        btn_frame_ice.grid(row=4, column=0, columnspan=4, pady=8, sticky="ew")
        self.ice_start_btn = ttk.Button(
            btn_frame_ice, text="Iniciar Icecast", command=self.start_icecast
        )
        self.ice_start_btn.pack(side="left", padx=5, expand=True, fill="x")
        self.ice_stop_btn = ttk.Button(
            btn_frame_ice,
            text="Parar Icecast",
            command=self.stop_icecast,
            state="disabled",
        )
        self.ice_stop_btn.pack(side="left", padx=5, expand=True, fill="x")

        ice_frame.columnconfigure(1, weight=1)
        ice_frame.columnconfigure(3, weight=1)

        # Save streaming config
        ttk.Button(
            frame,
            text="Salvar Configurações de Streaming",
            command=self.save_streaming_config,
        ).pack(fill="x", pady=10)

    def _create_config_tab(self, parent):
        frame = ttk.Frame(parent, padding="15")
        frame.pack(expand=True, fill="both")

        device_frame = ttk.LabelFrame(
            frame, text="Dispositivo de Gravação", padding="10"
        )
        device_frame.pack(fill="x", pady=5)
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(
            device_frame, textvariable=self.device_var, state="readonly", width=50
        )
        self.device_combo.grid(row=0, column=0, sticky="ew", pady=5)
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_select)
        ttk.Button(
            device_frame, text="Atualizar", command=self.refresh_devices_list
        ).grid(row=0, column=1, padx=10)
        self.device_details_var = tk.StringVar(
            value="Selecione um dispositivo para ver os detalhes."
        )
        ttk.Label(
            device_frame,
            textvariable=self.device_details_var,
            wraplength=450,
            justify=tk.LEFT,
        ).grid(row=1, column=0, columnspan=2, pady=5, sticky="w")
        device_frame.columnconfigure(0, weight=1)

        dir_frame = ttk.LabelFrame(frame, text="Diretório de Saída", padding="10")
        dir_frame.pack(fill="x", pady=10)
        self.output_dir_var = tk.StringVar(
            value=self.censura.config["recording"]["output_directory"]
        )
        ttk.Label(dir_frame, text="Salvar gravações em:").pack(anchor="w")
        ttk.Entry(dir_frame, textvariable=self.output_dir_var, state="readonly").pack(
            side="left", fill="x", expand=True, pady=5
        )
        ttk.Button(dir_frame, text="Procurar...", command=self.browse_directory).pack(
            side="left", padx=10
        )

        ttk.Button(
            frame,
            text="Aplicar e Salvar Configurações",
            command=self.apply_and_save_settings,
        ).pack(fill="x", pady=20)

    # ── Streaming controls ────────────────────────────────────────

    def start_rtmp(self):
        if not self.censura.is_recording:
            messagebox.showwarning(
                "Gravação Inativa",
                "Inicie a gravação antes de iniciar o streaming RTMP.\n"
                "O streaming envia o mesmo áudio que está sendo gravado.",
            )
            return
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
            messagebox.showwarning(
                "Gravação Inativa",
                "Inicie a gravação antes de iniciar o streaming Icecast.\n"
                "O streaming envia o mesmo áudio que está sendo gravado.",
            )
            return
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
        """Chamado pela thread do watchdog – agenda atualização na thread do Tk."""
        self.root.after(0, self._show_alert, message)

    def _show_alert(self, message):
        self.health_var.set(f"[{time.strftime('%H:%M:%S')}] {message}")

    def _on_stream_status(self, protocol, message):
        self.root.after(0, self._update_stream_status, protocol, message)

    def _update_stream_status(self, protocol, message):
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
            run_time = datetime.combine(tomorrow, datetime.min.time()) + timedelta(
                minutes=run_at_minutes_after_midnight
            )
            delay_seconds = max(1, int((run_time - now).total_seconds()))
        except Exception:
            delay_seconds = 60

        def run_job():
            try:
                target_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

                def worker():
                    try:
                        self.processor.reload_config()
                        self.processor.run_processing(
                            target_date,
                            keep_mp3=True,
                            progress_callback=None,
                            blocking=True,
                        )
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
        self.audio_devices = [
            dev
            for dev in self.censura.get_audio_devices()
            if dev["maxInputChannels"] > 0
        ]
        self.device_combo["values"] = [
            f"{dev['index']}: {dev['name']}" for dev in self.audio_devices
        ]
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
        directory = filedialog.askdirectory(
            initialdir=self.output_dir_var.get(),
            title="Selecione o diretório para salvar as gravações",
        )
        if directory:
            self.output_dir_var.set(directory)

    def apply_and_save_settings(self):
        selected_idx = self.device_combo.current()
        if selected_idx < 0:
            messagebox.showwarning(
                "Nenhuma Seleção", "Por favor, selecione um dispositivo da lista."
            )
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
            for child in self.root.winfo_children():
                if isinstance(child, ttk.Notebook):
                    config_tab_id = child.tabs()[2]
                    child.tab(config_tab_id, state="disabled")
            self.update_status()
        else:
            messagebox.showerror(
                "Erro de Gravação",
                "Não foi possível iniciar a gravação.\n"
                "Verifique se o dispositivo está correto e não está em uso.",
            )

    def stop_recording(self):
        self.stream_manager.stop_all()
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
                    config_tab_id = child.tabs()[2]
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
            msg = (
                f"Gravando chunk #{status['chunk_counter']}...\n"
                f"Tempo no chunk: {int(minutes):02d}:{int(seconds):02d}"
            )
            stalls = status.get("stall_count", 0)
            if stalls > 0:
                msg += f"  |  Stalls: {stalls}"

            stream_st = self.stream_manager.get_status()
            stream_parts = []
            if stream_st.get("rtmp_active"):
                stream_parts.append("RTMP")
            if stream_st.get("icecast_active"):
                stream_parts.append("Icecast")
            if stream_parts:
                msg += f"\nStreaming: {', '.join(stream_parts)}"

            self.status_var.set(msg)

        self.status_poller = self.root.after(1000, self.update_status)

    def toggle_monitoring(self):
        self.volume_slider.config(
            state="normal" if self.monitor_var.get() else "disabled"
        )
        self.censura.is_monitoring = self.monitor_var.get()

    def set_volume(self, value):
        self.censura.set_monitor_volume(float(value))

    def on_closing(self):
        if self.censura.is_recording:
            answer = messagebox.askyesnocancel(
                "Sair",
                "A gravação está em andamento.\n"
                "Deseja parar e sair?\n"
                "Sim: parar e sair\n"
                "Não: manter gravando em segundo plano",
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
