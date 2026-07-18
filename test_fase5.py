import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

# Importamos las funciones necesarias del script principal
from migrar_cuentas_tidal import (
    TidalMigrationManager,
    PROFILE_DIR_MAIN,
    PROFILE_DIR_PARENT,
    STEALTH_SCRIPT
)

def test_fase_v():
    print("="*70)
    print("  TEST DE SUBPROCESO: FASE V (PASOS 8, 9, 10 Y 11)")
    print("  (Aísla la configuración del plan familiar y cambio de contraseña)")
    print("="*70)
    
    # Pedir credenciales para el test
    client_email = input("\nIntroduce el correo del cliente: ").strip()
    target_pwd = input("Introduce la contraseña nueva definitiva para el cliente: ").strip()
    
    if not client_email or not target_pwd:
        print("\n[ERROR] El correo del cliente y la contraseña nueva son obligatorios.")
        return
        
    print("\n[INFO] Iniciando navegador principal (Perfil del Cliente)...")
    
    # Inicializar el manager
    manager = TidalMigrationManager(
        main_profile=PROFILE_DIR_MAIN,
        parent_profile=PROFILE_DIR_PARENT,
        client_email=client_email,
        client_pwd="", # No es necesaria para el restablecimiento
        target_pwd=target_pwd
    )
    
    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        manager.context = p.chromium.launch_persistent_context(
            user_data_dir=str(manager.main_profile),
            headless=False,
            args=launch_args,
            viewport={"width": 1280, "height": 900},
            locale="es-ES"
        )
        
        # Inyectar sigilo
        manager.context.add_init_script(STEALTH_SCRIPT)
        
        # Cerrar otras pestañas
        while len(manager.context.pages) > 1:
            try:
                manager.context.pages[-1].close()
            except:
                pass
                
        manager.page = manager.context.pages[0] if manager.context.pages else manager.context.new_page()
        manager.page.bring_to_front()
        
        try:
            print("\n" + "="*70)
            print("  [PASO PREVIO] LOGUEO MANUAL DE LA NUEVA CUENTA DEL CLIENTE")
            print("  Por favor, asegúrate de que la cuenta nueva del cliente esté LOGUEADA")
            print("  en la ventana de Chrome que se acaba de abrir.")
            print("="*70)
            input(">>> Cuando la cuenta esté logueada en la ventana, presiona Enter aquí para iniciar la Fase V <<<")
            
            # -------------------------------------------------------------
            # PASO 8: Solicitar restablecimiento de contraseña
            # -------------------------------------------------------------
            print("\n>>> EJECUTANDO PASO 8: Solicitando resetpass desde canal privado...")
            manager.step8_request_password_reset()
            print("[OK] Paso 8 completado.")
            time.sleep(2.0)
            
            # -------------------------------------------------------------
            # PASO 9: Enviar invitación familiar
            # -------------------------------------------------------------
            print("\n>>> EJECUTANDO PASO 9: Enviando invitación familiar desde la cuenta titular...")
            manager.step9_invite_to_family_plan(p)
            print("[OK] Paso 9 completado.")
            time.sleep(2.0)
            
            # -------------------------------------------------------------
            # PASO 10: Completar restablecimiento de contraseña
            # -------------------------------------------------------------
            print("\n>>> EJECUTANDO PASO 10: Buscando enlace de reset y cambiando contraseña...")
            manager.step10_complete_password_reset()
            print("[OK] Paso 10 completado.")
            time.sleep(2.0)
            
            # -------------------------------------------------------------
            # PASO 11: Aceptar invitación familiar en perfil del cliente
            # -------------------------------------------------------------
            print("\n>>> EJECUTANDO PASO 11: Buscando enlace de invitación y uniéndose...")
            manager.step11_accept_family_invite()
            print("[OK] Paso 11 completado.")
            
            print("\n" + "="*70)
            print("  ¡PRUEBA DE FASE V COMPLETADA CON ÉXITO!")
            print("  Se solicitó el reset, se envió la invitación, se cambió la contraseña")
            print("  y se aceptó la invitación al plan familiar en la cuenta del cliente.")
            print("="*70)
            
        except Exception as e:
            print(f"\n[ERROR EN EL TEST DE FASE V]: {e}")
            import traceback
            traceback.print_exc()
            try:
                manager.page.screenshot(path="test_fase5_error.png")
                print("  [Debug] Captura del error guardada como 'test_fase5_error.png'")
            except:
                pass
        finally:
            manager.context.close()

if __name__ == "__main__":
    test_fase_v()
