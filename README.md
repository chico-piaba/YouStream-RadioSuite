# Sistema de Gravador de Censura Digital

Este projeto contém um conjunto de scripts em Python para realizar a gravação contínua de áudio, ideal para fins de registro de transmissão de rádio (censura).

O sistema foi projetado para ser robusto e funcionar de forma contínua, dividindo as gravações em arquivos menores (chunks) e organizando-os em uma estrutura de pastas por data. Além da gravação, suporta **streaming ao vivo** via RTMP e Icecast, substituindo a necessidade de software externo como o OBS.

## Funcionalidades

- Gravação de áudio contínua a partir de uma fonte de entrada (microfone, mesa de som, etc.).
- Divisão das gravações em "chunks" com duração configurável (padrão: 15 minutos).
- Organização automática dos arquivos em pastas: `Ano/Mês-Dia`.
- **Streaming RTMP** – envia áudio ao vivo para YouTube Live, Facebook Live ou qualquer servidor RTMP.
- **Streaming Icecast** – envia áudio para servidores Icecast (rádio internet).
- **Watchdog de gravação** – detecta travamentos no dispositivo de áudio e tenta reiniciar automaticamente.
- Captura de áudio não-bloqueante via callback do PyAudio com `queue.Queue`.
- Rotinas de limpeza de memória (`gc.collect`) entre chunks.
- Configuração flexível através de um arquivo `config_censura.json`.
- Processamento automático de WAV para MP3/ZIP pós-meia-noite.
- Logs detalhados de operação.

## Arquitetura

```
interface_censura_digital.py   ← Interface gráfica (Tk)
    ├── gravador_censura_digital.py  ← Captura de áudio (PyAudio callback)
    │       ├── Grava arquivos WAV em chunks
    │       ├── Watchdog thread (detecção de stalls)
    │       └── Fan-out para StreamManager
    ├── stream_manager.py            ← Streaming RTMP/Icecast (FFmpeg)
    │       ├── Processo FFmpeg RTMP  (aac → flv)
    │       └── Processo FFmpeg Icecast (mp3 → icecast://)
    └── processador_audio.py         ← Conversão WAV→MP3, ZIP, extração de trechos
```

## Implantação e Execução em Windows

### 1. Pré-requisitos

- **Python 3.8+ instalado.** Baixe em [python.org](https://www.python.org/downloads/).
  - **Importante:** Durante a instalação, marque **"Add Python to PATH"**.
- **FFmpeg** (necessário para processamento e streaming). Baixe em [ffmpeg.org](https://ffmpeg.org/download.html) e adicione ao PATH.

### 2. Instalação das Dependências

Abra o **Prompt de Comando (CMD)** ou **PowerShell** e navegue até a pasta `censura-digital`:

```sh
cd caminho\para\o\projeto\censura-digital
```

**Método 1: Instalação Padrão (Recomendado)**

```sh
pip install -r requirements.txt
```

Se não houver erros, pule para a seção **"3. Configuração do Gravador"**. Se `PyAudio` falhar, tente:

**Método 2: Instalação com `pipwin`**

```sh
pip install pipwin
pipwin install pyaudio
```

**Método 3: Instalação Manual do Wheel**

1. Descubra sua versão do Python e arquitetura (`python` no cmd).
2. Baixe o `.whl` em [gohlke/pythonlibs](https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio).
3. Instale: `pip install PyAudio-0.2.11-cp310-cp310-win_amd64.whl` (adapte o nome).

### 3. Configuração do Gravador

1. **Listar Dispositivos de Áudio:**

    ```sh
    python teste_censura_digital.py
    ```

    Anote o número do dispositivo que corresponde à sua fonte de áudio.

2. **Editar o Arquivo de Configuração:**

    Copie `config_censura_exemplo.json` para `config_censura.json` (se ainda não existir) e edite:

    ```json
    {
      "audio": {
        "format": "paInt16",
        "channels": 1,
        "rate": 44100,
        "chunk_size": 1024,
        "device_index": 2
      },
      "recording": {
        "chunk_duration_minutes": 15,
        "output_directory": "gravacoes_radio",
        "filename_prefix": "radio",
        "max_chunks_per_day": 96
      },
      "streaming": {
        "rtmp": {
          "enabled": false,
          "url": "rtmp://servidor/live/chave",
          "audio_bitrate_kbps": 128
        },
        "icecast": {
          "enabled": false,
          "host": "localhost",
          "port": 8000,
          "mount": "/live",
          "source_password": "sua_senha",
          "audio_bitrate_kbps": 128
        }
      },
      "logging": {
        "log_file": "censura_digital.log",
        "log_level": "INFO"
      }
    }
    ```

### 4. Executando os Testes

```sh
python teste_censura_digital.py
```

Quando perguntado `Deseja fazer teste de gravação de 10 segundos? (s/n):`, digite `s` e pressione Enter.

### 5. Iniciando a Interface Gráfica

```sh
python interface_censura_digital.py
```

A interface possui três abas:

- **Gravação** – Iniciar/parar gravação, monitoramento de áudio, indicador de saúde (watchdog).
- **Streaming** – Configurar e iniciar/parar RTMP e Icecast independentemente.
- **Configurações** – Selecionar dispositivo de áudio e diretório de saída.

### 6. Gravação via Linha de Comando (sem interface)

```sh
python gravador_censura_digital.py
```

O terminal mostrará "Gravação iniciada". O programa grava em chunks de 15 minutos até `Ctrl+C`.

Opções adicionais:

```sh
python gravador_censura_digital.py --list-devices
python gravador_censura_digital.py --monitor
python gravador_censura_digital.py --config outro_config.json
```

## Streaming (RTMP e Icecast)

O sistema substitui a necessidade do OBS ao enviar o áudio capturado diretamente para servidores de streaming via FFmpeg.

### RTMP (YouTube Live, Facebook Live, etc.)

1. Na aba **Streaming**, insira a URL RTMP (ex: `rtmp://a.rtmp.youtube.com/live2/sua-chave`).
2. Configure o bitrate (padrão: 128 kbps).
3. Inicie a gravação na aba **Gravação**.
4. Clique **Iniciar RTMP**.

O áudio é codificado em AAC e enviado no formato FLV via FFmpeg.

### Icecast (Rádio Internet)

1. Na aba **Streaming**, configure Host, Porta, Mount Point e Senha do servidor Icecast.
2. Configure o bitrate (padrão: 128 kbps).
3. Inicie a gravação na aba **Gravação**.
4. Clique **Iniciar Icecast**.

O áudio é codificado em MP3 via libmp3lame e enviado ao servidor Icecast.

### Requisitos do FFmpeg para Streaming

O FFmpeg precisa ter suporte a:
- **AAC** (encoder nativo do FFmpeg) – para RTMP.
- **libmp3lame** – para Icecast. Builds oficiais do FFmpeg já incluem ambos.

## Watchdog e Monitoramento

O sistema inclui um **watchdog** que monitora a saúde da captura de áudio:

- Detecta quando o dispositivo para de enviar dados (stall) por mais de 10 segundos.
- Tenta reiniciar o stream de entrada automaticamente (até 5 tentativas).
- Exibe alertas na interface gráfica em tempo real.
- Registra todos os eventos no log.

## Limpeza e Manutenção

- **Limpeza de memória**: `gc.collect()` é executado ao final de cada chunk.
- **Processamento diário automático**: 5 minutos após meia-noite, o sistema converte os WAVs do dia anterior para MP3.
- **Limpeza de WAVs antigos**: Arquivos WAV são removidos após o período configurado em `delete_wav_after_days`.
