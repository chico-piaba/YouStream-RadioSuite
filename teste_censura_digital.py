#!/usr/bin/env python3
"""
Script de teste para o Sistema de Censura Digital
"""

import time
import json
from gravador_censura_digital import CensuraDigital

def teste_configuracao():
    """Testa o carregamento de configurações"""
    print("=== Teste de Configuração ===")
    
    # Testa configuração padrão
    censura = CensuraDigital("config_teste.json")
    print(f"✓ Configuração carregada: {censura.config_file}")
    print(f"✓ Diretório de saída: {censura.config['recording']['output_directory']}")
    print(f"✓ Duração do chunk: {censura.config['recording']['chunk_duration_minutes']} minutos")
    print()

def teste_dispositivos():
    """Testa listagem de dispositivos"""
    print("=== Teste de Dispositivos ===")
    
    try:
        censura = CensuraDigital()
        censura.list_audio_devices()
        print("✓ Dispositivos listados com sucesso")
    except Exception as e:
        print(f"✗ Erro ao listar dispositivos: {e}")
    print()

def teste_gravacao_curta():
    """Testa gravação de 10 segundos"""
    print("=== Teste de Gravação Curta ===")
    
    # Configuração para teste rápido
    config_teste = {
        "audio": {
            "format": "paInt16",
            "channels": 1,
            "rate": 44100,
            "chunk_size": 1024,
            "device_index": None
        },
        "recording": {
            "chunk_duration_minutes": 0.17,  # ~10 segundos
            "output_directory": "teste_gravacao",
            "filename_prefix": "teste",
            "max_chunks_per_day": 3
        },
        "logging": {
            "log_file": "teste_censura.log",
            "log_level": "INFO"
        }
    }
    
    # Salva configuração de teste
    with open("config_teste.json", "w") as f:
        json.dump(config_teste, f, indent=2)
    
    try:
        censura = CensuraDigital("config_teste.json")
        
        print("Iniciando gravação de teste (10 segundos)...")
        if censura.start_recording():
            print("✓ Gravação iniciada")
            
            # Aguarda 12 segundos
            for i in range(12):
                time.sleep(1)
                print(f"  Gravando... {i+1}/12 segundos")
            
            censura.stop_recording()
            print("✓ Gravação finalizada")
            
            # Verifica status
            status = censura.get_status()
            print(f"✓ Chunks gravados: {status['chunk_counter']}")
            
        else:
            print("✗ Falha ao iniciar gravação")
            
    except Exception as e:
        print(f"✗ Erro na gravação: {e}")
    print()

def teste_organizacao_arquivos():
    """Testa organização de arquivos"""
    print("=== Teste de Organização de Arquivos ===")
    
    try:
        censura = CensuraDigital()
        
        # Testa criação de diretório
        import datetime
        hoje = datetime.date.today()
        output_dir = censura.create_output_directory(hoje)
        print(f"✓ Diretório criado: {output_dir}")
        
        # Testa geração de nome de arquivo
        agora = datetime.datetime.now()
        filename = censura.generate_filename(agora)
        print(f"✓ Nome do arquivo: {filename}")
        
    except Exception as e:
        print(f"✗ Erro na organização: {e}")
    print()

def main():
    """Executa todos os testes"""
    print("🎙️  SISTEMA DE CENSURA DIGITAL - TESTES")
    print("=" * 50)
    
    try:
        teste_configuracao()
        teste_dispositivos()
        teste_organizacao_arquivos()
        
        # Pergunta se quer fazer teste de gravação
        resposta = input("Deseja fazer teste de gravação de 10 segundos? (s/n): ")
        if resposta.lower() in ['s', 'sim', 'y', 'yes']:
            teste_gravacao_curta()
        
        print("=" * 50)
        print("✓ Todos os testes concluídos!")
        print("\nPara usar o sistema:")
        print("1. Configure o arquivo config_censura.json")
        print("2. Execute: python gravador_censura_digital.py")
        
    except KeyboardInterrupt:
        print("\n\nTestes interrompidos pelo usuário")
    except Exception as e:
        print(f"\n✗ Erro geral nos testes: {e}")

if __name__ == "__main__":
    main() 