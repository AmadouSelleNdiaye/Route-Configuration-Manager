import streamlit as st
import geopandas as gpd
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.ops import unary_union
import json, datetime, random, os

# ==============================================
# CONFIG
# ==============================================
st.set_page_config(page_title="Routage Intelcom Auto", layout="wide")
st.title("🚚 Générateur JSON de routage Intelcom (auto shapefile)")

SHAPEFILE_PATH = "data/lfsa000b21a_e.shp"  # chemin automatique
DEPOT_LAT, DEPOT_LNG = 45.50270149065861, -73.72035650279874

# ==============================================
# OUTILS
# ==============================================
def ensure_wgs84(gdf):
    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf

def find_fsa_column(df):
    for c in df.columns:
        if c.upper() in ["CFSAUID", "FSA", "ZIP"]:
            return c
    for c in df.columns:
        if df[c].dtype == object and df[c].astype(str).str.match(r"^[A-Z]\d[A-Z]").any():
            return c
    raise ValueError("Aucune colonne FSA trouvée dans le shapefile.")

def union_to_polygon(geom):
    if isinstance(geom, MultiPolygon):
        return max(list(geom.geoms), key=lambda g: g.area)
    return geom

def polygon_to_str(geom: Polygon):
    coords = list(geom.exterior.coords)
    lines = [f"{x:.12f},{y:.12f},0" for x, y in coords]
    return "\r\n".join(lines) + "\r\n"

def distance_to_depot(geom, depot_point):
    try:
        return geom.centroid.distance(depot_point)
    except:
        return float("inf")

# ==============================================
# SECTION 1 : CHARGEMENT DU SHAPEFILE
# ==============================================
st.header("1️⃣ Chargement automatique du shapefile")

if not os.path.exists(SHAPEFILE_PATH):
    st.error(f"❌ Le fichier shapefile n'existe pas à {SHAPEFILE_PATH}")
    st.stop()

gdf = gpd.read_file(SHAPEFILE_PATH)
gdf = ensure_wgs84(gdf)
fsa_col = find_fsa_column(gdf)
all_fsas = sorted(gdf[fsa_col].astype(str).unique().tolist())
st.success(f"✅ Shapefile chargé ({len(all_fsas)} FSAs détectés via {fsa_col})")

# ==============================================
# SECTION 2 : PARAMÈTRES DU RÉSEAU
# ==============================================
st.header("2️⃣ Paramètres du réseau")

network_id = "4485"
network_name = "QC-MONT-STD-SORT1-CASCADE-TSL"
region = "INTLCM-MONT"
region_description = "QC-MONT-STD"
admissible_patterns = "MONT|1000|1499"
prefix, start_num, end_num = ("MONT", 1000, 1499)
route_gap = 5

# ==============================================
# SECTION 3 : CRÉATION DES ROUTES
# ==============================================
st.header("3️⃣ Création des routes à partir des FSAs")
if "routes" not in st.session_state:
    st.session_state.routes = []
if "used_nums" not in st.session_state:
    st.session_state.used_nums = []

selected_fsas = st.multiselect("Sélectionner les FSAs pour une nouvelle route :", all_fsas)
add_route = st.button("➕ Ajouter cette route")

if add_route:
    if not selected_fsas:
        st.warning("Veuillez choisir au moins une FSA.")
    else:
        sub = gdf[gdf[fsa_col].astype(str).isin(selected_fsas)]
        geom_union = unary_union(sub.geometry)
        geom_union = union_to_polygon(geom_union)

        num = start_num
        while num in st.session_state.used_nums:
            num += route_gap
        st.session_state.used_nums.append(num)
        route_name = f"{prefix}{num}"

        polygon_coords = polygon_to_str(geom_union)
        uid = random.randint(152400, 152999)
        poly_id = random.randint(88360, 88400)
        pref_id = random.randint(238760, 238999)

        route = {
            "id": str(uid),
            "routingParameterId": network_id,
            "name": route_name,
            "precedence": None,
            "softPrecedence": None,
            "sourceLat": None,
            "sourceLng": None,
            "sinkLat": None,
            "sinkLng": None,
            "adjacentRoutes": "",
            "representative": "",
            "excludedRoutes": "",
            "routingParameterUiVehiclePreferenceDTOs": [
                {
                    "id": str(pref_id),
                    "routingParameterVehicleId": str(uid),
                    "zip": ",".join(selected_fsas),
                    "tag": "",
                    "inPolygon": True,
                    "routingParameterUiPolygonDTO": {
                        "id": str(poly_id),
                        "name": route_name,
                        "polygonCoordinates": polygon_coords,
                        "routingParameterId": network_id
                    },
                    "value": 1.0,
                    "orderRank": 1
                }
            ],
            "hardTarget": True,
            "electric": False,
            "_geom": geom_union
        }
        st.session_state.routes.append(route)
        st.success(f"✅ Route {route_name} créée ({len(selected_fsas)} FSAs).")

# ==============================================
# SECTION 4 : CALCUL AUTOMATIQUE DES RELATIONS
# ==============================================
def compute_relations(routes, depot_point):
    # Route représentative : celle qui contient le dépôt
    containing = [r for r in routes if r["_geom"].contains(depot_point)]
    rep_name = containing[0]["name"] if containing else min(
        routes, key=lambda r: distance_to_depot(r["_geom"], depot_point)
    )["name"]

    ordered = sorted(routes, key=lambda r: distance_to_depot(r["_geom"], depot_point))
    for i, r in enumerate(ordered):
        r["representative"] = rep_name
        if i == 0:
            r["adjacentRoutes"] = ""
        else:
            prev = ordered[i - 1]
            r["adjacentRoutes"] = prev["name"]
        r.pop("_geom", None)

# ==============================================
# SECTION 5 : GÉNÉRATION DU JSON FINAL
# ==============================================
st.header("4️⃣ Génération du JSON")

if st.button("🧱 Générer la structure JSON complète"):
    if not st.session_state.routes:
        st.warning("Aucune route n’a été ajoutée.")
    else:
        depot_point = Point(DEPOT_LNG, DEPOT_LAT)
        compute_relations(st.session_state.routes, depot_point)

        polygons = [
            r["routingParameterUiVehiclePreferenceDTOs"][0]["routingParameterUiPolygonDTO"]
            for r in st.session_state.routes
        ]
        nodes = [
            {"id": int(r["id"]), "label": r["name"], "color": "#33cccc", "value": 1}
            for r in st.session_state.routes
        ]
        edges = []
        name_to_id = {r["name"]: int(r["id"]) for r in st.session_state.routes}
        for r in st.session_state.routes:
            if r["adjacentRoutes"] and r["adjacentRoutes"] in name_to_id:
                edges.append({"from": int(r["id"]), "to": name_to_id[r["adjacentRoutes"]]})

        data = {
            "id": network_id,
            "name": network_name,
            "region": region,
            "regionDescription": region_description,
            "depotLocation": {"lat": DEPOT_LAT, "lng": DEPOT_LNG},
            "isDefault": False,
            "isValidGraph": True,
            "admissibleRoutePatterns": admissible_patterns,
            "postalCodePrefixes": "",
            "active": 1,
            "routingParameterUiVehicleDTOs": st.session_state.routes,
            "routingParameterUiPolygonDTOs": polygons,
            "routingParameterNodeDTOs": nodes,
            "routingParameterEdgeDTOs": edges,
            "isCascade": 1,
            "routeNumberGap": route_gap,
            "canEditCascade": None,
            "routingParameterUiAuditStatusDTO": {
                "routingParameterId": network_id,
                "auditCreatedUserInfo": {
                    "userName": "admin@intelcomexpress.com",
                    "timestamp": int(datetime.datetime.now().timestamp() * 1000)
                },
                "auditLastUpdateUserInfo": {
                    "userName": "admin@intelcomexpress.com",
                    "timestamp": int(datetime.datetime.now().timestamp() * 1000)
                }
            },
            "nbVehicle": len(st.session_state.routes)
        }

        st.subheader("🧾 Structure JSON générée")
        st.json(data)

        st.download_button(
            "📥 Télécharger le JSON complet",
            data=json.dumps(data, indent=4),
            file_name=f"{network_name}.json",
            mime="application/json"
        )
