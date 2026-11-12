"""Data Explorer REST API Blueprint for FloatChat.

Provides endpoints for exploring ARGO floats, profiles, spatial map coordinates,
and streaming data exports. All endpoints adhere to FloatChat constraints:
- Parameterized SQL only (no SQL injection risk).
- Server-side pagination with bounded page sizes.
- No heavy JSON measurement arrays loaded in list queries.
- Original raw observations preserved with explicit scientific annotations.
- Consistent JSON response structures and HTTP status codes.
- Secure failure handling (generic 503 without leaking hostnames, credentials, or SQL text).
"""

from datetime import datetime
import json
import logging
from flask import Blueprint, request, jsonify, Response, stream_with_context
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


def _parse_int(val, name, min_val=None, max_val=None):
    if val is None or val == "":
        return None
    try:
        ival = int(val)
    except (ValueError, TypeError):
        raise ValueError(f"Parameter '{name}' must be an integer.")
    if min_val is not None and ival < min_val:
        raise ValueError(f"Parameter '{name}' must be >= {min_val}.")
    if max_val is not None and ival > max_val:
        raise ValueError(f"Parameter '{name}' must be <= {max_val}.")
    return ival


def _parse_float(val, name, min_val=None, max_val=None):
    if val is None or val == "":
        return None
    try:
        fval = float(val)
    except (ValueError, TypeError):
        raise ValueError(f"Parameter '{name}' must be a valid number.")
    if min_val is not None and fval < min_val:
        raise ValueError(f"Parameter '{name}' must be >= {min_val}.")
    if max_val is not None and fval > max_val:
        raise ValueError(f"Parameter '{name}' must be <= {max_val}.")
    return fval


def _parse_datetime(val, name):
    if val is None or str(val).strip() == "":
        return None
    cleaned = str(val).strip().rstrip("Z")
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(cleaned)
    except Exception:
        raise ValueError(
            f"Parameter '{name}' has an invalid date format: '{val}'. "
            f"Expected YYYY-MM-DD or ISO 8601 string."
        )


def create_explorer_blueprint(db_manager):
    """Factory function creating the explorer blueprint bound to a DatabaseManager."""
    explorer_bp = Blueprint("explorer_api", __name__)

    # =========================================================================
    # 1. OVERVIEW
    # =========================================================================
    @explorer_bp.route("/overview", methods=["GET"])
    def get_overview():
        """Returns dataset-level counts, date range, geo bounds, parameter flags, and projects/platforms status."""
        try:
            data = db_manager.get_data_overview()
            return jsonify(data), 200
        except SQLAlchemyError as sqle:
            logging.error(f"Database error in get_overview: {sqle}")
            return jsonify({"error": "Database service is temporarily unavailable. Please try again later."}), 503
        except Exception as e:
            logging.error(f"Error fetching data overview: {e}")
            return jsonify({"error": "Failed to retrieve overview statistics."}), 500

    # =========================================================================
    # 2. FLOATS LIST (Paginated, Searchable, Sortable)
    # =========================================================================
    @explorer_bp.route("/floats", methods=["GET"])
    def list_floats():
        """Lists floats with server-side pagination, search by WMO, and sorting."""
        try:
            page = _parse_int(request.args.get("page", 1), "page", min_val=1)
            limit = _parse_int(request.args.get("limit", 20), "limit", min_val=1, max_val=100)
            search = request.args.get("search", "").strip() or None

            sort_by = request.args.get("sort_by", "wmo_number").lower()
            allowed_sorts = ("wmo_number", "float_id", "profile_count", "min_time", "max_time")
            if sort_by not in allowed_sorts:
                return jsonify({
                    "error": f"Invalid sort_by: '{sort_by}'. Allowed columns: {list(allowed_sorts)}"
                }), 400

            sort_dir = request.args.get("sort_dir", "asc").lower()
            if sort_dir not in ("asc", "desc"):
                return jsonify({"error": "Invalid sort_dir: must be 'asc' or 'desc'."}), 400

            result = db_manager.get_floats_paginated(
                page=page,
                limit=limit,
                search=search,
                sort_by=sort_by,
                sort_dir=sort_dir
            )
            return jsonify(result), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        except SQLAlchemyError as sqle:
            logging.error(f"Database error in list_floats: {sqle}")
            return jsonify({"error": "Database service is temporarily unavailable. Please try again later."}), 503
        except Exception as e:
            logging.error(f"Error listing floats: {e}")
            return jsonify({"error": "Failed to retrieve floats."}), 500

    # =========================================================================
    # 3. FLOAT DETAIL BY WMO
    # =========================================================================
    @explorer_bp.route("/floats/<int:wmo_number>", methods=["GET"])
    def get_float_by_wmo(wmo_number):
        """Returns metadata and profile summary for a specific float."""
        try:
            float_data = db_manager.get_float_by_wmo(wmo_number)
            if not float_data:
                return jsonify({"error": f"Float with WMO {wmo_number} not found."}), 404
            return jsonify({"float": float_data}), 200
        except SQLAlchemyError as sqle:
            logging.error(f"Database error in get_float_by_wmo: {sqle}")
            return jsonify({"error": "Database service is temporarily unavailable. Please try again later."}), 503
        except Exception as e:
            logging.error(f"Error fetching float WMO {wmo_number}: {e}")
            return jsonify({"error": "Failed to retrieve float details."}), 500

    # =========================================================================
    # 4. PROFILES LIST (Paginated, Filterable, Sortable)
    # =========================================================================
    @explorer_bp.route("/profiles", methods=["GET"])
    def list_profiles():
        """Lists profiles without JSON measurement arrays, with server-side filters."""
        try:
            page = _parse_int(request.args.get("page", 1), "page", min_val=1)
            limit = _parse_int(request.args.get("limit", 20), "limit", min_val=1, max_val=100)
            wmo_number = _parse_int(request.args.get("wmo_number"), "wmo_number", min_val=1)
            profile_id = _parse_int(request.args.get("profile_id"), "profile_id", min_val=1)
            cycle_number = _parse_int(request.args.get("cycle_number"), "cycle_number", min_val=0)

            start_date = _parse_datetime(request.args.get("start_date"), "start_date")
            end_date = _parse_datetime(request.args.get("end_date"), "end_date")
            if start_date and end_date and start_date > end_date:
                return jsonify({"error": "start_date cannot be later than end_date."}), 400

            min_lat = _parse_float(request.args.get("min_lat"), "min_lat", min_val=-90.0, max_val=90.0)
            max_lat = _parse_float(request.args.get("max_lat"), "max_lat", min_val=-90.0, max_val=90.0)
            if min_lat is not None and max_lat is not None and min_lat > max_lat:
                return jsonify({"error": "min_lat cannot be greater than max_lat."}), 400

            min_lon = _parse_float(request.args.get("min_lon"), "min_lon", min_val=-180.0, max_val=180.0)
            max_lon = _parse_float(request.args.get("max_lon"), "max_lon", min_val=-180.0, max_val=180.0)
            if min_lon is not None and max_lon is not None and min_lon > max_lon:
                return jsonify({"error": "min_lon cannot be greater than max_lon."}), 400

            sort_by = request.args.get("sort_by", "profile_time").lower()
            allowed_sorts = (
                "profile_time",
                "profile_id",
                "cycle_number",
                "latitude",
                "longitude",
                "wmo_number",
            )
            if sort_by not in allowed_sorts:
                return jsonify({
                    "error": f"Invalid sort_by: '{sort_by}'. Allowed columns: {list(allowed_sorts)}"
                }), 400

            sort_dir = request.args.get("sort_dir", "desc").lower()
            if sort_dir not in ("asc", "desc"):
                return jsonify({"error": "Invalid sort_dir: must be 'asc' or 'desc'."}), 400

            result = db_manager.get_profiles_paginated(
                page=page,
                limit=limit,
                wmo_number=wmo_number,
                profile_id=profile_id,
                cycle_number=cycle_number,
                start_date=start_date,
                end_date=end_date,
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                sort_by=sort_by,
                sort_dir=sort_dir
            )
            return jsonify(result), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        except SQLAlchemyError as sqle:
            logging.error(f"Database error in list_profiles: {sqle}")
            return jsonify({"error": "Database service is temporarily unavailable. Please try again later."}), 503
        except Exception as e:
            logging.error(f"Error listing profiles: {e}")
            return jsonify({"error": "Failed to retrieve profiles."}), 500

    # =========================================================================
    # 5. PROFILE DETAIL (Measurements + Metadata)
    # =========================================================================
    @explorer_bp.route("/profiles/<int:profile_id>", methods=["GET"])
    def get_profile_detail(profile_id):
        """Returns single profile metadata and index-aligned measurement arrays."""
        try:
            profile_data = db_manager.get_profile_by_id(profile_id)
            if not profile_data:
                return jsonify({"error": f"Profile with ID {profile_id} not found."}), 404
            return jsonify({"profile": profile_data}), 200
        except SQLAlchemyError as sqle:
            logging.error(f"Database error in get_profile_detail: {sqle}")
            return jsonify({"error": "Database service is temporarily unavailable. Please try again later."}), 503
        except Exception as e:
            logging.error(f"Error fetching profile {profile_id}: {e}")
            return jsonify({"error": "Failed to retrieve profile detail."}), 500

    # =========================================================================
    # 6. MAP POINTS (Capped at 5000 with truncated flag)
    # =========================================================================
    @explorer_bp.route("/map-points", methods=["GET"])
    def get_map_points():
        """Returns lightweight coordinates for spatial plotting, capped at 5000 points."""
        try:
            wmo_number = _parse_int(request.args.get("wmo_number"), "wmo_number", min_val=1)
            cycle_number = _parse_int(request.args.get("cycle_number"), "cycle_number", min_val=0)
            start_date = _parse_datetime(request.args.get("start_date"), "start_date")
            end_date = _parse_datetime(request.args.get("end_date"), "end_date")
            min_lat = _parse_float(request.args.get("min_lat"), "min_lat", min_val=-90.0, max_val=90.0)
            max_lat = _parse_float(request.args.get("max_lat"), "max_lat", min_val=-90.0, max_val=90.0)
            min_lon = _parse_float(request.args.get("min_lon"), "min_lon", min_val=-180.0, max_val=180.0)
            max_lon = _parse_float(request.args.get("max_lon"), "max_lon", min_val=-180.0, max_val=180.0)

            result = db_manager.get_map_points(
                wmo_number=wmo_number,
                cycle_number=cycle_number,
                start_date=start_date,
                end_date=end_date,
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                cap=5000
            )
            return jsonify(result), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        except SQLAlchemyError as sqle:
            logging.error(f"Database error in get_map_points: {sqle}")
            return jsonify({"error": "Database service is temporarily unavailable. Please try again later."}), 503
        except Exception as e:
            logging.error(f"Error fetching map points: {e}")
            return jsonify({"error": "Failed to retrieve map coordinates."}), 500

    # =========================================================================
    # 7. EXPORT (Streaming CSV or JSON, supports content=profiles or measurements)
    # =========================================================================
    @explorer_bp.route("/export", methods=["GET"])
    def export_data():
        """Streams filtered profiles or depth-level measurements using an unbuffered server-side cursor."""
        try:
            fmt = request.args.get("format", "csv").lower().strip()
            if fmt not in ("csv", "json"):
                return jsonify({"error": "Invalid format: must be 'csv' or 'json'."}), 400

            content = request.args.get("content", "profiles").lower().strip()
            if content not in ("profiles", "measurements"):
                return jsonify({"error": "Invalid content: must be 'profiles' or 'measurements'."}), 400

            wmo_number = _parse_int(request.args.get("wmo_number"), "wmo_number", min_val=1)
            profile_id = _parse_int(request.args.get("profile_id"), "profile_id", min_val=1)
            cycle_number = _parse_int(request.args.get("cycle_number"), "cycle_number", min_val=0)
            start_date = _parse_datetime(request.args.get("start_date"), "start_date")
            end_date = _parse_datetime(request.args.get("end_date"), "end_date")
            min_lat = _parse_float(request.args.get("min_lat"), "min_lat", min_val=-90.0, max_val=90.0)
            max_lat = _parse_float(request.args.get("max_lat"), "max_lat", min_val=-90.0, max_val=90.0)
            min_lon = _parse_float(request.args.get("min_lon"), "min_lon", min_val=-180.0, max_val=180.0)
            max_lon = _parse_float(request.args.get("max_lon"), "max_lon", min_val=-180.0, max_val=180.0)

            where_sql, params = db_manager._build_profile_filters(
                wmo_number=wmo_number,
                profile_id=profile_id,
                cycle_number=cycle_number,
                start_date=start_date,
                end_date=end_date,
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon
            )

            # Build query based on content
            if content == "measurements" or fmt == "json":
                query_sql = f"""
                    SELECT 
                        p.profile_id,
                        f.wmo_number,
                        p.cycle_number,
                        p.profile_time,
                        p.latitude,
                        p.longitude,
                        p.pressure,
                        p.temperature,
                        p.salinity
                    FROM argo_profiles p
                    JOIN argo_floats f ON p.float_id = f.float_id
                    {where_sql}
                    ORDER BY p.profile_time ASC, p.profile_id ASC
                """
            else:
                query_sql = f"""
                    SELECT 
                        p.profile_id,
                        f.wmo_number,
                        p.cycle_number,
                        p.profile_time,
                        p.latitude,
                        p.longitude,
                        JSON_LENGTH(p.pressure) AS level_count
                    FROM argo_profiles p
                    JOIN argo_floats f ON p.float_id = f.float_id
                    {where_sql}
                    ORDER BY p.profile_time ASC, p.profile_id ASC
                """

            if fmt == "csv":
                def generate_csv():
                    yield "# FloatChat ARGO Ocean Data Export\n"
                    yield "# Data Version: 1.0 (Raw, unadjusted, no QC flags)\n"
                    yield "# Notes: Missing/fill values are represented as blank\n"

                    if content == "measurements":
                        yield "profile_id,wmo_number,cycle_number,profile_time,latitude,longitude,level_index,pressure_dbar,temperature_degC,salinity_psu\n"
                    else:
                        yield "profile_id,wmo_number,cycle_number,profile_time,latitude,longitude,level_count\n"

                    with db_manager.mysql_engine.connect() as conn:
                        # Server-side unbuffered streaming cursor
                        result = conn.execution_options(stream_results=True).execute(text(query_sql), params)
                        while True:
                            chunk = result.fetchmany(50)
                            if not chunk:
                                break
                            for row in chunk:
                                time_str = row[3].isoformat() if row[3] is not None else ""
                                lat_str = f"{row[4]:.4f}" if row[4] is not None else ""
                                lon_str = f"{row[5]:.4f}" if row[5] is not None else ""

                                if content == "measurements":
                                    p_arr = json.loads(row[6]) if row[6] else []
                                    t_arr = json.loads(row[7]) if row[7] else []
                                    s_arr = json.loads(row[8]) if row[8] else []
                                    n_levels = max(len(p_arr), len(t_arr), len(s_arr))

                                    for idx in range(n_levels):
                                        pval = p_arr[idx] if idx < len(p_arr) and p_arr[idx] is not None else ""
                                        tval = t_arr[idx] if idx < len(t_arr) and t_arr[idx] is not None else ""
                                        sval = s_arr[idx] if idx < len(s_arr) and s_arr[idx] is not None else ""

                                        p_str = f"{pval:.2f}" if isinstance(pval, (int, float)) else ""
                                        t_str = f"{tval:.3f}" if isinstance(tval, (int, float)) else ""
                                        s_str = f"{sval:.3f}" if isinstance(sval, (int, float)) else ""

                                        yield f"{row[0]},{row[1]},{row[2]},{time_str},{lat_str},{lon_str},{idx},{p_str},{t_str},{s_str}\n"
                                else:
                                    lvl_str = str(row[6]) if row[6] is not None else ""
                                    yield f"{row[0]},{row[1]},{row[2]},{time_str},{lat_str},{lon_str},{lvl_str}\n"

                filename = "floatchat_argo_measurements.csv" if content == "measurements" else "floatchat_argo_profiles.csv"
                return Response(
                    stream_with_context(generate_csv()),
                    mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"}
                )

            else:
                def generate_json():
                    yield '{"metadata":{"version":"1.0","data_annotation":"Raw, unadjusted, no QC flags","qc_available":false,"content":"' + content + '"},"profiles":['
                    first = True
                    with db_manager.mysql_engine.connect() as conn:
                        result = conn.execution_options(stream_results=True).execute(text(query_sql), params)
                        while True:
                            chunk = result.fetchmany(50)
                            if not chunk:
                                break
                            for row in chunk:
                                if not first:
                                    yield ","
                                else:
                                    first = False

                                p_arr = json.loads(row[6]) if row[6] else []
                                t_arr = json.loads(row[7]) if row[7] else []
                                s_arr = json.loads(row[8]) if row[8] else []

                                item = {
                                    "profile_id": row[0],
                                    "wmo_number": row[1],
                                    "cycle_number": row[2],
                                    "profile_time": row[3].isoformat() if row[3] else None,
                                    "latitude": float(row[4]) if row[4] is not None else None,
                                    "longitude": float(row[5]) if row[5] is not None else None,
                                    "level_count": max(len(p_arr), len(t_arr), len(s_arr)),
                                    "measurements": {
                                        "pressure": p_arr,
                                        "temperature": t_arr,
                                        "salinity": s_arr
                                    }
                                }
                                yield json.dumps(item)
                    yield "]}"

                return Response(
                    stream_with_context(generate_json()),
                    mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=floatchat_argo_profiles.json"}
                )

        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        except SQLAlchemyError as sqle:
            logging.error(f"Database error in export: {sqle}")
            return jsonify({"error": "Database service is temporarily unavailable. Please try again later."}), 503
        except Exception as e:
            logging.error(f"Error during export: {e}")
            return jsonify({"error": "Failed to export data."}), 500

    return explorer_bp
