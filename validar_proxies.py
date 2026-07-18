import os
import sys
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

def parse_proxy_line(line_clean):
    # Remover prefijo http:// o https:// si está presente
    raw_line = line_clean
    if raw_line.lower().startswith("http://"):
        raw_line = raw_line[7:]
    elif raw_line.lower().startswith("https://"):
        raw_line = raw_line[8:]
        
    server, username, password = None, None, None
    
    # Formato 1: host;port;user;pass
    if ";" in raw_line:
        parts = raw_line.split(";")
        if len(parts) >= 4:
            host = parts[0].strip()
            port = parts[1].strip()
            username = parts[2].strip()
            password = parts[3].strip()
            server = f"http://{host}:{port}"
        elif len(parts) == 2:
            host = parts[0].strip()
            port = parts[1].strip()
            server = f"http://{host}:{port}"
            
    # Formato 2: host:port:user:pass (sin @)
    elif raw_line.count(":") >= 3 and "@" not in raw_line:
        parts = raw_line.split(":")
        host = parts[0].strip()
        port = parts[1].strip()
        username = parts[2].strip()
        password = parts[3].strip()
        server = f"http://{host}:{port}"
        
    # Formato 3: user:pass@host:port (estándar)
    elif "@" in raw_line:
        part_user_pass, part_host_port = raw_line.split("@", 1)
        server = f"http://{part_host_port.strip()}"
        if ":" in part_user_pass:
            username, password = part_user_pass.split(":", 1)
            username = username.strip()
            password = password.strip()
            
    # Formato 4: host:port (sin autenticación)
    elif ":" in raw_line:
        parts = raw_line.split(":")
        if len(parts) == 2:
            host = parts[0].strip()
            port = parts[1].strip()
            server = f"http://{host}:{port}"
            
    if server:
        if password and password.endswith("@"):
            password = password[:-1].strip()
        return {
            "server": server,
            "username": username,
            "password": password,
            "raw": line_clean
        }
    return None

def test_single_proxy(proxy_entry):
    import requests
    server = proxy_entry["server"]
    username = proxy_entry["username"]
    password = proxy_entry["password"]
    
    formatted_proxy = {
        "http": f"http://{username}:{password}@{server.replace('http://', '')}" if username else server,
        "https": f"http://{username}:{password}@{server.replace('http://', '')}" if username else server,
    }
    
    start_time = time.time()
    try:
        # Petición rápida para validar velocidad, conexión y credenciales
        r = requests.get("https://httpbin.org/ip", proxies=formatted_proxy, timeout=4)
        latency = time.time() - start_time
        if r.status_code == 200:
            origin_ip = r.json().get("origin")
            return {
                "success": True,
                "proxy": proxy_entry,
                "latency": latency,
                "ip": origin_ip
            }
    except Exception:
        pass
    return {
        "success": False,
        "proxy": proxy_entry
    }

def main():
    print("=" * 70)
    print("    VALIDADOR CONCURRENTE DE PROXIES RESIDENCIALES (TIDAL)")
    print("=" * 70)
    
    # Preguntar región a validar
    region = input("¿Qué proxies deseas validar? (pe = Perú / ng = Nigeria, por defecto 'pe'): ").strip().lower()
    if region not in ("pe", "ng", "peru", "nigeria"):
        region = "pe"
        
    target_key = "pe" if region in ("pe", "peru") else "ng"
    filename = "lista_proxies_pe.txt" if target_key == "pe" else "lista_proxies_ng.txt"
    
    if not os.path.exists(filename):
        # Crear archivo vacío para guiar al usuario
        with open(filename, "w", encoding="utf-8") as f:
            f.write("# Pega aquí tu lista cruda de proxies para " + ("PERÚ" if target_key == "pe" else "NIGERIA") + "\n")
            f.write("# Formatos soportados: host;port;user;pass  O  user:pass@host:port  O  host:port:user:pass\n")
        print(f"\n[AVISO] Se ha creado el archivo '{filename}'.")
        print(f"Por favor, abre '{filename}', pega tu lista de proxies, guárdalo y vuelve a ejecutar este validador.")
        input("\nPresiona Enter para salir...")
        return
        
    # Leer proxies del archivo
    proxies_a_probar = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            line_clean = line.strip()
            if not line_clean or line_clean.startswith("#") or line_clean.startswith("["):
                continue
            parsed = parse_proxy_line(line_clean)
            if parsed:
                proxies_a_probar.append(parsed)
                
    total_proxies = len(proxies_a_probar)
    print(f"\nSe detectaron {total_proxies} proxies en '{filename}'.")
    if total_proxies == 0:
        print("No se encontraron proxies válidos para probar en el archivo. Pega tu lista de IPs primero.")
        input("\nPresiona Enter para salir...")
        return
        
    # Determinar número óptimo de hilos
    hilos = min(150, max(20, total_proxies // 4))
    print(f"Probando proxies en paralelo usando {hilos} hilos... Espera un momento.")
    
    validos = []
    procesados = 0
    
    with ThreadPoolExecutor(max_workers=hilos) as executor:
        futures = {executor.submit(test_single_proxy, p): p for p in proxies_a_probar}
        
        for future in as_completed(futures):
            procesados += 1
            res = future.result()
            if res["success"]:
                validos.append(res)
            
            # Mostrar progreso dinámico en la misma línea
            sys.stdout.write(f"\rProgreso: {procesados}/{total_proxies} | Funcionan: {len(validos)}")
            sys.stdout.flush()
            
    print(f"\n\nPrueba finalizada.")
    print(f"Total analizados: {total_proxies}")
    print(f"Funcionan correctamente: {len(validos)}")
    print(f"Fallaron (sin conexión/auth incorrecto): {total_proxies - len(validos)}")
    
    if len(validos) == 0:
        print("\n[WARN] Ninguno de los proxies probados funcionó. No se actualizó 'proxies.txt'.")
        input("\nPresiona Enter para salir...")
        return
        
    # Ordenar por menor latencia (más rápido primero)
    validos.sort(key=lambda x: x["latency"])
    
    # Cargar la configuración actual de proxies.txt para mantener la otra región intacta
    proxies_existentes = {"pe": [], "ng": []}
    if os.path.exists("proxies.txt"):
        try:
            seccion_actual = None
            with open("proxies.txt", "r", encoding="utf-8") as f:
                for line in f:
                    line_clean = line.strip()
                    if line_clean.upper() in ("[PROXIES_PE]", "[PROXIES_PERU]"):
                        seccion_actual = "pe"
                        continue
                    elif line_clean.upper() in ("[PROXIES_NG]", "[PROXIES_NIGERIA]"):
                        seccion_actual = "ng"
                        continue
                    if seccion_actual and line_clean and not line_clean.startswith("#") and not "=" in line_clean:
                        proxies_existentes[seccion_actual].append(line_clean)
        except Exception:
            pass
            
    # Actualizar la sección objetivo con los proxies válidos y rápidos en formato limpio @
    lineas_limpias = []
    for item in validos:
        p = item["proxy"]
        host_port = p["server"].replace("http://", "").replace("https://", "")
        if p["username"] and p["password"]:
            lineas_limpias.append(f"{p['username']}:{p['password']}@{host_port}")
        else:
            lineas_limpias.append(host_port)
            
    proxies_existentes[target_key] = lineas_limpias
    
    # Escribir el nuevo proxies.txt ordenado
    try:
        with open("proxies.txt", "w", encoding="utf-8") as f:
            f.write("# ======================================================================\n")
            f.write("# CONFIGURACIÓN DE PROXIES VALIDADOS Y OPTIMIZADOS (ORDENADOS POR VELOCIDAD)\n")
            f.write("# Generado automáticamente por validar_proxies.py\n")
            f.write("# ======================================================================\n\n")
            
            f.write("[PROXIES_PE]\n")
            for item in proxies_existentes["pe"]:
                f.write(f"{item}\n")
            f.write("\n")
            
            f.write("[PROXIES_NG]\n")
            for item in proxies_existentes["ng"]:
                f.write(f"{item}\n")
            f.write("\n")
            
        print(f"\n[OK] ¡Archivo 'proxies.txt' actualizado con éxito!")
        print(f"Se guardaron {len(lineas_limpias)} proxies válidos para {'PERÚ' if target_key == 'pe' else 'NIGERIA'}, ordenados de más rápido a más lento.")
    except Exception as e:
        print(f"\n[ERROR] No se pudo escribir en 'proxies.txt': {e}")
        
    input("\nPresiona Enter para finalizar...")

if __name__ == "__main__":
    main()
