import streamlit as st
from streamlit_folium import st_folium
import folium
import json
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon
from pathlib import Path
import hashlib
import io
import datetime
from io import StringIO

# --- Configuration de la page ---
st.set_page_config(page_title="♻️ Restructuration FSA", layout="wide")
st.title("♻️ Recalcul et export des zones postales (FSA)")

st.write("""
Cette page permet de **recalculer toutes les zones (`polygonCoordinates`)**
en utilisant les **ZIPs du JSON** et les **géométries FSA** du shapefile.
Un fichier restructuré est automatiquement sauvegardé dans `data/`
et peut être téléchargé ci-dessous.
""")

# --- File uploader pour le JSON ---
uploaded_json = st.file_uploader("📂 Charger le fichier JSON", type=["json"])

if uploaded_json is not None:
    try:
        data = json.load(StringIO(uploaded_json.getvalue().decode("utf-8")))
        st.success(f"✅ Fichier JSON chargé : {uploaded_json.name}")
    except Exception as e:
        st.error(f"❌ Erreur lors du chargement du JSON : {e}")
        st.stop()
else:
    st.info("⬆️ Veuillez importer un fichier JSON pour continuer.")
    st.stop()

# --- Chargement du shapefile ---
shapefile_path = Path("data/lfsa000b21a_e.shp")

try:
    gdf = gpd.read_file(shapefile_path)[["CFSAUID", "geometry"]]
    if gdf.crs.to_string().lower() != "epsg:4326":
        gdf = gdf.to_crs(epsg=4326)
    st.success("✅ Fichiers chargés avec succès.")
except Exception as e:
    st.error(f"❌ Erreur de chargement du shapefile : {e}")
    st.stop()

# --- Fonctions utilitaires ---
def color_from_name(name: str) -> str:
    h = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"#{h}"

def polygon_to_text(geom) -> str:
    """Convertit Polygon/MultiPolygon en texte lon,lat,0"""
    parts = []
    if isinstance(geom, Polygon):
        coords = list(geom.exterior.coords)
        parts.append("\r\n".join([f"{x:.8f},{y:.8f},0" for x, y in coords]))
    elif isinstance(geom, MultiPolygon):
        for p in geom.geoms:
            coords = list(p.exterior.coords)
            parts.append("\r\n".join([f"{x:.8f},{y:.8f},0" for x, y in coords]))
    return "\r\n\r\n".join(parts)

def normalize_geom(geom):
    """Nettoie la géométrie."""
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    return None

# --- Bouton principal ---
if st.button("🧩 Recalculer les zones et afficher la carte"):
    modified_data = json.loads(json.dumps(data))
    new_polygons = []
    updated = 0
    errors = 0

    progress_bar = st.progress(0, text="Restructuration en cours...")
    total_routes = len(modified_data.get("routingParameterUiVehicleDTOs", []))
    processed_routes = 0

    for route in modified_data.get("routingParameterUiVehicleDTOs", []):
        rname = route.get("name", "Unknown")
        for pref in route.get("routingParameterUiVehiclePreferenceDTOs", []):
            zips = [z.strip().upper() for z in str(pref.get("zip", "")).split(",") if z.strip()]
            if not zips:
                continue

            fsa_match = gdf[gdf["CFSAUID"].isin(zips)]
            if fsa_match.empty:
                errors += 1
                continue

            merged_geom = fsa_match.geometry.union_all()
            merged_norm = normalize_geom(merged_geom)
            if not merged_norm:
                errors += 1
                continue

            poly_text = polygon_to_text(merged_norm)
            pref.setdefault("routingParameterUiPolygonDTO", {})
            pref["routingParameterUiPolygonDTO"]["polygonCoordinates"] = poly_text
            updated += 1

            new_polygons.append({
                "route_name": rname,
                "shapely": merged_norm
            })

        processed_routes += 1
        progress_bar.progress(
            processed_routes / total_routes,
            text=f"Traitement de {processed_routes}/{total_routes} routes..."
        )

    progress_bar.empty()
    st.session_state["new_polygons"] = new_polygons
    st.session_state["modified_data"] = modified_data
    st.session_state["updated"] = updated
    st.session_state["errors"] = errors

# --- Affichage de la carte et téléchargement ---
if "new_polygons" in st.session_state and st.session_state["new_polygons"]:
    updated = st.session_state["updated"]
    errors = st.session_state["errors"]
    new_polygons = st.session_state["new_polygons"]
    modified_data = st.session_state["modified_data"]

    st.success(f"✅ {updated} zones restructurées avec succès.")
    if errors:
        st.warning(f"⚠️ {errors} zones ignorées (aucun FSA trouvé).")

    # --- Nouvelle carte ---
    m_new = folium.Map(location=[45.5017, -73.5673], zoom_start=10)

    for poly in new_polygons:
        color = color_from_name(poly["route_name"])
        folium.GeoJson(
            poly["shapely"],
            tooltip=f"{poly['route_name']}",
            style_function=lambda x, c=color: {
                "fillColor": c, "color": c, "weight": 2, "fillOpacity": 0.45
            }
        ).add_to(m_new)

    # ➕ Dépôt
    if "depotLocation" in data:
        depot = data["depotLocation"]
        lat_key = "latitude" if "latitude" in depot else "lat"
        lon_key = "longitude" if "longitude" in depot else "lng"
        if depot.get(lat_key) and depot.get(lon_key):
            folium.Marker(
                [float(depot[lat_key]), float(depot[lon_key])],
                popup="📦 Dépôt principal",
                icon=folium.Icon(color="red", icon="home", prefix="fa")
            ).add_to(m_new)

    st_folium(m_new, width=950, height=600)

    # --- Sauvegarde automatique ---
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = Path(f"data/QC-MONT-STD-SORT2-CASCADE-ASN-reshaped-{timestamp}.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(modified_data, f, indent=4, ensure_ascii=False)

    st.info(f"💾 Fichier sauvegardé automatiquement : `{output_path.name}`")

    # --- Téléchargement du JSON restructuré ---
    with open(output_path, "rb") as f:
        st.download_button(
            label="📥 Télécharger le JSON restructuré",
            data=f,
            file_name=output_path.name,
            mime="application/json"
        )
