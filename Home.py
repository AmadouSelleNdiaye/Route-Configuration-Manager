from io import StringIO
import streamlit as st
from streamlit_folium import st_folium
import folium
import json
from shapely.geometry import Point, Polygon, MultiPolygon
from pathlib import Path
import geopandas as gpd
import re
import hashlib
import uuid

# --- Configuration ---
st.set_page_config(page_title="Carte OpenStreetMap — Gestion dynamique", layout="wide")
st.title("🗺️ Carte interactive — Édition dynamique des zones par codes postaux")

# --- Chemins des fichiers ---
json_path = Path("data/QC-MONT-STD-SORT2-CASCADE-ASN.json")
shapefile_path = Path("data/lfsa000b21a_e.shp")

# --- Uploader pour le JSON ---
uploaded_json = st.file_uploader("📂 Charger le fichier JSON de configuration", type=["json"])

if not uploaded_json:
    st.info("Veuillez charger un fichier JSON pour commencer.")
    st.stop()

try:
    data = json.load(StringIO(uploaded_json.getvalue().decode("utf-8")))
    st.success(f"✅ Fichier JSON chargé avec succès : {uploaded_json.name}")
except Exception as e:
    st.error(f"❌ Erreur lors de la lecture du JSON : {e}")
    st.stop()

# --- Charger le shapefile ---
try:
    gdf = gpd.read_file(shapefile_path)[["CFSAUID", "geometry"]]
    gdf["CFSAUID"] = gdf["CFSAUID"].astype(str).str.upper().str.strip()
    if gdf.crs and gdf.crs.to_string().lower() != "epsg:4326":
        gdf = gdf.to_crs(epsg=4326)
    st.success("✅ Shapefile chargé et reprojeté en EPSG:4326.")
except Exception as e:
    st.error(f"❌ Erreur de chargement du shapefile : {e}")
    st.stop()

# --- Vérification des routes admissibles ---
admissible_patterns = data.get("admissibleRoutePatterns", "")
match = re.match(r"([A-Z]+)\|(\d+)\|(\d+)", admissible_patterns)
if not match:
    st.error("❌ Format invalide pour 'admissibleRoutePatterns' (ex: MONT|1500|1999).")
    st.stop()

prefix, min_route, max_route = match.groups()
min_route, max_route = int(min_route), int(max_route)
st.info(f"✅ Routes admissibles : {prefix}{min_route} → {prefix}{max_route}")

# --- Fonctions utilitaires ---
def color_from_name(name: str) -> str:
    h = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"#{h}"

def polygon_to_text(geom) -> str:
    """Convertit un Polygon/MultiPolygon en texte lon,lat,0 (avec lignes vides entre parties)."""
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
    """Nettoie et homogénéise la géométrie fusionnée (Polygon/MultiPolygon)."""
    if geom is None:
        return None
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    polys = []
    for g in getattr(geom, "geoms", []):
        if isinstance(g, Polygon):
            polys.append(g)
        elif isinstance(g, MultiPolygon):
            polys.extend(list(g.geoms))
    if not polys:
        return None
    if len(polys) == 1:
        return polys[0]
    return MultiPolygon(polys)

def parse_polygon_text(coords_text):
    """Parse polygonCoordinates -> parts_latlon, shapely"""
    if not coords_text or not str(coords_text).strip():
        return None
    parts_raw = re.split(r"\r?\n\s*\r?\n", coords_text.strip())
    parts_latlon = []
    shapely_parts = []
    for part in parts_raw:
        lines = [ln.strip() for ln in part.splitlines() if ln.strip()]
        coords_latlon = []
        coords_lonlat = []
        for line in lines:
            fields = [f.strip() for f in line.split(",") if f.strip()]
            if len(fields) < 2:
                continue
            try:
                lon = float(fields[0])
                lat = float(fields[1])
            except ValueError:
                continue
            coords_latlon.append((lat, lon))
            coords_lonlat.append((lon, lat))
        if len(coords_lonlat) >= 3:
            parts_latlon.append(coords_latlon)
            shapely_parts.append(Polygon(coords_lonlat))
    if not shapely_parts:
        return None
    shapely_geom = shapely_parts[0] if len(shapely_parts) == 1 else MultiPolygon(shapely_parts)
    return {"parts_latlon": parts_latlon, "shapely": shapely_geom}

# --- Charger les polygones du JSON ---
def build_polygons_from_data(data):
    polygons = []
    for route in data.get("routingParameterUiVehicleDTOs", []):
        rname = route.get("name", "Unknown")
        for pref in route.get("routingParameterUiVehiclePreferenceDTOs", []):
            poly_data = pref.get("routingParameterUiPolygonDTO")
            if not poly_data or not isinstance(poly_data, dict):
                continue
            parsed = parse_polygon_text(poly_data.get("polygonCoordinates", ""))
            if parsed:
                polygons.append({
                    "route_obj": route,
                    "pref_obj": pref,
                    "route_name": rname,
                    "zone_name": poly_data.get("name", "Unknown"),
                    "zip": pref.get("zip", ""),
                    "parts": parsed["parts_latlon"],
                    "shapely": parsed["shapely"]
                })
    return polygons

polygons = build_polygons_from_data(data)

# --- Affichage de la carte ---
def show_map(polygons, highlight=None):
    m = folium.Map(location=[45.5017, -73.5673], zoom_start=10)
    # --- Ajouter le dépôt sur la carte ---
    if "depotLocation" in data:
        depot = data["depotLocation"]
        lat_key = "latitude" if "latitude" in depot else "lat"
        lon_key = "longitude" if "longitude" in depot else "lng"
        depot_lat = depot.get(lat_key)
        depot_lon = depot.get(lon_key)
        if depot_lat and depot_lon:
            folium.Marker(
                [float(depot_lat), float(depot_lon)],
                popup="📦 Dépôt principal",
                tooltip="Dépôt",
                icon=folium.Icon(color="red", icon="home", prefix="fa")
            ).add_to(m)

    for poly in polygons:
        color = color_from_name(poly["route_name"])
        weight = 5 if highlight and poly["route_name"] == highlight else 2
        for part in poly["parts"]:
            folium.Polygon(
                locations=part,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.35,
                weight=weight,
                tooltip=f"{poly['zone_name']} ({poly['route_name']})"
            ).add_to(m)
    return m

# --- Carte initiale ---
m = show_map(polygons)
st_data = st_folium(m, width=950, height=600)

# --- Gestion du clic ---
if st_data and st_data.get("last_clicked"):
    lat = st_data["last_clicked"]["lat"]
    lon = st_data["last_clicked"]["lng"]
    point = Point(lon, lat)

    found = None
    for poly in polygons:
        try:
            if poly["shapely"].intersects(point):
                found = poly
                break
        except Exception:
            continue

    if found:
        route_name = found["route_name"]
        st.markdown("### 🧭 Zone sélectionnée")
        st.write(f"**Coordonnées du clic :** ({lat:.6f}, {lon:.6f})")
        st.success(f"✅ Polygone trouvé : {found['zone_name']} ({route_name})")

        # Champs modifiables
        new_route_name = st.text_input("Nom de la route :", route_name)
        new_zone_name = st.text_input("Nom de la zone :", found["zone_name"])

        route_prefs = found["route_obj"].get("routingParameterUiVehiclePreferenceDTOs", [])
        zips_in_route = set()
        for p in route_prefs:
            zips_in_route.update([z.strip().upper() for z in str(p.get("zip", "")).split(",") if z.strip()])
        st.text("Codes postaux existants pour cette route :")
        st.write(", ".join(sorted(zips_in_route)) if zips_in_route else "Aucun")

        st.markdown("#### 🧩 Gestion des codes postaux")
        zip_to_add = st.text_input("Ajouter ZIP (ex: H9S,H9T)", "")
        zip_to_remove = st.text_input("Supprimer ZIP (ex: H9X,H9W)", "")
        new_adj = st.text_input("Routes adjacentes :", found["route_obj"].get("adjacentRoutes", ""))

        if st.button("💾 Appliquer les changements"):
            all_route_names = [p["route_name"] for p in polygons]
            match = re.match(rf"^{prefix}(\d+)$", new_route_name)
            if not match:
                st.error(f"🚫 Le nom doit commencer par '{prefix}' suivi d’un nombre.")
            else:
                route_num = int(match.group(1))
                if not (min_route <= route_num <= max_route):
                    st.error(f"🚫 Numéro {route_num} hors de la plage {min_route}–{max_route}.")
                elif new_route_name != route_name and new_route_name in all_route_names:
                    st.error(f"🚫 Le nom '{new_route_name}' est déjà utilisé.")
                else:
                    found["route_obj"]["name"] = new_route_name
                    found["pref_obj"]["routingParameterUiPolygonDTO"]["name"] = new_zone_name
                    found["route_obj"]["adjacentRoutes"] = new_adj

                    # --- Suppression ZIP (modifie la préférence actuelle uniquement) ---
                    removed_zips = set()
                    if zip_to_remove:
                        to_remove = {z.strip().upper() for z in zip_to_remove.split(",") if z.strip()}
                        curr_pref_zips = {z.strip().upper() for z in str(found["pref_obj"].get("zip", "")).split(",") if z.strip()}
                        remaining = curr_pref_zips - to_remove
                        removed_zips = curr_pref_zips & to_remove
                        if remaining:
                            found["pref_obj"]["zip"] = ",".join(sorted(remaining))
                            zip_geoms = gdf[gdf["CFSAUID"].isin(list(remaining))]
                            if not zip_geoms.empty:
                                merged = zip_geoms.unary_union
                                merged_norm = normalize_geom(merged)
                                if merged_norm:
                                    found["pref_obj"]["routingParameterUiPolygonDTO"]["polygonCoordinates"] = polygon_to_text(merged_norm)
                        else:
                            prefs_list = found["route_obj"].get("routingParameterUiVehiclePreferenceDTOs", [])
                            if found["pref_obj"] in prefs_list:
                                prefs_list.remove(found["pref_obj"])

                    # --- Ajout ZIP (crée une nouvelle préférence avec nom unique) ---
                    added_zips = set()
                    if zip_to_add:
                        to_add = [z.strip().upper() for z in zip_to_add.split(",") if z.strip()]
                        to_create = [z for z in to_add if z not in zips_in_route]
                        duplicates = [z for z in to_add if z in zips_in_route]
                        if duplicates:
                            st.warning(f"⚠️ ZIPs déjà présents : {', '.join(duplicates)}")
                        if to_create:
                            zip_geoms = gdf[gdf["CFSAUID"].isin(to_create)]
                            if not zip_geoms.empty:
                                merged = zip_geoms.unary_union
                                merged_norm = normalize_geom(merged)
                                if merged_norm:
                                    poly_text = polygon_to_text(merged_norm)

                                    # 🔹 Génération d'un nom unique de zone
                                    base_zone_name = found["zone_name"].strip()
                                    existing_names = [
                                        p.get("routingParameterUiPolygonDTO", {}).get("name", "")
                                        for p in found["route_obj"].get("routingParameterUiVehiclePreferenceDTOs", [])
                                    ]
                                    new_zone_name_unique = base_zone_name
                                    if base_zone_name in existing_names:
                                        i = 2
                                        while f"{base_zone_name}{i}" in existing_names:
                                            i += 1
                                        new_zone_name_unique = f"{base_zone_name}{i}"

                                    new_pref = {
                                        "id": str(uuid.uuid4().int)[:12],
                                        "routingParameterVehicleId": str(found["route_obj"].get("id", "")),
                                        "zip": ",".join(sorted(to_create)),
                                        "tag": "",
                                        "inPolygon": True,
                                        "routingParameterUiPolygonDTO": {
                                            "id": str(uuid.uuid4().int)[:12],
                                            "name": new_zone_name_unique,
                                            "polygonCoordinates": poly_text,
                                            "routingParameterId": str(found["route_obj"].get("routingParameterId", data.get("id")))
                                        },
                                        "value": 1.0,
                                        "orderRank": 1
                                    }
                                    found["route_obj"].setdefault("routingParameterUiVehiclePreferenceDTOs", []).append(new_pref)
                                    added_zips.update(to_create)
                                    st.info(f"✅ Nouvelle préférence '{new_zone_name_unique}' créée pour {len(to_create)} ZIP(s).")
                            else:
                                st.warning("⚠️ Aucun polygone trouvé pour ces ZIPs dans le shapefile.")

                    # --- Sauvegarde ---
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=4, ensure_ascii=False)
                    st.success("✅ Modifications sauvegardées.")

                    # --- Rechargement ---
                    polygons = build_polygons_from_data(data)
                    st.subheader("🗺️ Carte mise à jour")
                    m2 = show_map(polygons, highlight=new_route_name)
                    st_folium(m2, width=950, height=600)

                    if added_zips or removed_zips:
                        msg = []
                        if added_zips:
                            msg.append(f"Ajoutés : {', '.join(sorted(added_zips))}")
                        if removed_zips:
                            msg.append(f"Retirés : {', '.join(sorted(removed_zips))}")
                        st.info(" / ".join(msg))
    else:
        st.warning("⚠️ Aucun polygone trouvé pour ce point.")
else:
    st.info("🖱️ Cliquez sur un polygone pour afficher et modifier ses informations.")
