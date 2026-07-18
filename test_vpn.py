import time
import sys
from migrar_cuentas_tidal import vpn_surfshark_conectar, vpn_surfshark_desconectar

def test_flujo_vpn():
    print("=== INICIANDO TEST DE AUTOMATIZACIÓN DE VPN SURFSHARK ===")
    
    # 1. Test de Conexión a Nigeria
    print("\n[TEST 1] Conectando a la IP de Nigeria...")
    conectar_ok = vpn_surfshark_conectar("nigeria")
    if conectar_ok:
        print("[OK] Conexión completada exitosamente en el test.")
    else:
        print("[ERROR] Falló la conexión automática. Verifique que Surfshark esté instalado y configurado correctamente.")
        
    print("\nEsperando 10 segundos con la VPN activa...")
    time.sleep(10.0)
    
    # 2. Test de Desconexión
    print("\n[TEST 2] Desconectando la VPN...")
    desconectar_ok = vpn_surfshark_desconectar()
    if desconectar_ok:
        print("[OK] Desconexión completada exitosamente en el test.")
    else:
        print("[ERROR] Falló la desconexión automática.")
        
    print("\n=== TEST DE VPN FINALIZADO ===")

if __name__ == "__main__":
    test_flujo_vpn()
