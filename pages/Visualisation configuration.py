import streamlit as st
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
from streamlit_folium import st_folium
import folium
import ast
import re
import os

# --- Configuration de la page ---
st.set_page_config(page_title="Visualisation FSAs et volumes", layout="wide")
logo_icon = "data/logo_intelcom_2024.png"
logo_image = "data/logo_intelcom_2024.png"
st.logo(icon_image=logo_icon, image=logo_image)
st.title("🛰️ Visualisation des regroupements FSA par station")

SHAPEFILE_PATH = "data/lfsa000b21a_e.shp"
STATION_FSA_CSV = "data/fsa_liste_par_station.csv"
PACKAGE_COUNTS_CSV = "data/package_count_par_fsa.csv"


def ensure_wgs84(gdf):
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf


def find_fsa_column(df):
    for col in df.columns:
        if col.upper() in {"CFSAUID", "FSA", "ZIP"}:
            return col
    for col in df.columns:
        series = df[col]
        if series.dtype == object and series.astype(str).str.match(r"^[A-Z]\d[A-Z]").any():
            return col
    raise ValueError("Aucune colonne de type FSA trouvée dans le shapefile.")


def union_to_polygon(geom):
    if isinstance(geom, MultiPolygon):
        return max(list(geom.geoms), key=lambda g: g.area)
    return geom


def parse_fsa_list(raw_value):
    if raw_value is None:
        return []
    text = str(raw_value).strip()
    if not text:
        return []
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        parsed = None
    if isinstance(parsed, (list, tuple)):
        cleaned = [str(x).strip().upper() for x in parsed if str(x).strip()]
        return list(dict.fromkeys(cleaned))
    tokens = [tok.strip().upper() for tok in re.split(r"[\\s,;]+", text) if tok.strip()]
    return list(dict.fromkeys(tokens))


@st.cache_data(show_spinner=False)
def load_station_fsa_map(path):
    if not os.path.exists(path):
        return {}, {}
    df = pd.read_csv(path)
    mapping = {}
    locations = {}
    for _, row in df.iterrows():
        station_raw = row.get("Station")
        if pd.isna(station_raw):
            continue
        station = str(station_raw).strip()
        if not station:
            continue
        fsas = parse_fsa_list(row.get("FSA"))
        if fsas:
            mapping[station] = fsas
        lat = row.get("Latitude")
        lon = row.get("Longitude")
        if pd.notna(lat) and pd.notna(lon):
            try:
                locations[station] = (float(lat), float(lon))
            except (TypeError, ValueError):
                continue
    return mapping, locations


@st.cache_data(show_spinner=False)
def load_package_counts(path):
    if not os.path.exists(path):
        return {}
    df = pd.read_csv(path)
    if "FSA" not in df.columns or "Package Count" not in df.columns:
        return {}
    df["FSA"] = df["FSA"].astype(str).str.upper().str.strip()
    df["Package Count"] = pd.to_numeric(df["Package Count"], errors="coerce").fillna(0.0)
    return dict(zip(df["FSA"], df["Package Count"]))


def geom_to_latlon_parts(geom):
    parts = []
    if isinstance(geom, Polygon):
        coords = [(lat, lon) for lon, lat in geom.exterior.coords]
        parts.append(coords)
    elif isinstance(geom, MultiPolygon):
        for g in geom.geoms:
            coords = [(lat, lon) for lon, lat in g.exterior.coords]
            parts.append(coords)
    return parts


def group_fsas_by_target(fsas, pkg_map, gdf, fsa_col, target):
    records = []
    missing_geom = []
    missing_pkg = []

    for fsa in fsas:
        key = str(fsa).strip().upper()
        if not key:
            continue
        pkg = float(pkg_map.get(key, 0.0))
        if key not in pkg_map:
            missing_pkg.append(key)
        subset = gdf[gdf[fsa_col].astype(str).str.upper() == key]
        if subset.empty:
            missing_geom.append(key)
            continue
        geom = union_to_polygon(unary_union(subset.geometry))
        records.append({"fsa": key, "pkg": pkg, "geom": geom, "centroid": geom.centroid})

    if not records:
        return [], missing_geom, missing_pkg

    remaining = sorted(records, key=lambda r: r["pkg"], reverse=True)
    groups = []

    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        total_pkg = seed["pkg"]
        group_geom = seed["geom"]

        while remaining and total_pkg < target:
            centroid = group_geom.centroid
            idx_closest = min(
                range(len(remaining)),
                key=lambda idx: centroid.distance(remaining[idx]["centroid"])
            )
            candidate = remaining.pop(idx_closest)
            group.append(candidate)
            total_pkg += candidate["pkg"]
            group_geom = union_to_polygon(unary_union([group_geom, candidate["geom"]]))

        groups.append({
            "fsas": [item["fsa"] for item in group],
            "total_pkg": total_pkg,
            "geom": group_geom,
            "centroid": group_geom.centroid
        })

    groups.sort(key=lambda g: g["centroid"].y, reverse=True)
    return groups, missing_geom, missing_pkg


# --- Chargement des données ---
if not os.path.exists(SHAPEFILE_PATH):
    st.error(f"❌ Shapefile introuvable à `{SHAPEFILE_PATH}`.")
    st.stop()

gdf = gpd.read_file(SHAPEFILE_PATH)
gdf = ensure_wgs84(gdf)
fsa_col = find_fsa_column(gdf)
st.success(f"✅ Shapefile chargé ({len(gdf)} entrées) via la colonne `{fsa_col}`.")

station_map, station_locations = load_station_fsa_map(STATION_FSA_CSV)
pkg_map = load_package_counts(PACKAGE_COUNTS_CSV)

if not station_map:
    st.error("⚠️ Impossible de charger `fsa_liste_par_station.csv`. Vérifiez son contenu.")
    st.stop()

stations = sorted(station_map.keys())
selected_station = st.selectbox("Station à visualiser", stations)
target_packages = st.number_input(
    "Objectif de colis par polygone",
    min_value=10.0,
    value=250.0,
    step=10.0,
    help="Regroupement approximatif sur base des volumes disponibles."
)

station_fsas = station_map.get(selected_station, [])
if not station_fsas:
    st.warning("Aucune FSA disponible pour cette station.")
    st.stop()

groups, missing_geom, missing_pkg = group_fsas_by_target(station_fsas, pkg_map, gdf, fsa_col, target_packages)
total_pkg = sum(pkg_map.get(f, 0.0) for f in station_fsas)

col_a, col_b, col_c = st.columns(3)
col_a.metric("FSAs utilisées", f"{len(station_fsas) - len(missing_geom)} / {len(station_fsas)}")
col_b.metric("Colis estimés", f"{total_pkg:.0f}")
col_c.metric("Groupes générés", len(groups))

if missing_geom:
    st.warning("FSAs sans géométrie : " + ", ".join(sorted(set(missing_geom))))
if missing_pkg:
    st.info("FSAs sans volume colis (0) : " + ", ".join(sorted(set(missing_pkg))))

if groups:
    table_data = []
    for idx, grp in enumerate(groups, start=1):
        table_data.append({
            "Groupe": idx,
            "FSAs": ", ".join(grp["fsas"]),
            "Nb FSAs": len(grp["fsas"]),
            "Total colis": round(grp["total_pkg"], 1)
        })
    st.subheader("📋 Détail des regroupements proposés")
    st.dataframe(pd.DataFrame(table_data), width="stretch")

    if selected_station in station_locations:
        center_lat, center_lng = station_locations[selected_station]
    else:
        union_geom = union_to_polygon(unary_union([grp["geom"] for grp in groups]))
        centroid = union_geom.centroid
        center_lat, center_lng = centroid.y, centroid.x

    folium_map = folium.Map(location=[center_lat, center_lng], zoom_start=9)
    palette = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
        "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
        "#bcbd22", "#17becf"
    ]

    for idx, grp in enumerate(groups, start=1):
        color = palette[(idx - 1) % len(palette)]
        tooltip = folium.Tooltip(
            f"Groupe {idx}<br>Colis ~ {grp['total_pkg']:.0f}<br>FSAs: {', '.join(grp['fsas'])}",
            sticky=True
        )
        for part in geom_to_latlon_parts(grp["geom"]):
            folium.Polygon(
                locations=part,
                color=color,
                fill=True,
                fill_opacity=0.35,
                weight=2,
                tooltip=tooltip
            ).add_to(folium_map)

    if selected_station in station_locations:
        lat, lng = station_locations[selected_station]
        folium.Marker(
            [lat, lng],
            icon=folium.Icon(color="red", icon="home", prefix="fa"),
            tooltip=f"Station {selected_station}"
        ).add_to(folium_map)

    st.subheader("🗺️ Carte des regroupements")
    st_folium(folium_map, width=950, height=600)

st.caption(
    "ℹ️ Cette page propose un regroupement automatique des FSAs par proximité et volumes. "
    "La répartition fine d'une même FSA entre plusieurs polygones pourra être ajustée lors de la génération du JSON."
)
