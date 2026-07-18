import re

with open("tmm_body.html", "r", encoding="utf-8") as f:
    html = f.read()

# Buscar "Archivo"
matches = list(re.finditer(r"Archivo", html, re.I))
print(f"Encontrados {len(matches)} coincidencias de 'Archivo'")
for idx, match in enumerate(matches[:10]):
    pos = match.start()
    print(f"\n[{idx}] Coincidencia 'Archivo':")
    print(html[max(0, pos-150):pos+250])
