import streamlit as st
import geopandas as gpd
import folium
from streamlit_folium import st_folium
import json
from shapely.geometry import MultiPolygon, Polygon
from pathlib import Path
import hashlib
import io
import uuid
import re

st.set_page_config(page_title="Création manuelle de configuration", layout="wide")
st.title("🆕 Création manuelle — Ajout de routes avec validations et dépôt fixe")

# --- Charger le shapefile ---
shapefile_path = Path("data/lfsa000b21a_e.shp")

try:
    gdf = gpd.read_file(shapefile_path)[["CFSAUID", "geometry"]]
    gdf["CFSAUID"] = gdf["CFSAUID"].astype(str).str.upper().str.strip()
    if gdf.crs and gdf.crs.to_string().lower() != "epsg:4326":
        gdf = gdf.to_crs(epsg=4326)
    st.success("✅ Shapefile chargé et reprojeté en EPSG:4326.")
except Exception as e:
    st.error(f"❌ Erreur de chargement du shapefile : {e}")
    st.stop()

# --- Fonctions utilitaires ---
def color_from_name(name: str) -> str:
    h = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"#{h}"

def polygon_to_text(geom) -> str:
    """Convertit une géométrie en texte lon,lat,0"""
    parts = []
    if isinstance(geom, Polygon):
        coords = list(geom.exterior.coords)
        parts.append("\r\n".join([f"{x:.8f},{y:.8f},0" for x, y in coords]))
    elif isinstance(geom, MultiPolygon):
        for p in geom.geoms:
            coords = list(p.exterior.coords)
            parts.append("\r\n".join([f"{x:.8f},{y:.8f},0" for x, y in coords]))
    return "\r\n\r\n".join(parts)

# --- Paramètres généraux ---
st.subheader("⚙️ Paramètres généraux")

prefix = st.text_input("Préfixe des routes (ex: MONT)", "MONT").strip().upper()
min_route = st.number_input("Numéro minimum de route", value=1500, min_value=1)
max_route = st.number_input("Numéro maximum de route", value=1520, min_value=min_route)
depot_lat = st.number_input("Latitude du dépôt", value=45.5017, format="%.6f")
depot_lon = st.number_input("Longitude du dépôt", value=-73.5673, format="%.6f")

st.markdown("---")
st.subheader("🚗 Ajouter des routes manuellement")

# Stocker les routes
if "routes" not in st.session_state:
    st.session_state.routes = []

# Formulaire d’ajout
with st.form("add_route_form"):
    new_route_name = st.text_input("Nom de la route (ex: MONT1500)").strip().upper()
    available_fsas = sorted(gdf["CFSAUID"].unique())
    selected_fsas = st.multiselect("FSA associés :", available_fsas, key="fsas_new")
    add_button = st.form_submit_button("➕ Ajouter la route")

# --- Validation du nom ---
if add_button:
    if not new_route_name:
        st.error("🚫 Le nom de la route est obligatoire.")
    elif not re.match(rf"^{prefix}(\d+)$", new_route_name):
        st.error(f"🚫 Le nom doit commencer par '{prefix}' suivi d’un nombre (ex: {prefix}1500).")
    else:
        route_num = int(re.findall(r"\d+", new_route_name)[0])
        if not (min_route <= route_num <= max_route):
            st.error(f"🚫 Le numéro {route_num} est hors de la plage {min_route}-{max_route}.")
        elif any(r["name"] == new_route_name for r in st.session_state.routes):
            st.error(f"🚫 La route '{new_route_name}' existe déjà.")
        elif not selected_fsas:
            st.error("🚫 Tu dois sélectionner au moins un FSA.")
        else:
            st.session_state.routes.append({
                "name": new_route_name,
                "fsas": selected_fsas
            })
            st.success(f"✅ Route {new_route_name} ajoutée avec {len(selected_fsas)} FSA.")

# --- Liste des routes ajoutées ---
if st.session_state.routes:
    st.markdown("### 🧩 Routes actuellement configurées")
    for route in st.session_state.routes:
        st.markdown(f"**{route['name']}** → {len(route['fsas'])} FSA : {', '.join(route['fsas'])}")

    if st.button("🚀 Générer la configuration finale"):
        # Construire la configuration principale
        config = {
            "id": str(uuid.uuid4())[:12],
            "admissibleRoutePatterns": f"{prefix}|{min_route}|{max_route}",
            "depotLocation": {"latitude": depot_lat, "longitude": depot_lon},
            "routingParameterUiVehicleDTOs": []
        }

        # Carte principale
        m = folium.Map(location=[depot_lat, depot_lon], zoom_start=10)

        for i, route in enumerate(st.session_state.routes, start=1):
            route_name = route["name"]
            fsas = route["fsas"]

            gdf_selected = gdf[gdf["CFSAUID"].isin(fsas)]
            if gdf_selected.empty:
                st.warning(f"⚠️ Aucun FSA trouvé pour {route_name}.")
                continue

            merged_geom = gdf_selected.unary_union
            poly_text = polygon_to_text(merged_geom)
            color = color_from_name(route_name)

            # Affichage sur la carte
            folium.GeoJson(
                merged_geom,
                name=route_name,
                tooltip=f"{route_name} — {len(fsas)} FSA",
                style_function=lambda x, c=color: {"fillColor": c, "color": c, "weight": 2, "fillOpacity": 0.45}
            ).add_to(m)

            # Objet JSON
            route_obj = {
                "id": str(uuid.uuid4())[:12],
                "name": route_name,
                "adjacentRoutes": "",
                "representative": i == 1,
                "routingParameterUiVehiclePreferenceDTOs": [
                    {
                        "id": str(uuid.uuid4())[:12],
                        "zip": ",".join(fsas),
                        "routingParameterUiPolygonDTO": {
                            "id": str(uuid.uuid4())[:12],
                            "name": f"{route_name}_ZONE",
                            "polygonCoordinates": poly_text
                        },
                        "value": 1.0,
                        "orderRank": 1
                    }
                ]
            }
            config["routingParameterUiVehicleDTOs"].append(route_obj)

        # Ajout du dépôt sur la carte
        folium.Marker(
            [depot_lat, depot_lon],
            popup="📦 Dépôt principal",
            tooltip="Dépôt d'origine",
            icon=folium.Icon(color="red", icon="home", prefix="fa")
        ).add_to(m)

        # Résumé carte + JSON
        st.subheader("🗺️ Carte générée")
        st_folium(m, width=950, height=600)

        st.subheader("🧾 JSON généré")
        st.json(config)

        # Téléchargement
        buffer = io.BytesIO()
        json_bytes = json.dumps(config, indent=4, ensure_ascii=False).encode("utf-8")
        buffer.write(json_bytes)
        buffer.seek(0)
        st.download_button(
            label="📥 Télécharger la configuration complète",
            data=buffer,
            file_name=f"{prefix}_CONFIG_MANUEL.json",
            mime="application/json"
        )

    # Réinitialisation
    if st.button("🗑️ Réinitialiser toutes les routes"):
        st.session_state.routes = []
        st.experimental_rerun()
else:
    st.info("🧭 Aucune route ajoutée. Utilise le formulaire ci-dessus pour commencer.")
