import streamlit as st
from streamlit_folium import st_folium
import folium
import json
import gzip
import pickle
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.ops import unary_union
import geopandas as gpd
from pathlib import Path
import re
import hashlib
import uuid

# --- Configuration ---
st.set_page_config(page_title="Carte OpenStreetMap — Gestion dynamique", layout="wide")
logo_icon = "data/logo_intelcom_2024.png"
logo_image = "data/logo_intelcom_2024.png"
st.logo(icon_image=logo_icon, image=logo_image)
st.title("🗺️ Carte interactive — Édition dynamique des zones par codes postaux")

# --- Chemins des fichiers ---
shapefile_path = Path("data/lfsa000b21a_e.shp")
FSA_CACHE_DIR = Path("data/.cache")
FSA_CACHE_PATH = FSA_CACHE_DIR / "fsa_geom_cache.pkl.gz"
FSA_CACHE_VERSION = 1

# --- Uploader pour le JSON ---
uploaded_json = st.file_uploader("📂 Charger le fichier JSON de configuration", type=["json"])

if not uploaded_json:
    st.info("Veuillez charger un fichier JSON pour commencer.")
    st.stop()

uploaded_bytes = uploaded_json.getvalue()
file_signature = hashlib.sha1(uploaded_bytes).hexdigest()

if (
    st.session_state.get("home_uploaded_signature") != file_signature
    or "home_json_data" not in st.session_state
):
    try:
        parsed_data = json.loads(uploaded_bytes.decode("utf-8"))
    except Exception as e:
        st.error(f"❌ Erreur lors de la lecture du JSON : {e}")
        st.stop()
    st.session_state["home_uploaded_signature"] = file_signature
    st.session_state["home_uploaded_name"] = uploaded_json.name
    st.session_state["home_json_data"] = parsed_data
    st.session_state["home_download_name"] = f"UPDATED_{uploaded_json.name}"
    st.session_state["home_download_name_input"] = f"UPDATED_{uploaded_json.name}"
    st.session_state["home_polygons_cache"] = None

data = st.session_state.get("home_json_data")
if data is None:
    st.error("❌ Impossible de charger les données depuis la session.")
    st.stop()

st.success(f"✅ Fichier JSON chargé avec succès : {uploaded_json.name}")

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

def _compute_file_signature(path: Path) -> str:
    stats = path.stat()
    return f"{path.name}:{stats.st_size}:{stats.st_mtime_ns}"


def _load_cached_fsa_geoms(expected_signature: str, version: int):
    if not FSA_CACHE_PATH.exists():
        return None
    try:
        with gzip.open(FSA_CACHE_PATH, "rb") as fh:
            payload = pickle.load(fh)
    except Exception:
        return None
    if payload.get("version") != version:
        return None
    if payload.get("signature") != expected_signature:
        return None
    return payload.get("data")


def _write_cached_fsa_geoms(signature: str, version: int, fsa_geom_map):
    try:
        FSA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with gzip.open(FSA_CACHE_PATH, "wb") as fh:
            pickle.dump(
                {
                    "version": version,
                    "signature": signature,
                    "data": fsa_geom_map,
                },
                fh,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
    except Exception as exc:
        st.warning(f"⚠️ Impossible de persister le cache FSA : {exc}")


@st.cache_resource(show_spinner=False)
def load_fsa_geom_map(path: str, signature: str, cache_version: int):
    cached = _load_cached_fsa_geoms(signature, cache_version)
    if cached is not None:
        return cached, True

    gdf_local = gpd.read_file(path)[["CFSAUID", "geometry"]]
    gdf_local["CFSAUID"] = gdf_local["CFSAUID"].astype(str).str.upper().str.strip()
    if gdf_local.crs and gdf_local.crs.to_string().lower() != "epsg:4326":
        gdf_local = gdf_local.to_crs(epsg=4326)

    fsa_geoms = {}
    for fsa, sub in gdf_local.groupby("CFSAUID"):
        merged = sub.geometry.unary_union
        merged_norm = normalize_geom(merged)
        if merged_norm:
            fsa_geoms[fsa] = merged_norm

    _write_cached_fsa_geoms(signature, cache_version, fsa_geoms)
    return fsa_geoms, False


def ensure_fsa_geom_map():
    if "home_fsa_geom_map" in st.session_state:
        return st.session_state["home_fsa_geom_map"]

    if not shapefile_path.exists():
        st.error(f"❌ Shapefile introuvable : {shapefile_path}")
        st.stop()

    signature = _compute_file_signature(shapefile_path)
    try:
        with st.spinner("Chargement des géométries FSA (peut prendre quelques minutes la première fois)..."):
            fsa_geom_map, from_cache = load_fsa_geom_map(
                str(shapefile_path), signature, FSA_CACHE_VERSION
            )
    except Exception as exc:
        st.error(f"❌ Erreur de chargement des géométries FSA : {exc}")
        st.stop()

    st.session_state["home_fsa_geom_map"] = fsa_geom_map
    source_label = "cache local" if from_cache else "shapefile (cache mis à jour)"
    st.info(f"📁 Référentiel FSA chargé depuis le {source_label}.")
    return fsa_geom_map

def merge_fsas_to_geom(fsas, fsa_geom_map):
    geoms = [fsa_geom_map.get(f.upper().strip()) for f in fsas if f and f.upper().strip() in fsa_geom_map]
    geoms = [g for g in geoms if g is not None]
    if not geoms:
        return None
    if len(geoms) == 1:
        return normalize_geom(geoms[0])
    merged = unary_union(geoms)
    return normalize_geom(merged)

def geom_to_latlon_parts(geom):
    parts = []
    if geom is None:
        return parts
    if isinstance(geom, Polygon):
        parts.append([(lat, lon) for lon, lat in geom.exterior.coords])
    elif isinstance(geom, MultiPolygon):
        for g in geom.geoms:
            parts.append([(lat, lon) for lon, lat in g.exterior.coords])
    return parts

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

def get_polygons_cache():
    cached_polygons = st.session_state.get("home_polygons_cache")
    if cached_polygons is None:
        cached_polygons = build_polygons_from_data(data)
        st.session_state["home_polygons_cache"] = cached_polygons
    return cached_polygons

polygons = get_polygons_cache()
map_container = st.container()

# --- Coordonnées du dépôt ---
def get_depot_coordinates(data):
    depot = data.get("depotLocation")
    if not isinstance(depot, dict):
        return None
    lat = depot.get("latitude")
    if lat is None:
        lat = depot.get("lat")
    lon = depot.get("longitude")
    if lon is None:
        lon = depot.get("lng")
    if lon is None:
        lon = depot.get("long")
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None

# --- Affichage de la carte ---
def show_map(polygons, highlight=None):
    depot_coords = get_depot_coordinates(data)
    if depot_coords:
        depot_lat, depot_lon = depot_coords
        map_location = [depot_lat, depot_lon]
    else:
        map_location = [45.5017, -73.5673]
    m = folium.Map(location=map_location, zoom_start=10)

    # --- Ajouter le dépôt sur la carte ---
    if depot_coords:
        folium.Marker(
            [depot_coords[0], depot_coords[1]],
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
highlight_route = st.session_state.get("home_highlight_route")
with map_container:
    m = show_map(polygons, highlight=highlight_route)
    st_data = st_folium(m, width=950, height=600)

if highlight_route:
    st.session_state["home_highlight_route"] = None

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
        curr_pref_zips = {z.strip().upper() for z in str(found["pref_obj"].get("zip", "")).split(",") if z.strip()}

        st.text("Codes postaux existants pour cette route :")
        st.write(", ".join(sorted(zips_in_route)) if zips_in_route else "Aucun")

        st.markdown("#### 🧩 Gestion des codes postaux")
        pref_unique_key = f"{found['route_obj'].get('id', 'route')}_{found['pref_obj'].get('id', 'pref')}"
        zip_to_remove = st.multiselect(
            "Supprimer des ZIPs de cette zone",
            sorted(curr_pref_zips),
            key=f"remove_{pref_unique_key}"
        )

        fsa_geom_map = st.session_state.get("home_fsa_geom_map")
        fsa_geom_ready = fsa_geom_map is not None
        zip_to_add = []
        if not fsa_geom_ready:
            st.info("ℹ️ Chargez le référentiel FSA uniquement si vous devez ajouter ou recalculer des ZIPs (opération lourde).")
            if st.button("📁 Charger le référentiel FSA", key=f"load_fsa_{pref_unique_key}"):
                fsa_geom_map = ensure_fsa_geom_map()
                fsa_geom_ready = True
        if fsa_geom_ready:
            available_fsas = sorted(fsa_geom_map.keys())
            zip_to_add = st.multiselect(
                "Ajouter des ZIPs à la route",
                available_fsas,
                key=f"add_{pref_unique_key}"
            )
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
                    found["pref_obj"]["zip"] = found["pref_obj"].get("zip", "")

                    for poly_entry in polygons:
                        if poly_entry["route_obj"] is found["route_obj"]:
                            poly_entry["route_name"] = new_route_name
                    found["route_name"] = new_route_name
                    found["zone_name"] = new_zone_name

                    requires_fsa = bool(zip_to_remove or zip_to_add)
                    if requires_fsa and fsa_geom_map is None:
                        fsa_geom_map = ensure_fsa_geom_map()

                    # --- Suppression ZIP (modifie la préférence actuelle uniquement) ---
                    curr_pref_zips_set = {z.strip().upper() for z in str(found["pref_obj"].get("zip", "")).split(",") if z.strip()}
                    pref_removed = False
                    removed_zips = set()
                    if zip_to_remove:
                        to_remove = {z.strip().upper() for z in zip_to_remove if z}
                        remaining = curr_pref_zips_set - to_remove
                        removed_zips = curr_pref_zips_set & to_remove
                        if remaining:
                            found["pref_obj"]["zip"] = ",".join(sorted(remaining))
                            found["zip"] = found["pref_obj"]["zip"]
                            missing_geom_remove = [z for z in remaining if z not in fsa_geom_map]
                            if missing_geom_remove:
                                st.warning(f"⚠️ Impossible de recalculer complètement le polygone, géométries manquantes pour : {', '.join(sorted(missing_geom_remove))}")
                            else:
                                merged_geom = merge_fsas_to_geom(sorted(remaining), fsa_geom_map)
                                if merged_geom:
                                    found["pref_obj"]["routingParameterUiPolygonDTO"]["polygonCoordinates"] = polygon_to_text(merged_geom)
                                    found["shapely"] = merged_geom
                                    found["parts"] = geom_to_latlon_parts(merged_geom)
                            curr_pref_zips_set = remaining
                        else:
                            prefs_list = found["route_obj"].get("routingParameterUiVehiclePreferenceDTOs", [])
                            if found["pref_obj"] in prefs_list:
                                removed_pref_obj = found["pref_obj"]
                                prefs_list.remove(removed_pref_obj)
                                polygons[:] = [p for p in polygons if p["pref_obj"] is not removed_pref_obj]
                                pref_removed = True
                            curr_pref_zips_set = set()

                    # Recalculer les zips présents dans la route après suppression
                    route_prefs = found["route_obj"].get("routingParameterUiVehiclePreferenceDTOs", [])
                    zips_in_route = set()
                    for pref in route_prefs:
                        zips_in_route.update([z.strip().upper() for z in str(pref.get("zip", "")).split(",") if z.strip()])

                    # --- Ajout ZIP ---
                    added_zips = set()
                    if zip_to_add:
                        to_add = [z.strip().upper() for z in zip_to_add if z]
                        to_create = [z for z in to_add if z not in zips_in_route]
                        duplicates = [z for z in to_add if z in zips_in_route]
                        if duplicates:
                            st.warning(f"⚠️ ZIPs déjà présents : {', '.join(sorted(set(duplicates)))}")
                        if to_create:
                            missing_geom_add = [z for z in to_create if z not in fsa_geom_map]
                            usable_zips = [z for z in to_create if z in fsa_geom_map]
                            if missing_geom_add:
                                st.warning(f"⚠️ Géométries introuvables pour : {', '.join(sorted(missing_geom_add))}")
                            if usable_zips:
                                new_geom = merge_fsas_to_geom(usable_zips, fsa_geom_map)
                                merged_into_current = False
                                if not pref_removed:
                                    existing_zips_list = sorted(curr_pref_zips_set)
                                    existing_geom = merge_fsas_to_geom(existing_zips_list, fsa_geom_map) if existing_zips_list else None
                                else:
                                    existing_zips_list = []
                                    existing_geom = None

                                if existing_geom and new_geom:
                                    existing_buffer = existing_geom.buffer(0)
                                    new_buffer = new_geom.buffer(0)
                                    if existing_buffer.intersects(new_buffer) or existing_buffer.distance(new_buffer) < 1e-6:
                                        combined_geom = normalize_geom(unary_union([existing_geom, new_geom]))
                                        combined_zips = sorted(set(existing_zips_list) | set(usable_zips))
                                        found["pref_obj"]["zip"] = ",".join(combined_zips)
                                        found["zip"] = found["pref_obj"]["zip"]
                                        found["pref_obj"]["routingParameterUiPolygonDTO"]["polygonCoordinates"] = polygon_to_text(combined_geom)
                                        found["shapely"] = combined_geom
                                        found["parts"] = geom_to_latlon_parts(combined_geom)
                                        curr_pref_zips_set = set(combined_zips)
                                        added_zips.update(usable_zips)
                                        zips_in_route.update(usable_zips)
                                        merged_into_current = True
                                        st.info(f"✅ ZIPs fusionnés dans la zone '{found['pref_obj']['routingParameterUiPolygonDTO']['name']}'.")

                                if not merged_into_current and new_geom:
                                    poly_text = polygon_to_text(new_geom)

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
                                        "zip": ",".join(sorted(usable_zips)),
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
                                    added_zips.update(usable_zips)
                                    zips_in_route.update(usable_zips)
                                    polygons.append({
                                        "route_obj": found["route_obj"],
                                        "pref_obj": new_pref,
                                        "route_name": new_route_name,
                                        "zone_name": new_zone_name_unique,
                                        "zip": new_pref["zip"],
                                        "parts": geom_to_latlon_parts(new_geom),
                                        "shapely": new_geom
                                    })
                                    st.info(f"✅ Nouvelle préférence '{new_zone_name_unique}' créée pour {len(usable_zips)} ZIP(s).")
                                elif not new_geom:
                                    st.warning("⚠️ Impossible de calculer le polygone des ZIPs sélectionnés.")
                            else:
                                st.warning("⚠️ Aucun polygone exploitable pour les ZIPs sélectionnés.")

                    # --- Sauvegarde ---
                    st.session_state["home_json_data"] = data
                    st.session_state["home_polygons_cache"] = polygons
                    st.success("✅ Modifications sauvegardées en mémoire.")

                    st.session_state["home_highlight_route"] = new_route_name

                    # --- Mise à jour de la carte sans recalcul global ---
                    map_container.empty()
                    with map_container:
                        m_updated = show_map(polygons, highlight=new_route_name)
                        st_folium(m_updated, width=950, height=600)

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

# --- Téléchargement de la configuration mise à jour ---
if "home_json_data" in st.session_state and st.session_state["home_json_data"]:
    default_download_name = st.session_state.get("home_download_name") or f"UPDATED_{uploaded_json.name}"
    download_name_input = st.text_input(
        "Nom du fichier exporté",
        value=default_download_name,
        key="home_download_name_input"
    ).strip()
    download_name = download_name_input or default_download_name
    if not download_name.endswith(".json"):
        download_name = f"{download_name}.json"
    st.session_state["home_download_name"] = download_name

    download_payload = json.dumps(
        st.session_state["home_json_data"],
        indent=4,
        ensure_ascii=False
    )
    st.download_button(
        label="⬇️ Télécharger la configuration mise à jour",
        data=download_payload,
        file_name=download_name,
        mime="application/json"
    )
