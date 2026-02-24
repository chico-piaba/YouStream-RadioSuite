#!/usr/bin/env python3
"""
Launcher para a Interface Gráfica do Sistema de Censura Digital
"""

import sys
import os
import subprocess
import platform

def check_dependencies():
    """Verifica se as dependências estão instaladas"""
    required_modules = ['tkinter', 'pyaudio', 'numpy']
    missing_modules = []
    
    for module in required_modules:
        try:
            if module == 'tkinter':
                import tkinter
            elif module == 'pyaudio':
                import pyaudio
            elif module == 'numpy':
                import numpy
        except ImportError:
            missing_modules.append(module)
    
    return missing_modules

def install_dependencies():
    """Instala dependências faltantes"""
    try:
        # Instala pyaudio e numpy
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt'], check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def main():
    """Função principal"""
    print("🎙️  SISTEMA DE CENSURA DIGITAL - INTERFACE GRÁFICA")
    print("=" * 60)
    
    # Verifica dependências
    missing = check_dependencies()
    
    if missing:
        print(f"❌ Dependências faltantes: {', '.join(missing)}")
        print("💾 Instalando dependências...")
        
        if install_dependencies():
            print("✅ Dependências instaladas com sucesso!")
        else:
            print("❌ Erro ao instalar dependências.")
            print("🔧 Instale manualmente:")
            print("   pip install -r requirements.txt")
            return
    
    # Verifica se o sistema principal existe
    if not os.path.exists('gravador_censura_digital.py'):
        print("❌ Arquivo gravador_censura_digital.py não encontrado!")
        print("🔧 Certifique-se de estar no diretório correto.")
        return
    
    if not os.path.exists('interface_censura_digital.py'):
        print("❌ Arquivo interface_censura_digital.py não encontrado!")
        print("🔧 Certifique-se de estar no diretório correto.")
        return
    
    if not os.path.exists('stream_manager.py'):
        print("❌ Arquivo stream_manager.py não encontrado!")
        print("🔧 Certifique-se de estar no diretório correto.")
        return
    
    # Lança a interface
    print("🚀 Iniciando interface gráfica...")
    try:
        from interface_censura_digital import main as interface_main
        interface_main()
    except Exception as e:
        print(f"❌ Erro ao iniciar interface: {e}")
        print("🔧 Tente executar diretamente: python3 interface_censura_digital.py")

if __name__ == "__main__":
    main() 