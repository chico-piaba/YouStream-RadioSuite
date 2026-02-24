# Guia de Uso do Processador de Áudio

Este guia explica como usar o script `processador_audio.py` para converter os arquivos `.wav` gravados em `.mp3` e compactá-los em um arquivo `.zip`.

A etapa mais importante é a instalação do **FFmpeg**, uma ferramenta externa que o script utiliza para realizar a conversão de áudio. **Este é um passo que você só precisa fazer uma vez.**

## Passo 1: Instalar o FFmpeg no Windows

FFmpeg é uma coleção de softwares livres para manipulação de áudio, vídeo e outras mídias. O `pydub` (nossa biblioteca de áudio) precisa dele para funcionar.

1.  **Baixe o FFmpeg:**
    *   Vá para o site oficial de builds para Windows: **[https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/)**
    *   Procure pela seção **"release builds"**.
    *   Clique no link `ffmpeg-release-full.7z` para baixar a versão completa.

    ![Download do FFmpeg](https://i.imgur.com/3fs3YhA.png)

2.  **Descompacte o Arquivo:**
    *   O arquivo baixado tem a extensão `.7z`. Você precisará de um programa como o [7-Zip](https://www.7-zip.org/) ou [WinRAR](https://www.win-rar.com/) para descompactá-lo.
    *   Descompacte o conteúdo em um local permanente no seu computador. Um bom lugar é diretamente no seu disco local, como `C:\ffmpeg`.
    *   Após descompactar, você terá uma pasta chamada `ffmpeg-7.0-release-full` (ou similar). Renomeie esta pasta para apenas `ffmpeg` para facilitar.
    *   Dentro de `C:\ffmpeg`, você deve encontrar uma pasta chamada `bin`, que contém os executáveis `ffmpeg.exe`, `ffplay.exe` e `ffprobe.exe`. O caminho final para a pasta `bin` deve ser `C:\ffmpeg\bin`.

3.  **Adicione o FFmpeg ao PATH do Windows:**
    *   Este é o passo crucial. Ele permite que o sistema encontre o `ffmpeg.exe` de qualquer lugar.
    *   No menu Iniciar, pesquise por **"Editar as variáveis de ambiente do sistema"** e abra.
    *   Na janela que abrir, clique no botão **"Variáveis de Ambiente..."**.
    *   Na seção "Variáveis do sistema" (a segunda lista), encontre a variável **`Path`** e clique duas vezes sobre ela.
    *   Na nova janela, clique em **"Novo"** e cole o caminho para a sua pasta `bin` do FFmpeg. Usando o nosso exemplo, seria: `C:\ffmpeg\bin`
    *   Clique em **"OK"** em todas as janelas para salvar as alterações.

    ![Adicionar ao PATH](https://i.imgur.com/uRjJzD4.png)

4.  **Verifique a Instalação:**
    *   **Feche e reabra qualquer terminal (CMD, PowerShell) que estiver aberto.** Isso é importante para que ele carregue as novas variáveis de ambiente.
    *   No novo terminal, digite o comando:
        ```sh
        ffmpeg -version
        ```
    *   Se a instalação deu certo, você verá informações sobre a versão do FFmpeg. Se receber um erro de "comando não encontrado", revise os passos anteriores.

## Passo 2: Instalar as Dependências do Python

Se você ainda não o fez, instale a biblioteca `pydub` através do `requirements.txt`.

1.  Abra o terminal (CMD ou PowerShell) na pasta `censura-digital`.
2.  Ative seu ambiente virtual (ex: `.\venv\Scripts\activate`).
3.  Execute:
    ```sh
    pip install -r requirements.txt
    ```

## Passo 3: Executar o Script de Processamento

Com o FFmpeg e o `pydub` instalados, você pode executar o processador.

1.  **Abra o terminal** na pasta do projeto.
2.  Execute o script passando a **data** que você deseja processar como argumento, no formato `AAAA-MM-DD`.

**Exemplo de uso:**

Para processar as gravações do dia 5 de julho de 2025:

```sh
python processador_audio.py 2025-07-05
```

O script irá:
- Procurar os arquivos `.wav` em `gravacoes_radio/2025/07-05/`.
- Converter cada um para `.mp3` e salvá-los na mesma pasta por data.
- Criar um arquivo `processados/gravacoes_2025-07-05.zip`.
- Opcionalmente, manter os `.mp3` individuais (`--keep-mp3`), além do `.zip` final.

## Seção "processing" em config_censura.json

Você pode ajustar parâmetros de conversão e limpeza no arquivo `config_censura.json` (seção opcional `processing`):

```json
{
  "processing": {
    "mp3_bitrate_kbps": 128,
    "ffmpeg_path": "ffmpeg",
    "ffmpeg_threads": 1,
    "delete_wav_after_days": 1,
    "process_priority": "low"
  }
}
```

- `mp3_bitrate_kbps`: bitrate alvo para o MP3 (padrão 128).
- `ffmpeg_path`: caminho para o executável do FFmpeg, usado se não estiver no PATH.
- `ffmpeg_threads`: limita o uso de threads do FFmpeg para não competir com a gravação.
- `delete_wav_after_days`: número de dias para reter os WAVs após conversão (padrão 1 dia).
- `process_priority`: define prioridade do processo de conversão ("low" recomendado).

### Opções Adicionais

Você pode usar alguns argumentos para customizar a execução:

-   `--keep-mp3`: Mantém os arquivos `.mp3` individuais após criar o zip.
    ```sh
    python processador_audio.py 2025-07-05 --keep-mp3
    ```
-   `--base-dir "C:\Outro\Caminho"`: Especifica um diretório diferente para procurar as gravações.
-   `--output-dir "C:\Saida"`: Especifica um diretório diferente para salvar os arquivos processados. 