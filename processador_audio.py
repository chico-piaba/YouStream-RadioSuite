#!/usr/bin/env python3
"""
Processador de Áudio para o Sistema de Censura Digital
Contém a lógica de processamento para ser usada por UIs ou scripts.
"""
from __future__ import annotations

import os
import argparse
import logging
import zipfile
import subprocess
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
import json
import platform
import shutil
import tempfile

class AudioProcessor:
    def __init__(self, output_dir="processados", config_file="config_censura.json"):
        self.output_dir = Path(output_dir)
        self.config_file = config_file
        self.logger = logging.getLogger(__name__)
        self.stop_processing_flag = threading.Event()
        self._load_config()

    def _load_config(self):
        """Carrega configurações do arquivo de configuração."""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except FileNotFoundError:
            self.logger.warning(f"Arquivo de config '{self.config_file}' não encontrado. Usando padrões.")
            config = {}
        except Exception as e:
            self.logger.error(f"Erro ao ler config '{self.config_file}': {e}. Usando padrões.")
            config = {}

        recording_cfg = config.get("recording", {})
        processing_cfg = config.get("processing", {})

        self.base_dir = Path(recording_cfg.get("output_directory", "gravacoes_radio"))
        self.mp3_bitrate_kbps = int(processing_cfg.get("mp3_bitrate_kbps", 128))
        self.ffmpeg_path = processing_cfg.get("ffmpeg_path") or "ffmpeg"
        self.ffmpeg_threads = int(processing_cfg.get("ffmpeg_threads", 1))
        self.delete_wav_after_days = int(processing_cfg.get("delete_wav_after_days", 1))
        self.process_priority = str(processing_cfg.get("process_priority", "low")).lower()

        # Resolve caminho do ffmpeg se possível
        self.ffmpeg_cmd = self._resolve_ffmpeg_command(self.ffmpeg_path)

        self.logger.info(f"Processador configurado para buscar em: {self.base_dir}")
        self.logger.info(f"FFmpeg: {self.ffmpeg_cmd} | Bitrate: {self.mp3_bitrate_kbps} kbps | Threads: {self.ffmpeg_threads}")

    def _resolve_ffmpeg_command(self, preferred_path: str) -> str:
        """Resolve o executável do ffmpeg a partir do PATH ou de um caminho fornecido."""
        if preferred_path and Path(preferred_path).exists():
            return str(Path(preferred_path))
        # Tenta achar no PATH
        found = shutil.which("ffmpeg")
        if found:
            return found
        # Tenta variações no Windows
        if platform.system().lower().startswith("win"):
            found = shutil.which("ffmpeg.exe")
            if found:
                return found
        # Não encontrado, retorna o preferido (deixará o erro acontecer ao executar)
        return preferred_path or "ffmpeg"

    def reload_config(self):
        """Recarrega a configuração para garantir que o base_dir esteja atualizado."""
        self.logger.info("Recarregando configuração do processador...")
        self._load_config()

    def stop(self):
        """Sinaliza para a thread de processamento parar."""
        self.stop_processing_flag.set()

    def _find_wav_files(self, target_date: datetime.date) -> list[Path]:
        # Estrutura: gravacoes_radio/AAAA/MM-DD/
        date_dir = self.base_dir / target_date.strftime("%Y") / target_date.strftime("%m-%d")
        if not date_dir.is_dir():
            self.logger.warning(f"Diretório para a data {target_date.strftime('%Y-%m-%d')} não encontrado: {date_dir}")
            return []
        wav_files = sorted(list(date_dir.glob("*.wav")))
        self.logger.info(f"Encontrados {len(wav_files)} arquivos .wav em {date_dir}")
        return wav_files

    def _convert_wav_to_mp3(self, wav_path: Path, output_dir: Path) -> Path | None:
        try:
            parts = wav_path.stem.split('_')
            if len(parts) < 3:
                self.logger.error(f"Nome de arquivo com formato inesperado, pulando: {wav_path.name}")
                return None
            timestamp_str = f"{parts[-2]}_{parts[-1]}"
            dt_obj = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
            new_filename = dt_obj.strftime("%Y-%m-%d_%H-%M-%S") + ".mp3"
            mp3_path = output_dir / new_filename
            
            # Monta comando do FFmpeg com bitrate configurável e limite de threads
            command = [self.ffmpeg_cmd, "-hide_banner", "-loglevel", "error", "-y", "-threads", str(self.ffmpeg_threads), "-i", str(wav_path), "-vn", "-b:a", f"{self.mp3_bitrate_kbps}k", str(mp3_path)]

            creationflags = 0
            preexec_fn = None
            system_name = platform.system().lower()
            if self.process_priority == "low":
                if system_name.startswith("win"):
                    try:
                        creationflags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                    except Exception:
                        creationflags = 0
                else:
                    # Em sistemas POSIX, ajusta o nice do processo filho
                    def _set_low_priority():
                        try:
                            os.nice(10)
                        except Exception:
                            pass
                    preexec_fn = _set_low_priority

            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            )
            return mp3_path
        except FileNotFoundError:
            self.logger.error("ERRO CRÍTICO: Comando 'ffmpeg' não encontrado!")
            raise
        except subprocess.CalledProcessError:
            self.logger.error(f"Falha ao converter {wav_path.name}. O FFmpeg retornou um erro.")
            return None
        except Exception as e:
            self.logger.error(f"Um erro inesperado ocorreu durante a conversão de {wav_path.name}: {e}")
            return None

    def _create_zip_file(self, files: list[Path], output_zip_path: Path, progress_callback=None):
        if not files:
            self.logger.warning("Nenhum arquivo para compactar.")
            if progress_callback: progress_callback("Nenhum arquivo para compactar.")
            return
        
        if progress_callback: progress_callback(f"Criando arquivo ZIP: {output_zip_path.name}")
        with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file in files:
                zf.write(file, arcname=file.name)
        
        file_size_mb = output_zip_path.stat().st_size / (1024 * 1024)
        if progress_callback: progress_callback(f"Arquivo ZIP criado com sucesso! Tamanho: {file_size_mb:.2f} MB")

    def run_processing(self, target_date_str: str, keep_mp3: bool = True, progress_callback=None, blocking: bool = False):
        """Processa uma data específica. Se blocking=True, executa de forma síncrona."""
        self.stop_processing_flag.clear()  # Reseta o sinalizador no início

        def task():
            try:
                target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
            except ValueError:
                if progress_callback: progress_callback("ERRO: Formato de data inválido. Use AAAA-MM-DD.")
                return

            # MP3s serão salvos na mesma pasta das WAVs (por data)
            mp3_output_dir = self.base_dir / target_date.strftime("%Y") / target_date.strftime("%m-%d")
            mp3_output_dir.mkdir(parents=True, exist_ok=True)

            if progress_callback: progress_callback(f"Procurando arquivos .wav para {target_date_str}...")
            wav_files = self._find_wav_files(target_date)
            if not wav_files:
                if progress_callback: progress_callback("Nenhum arquivo .wav encontrado para esta data.")
                return

            if progress_callback: progress_callback(f"{len(wav_files)} arquivos encontrados. Iniciando conversão para MP3...")

            mp3_files = []
            for i, wav_file in enumerate(wav_files):
                if self.stop_processing_flag.is_set():
                    if progress_callback: progress_callback("\nProcessamento cancelado pelo usuário.")
                    break

                if progress_callback: progress_callback(f"({i+1}/{len(wav_files)}) Convertendo {wav_file.name}...")
                mp3_file = self._convert_wav_to_mp3(wav_file, mp3_output_dir)
                if mp3_file:
                    mp3_files.append(mp3_file)

            if mp3_files:
                zip_filename = f"gravacoes_{target_date.strftime('%Y-%m-%d')}.zip"
                zip_output_path = self.output_dir / zip_filename
                self._create_zip_file(mp3_files, zip_output_path, progress_callback)

                if not keep_mp3:
                    if progress_callback: progress_callback("Limpando arquivos MP3 temporários...")
                    for mp3_file in mp3_files:
                        try:
                            mp3_file.unlink()
                        except OSError as e:
                            self.logger.error(f"Erro ao deletar {mp3_file}: {e}")

            if not self.stop_processing_flag.is_set():
                if progress_callback: progress_callback("\nProcessamento concluído!")

        if blocking:
            task()
        else:
            threading.Thread(target=task, daemon=True).start()

    def cleanup_old_wavs(self, keep_days: int = None):
        """Remove arquivos WAV cujo diretório de data é mais antigo que keep_days."""
        keep_days = self.delete_wav_after_days if keep_days is None else keep_days
        today = date.today()
        years_dir = self.base_dir
        if not years_dir.exists():
            return
        for year_dir in years_dir.iterdir():
            if not year_dir.is_dir():
                continue
            for md_dir in year_dir.iterdir():
                if not md_dir.is_dir():
                    continue
                try:
                    md = datetime.strptime(md_dir.name, "%m-%d").date().replace(year=int(year_dir.name))
                except Exception:
                    continue
                delta = (today - md).days
                if delta > keep_days:
                    for wav_file in md_dir.glob("*.wav"):
                        try:
                            wav_file.unlink()
                            self.logger.info(f"WAV removido (retenção {keep_days}d): {wav_file}")
                        except OSError as e:
                            self.logger.error(f"Erro ao remover {wav_file}: {e}")

    # ---------------- Intervalo / Trechos -----------------
    def _parse_wav_start(self, wav_path: Path) -> datetime | None:
        """Extrai datetime de início a partir do nome do arquivo gerado pelo gravador."""
        try:
            parts = wav_path.stem.split('_')
            timestamp_str = f"{parts[-2]}_{parts[-1]}"
            return datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
        except Exception:
            return None

    def _wav_files_covering_range(self, start_dt: datetime, end_dt: datetime) -> list[Path]:
        """Retorna os arquivos WAV que cobrem (mesmo que parcialmente) o intervalo [start_dt, end_dt)."""
        files: list[Path] = []
        day = start_dt.date()
        while day <= end_dt.date():
            files.extend(self._find_wav_files(day))
            day = day + timedelta(days=1)
        # Filtra apenas os que têm timestamp válido e ordena
        annotated: list[tuple[Path, datetime]] = []
        for f in files:
            dt = self._parse_wav_start(f)
            if dt:
                annotated.append((f, dt))
        annotated.sort(key=lambda x: x[1])
        if not annotated:
            return []
        # Determina duração esperada de cada chunk pelos metadados de gravação
        chunk_minutes = 15
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                chunk_minutes = int(cfg.get("recording", {}).get("chunk_duration_minutes", 15))
        except Exception:
            pass
        chunk_delta = timedelta(minutes=chunk_minutes)

        covering: list[Path] = []
        for path, dt0 in annotated:
            dt1 = dt0 + chunk_delta
            # Se sobrepõe ao intervalo?
            if dt0 < end_dt and dt1 > start_dt:
                covering.append(path)
        return covering

    def extract_interval(self, start_str: str, end_str: str, progress_callback=None, blocking: bool = False) -> Path | None:
        """Extrai um trecho entre start_str e end_str (formato 'YYYY-MM-DD HH:MM:SS') e salva MP3 reencodado.

        Retorna o caminho do arquivo MP3 gerado, ou None em caso de falha.
        """
        self.stop_processing_flag.clear()

        def task() -> Path | None:
            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                if progress_callback: progress_callback("ERRO: Formato inválido. Use 'AAAA-MM-DD HH:MM:SS'.")
                return None

            if end_dt <= start_dt:
                if progress_callback: progress_callback("ERRO: O horário final deve ser após o inicial.")
                return None

            # Localiza arquivos que cobrem o intervalo
            wavs = self._wav_files_covering_range(start_dt, end_dt)
            if not wavs:
                if progress_callback: progress_callback("Nenhum WAV cobre o intervalo solicitado.")
                return None

            # Define saída
            interval_dir = self.output_dir / "intervalos"
            interval_dir.mkdir(parents=True, exist_ok=True)
            out_name = f"intervalo_{start_dt.strftime('%Y-%m-%d_%H-%M-%S')}__{end_dt.strftime('%Y-%m-%d_%H-%M-%S')}.mp3"
            out_path = interval_dir / out_name

            # Se apenas um arquivo cobre, recorta direto. Caso contrário, mescla primeiro.
            temp_dir = Path(tempfile.mkdtemp(prefix="censura_tmp_"))
            merged_wav = temp_dir / "merged.wav"
            creationflags = 0
            preexec_fn = None
            system_name = platform.system().lower()
            if self.process_priority == "low":
                if system_name.startswith("win"):
                    try:
                        creationflags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
                    except Exception:
                        creationflags = 0
                else:
                    def _set_low_priority():
                        try:
                            os.nice(10)
                        except Exception:
                            pass
                    preexec_fn = _set_low_priority

            try:
                if progress_callback: progress_callback("Preparando arquivos para recorte do intervalo...")
                if len(wavs) == 1:
                    # Usa o próprio WAV
                    src_wav = wavs[0]
                else:
                    # Concatena os WAVs com demuxer concat (cópia de stream)
                    list_file = temp_dir / "list.txt"
                    with open(list_file, 'w', encoding='utf-8') as lf:
                        for w in wavs:
                            lf.write(f"file '{w.as_posix()}'\n")
                    cmd_concat = [self.ffmpeg_cmd, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(merged_wav)]
                    subprocess.run(cmd_concat, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags, preexec_fn=preexec_fn)
                    src_wav = merged_wav

                # Calcula offset e duração
                first_start = self._parse_wav_start(wavs[0]) or start_dt
                offset_sec = max(0.0, (start_dt - first_start).total_seconds())
                duration_sec = (end_dt - start_dt).total_seconds()

                if progress_callback: progress_callback("Gerando arquivo final do intervalo (MP3 128kbps)...")
                cmd_cut = [
                    self.ffmpeg_cmd, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(offset_sec), "-t", str(duration_sec),
                    "-i", str(src_wav),
                    "-vn", "-b:a", f"{self.mp3_bitrate_kbps}k",
                    str(out_path)
                ]
                subprocess.run(cmd_cut, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags, preexec_fn=preexec_fn)

                if progress_callback: progress_callback(f"Intervalo criado: {out_path.name}")
                return out_path
            except FileNotFoundError:
                self.logger.error("ERRO CRÍTICO: FFmpeg não encontrado para extração de trecho.")
                if progress_callback: progress_callback("ERRO: FFmpeg não encontrado.")
                return None
            except subprocess.CalledProcessError:
                self.logger.error("Falha durante a extração do trecho.")
                if progress_callback: progress_callback("ERRO: Falha durante a extração do trecho.")
                return None
            finally:
                # Limpa temporários
                try:
                    for f in temp_dir.glob('*'):
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    temp_dir.rmdir()
                except Exception:
                    pass

        if blocking:
            return task()
        else:
            threading.Thread(target=task, daemon=True).start()
            return None

def main_cli():
    """Interface de Linha de Comando para o processador."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    parser = argparse.ArgumentParser(description="Processador de áudios do Sistema de Censura Digital.")
    parser.add_argument("target_date", help="Data no formato YYYY-MM-DD.")
    parser.add_argument("--output-dir", default="processados", help="Diretório para salvar MP3 e ZIP.")
    parser.add_argument("--config-file", default="config_censura.json", help="Arquivo de configuração JSON.")
    parser.add_argument("--keep-mp3", action="store_true", help="Manter MP3s após criar o ZIP.")
    args = parser.parse_args()

    processor = AudioProcessor(output_dir=args.output_dir, config_file=args.config_file)
    
    # Para CLI, o progresso é o log padrão
    processor.run_processing(args.target_date, args.keep_mp3, progress_callback=logging.info)

if __name__ == "__main__":
    main_cli() 