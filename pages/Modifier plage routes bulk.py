import streamlit as st
import json
import re
from pathlib import Path
import io
import datetime
from io import StringIO
import os

# --- Configuration ---
st.set_page_config(page_title="Modifier la plage de routes", layout="wide")
st.title("🧭 Mise à jour cohérente des routes, adjacences et labels")

# --- File uploader pour le JSON ---
uploaded_file = st.file_uploader("📂 Charger le fichier JSON de configuration", type=["json"])

if not uploaded_file:
    st.info("Veuillez charger un fichier JSON pour commencer.")
    st.stop()

# --- Lecture du JSON uploadé ---
try:
    data = json.load(StringIO(uploaded_file.getvalue().decode("utf-8")))
    st.success(f"✅ Fichier JSON chargé avec succès : {uploaded_file.name}")
except Exception as e:
    st.error(f"❌ Erreur de lecture du JSON : {e}")
    st.stop()

# --- Définir un chemin temporaire pour la sauvegarde ---
json_path = Path(uploaded_file.name)

# --- Extraire la plage actuelle ---
current_pattern = data.get("admissibleRoutePatterns", "")
match = re.match(r"([A-Z]+)\|(\d+)\|(\d+)", current_pattern)
if not match:
    st.error("❌ Format invalide pour 'admissibleRoutePatterns'. Exemple attendu : MONT|1500|1999")
    st.stop()

prefix, min_route, max_route = match.groups()
min_route, max_route = int(min_route), int(max_route)
st.info(f"### Plage actuelle : {prefix}{min_route} → {prefix}{max_route}")

# --- Nouvelle plage ---
st.markdown("### ✏️ Nouvelle plage de routes")
col1, col2 = st.columns(2)
with col1:
    new_min = st.number_input("Nouveau minimum", min_value=0, value=min_route, step=1)
with col2:
    new_max = st.number_input("Nouveau maximum", min_value=0, value=max_route, step=1)

# --- Route number gap (écart) ---
route_gap = data.get("routeNumberGap", 1)
st.info(f"ℹ️ Espace entre les routes actuel (routeNumberGap) : {route_gap}")

# --- Vérification dynamique ---
valid_range = True
if new_min >= new_max:
    st.error("🚫 Le minimum doit être strictement inférieur au maximum.")
    valid_range = False
elif new_min < 0 or new_max < 0:
    st.error("🚫 Les valeurs doivent être positives.")
    valid_range = False
else:
    st.success(f"✅ Nouvelle plage valide : {prefix}{new_min} → {prefix}{new_max}")

# --- Option de mise à jour complète ---
st.markdown("### ⚙️ Options de mise à jour")
st.caption("Les `name`, `adjacentRoutes`, `representative`, et `labels` seront mis à jour en cohérence.")
adjust = st.checkbox("🔁 Mettre à jour aussi les `adjacentRoutes`, `representative` et `labels`", value=True)

# --- Application ---
if st.button("💾 Appliquer les changements"):
    if not valid_range:
        st.error("❌ Impossible d’appliquer les changements : plage invalide.")
        st.stop()

    log_entries = []  # journalisation des changements

    # Étape 1 — Mettre à jour la plage
    data["admissibleRoutePatterns"] = f"{prefix}|{new_min}|{new_max}"
    log_entries.append(f"[INFO] Plage mise à jour : {prefix}{new_min} → {prefix}{new_max}")

    # Étape 2 — Calcul correspondance old_name → new_name avec gap constant
    name_mapping = {}
    current_num = new_min

    for route in data.get("routingParameterUiVehicleDTOs", []):
        old_name = route.get("name", "")
        if not re.match(rf"{prefix}\d+", old_name):
            continue

        new_name = f"{prefix}{current_num}"
        name_mapping[old_name] = new_name
        log_entries.append(f"[RENAME] {old_name} → {new_name}")
        current_num += route_gap  # progression régulière

    # Étape 3 — Renommer les routes
    updated_routes = []
    for route in data.get("routingParameterUiVehicleDTOs", []):
        old_name = route.get("name", "")
        if old_name in name_mapping:
            new_name = name_mapping[old_name]
            if new_name != old_name:
                route["name"] = new_name
                updated_routes.append((old_name, new_name))
                log_entries.append(f"[UPDATED_ROUTE] Route renommée : {old_name} → {new_name}")

    # Étape 4 — Adapter adjacentRoutes et representative
    if adjust:
        for route in data.get("routingParameterUiVehicleDTOs", []):
            if route.get("adjacentRoutes"):
                old_adj = route["adjacentRoutes"]
                adj_list = re.split(r"[;,]", old_adj)
                adj_list = [a.strip() for a in adj_list if a.strip()]
                new_adj_list = [name_mapping.get(a, a) for a in adj_list]
                route["adjacentRoutes"] = ",".join(new_adj_list)
                log_entries.append(f"[ADJ] {route['name']} : {old_adj} → {route['adjacentRoutes']}")

            if route.get("representative"):
                old_rep = route["representative"].strip()
                if old_rep in name_mapping:
                    route["representative"] = name_mapping[old_rep]
                    log_entries.append(f"[REP] {route['name']} : representative {old_rep} → {route['representative']}")

    # Étape 5 — Mettre à jour labels dans routingParameterNodeDTOs
    if adjust and "routingParameterNodeDTOs" in data:
        for node in data["routingParameterNodeDTOs"]:
            label = node.get("label", "")
            if isinstance(label, str) and label:
                parts = re.split(r"[;,]", label)
                updated = [name_mapping.get(p.strip(), p.strip()) for p in parts]
                node["label"] = ",".join(updated)
                log_entries.append(f"[NODE] Label mis à jour : {label} → {node['label']}")

    # Étape 6 — Validation du JSON avant sauvegarde
    try:
        buffer = io.StringIO()
        json.dump(data, buffer, indent=4, ensure_ascii=False)
        buffer.seek(0)
        json.loads(buffer.getvalue())
        st.success("✅ Structure JSON valide avant sauvegarde.")
    except json.JSONDecodeError as e:
        st.error(f"❌ Structure JSON invalide : {e}")
        st.stop()

    # Étape 7 — Vérification de cohérence post-sauvegarde
    all_names = {r["name"] for r in data.get("routingParameterUiVehicleDTOs", [])}
    broken_refs = set()

    for route in data.get("routingParameterUiVehicleDTOs", []):
        for field in ("adjacentRoutes", "representative"):
            if route.get(field):
                for ref in re.split(r"[;,]", route[field]):
                    ref = ref.strip()
                    if ref and ref not in all_names:
                        broken_refs.add(ref)

    if "routingParameterNodeDTOs" in data:
        for node in data["routingParameterNodeDTOs"]:
            for label in re.split(r"[;,]", node.get("label", "")):
                label = label.strip()
                if label and label not in all_names:
                    broken_refs.add(label)

    if broken_refs:
        st.warning(f"⚠️ Références non résolues après mise à jour : {', '.join(sorted(broken_refs))}")
        log_entries.append(f"[WARN] Références non résolues : {', '.join(sorted(broken_refs))}")
    else:
        st.success("✅ Vérification finale : toutes les références sont cohérentes.")
        log_entries.append("[OK] Vérification finale : toutes les références cohérentes.")

    # Étape 8 — Résumé et téléchargement
    if updated_routes:
        st.markdown("### 🔁 Routes renommées :")
        for old, new in updated_routes:
            st.write(f"- {old} → {new}")

    # --- Étape 9 : Sauvegarde des logs dans /logs ---
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_filename = logs_dir / f"CHANGE_LOG_{timestamp}.txt"

    with open(log_filename, "w", encoding="utf-8") as log_file:
        log_file.write("\n".join(log_entries))

    st.success(f"📁 Fichier de log enregistré dans : `{log_filename}`")

    st.download_button(
        label="📜 Télécharger le fichier de log",
        data="\n".join(log_entries),
        file_name=f"CHANGE_LOG_{timestamp}.txt",
        mime="text/plain"
    )

    st.markdown("---")
    st.download_button(
        label="⬇️ Télécharger le fichier JSON modifié",
        data=buffer.getvalue(),
        file_name=f"UPDATED_{timestamp}_{uploaded_file.name}",
        mime="application/json",
    )
