# app.py
"""
Kenya GIS Explorer - Combined Professional Dashboard + Modern Minimal Map Studio
Backend: Flask serving an advanced Leaflet frontend.
Features:
 - Earth Engine initialization (service account key.json) OR fallback to local GeoJSON files
 - Endpoints:
    GET  /            -> render index.html
    GET  /health      -> health JSON
    GET  /get_counties -> geojson (counties, tries EE asset then local file)
    GET  /get_constituencies -> geojson (constituency asset or local file)
    GET  /get_kenya_inset -> geojson (inset layer)
    POST /download_feature -> download a single feature as geojson { "type": "county|const", "id_prop": "COUNTY_NAM", "id_val": "Nairobi" }
    POST /properties -> return properties for a given feature
    POST /upload_geojson -> (optional) upload fallback geojson files (secure-ish)
 - Caching to reduce repeated Earth Engine calls
 - Detailed logging and friendly JSON error responses
"""

import os
import io
import json
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, jsonify, request, send_file, abort, make_response, redirect, url_for
from werkzeug.utils import secure_filename

# Try to import Earth Engine API (optional)
try:
    import ee
except Exception:
    ee = None

# GeoJSON/local handling (GeoPandas optional)
try:
    import geopandas as gpd
except Exception:
    gpd = None

# ---- Configuration ----
APP_NAME = "Kenya GIS Explorer"
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT_DIR, "static")
DATA_DIR = os.path.join(STATIC_DIR, "data")
TEMPLATES_DIR = os.path.join(ROOT_DIR, "templates")
KEY_PATH = os.path.join(ROOT_DIR, "key.json")   # service account JSON (optional)

# Earth Engine asset paths (as provided)
EE_COUNTIES_ASSET = "projects/ee-celestakim019/assets/counties"
EE_CONST_ASSET = "projects/ee-celestakim019/assets/Constituency"
EE_KENYA_ASSET = "projects/ee-celestakim019/assets/KENYA"

# Local fallback file names (user should provide these in static/data/)
LOCAL_COUNTIES = os.path.join(DATA_DIR, "gadm41_KEN_3.json")
LOCAL_CONST = os.path.join(DATA_DIR, "Constituency.geojson")
LOCAL_KENYA = os.path.join(DATA_DIR, "Kenya.geojson")

ALLOWED_UPLOAD_EXT = {"json", "geojson"}

# Simple cache lifetime (seconds)
CACHE_TTL = 300

# Theme / mode defaults
VALID_MODES = ["system", "green", "yellow"]
DEFAULT_MODE = "system"

# Flask app
app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATES_DIR)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB

# Logging
logger = logging.getLogger("kenya_gis")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(ch)

# Global cache store
_cache = {
    "counties": {"ts": None, "geojson": None},
    "constituency": {"ts": None, "geojson": None},
    "kenya_inset": {"ts": None, "geojson": None}
}

# Global UI mode
ui_mode = DEFAULT_MODE

# ---- Utilities ----

def cache_valid(entry):
    if not entry or not entry.get("ts") or not entry.get("geojson"):
        return False
    return (datetime.utcnow() - entry["ts"]).total_seconds() < CACHE_TTL

def set_cache(key, geojson):
    _cache[key] = {"ts": datetime.utcnow(), "geojson": geojson}

def read_local_json(path):
    if not os.path.exists(path):
        logger.warning("Local file not found: %s", path)
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data
    except Exception as e:
        logger.exception("Failed to read JSON %s: %s", path, e)
        return None

def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)

def require_json(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not request.is_json:
            return jsonify({"error":"JSON body required"}), 400
        return f(*args, **kwargs)
    return wrapper

# ---- Earth Engine initialization ----
_EE_INIT_OK = False
_EE_INIT_MSG = "Earth Engine not initialized"

def init_earth_engine():
    global _EE_INIT_OK, _EE_INIT_MSG
    if ee is None:
        _EE_INIT_OK = False
        _EE_INIT_MSG = "earthengine-api Python package not installed"
        logger.warning(_EE_INIT_MSG)
        return _EE_INIT_OK, _EE_INIT_MSG

    # Try service account using key.json if present
    if os.path.exists(KEY_PATH):
        try:
            with open(KEY_PATH, "r", encoding="utf-8") as fh:
                key = json.load(fh)
            client_email = key.get("client_email")
            if client_email and hasattr(ee, "ServiceAccountCredentials"):
                creds = ee.ServiceAccountCredentials(client_email, KEY_PATH)
                ee.Initialize(creds)
                _EE_INIT_OK = True
                _EE_INIT_MSG = f"Initialized EE with service account {client_email}"
                logger.info(_EE_INIT_MSG)
                return True, _EE_INIT_MSG
        except Exception as ex:
            _EE_INIT_OK = False
            _EE_INIT_MSG = f"EE service account init failed: {ex}"
            logger.exception(_EE_INIT_MSG)

    # Try default auth (maybe user has gcloud credentials)
    try:
        ee.Initialize()
        _EE_INIT_OK = True
        _EE_INIT_MSG = "Initialized EE with default credentials"
        logger.info(_EE_INIT_MSG)
        return True, _EE_INIT_MSG
    except Exception as ex:
        _EE_INIT_OK = False
        _EE_INIT_MSG = f"EE default init failed: {ex}"
        logger.warning(_EE_INIT_MSG)
        return False, _EE_INIT_MSG

# Attempt to init on startup (non-blocking)
init_earth_engine()

# ---- EE helpers ----
def ee_fc_to_geojson(fc):
    """
    Convert an ee.FeatureCollection to GeoJSON (Python dict) using getInfo().
    Warning: getInfo() is blocking and can be slow; used with caching.
    """
    try:
        info = fc.getInfo()
        return info
    except Exception as e:
        logger.exception("ee getInfo failed: %s", e)
        raise

def fetch_ee_asset_geojson(asset_id):
    """
    Try to fetch a FeatureCollection from Earth Engine and return geojson dict.
    """
    if not _EE_INIT_OK:
        raise RuntimeError("Earth Engine not initialized")
    try:
        fc = ee.FeatureCollection(asset_id)
        geojson = ee_fc_to_geojson(fc)
        # Ensure type is FeatureCollection
        if isinstance(geojson, dict) and geojson.get("type") == "FeatureCollection":
            return geojson
        else:
            raise RuntimeError("EE returned unexpected structure")
    except Exception as e:
        logger.exception("Failed to fetch EE asset %s: %s", asset_id, e)
        raise

# ---- Data access functions ----

def get_counties_geojson(force=False):
    """
    Return counties GeoJSON (cached). Try EE asset then local fallback files.
    """
    key = "counties"
    if not force and cache_valid(_cache[key]):
        logger.debug("Returning counties from cache")
        return _cache[key]["geojson"]
    # Try EE asset
    if _EE_INIT_OK:
        try:
            gj = fetch_ee_asset_geojson(EE_COUNTIES_ASSET)
            set_cache(key, gj)
            return gj
        except Exception:
            logger.info("EE counties failed, will try local fallback")
    # Local fallback
    local = read_local_json(LOCAL_COUNTIES)
    if local:
        set_cache(key, local)
        return local
    # Nothing
    return None

def get_constituency_geojson(force=False):
    key = "constituency"
    if not force and cache_valid(_cache[key]):
        return _cache[key]["geojson"]
    if _EE_INIT_OK:
        try:
            gj = fetch_ee_asset_geojson(EE_CONST_ASSET)
            set_cache(key, gj)
            return gj
        except Exception:
            logger.info("EE constituency asset failed, trying local")
    local = read_local_json(LOCAL_CONST)
    if local:
        set_cache(key, local)
        return local
    return None

def get_kenya_inset_geojson(force=False):
    key = "kenya_inset"
    if not force and cache_valid(_cache[key]):
        return _cache[key]["geojson"]
    if _EE_INIT_OK:
        try:
            gj = fetch_ee_asset_geojson(EE_KENYA_ASSET)
            set_cache(key, gj)
            return gj
        except Exception:
            logger.info("EE kenya inset asset failed, trying local")
    local = read_local_json(LOCAL_KENYA)
    if local:
        set_cache(key, local)
        return local
    return None

# Helper to find feature by property
def find_features(geojson, prop_name, prop_value):
    if not geojson or "features" not in geojson:
        return []
    matches = []
    for feat in geojson["features"]:
        props = feat.get("properties", {})
        # direct match
        val = props.get(prop_name)
        if val is not None:
            try:
                if str(val).strip().lower() == str(prop_value).strip().lower():
                    matches.append(feat)
                    continue
            except Exception:
                pass
        # fuzzy: check all string props
        for k, v in props.items():
            try:
                if isinstance(v, str) and str(v).strip().lower() == str(prop_value).strip().lower():
                    matches.append(feat)
                    break
            except Exception:
                continue
    return matches

# ---- Routes ----

@app.route("/")
def index():
    """Render main page. Provide info on whether EE is available and which mode is active."""
    mode = ui_mode
    ee_status = {"ok": _EE_INIT_OK, "msg": _EE_INIT_MSG}
    return render_template("index.html", ee_status=ee_status, mode=mode, app_name=APP_NAME)

@app.route("/health")
def health():
    return jsonify({
        "status":"ok",
        "time": datetime.utcnow().isoformat(),
        "ee": {"ok": _EE_INIT_OK, "msg": _EE_INIT_MSG},
        "cache": {k: (v["ts"].isoformat() if v.get("ts") else None) for k,v in _cache.items()}
    })

@app.route("/get_counties")
def get_counties():
    force = request.args.get("refresh", "0") == "1"
    try:
        gj = get_counties_geojson(force=force)
        if not gj:
            return jsonify({"error":"Counties data not available"}), 503
        return jsonify(gj)
    except Exception as e:
        logger.exception("Error /get_counties: %s", e)
        return jsonify({"error":"Server error fetching counties", "details": str(e)}), 500

@app.route("/get_constituencies")
def get_constituencies():
    force = request.args.get("refresh", "0") == "1"
    try:
        gj = get_constituency_geojson(force=force)
        if not gj:
            return jsonify({"error":"Constituency data not available"}), 503
        return jsonify(gj)
    except Exception as e:
        logger.exception("Error /get_constituencies: %s", e)
        return jsonify({"error":"Server error fetching constituencies", "details": str(e)}), 500

@app.route("/get_kenya_inset")
def get_kenya_inset():
    force = request.args.get("refresh", "0") == "1"
    try:
        gj = get_kenya_inset_geojson(force=force)
        if not gj:
            return jsonify({"error":"Kenya inset data not available"}), 503
        return jsonify(gj)
    except Exception as e:
        logger.exception("Error /get_kenya_inset: %s", e)
        return jsonify({"error":"Server error fetching kenya inset", "details": str(e)}), 500

@app.route("/properties", methods=["POST"])
@require_json
def properties():
    """
    POST JSON: { "layer": "counties|constituency|kenya", "id_prop": "COUNTY_NAM", "id_val": "Nairobi" }
    Returns the properties dict for the first matched feature (case-insensitive).
    """
    try:
        data = request.get_json()
        layer = data.get("layer")
        id_prop = data.get("id_prop")
        id_val = data.get("id_val")
        if not layer or not id_val:
            return jsonify({"error":"layer and id_val are required"}), 400

        if layer == "counties":
            gj = get_counties_geojson()
        elif layer == "constituency":
            gj = get_constituency_geojson()
        elif layer == "kenya":
            gj = get_kenya_inset_geojson()
        else:
            return jsonify({"error":"Unknown layer"}), 400

        # Try to match
        matches = []
        if id_prop:
            matches = find_features(gj, id_prop, id_val)
        if not matches:
            # fallback: search all properties for equality
            matches = find_features(gj, "any", id_val)

        if not matches:
            return jsonify({"error":"Feature not found", "layer":layer, "id_val":id_val}), 404

        feat = matches[0]
        return jsonify({"properties": feat.get("properties", {}), "feature": feat})

    except Exception as e:
        logger.exception("Error /properties: %s", e)
        return jsonify({"error":"Server error", "details": str(e)}), 500

@app.route("/download_feature", methods=["POST"])
@require_json
def download_feature():
    """
    Download a single feature as a GeoJSON file.
    POST body: { "layer": "counties|constituency|kenya", "id_prop": "COUNTY_NAM", "id_val": "Nairobi" }
    """
    try:
        data = request.get_json()
        layer = data.get("layer")
        id_prop = data.get("id_prop")
        id_val = data.get("id_val")
        filename_hint = data.get("filename") or id_val or "feature"

        if not layer or not id_val:
            return jsonify({"error":"layer and id_val are required"}), 400

        if layer == "counties":
            gj = get_counties_geojson()
        elif layer == "constituency":
            gj = get_constituency_geojson()
        elif layer == "kenya":
            gj = get_kenya_inset_geojson()
        else:
            return jsonify({"error":"Unknown layer"}), 400

        # find matches
        matches = []
        if id_prop:
            matches = find_features(gj, id_prop, id_val)
        if not matches:
            matches = find_features(gj, "any", id_val)

        if not matches:
            return jsonify({"error":"Feature not found"}), 404

        feat = matches[0]
        out = {"type":"FeatureCollection", "features":[feat]}
        bio = io.BytesIO()
        bio.write(json.dumps(out, indent=2).encode("utf-8"))
        bio.seek(0)
        safe_name = secure_filename(f"{filename_hint}.geojson")
        return send_file(bio, mimetype="application/geo+json", as_attachment=True, download_name=safe_name)
    except Exception as e:
        logger.exception("Error /download_feature: %s", e)
        return jsonify({"error":"Server error", "details": str(e)}), 500

# Simple endpoint to change UI mode
@app.route("/set_mode/<mode>")
def set_mode(mode):
    global ui_mode
    if mode not in VALID_MODES:
        return jsonify({"error":"invalid mode", "valid": VALID_MODES}), 400
    ui_mode = mode
    return redirect(url_for("index"))

# Optional upload endpoints for local fallback (protected by simple token)
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "")  # optional; set as env var in production

@app.route("/upload_geojson", methods=["POST"])
def upload_geojson():
    """
    Accepts multipart form 'file' and saves to static/data with the provided filename param.
    Requires either UPLOAD_TOKEN env var or omitted for dev. Use with caution (no auth).
    Form fields:
      - file: uploaded file
      - save_as: filename (optional; defaults to uploaded filename)
    """
    try:
        token = request.form.get("token", "")
        if UPLOAD_TOKEN and token != UPLOAD_TOKEN:
            return jsonify({"error":"invalid upload token"}), 401

        if "file" not in request.files:
            return jsonify({"error":"no file part"}), 400
        file = request.files["file"]
        if not file or file.filename == "":
            return jsonify({"error":"no selected file"}), 400

        fname = secure_filename(file.filename)
        ext = fname.rsplit(".",1)[-1].lower()
        if ext not in ALLOWED_UPLOAD_EXT:
            return jsonify({"error":"unsupported extension"}), 400

        save_as = request.form.get("save_as") or fname
        save_path = os.path.join(DATA_DIR, secure_filename(save_as))
        ensure_data_dir()
        file.save(save_path)
        # clear cache entries so new uploads take effect
        _cache["counties"] = {"ts": None, "geojson": None}
        _cache["constituency"] = {"ts": None, "geojson": None}
        _cache["kenya_inset"] = {"ts": None, "geojson": None}
        logger.info("Uploaded file saved to %s", save_path)
        return jsonify({"status":"ok", "path": save_path})
    except Exception as e:
        logger.exception("Upload error: %s", e)
        return jsonify({"error":"server error", "details": str(e)}), 500

# Info endpoint for client metadata
@app.route("/meta")
def meta():
    return jsonify({
        "app": APP_NAME,
        "time": datetime.utcnow().isoformat(),
        "ee_init": {"ok": _EE_INIT_OK, "msg": _EE_INIT_MSG},
        "mode": ui_mode
    })

# Custom error handlers
@app.errorhandler(404)
def page_not_found(e):
    return jsonify({"error":"not found"}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.exception("Internal server error: %s", e)
    return jsonify({"error":"internal server error"}), 500

# ---- Serve (main) ----
if __name__ == "__main__":
    ensure_data_dir()
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1")
    logger.info("Starting %s on %s:%s", APP_NAME, host, port)
    logger.info("Earth Engine available: %s (%s)", _EE_INIT_OK, _EE_INIT_MSG)
    app.run(host=host, port=port, debug=True)
