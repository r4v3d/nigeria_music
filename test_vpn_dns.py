# -*- coding: utf-8 -*-
"""
Test: Verifica que el flujo VPN Nigeria + navegacion a Tidal funciona
correctamente tras las correcciones de DNS.
"""
import time
import subprocess
import sys
import os

# Forzar UTF-8 en stdout para evitar errores de codificacion
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from migrar_cuentas_tidal import vpn_surfshark_conectar, vpn_surfshark_desconectar

def test_vpn_y_navegacion():
    print("=" * 70)
    print("  TEST: VPN Nigeria + Navegacion a Tidal (con fix DNS)")
    print("=" * 70)

    # --- PASO 1: Conectar VPN ---
    print("\n[1/6] Conectando VPN a Nigeria...")
    if not vpn_surfshark_conectar("nigeria"):
        print("  [WARN] No se pudo conectar automaticamente.")
        print("  Por favor, conecta la VPN a Nigeria manualmente.")
        input(">>> Presiona Enter cuando la VPN este ACTIVA <<<")
    else:
        print("  [OK] VPN conectada.")

    # --- PASO 2: Flush DNS + espera (FIX APLICADO) ---
    print("\n[2/6] Limpiando cache DNS del sistema...")
    result = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, text=True)
    print(f"  {result.stdout.strip()}")
    print("  Esperando 8 segundos para estabilizacion de red...")
    time.sleep(8.0)
    print("  [OK] Espera completada.")

    # --- PASO 3: Verificar DNS desde Python ---
    print("\n[3/6] Verificando resolucion DNS desde Python...")
    import socket
    dominios = ["tidal.com", "account.tidal.com", "login.tidal.com", "www.google.com"]
    dns_ok = True
    for dominio in dominios:
        try:
            ip = socket.gethostbyname(dominio)
            print(f"  [OK] {dominio} -> {ip}")
        except socket.gaierror as e:
            print(f"  [FALLO] {dominio} -> {e}")
            dns_ok = False

    if not dns_ok:
        print("\n  [WARN] Algunos dominios no resuelven desde Python.")
        print("  Intentando flush DNS adicional...")
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True)
        time.sleep(5.0)
        for dominio in dominios:
            try:
                ip = socket.gethostbyname(dominio)
                print(f"  [OK] {dominio} -> {ip} (tras segundo flush)")
            except socket.gaierror as e:
                print(f"  [FALLO] {dominio} -> {e} (sigue fallando)")
        print("  [INFO] Continuando con test de Playwright (Chrome usa su propio resolver DNS)...")

    # --- PASO 4: Lanzar Chrome con Playwright ---
    print("\n[4/6] Lanzando Chrome con Playwright (flags de fix DNS)...")
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        import tempfile
        temp_profile = tempfile.mkdtemp(prefix="test_vpn_dns_")
        
        launch_args = ["--disable-blink-features=AutomationControlled", "--dns-prefetch-disable"]
        
        context = p.chromium.launch_persistent_context(
            user_data_dir=temp_profile,
            channel="chrome",
            headless=False,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 800},
            locale="es-ES"
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.bring_to_front()

        # --- PASO 5: Navegar con reintentos ---
        print("\n[5/6] Navegando a tidal.com/pricing y account.tidal.com...")
        
        max_intentos = 3
        exito = False
        
        for intento in range(1, max_intentos + 1):
            print(f"\n  --- Intento {intento}/{max_intentos} ---")
            
            try:
                print("  Cargando https://tidal.com/pricing ...")
                page.goto("https://tidal.com/pricing", wait_until="domcontentloaded", timeout=30000)
                time.sleep(3.0)
                print(f"  URL actual: {page.url}")
                print(f"  Titulo: {page.title()}")
            except Exception as e:
                print(f"  [ERROR] Error cargando pricing: {e}")

            try:
                print("  Cargando https://account.tidal.com/ ...")
                page.goto("https://account.tidal.com/", wait_until="domcontentloaded", timeout=30000,
                          referer="https://tidal.com/pricing")
                time.sleep(3.0)
                print(f"  URL actual: {page.url}")
                print(f"  Titulo: {page.title()}")
            except Exception as e:
                print(f"  [ERROR] Error cargando account: {e}")

            # Verificar contenido
            try:
                content = page.content()
                if "ERR_NAME_NOT_RESOLVED" in content:
                    raise Exception("ERR_NAME_NOT_RESOLVED detectado en la pagina")
                if "ERR_CONNECTION" in content:
                    raise Exception("ERR_CONNECTION detectado en la pagina")
                if "No se puede acceder" in content:
                    raise Exception("Pagina de error de Chrome detectada")
                
                print("\n  [OK] PAGINA CARGO CORRECTAMENTE - No hay errores de DNS/red")
                exito = True
                break
            except Exception as err:
                print(f"\n  [FALLO] {err}")
                if intento < max_intentos:
                    print("  Flushing DNS y reintentando en 5s...")
                    subprocess.run(["ipconfig", "/flushdns"], capture_output=True)
                    time.sleep(5.0)
        
        # Captura de pantalla
        script_dir = os.path.dirname(os.path.abspath(__file__))
        screenshot_path = os.path.join(script_dir, "test_vpn_dns_result.png")
        page.screenshot(path=screenshot_path)
        print(f"\n  Captura guardada: {screenshot_path}")

        context.close()
        
        import shutil
        try:
            shutil.rmtree(temp_profile, ignore_errors=True)
        except Exception:
            pass

    # --- PASO 6: Desconectar VPN ---
    print("\n[6/6] Desconectando VPN...")
    vpn_surfshark_desconectar()

    # --- RESULTADO ---
    print("\n" + "=" * 70)
    if exito:
        print("  [RESULTADO] TEST PASO: Navegacion a Tidal funciona con VPN Nigeria")
        print("  Las correcciones de DNS estan funcionando correctamente.")
    else:
        print("  [RESULTADO] TEST FALLO: La pagina no cargo tras todos los intentos.")
        print("  Puede ser bloqueo de Tidal a la IP de VPN (403/Cloudflare)")
        print("  o un problema persistente de DNS en esta red.")
    print("=" * 70)
    return exito


if __name__ == "__main__":
    ok = test_vpn_y_navegacion()
    sys.exit(0 if ok else 1)
