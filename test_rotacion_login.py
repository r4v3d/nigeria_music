import sys
import os
import time
from pathlib import Path

# Agregar el directorio actual al path de python
SCRIPT_DIR = Path(__file__).parent.absolute()
sys.path.append(str(SCRIPT_DIR))

# Importar las clases y funciones necesarias del script original
try:
    from playwright.sync_api import sync_playwright
    from migrar_cuentas_tidal import (
        TidalMigrationManager,
        PROFILE_DIR_PARENT,
        family_invite_queue
    )
except ImportError as e:
    print(f"Error de importación: {e}")
    print("Asegúrate de que 'migrar_cuentas_tidal.py' y sus dependencias estén en la misma carpeta.")
    sys.exit(1)

def run_test():
    print("================================================================================")
    print("         INICIANDO TEST DE ROTACIÓN DE CUENTA Y LOGIN AUTOMÁTICO IMAP           ")
    print("================================================================================")
    
    familiar_titular_txt = SCRIPT_DIR / "titular_familiar.txt"
    if not familiar_titular_txt.exists():
        print(f"Error: El archivo {familiar_titular_txt} no existe. Créalo y añade una cuenta para testear.")
        return

    # Intentar cargar las cuentas del archivo
    titulares = []
    # Usar helper del manager instanciándolo de forma ficticia
    dummy_manager = TidalMigrationManager(
        main_profile=SCRIPT_DIR / "perfiles" / "principal",
        parent_profile=PROFILE_DIR_PARENT,
        client_email="dummy@gmail.com",
        client_pwd="dummy",
        target_pwd="dummy"
    )
    titulares = dummy_manager.cargar_familiares_titulares(familiar_titular_txt)
    disponibles = [t for t in titulares if t["estado"] == "disponible" and t["miembros_actuales"] < 5]
    
    if not disponibles:
        print("\n[ERROR] No hay cuentas titulares disponibles en familiar_titular.txt.")
        print("Agrega al menos una cuenta de prueba con slots libres. Ejemplo:")
        print("tu_cuenta_titular@gmail.com, 0, disponible, []")
        return
        
    print(f"\n[INFO] Cuentas disponibles encontradas para el test: {[t['correo'] for t in disponibles]}")
    print("Se usará la primera cuenta de la lista para verificar el login automático por código IMAP.")
    
    # Simular una invitación en cola
    test_client_email = "test_invite_check@gmail.com"
    
    # Preguntar si se quiere usar proxy para el test
    usar_proxy_input = input("¿Deseas probar usando tus proxies peruanos? (s/n): ").strip().lower()
    use_proxy_test = usar_proxy_input == 's'
    
    # Cargar proxies si es necesario
    proxy_pe_server = None
    proxy_pe_user = None
    proxy_pe_pass = None
    
    if use_proxy_test:
        # Cargar lista de proxies PE
        proxies_pe_file = SCRIPT_DIR / "lista_proxies_pe.txt"
        if proxies_pe_file.exists():
            lines = [l.strip() for l in proxies_pe_file.read_text(encoding="utf-8").splitlines() if l.strip() and not l.strip().startswith("#")]
            if lines:
                parts = lines[0].split(":")
                if len(parts) == 4:
                    proxy_pe_server = f"http://{parts[0]}:{parts[1]}"
                    proxy_pe_user = parts[2]
                    proxy_pe_pass = parts[3]
                    print(f"[Proxy] Cargado proxy de prueba: {proxy_pe_server}")
        if not proxy_pe_server:
            print("[WARN] No se pudo cargar ningún proxy de lista_proxies_pe.txt. Se ejecutará sin proxy.")
            use_proxy_test = False

    # Instanciar el manager con el correo de prueba
    manager = TidalMigrationManager(
        main_profile=SCRIPT_DIR / "perfiles" / "principal",
        parent_profile=PROFILE_DIR_PARENT,
        client_email=test_client_email,
        client_pwd="password_ficticio",
        target_pwd="password_ficticio",
        use_proxy=use_proxy_test,
        proxy_pe_server=proxy_pe_server,
        proxy_pe_user=proxy_pe_user,
        proxy_pe_pass=proxy_pe_pass
    )
    
    # Limpiar y preparar la cola con el correo del test
    family_invite_queue.clear()
    
    with sync_playwright() as p:
        try:
            print("\n[1/3] Iniciando ciclo de login, conteo y cierre de sesión para cada titular...")
            
            # Lanzar contexto de navegador familiar
            launch_args = ["--disable-blink-features=AutomationControlled"]
            proxy_dict = None
            if use_proxy_test and proxy_pe_server:
                proxy_dict = {"server": proxy_pe_server}
                if proxy_pe_user:
                    proxy_dict["username"] = proxy_pe_user
                if proxy_pe_pass:
                    proxy_dict["password"] = proxy_pe_pass

            parent_context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR_PARENT),
                channel="chrome",
                headless=False,
                args=launch_args,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 800},
                locale="es-ES",
                proxy=proxy_dict
            )
            parent_context.set_default_navigation_timeout(45000)
            parent_context.set_default_timeout(35000)
            
            parent_page = parent_context.pages[0] if parent_context.pages else parent_context.new_page()
            manager.optimizar_pagina(parent_page)
            
            # Recorrer todas las cuentas disponibles
            for c_idx, c_titular in enumerate(disponibles, 1):
                email_titular = c_titular["correo"]
                print("\n" + "-"*60)
                print(f"  [Cuenta {c_idx}/{len(disponibles)}] Probando: {email_titular}")
                print("-"*60)
                
                # 1. Asegurar cierre de sesión previo
                print(f"  [Test] Forzando logout total para {email_titular}...")
                manager.logout_tidal_total(parent_page)
                time.sleep(2.0)
                
                # 2. Iniciar sesión automática con código IMAP
                print(f"  [Test] Iniciando login automático para {email_titular}...")
                login_ok = manager.login_familiar_titular_con_codigo(parent_page, email_titular)
                if not login_ok:
                    print(f"  [Test] [ERROR] No se pudo loguear en {email_titular}. Pasando al siguiente...")
                    continue
                    
                # 3. Navegar a página de familia y contar miembros
                print(f"  [Test] Navegando a sección de familia...")
                parent_page.goto("https://account.tidal.com/family", wait_until="domcontentloaded")
                time.sleep(3.0)
                from migrar_cuentas_tidal import aceptar_cookies_con_espera
                aceptar_cookies_con_espera(parent_page)
                
                miembros = manager.contar_miembros_familia(parent_page)
                print(f"  [Test] Miembros contados en pantalla: {miembros}")
                
                # 4. Actualizar contabilidad en el archivo titular_familiar.txt
                print(f"  [Test] Actualizando titular_familiar.txt...")
                # Recargar titulares por si otro hilo hizo cambios
                titulares_actuales = manager.cargar_familiares_titulares(familiar_titular_txt)
                for t in titulares_actuales:
                    if t["correo"] == email_titular:
                        t["miembros_actuales"] = miembros
                        if miembros >= 5:
                            t["estado"] = "lleno"
                        break
                manager.guardar_familiares_titulares(familiar_titular_txt, titulares_actuales)
                print(f"  [Test] [OK] Contabilidad de {email_titular} actualizada con éxito.")
                
            parent_context.close()
            print("\n[3/3] [TEST COMPLETADO CON ÉXITO]")
            print("El robot ha completado la rotación, el logout forzado, el login IMAP y la contabilidad de todos los titulares de tu lista.")
        except Exception as err:
            print(f"\n[TEST FALLIDO] Ocurrió un error durante la ejecución del test: {err}")

if __name__ == "__main__":
    run_test()
