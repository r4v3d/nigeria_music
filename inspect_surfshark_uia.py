import os
import sys
import time
from pywinauto import Application

def get_surfshark_pids():
    pids = []
    try:
        with os.popen('tasklist /FI "IMAGENAME eq Surfshark.exe" /NH') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == 'surfshark.exe':
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
    except Exception:
        pass
    return pids

def inspect_uia():
    print("=== INSPECTOR UIA DE SURFSHARK (CORREGIDO) ===")
    pids = get_surfshark_pids()
    if not pids:
        print("Surfshark no está corriendo. Por favor ábrelo primero.")
        return
        
    pid = pids[0]
    print(f"Conectando al PID {pid}...")
    try:
        app = Application(backend="uia").connect(process=pid, timeout=10)
        wnd = app.window(title_re=".*Surfshark.*", visible_only=False)
        wnd.set_focus()
        print("Enfocado con éxito. Esperando 1 segundo...")
        time.sleep(1.0)
        
        print("Mapeando elementos de la interfaz de usuario...")
        desc = wnd.descendants()
        print(f"Total elementos encontrados: {len(desc)}")
        
        output_file = "surfshark_elements.txt"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"Surfshark UI Element Map (PID {pid})\n")
            f.write("="*80 + "\n")
            for i, e in enumerate(desc, 1):
                try:
                    # En pywinauto UIA, la info de tipo está en element_info
                    info = e.element_info
                    ctype = info.control_type or "Unknown"
                    name = info.name or ""
                    # Evitar llamadas directas si no se está seguro de los atributos
                    try:
                        text = e.window_text() or ""
                    except Exception:
                        text = ""
                    try:
                        rect = e.rectangle()
                    except Exception:
                        rect = "NoRect"
                    try:
                        class_name = info.class_name or ""
                    except Exception:
                        class_name = ""
                        
                    f.write(f"{i:03d}. Type: {ctype:<12} | Class: {class_name:<20} | Name: {repr(name):<35} | Text: {repr(text):<35} | Rect: {rect}\n")
                except Exception as ex:
                    f.write(f"{i:03d}. Error al mapear elemento: {ex}\n")
                    
        print(f"✅ Mapeo completado con éxito. Guardado en: {output_file}")
    except Exception as e:
        print(f"❌ Error al conectar o mapear: {e}")

if __name__ == "__main__":
    inspect_uia()
