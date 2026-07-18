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
    DOWNLOADS_DIR,
    STEALTH_SCRIPT
)

def test_flujo_eliminacion_creacion():
    print("="*70)
    print("  TEST DE SUBPROCESO: ELIMINACIÓN Y CREACIÓN DE CUENTA TIDAL")
    print("  (Aísla Pasos 1, 2, 5 y 6 para verificar su correcto funcionamiento)")
    print("="*70)
    
    # Pedir credenciales para el test
    client_email = input("\nIntroduce el correo del cliente: ").strip()
    client_pwd = input("Introduce la contraseña actual del cliente: ").strip()
    target_pwd = input("Introduce la contraseña nueva definitiva: ").strip()
    
    if not client_email or not client_pwd or not target_pwd:
        print("\n[ERROR] Todos los campos son obligatorios para realizar el test.")
        return
        
    print("\n[INFO] Iniciando navegador...")
    
    # Inicializar el manager con las rutas de perfiles existentes
    manager = TidalMigrationManager(
        main_profile=PROFILE_DIR_MAIN,
        parent_profile=PROFILE_DIR_PARENT,
        client_email=client_email,
        client_pwd=client_pwd,
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
        
        # Inyectar sigilo (stealth) para evitar detección de robots de CloudFront
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
            # -------------------------------------------------------------
            # PASO 1: Iniciar sesión
            # -------------------------------------------------------------
            print("\n>>> EJECUTANDO PASO 1: Login en cuenta actual del cliente...")
            manager.step1_login_tidal()
            print("[OK] Paso 1 completado.")
            time.sleep(2.0)
            
            # -------------------------------------------------------------
            # PASO 2: Cambiar correo a cakeseller con puntos
            # -------------------------------------------------------------
            print("\n>>> EJECUTANDO PASO 2: Cambiando correo a cakeseller con puntos...")
            manager.step2_change_email()
            print(f"[OK] Paso 2 completado. Correo temporal: {manager.new_email_temp}")
            time.sleep(2.0)
            
            # -------------------------------------------------------------
            # PASO 5: Eliminar cuenta
            # -------------------------------------------------------------
            print("\n>>> EJECUTANDO PASO 5: Iniciando proceso de eliminación...")
            manager.step5_delete_account()
            print("[OK] Paso 5 completado. Cuenta eliminada con éxito.")
            time.sleep(3.0)
            
            # -------------------------------------------------------------
            # PASO 6: Crear nueva cuenta con VPN Nigeria
            # -------------------------------------------------------------
            print("\n" + "=" * 70)
            print("  ATENCIÓN: ACTIVA TU VPN EN NIGERIA AHORA")
            print("  Asegúrate de que la VPN esté activa antes de proceder a la creación.")
            print("=" * 70)
            input(">>> Presiona Enter cuando la VPN a Nigeria esté ACTIVA <<<")
            
            print("\n>>> EJECUTANDO PASO 6: Creación de la nueva cuenta...")
            manager.step6_create_account()
            print("[OK] Paso 6 completado. Cuenta creada exitosamente.")
            
            print("\n" + "="*70)
            print("  ¡PRUEBA EXITOSA!")
            print(f"  La cuenta {client_email} fue eliminada y recreada correctamente.")
            print("="*70)
            
        except Exception as e:
            print(f"\n[ERROR EN EL TEST]: {e}")
            import traceback
            traceback.print_exc()
            try:
                manager.page.screenshot(path="test_error_deletion_creation.png")
                print("  [Debug] Captura del error guardada como 'test_error_deletion_creation.png'")
            except:
                pass
        finally:
            manager.context.close()

if __name__ == "__main__":
    test_flujo_eliminacion_creacion()
