#!/usr/bin/env python3
"""
Sistema de Censura Digital - Interface Gráfica (Versão Safe - sem PyAudio)
Versão compatível para build Windows sem dependências de áudio
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import sys
import os
import json
import threading
import time
from pathlib import Path
import platform

# Bandeja do sistema (Windows)
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

class CensuraDigitalInterface:
    def __init__(self, root):
        self.root = root
        self.root.title("Sistema de Censura Digital v1.0 (Safe Mode)")
        self.root.geometry("600x500")
        
        # Estado da gravação (simulado)
        self.is_recording = False
        self.recording_time = 0
        
        # Configurar estilo
        self.setup_ui()
        
        # Tray (apenas Windows)
        self.tray_icon = None
        if platform.system().lower().startswith("win") and TRAY_AVAILABLE:
            self._setup_tray()

        # Timer para atualizar interface
        self.update_interface()
        
        # Fechamento: minimizar para a bandeja se disponível
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        
    def setup_ui(self):
        """Configura interface do usuário"""
        
        # Frame principal
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Título
        title_label = ttk.Label(main_frame, text="Sistema de Censura Digital", 
                               font=("Arial", 16, "bold"))
        title_label.grid(row=0, column=0, columnspan=3, pady=(0, 20))
        
        # Status
        self.status_var = tk.StringVar(value="Pronto para usar (Modo Simulação)")
        status_label = ttk.Label(main_frame, textvariable=self.status_var)
        status_label.grid(row=1, column=0, columnspan=3, pady=(0, 10))
        
        # Controles de gravação
        controls_frame = ttk.LabelFrame(main_frame, text="Controles de Gravação", padding="10")
        controls_frame.grid(row=2, column=0, columnspan=3, pady=(0, 10), sticky=(tk.W, tk.E))
        
        self.start_btn = ttk.Button(controls_frame, text="▶ Iniciar Gravação", 
                                   command=self.start_recording)
        self.start_btn.grid(row=0, column=0, padx=(0, 10), pady=5)
        
        self.stop_btn = ttk.Button(controls_frame, text="⏹ Parar Gravação", 
                                  command=self.stop_recording, state='disabled')
        self.stop_btn.grid(row=0, column=1, pady=5)
        
        # Monitor de áudio (simulado)
        monitor_frame = ttk.LabelFrame(main_frame, text="Monitor de Áudio (Simulado)", padding="10")
        monitor_frame.grid(row=3, column=0, columnspan=3, pady=(0, 10), sticky=(tk.W, tk.E))
        
        self.level_var = tk.DoubleVar()
        self.level_progress = ttk.Progressbar(monitor_frame, variable=self.level_var, 
                                             maximum=100, length=300)
        self.level_progress.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E))
        
        self.level_label = ttk.Label(monitor_frame, text="Nível: 0%")
        self.level_label.grid(row=1, column=0, columnspan=2)
        
        # Informações da gravação
        info_frame = ttk.LabelFrame(main_frame, text="Informações da Gravação", padding="10")
        info_frame.grid(row=4, column=0, columnspan=3, pady=(0, 10), sticky=(tk.W, tk.E))
        
        self.info_var = tk.StringVar(value="Nenhuma gravação ativa")
        info_label = ttk.Label(info_frame, textvariable=self.info_var)
        info_label.grid(row=0, column=0)
        
        # Gerenciamento de arquivos
        files_frame = ttk.LabelFrame(main_frame, text="Gerenciamento de Arquivos", padding="10")
        files_frame.grid(row=5, column=0, columnspan=3, pady=(0, 10), sticky=(tk.W, tk.E))
        
        ttk.Button(files_frame, text="📁 Abrir Pasta de Gravações", 
                  command=self.open_recordings_folder).grid(row=0, column=0, padx=(0, 5))
        
        ttk.Button(files_frame, text="📁 Pasta de Hoje", 
                  command=self.open_today_folder).grid(row=0, column=1, padx=5)
        
        ttk.Button(files_frame, text="🔧 Configurações", 
                  command=self.open_config).grid(row=0, column=2, padx=(5, 0))
        
        # Configurações rápidas
        quick_frame = ttk.LabelFrame(main_frame, text="Configurações Rápidas", padding="10")
        quick_frame.grid(row=6, column=0, columnspan=3, pady=(0, 10), sticky=(tk.W, tk.E))
        
        ttk.Label(quick_frame, text="Duração (min):").grid(row=0, column=0)
        self.duration_var = tk.IntVar(value=15)
        duration_spin = ttk.Spinbox(quick_frame, from_=1, to=120, textvariable=self.duration_var)
        duration_spin.grid(row=0, column=1, padx=5)
        
        ttk.Label(quick_frame, text="Dispositivo:").grid(row=0, column=2, padx=(10, 0))
        self.device_var = tk.StringVar(value="Microfone Padrão (Simulado)")
        device_combo = ttk.Combobox(quick_frame, textvariable=self.device_var, 
                                   values=["Microfone Padrão (Simulado)", "Linha de Entrada (Simulado)"])
        device_combo.grid(row=0, column=3, padx=5)
        
        # Log de atividade
        log_frame = ttk.LabelFrame(main_frame, text="Log de Atividade", padding="10")
        log_frame.grid(row=7, column=0, columnspan=3, pady=(0, 0), sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Text widget com scrollbar
        self.log_text = tk.Text(log_frame, height=8, width=70)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        
        # Configurar redimensionamento
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(2, weight=1)
        main_frame.rowconfigure(7, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        
        # Log inicial
        self.add_log("Sistema iniciado em modo simulação")
        self.add_log("AVISO: Esta é uma versão de demonstração")
        self.add_log("Para gravação real, instale PyAudio")

    # ----------------------
    # Tray helpers
    # ----------------------
    def _create_tray_image(self):
        """Cria um ícone simples para a bandeja (círculo azul)."""
        img = Image.new('RGB', (64, 64), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.ellipse((12, 12, 52, 52), fill=(30, 144, 255), outline=(0, 102, 204))
        return img

    def _setup_tray(self):
        try:
            menu = (
                pystray.MenuItem('Abrir', self._tray_show_window),
                pystray.MenuItem('Iniciar Gravação', self._tray_start_recording),
                pystray.MenuItem('Parar Gravação', self._tray_stop_recording),
                pystray.MenuItem('Abrir Pasta de Gravações', self._tray_open_recordings),
                pystray.MenuItem('Sair', self._tray_exit),
            )
            self.tray_icon = pystray.Icon("censura_digital", self._create_tray_image(), "Censura Digital", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception:
            self.tray_icon = None

    def _tray_show_window(self, icon, item):
        self.root.after(0, self._show_window)

    def _tray_start_recording(self, icon, item):
        self.root.after(0, self.start_recording)

    def _tray_stop_recording(self, icon, item):
        self.root.after(0, self.stop_recording)

    def _tray_open_recordings(self, icon, item):
        self.root.after(0, self.open_recordings_folder)

    def _tray_exit(self, icon, item):
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        self.root.after(0, self.root.destroy)

    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _on_close(self):
        """Minimiza para a bandeja se disponível; senão fecha."""
        if self.tray_icon:
            try:
                self.root.withdraw()
                self.add_log("Janela oculta na bandeja do sistema")
                return
            except Exception:
                pass
        # Sem bandeja: fecha normalmente
        self.root.destroy()
        
    def add_log(self, message):
        """Adiciona mensagem ao log"""
        timestamp = time.strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}\n"
        self.log_text.insert(tk.END, log_message)
        self.log_text.see(tk.END)
        
    def start_recording(self):
        """Simula início da gravação"""
        if not self.is_recording:
            self.is_recording = True
            self.recording_time = 0
            self.status_var.set("Gravando... (simulação)")
            self.start_btn.config(state='disabled')
            self.stop_btn.config(state='normal')
            self.add_log("Gravação iniciada (simulação)")
            self.add_log(f"Duração configurada: {self.duration_var.get()} minutos")
            self.add_log(f"Dispositivo: {self.device_var.get()}")
            
    def stop_recording(self):
        """Simula parada da gravação"""
        if self.is_recording:
            self.is_recording = False
            self.status_var.set("Gravação parada")
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
            self.add_log("Gravação parada")
            self.add_log(f"Tempo total: {self.recording_time} segundos")
            
    def update_interface(self):
        """Atualiza interface periodicamente"""
        # Simula nível de áudio
        if self.is_recording:
            import random
            level = random.randint(20, 80)
            self.level_var.set(level)
            self.level_label.config(text=f"Nível: {level}%")
            self.recording_time += 1
            
            # Atualiza informações
            minutes = self.recording_time // 60
            seconds = self.recording_time % 60
            self.info_var.set(f"Gravando: {minutes:02d}:{seconds:02d}")
        else:
            self.level_var.set(0)
            self.level_label.config(text="Nível: 0%")
            if hasattr(self, 'recording_time') and self.recording_time > 0:
                self.info_var.set(f"Última gravação: {self.recording_time} segundos")
            else:
                self.info_var.set("Nenhuma gravação ativa")
        
        # Reagenda para 1 segundo
        self.root.after(1000, self.update_interface)
        
    def open_recordings_folder(self):
        """Abre pasta de gravações"""
        recordings_dir = Path("gravacoes_radio")
        if recordings_dir.exists():
            os.startfile(str(recordings_dir))
            self.add_log("Pasta de gravações aberta")
        else:
            messagebox.showinfo("Pasta", "Pasta de gravações não existe ainda.\nSerá criada na primeira gravação.")
            
    def open_today_folder(self):
        """Abre pasta do dia atual"""
        from datetime import date
        today = date.today()
        today_dir = Path("gravacoes_radio") / str(today.year) / f"{today.month:02d}-{today.strftime('%B')}" / f"{today.day:02d}"
        
        if today_dir.exists():
            os.startfile(str(today_dir))
            self.add_log("Pasta de hoje aberta")
        else:
            messagebox.showinfo("Pasta", "Pasta de hoje não existe ainda.\nSerá criada na primeira gravação.")
            
    def open_config(self):
        """Abre configurações"""
        config_file = "config_censura.json"
        if os.path.exists(config_file):
            os.startfile(config_file)
            self.add_log("Arquivo de configuração aberto")
        else:
            messagebox.showinfo("Config", "Arquivo de configuração não encontrado.\nCopie config_censura_exemplo.json para config_censura.json")

def check_dependencies():
    """Verifica dependências básicas"""
    missing = []
    
    try:
        import numpy
        import tkinter
    except ImportError as e:
        missing.append(str(e))
    
    if missing:
        msg = f"Dependências faltando: {', '.join(missing)}\n"
        msg += "Execute: pip install numpy"
        messagebox.showerror("Dependências", msg)
        return False
    
    return True

def main():
    """Função principal"""
    # Verificar dependências básicas
    if not check_dependencies():
        return
    
    # Mostrar aviso sobre modo simulação
    root = tk.Tk()
    root.withdraw()  # Esconder janela temporariamente
    
    msg = """SISTEMA DE CENSURA DIGITAL - MODO SIMULAÇÃO

Esta é uma versão de demonstração que simula a gravação.

Para gravação real de áudio:
1. Instale PyAudio: pip install pyaudio
2. Use a versão completa: interface_censura_digital.py

Continuar em modo simulação?"""
    
    if messagebox.askyesno("Modo Simulação", msg):
        root.deiconify()  # Mostrar janela
        app = CensuraDigitalInterface(root)
        
        try:
            root.mainloop()
        except KeyboardInterrupt:
            print("\nSistema finalizado pelo usuário")
    else:
        root.destroy()

if __name__ == "__main__":
    main() 