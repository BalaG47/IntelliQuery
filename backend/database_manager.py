import chromadb
import json
import logging

from sqlalchemy import create_engine, text
from sentence_transformers import SentenceTransformer

from config import (
    DB_USER,
    DB_PASSWORD,
    DB_HOST,
    DB_PORT,
    DB_NAME,
    CHROMA_PERSIST_DIR,
    CHROMA_COLLECTION_NAME
)


class DatabaseManager:
    """
    Manages all database interactions for both MySQL and ChromaDB.
    """

    def __init__(self):
        """
        Initializes MySQL, ChromaDB, and the embedding model.
        """

        self.mysql_engine = self._setup_mysql_connection()

        # ChromaDB
        self.chroma_client = chromadb.PersistentClient(
            path=CHROMA_PERSIST_DIR
        )

        self.embedding_model = SentenceTransformer(
            "all-MiniLM-L6-v2"
        )

        self.chroma_collection = (
            self.chroma_client.get_or_create_collection(
                name=CHROMA_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"}
            )
        )

        logging.info(
            "Database connections and embedding model initialized."
        )

    # ---------------------------------------------------------
    # MYSQL CONNECTION
    # ---------------------------------------------------------

    def _setup_mysql_connection(self):
        """
        Establishes a connection to the MySQL database.
        """

        try:
            connection_url = (
                f"mysql+mysqlconnector://"
                f"{DB_USER}:{DB_PASSWORD}@"
                f"{DB_HOST}:{DB_PORT}/{DB_NAME}"
            )

            engine = create_engine(connection_url)

            with engine.connect():
                logging.info(
                    f"Successfully connected to MySQL database: '{DB_NAME}'"
                )

            return engine

        except Exception as e:
            logging.error(f"Failed to connect to MySQL: {e}")
            logging.error(
                "Please ensure MySQL is running and connection details "
                "are correct."
            )
            raise

    # ---------------------------------------------------------
    # CREATE MYSQL TABLES
    # ---------------------------------------------------------

    def create_mysql_tables(self):
        """
        Creates the necessary MySQL tables if they don't already exist.
        """

        create_floats_table_sql = """
        CREATE TABLE IF NOT EXISTS argo_floats (
            float_id INT AUTO_INCREMENT PRIMARY KEY,
            wmo_number INT UNIQUE NOT NULL,
            project_name VARCHAR(255),
            platform_type VARCHAR(255)
        );
        """

        create_profiles_table_sql = """
        CREATE TABLE IF NOT EXISTS argo_profiles (
            profile_id INT AUTO_INCREMENT PRIMARY KEY,
            float_id INT NOT NULL,
            cycle_number INT NOT NULL,
            profile_time DATETIME NOT NULL,
            latitude FLOAT NOT NULL,
            longitude FLOAT NOT NULL,
            pressure JSON,
            temperature JSON,
            salinity JSON,
            bgc_params JSON,
            FOREIGN KEY (float_id)
                REFERENCES argo_floats(float_id),
            UNIQUE KEY (float_id, cycle_number)
        );
        """

        try:

            with self.mysql_engine.connect() as conn:

                conn.execute(
                    text(create_floats_table_sql)
                )

                conn.execute(
                    text(create_profiles_table_sql)
                )

                conn.commit()

            logging.info(
                "MySQL tables 'argo_floats' and 'argo_profiles' are ready."
            )

        except Exception as e:

            logging.error(
                f"Error creating MySQL tables: {e}"
            )

            raise

    # ---------------------------------------------------------
    # FLOAT MANAGEMENT
    # ---------------------------------------------------------

    def get_or_create_float(
        self,
        wmo_number,
        project_name,
        platform_type
    ):
        """
        Retrieves a float ID from MySQL or creates a new float.
        If the float exists but has missing metadata (NULL, empty, or 'N/A'),
        updates the missing metadata with incoming valid values.
        """

        with self.mysql_engine.connect() as conn:

            result = conn.execute(
                text("""
                    SELECT float_id, project_name, platform_type
                    FROM argo_floats
                    WHERE wmo_number = :wmo
                """),
                {
                    "wmo": wmo_number
                }
            ).fetchone()

            if result:
                float_id = result[0]
                existing_proj = result[1]
                existing_plat = result[2]

                update_fields = {}
                # Determine if project_name needs update (existing is NULL, empty, or 'N/A')
                is_proj_missing = existing_proj is None or str(existing_proj).strip() in ("", "N/A")
                is_new_proj_valid = project_name is not None and str(project_name).strip() not in ("", "N/A")
                if is_proj_missing and is_new_proj_valid:
                    update_fields["project_name"] = str(project_name).strip()

                # Determine if platform_type needs update (existing is NULL, empty, or 'N/A')
                is_plat_missing = existing_plat is None or str(existing_plat).strip() in ("", "N/A")
                is_new_plat_valid = platform_type is not None and str(platform_type).strip() not in ("", "N/A")
                if is_plat_missing and is_new_plat_valid:
                    update_fields["platform_type"] = str(platform_type).strip()

                if update_fields:
                    set_clauses = []
                    params = {"fid": float_id}
                    if "project_name" in update_fields:
                        set_clauses.append("project_name = :new_proj")
                        params["new_proj"] = update_fields["project_name"]
                    if "platform_type" in update_fields:
                        set_clauses.append("platform_type = :new_plat")
                        params["new_plat"] = update_fields["platform_type"]

                    update_sql = f"UPDATE argo_floats SET {', '.join(set_clauses)} WHERE float_id = :fid"
                    conn.execute(text(update_sql), params)
                    conn.commit()

                return float_id

            insert_sql = text("""
                INSERT INTO argo_floats
                (
                    wmo_number,
                    project_name,
                    platform_type
                )
                VALUES
                (
                    :wmo,
                    :proj,
                    :platform
                )
            """)

            result = conn.execute(
                insert_sql,
                {
                    "wmo": wmo_number,
                    "proj": project_name,
                    "platform": platform_type
                }
            )

            conn.commit()

            return result.lastrowid

    # ---------------------------------------------------------
    # PROFILE EXISTENCE CHECK
    # ---------------------------------------------------------

    def check_profile_exists(
        self,
        float_id,
        cycle_number
    ):
        """
        Checks whether a profile already exists.
        """

        with self.mysql_engine.connect() as conn:

            result = conn.execute(
                text("""
                    SELECT 1
                    FROM argo_profiles
                    WHERE float_id = :fid
                    AND cycle_number = :cn
                """),
                {
                    "fid": float_id,
                    "cn": cycle_number
                }
            ).fetchone()

            return result is not None

    # ---------------------------------------------------------
    # INSERT PROFILE
    # ---------------------------------------------------------

    def _get_profile_table_columns(self):
        if not hasattr(self, "_cached_profile_cols") or self._cached_profile_cols is None:
            try:
                with self.mysql_engine.connect() as conn:
                    rows = conn.execute(text("SHOW COLUMNS FROM argo_profiles")).fetchall()
                    self._cached_profile_cols = {row[0] for row in rows}
            except Exception as e:
                logging.warning(f"Could not inspect argo_profiles columns: {e}")
                self._cached_profile_cols = {
                    "float_id", "cycle_number", "profile_time", "latitude", "longitude",
                    "pressure", "temperature", "salinity", "bgc_params"
                }
        return self._cached_profile_cols

    def insert_profile(self, profile_data):
        """
        Inserts a new ARGO profile into MySQL.
        Dynamically adapts to available columns in argo_profiles to support
        both pre-migration and post-migration database schemas.
        """

        logging.info("Inserting profile into MySQL...")

        table_cols = self._get_profile_table_columns()
        insert_keys = [k for k in profile_data.keys() if k in table_cols]
        if not insert_keys:
            raise ValueError("No valid columns provided for argo_profiles insertion.")

        cols_str = ", ".join(insert_keys)
        vals_str = ", ".join(f":{k}" for k in insert_keys)
        filtered_data = {k: profile_data[k] for k in insert_keys}

        insert_sql = text(f"""
            INSERT INTO argo_profiles
            (
                {cols_str}
            )
            VALUES
            (
                {vals_str}
            )
        """)

        with self.mysql_engine.connect() as conn:

            result = conn.execute(
                insert_sql,
                filtered_data
            )

            conn.commit()

            return result.lastrowid

    # ---------------------------------------------------------
    # CREATE CHROMA DOCUMENT
    # ---------------------------------------------------------

    def _create_profile_document(
        self,
        profile_id_db,
        float_id_db,
        cycle,
        time,
        lat,
        lon,
        pressure,
        temperature,
        salinity
    ):
        """
        Creates a compact structured text representation of an ARGO
        profile for semantic search.

        No LLM is used here.
        """

        def clean_value(value):
            """
            Converts values into JSON-safe Python values.
            """

            if value is None:
                return []

            try:
                return [
                    float(v)
                    for v in value
                    if v is not None
                ]

            except (TypeError, ValueError):
                return []

        pressure_values = clean_value(pressure)
        temperature_values = clean_value(temperature)
        salinity_values = clean_value(salinity)

        document = f"""
ARGO Oceanographic Profile

Profile ID: {profile_id_db}
Float WMO ID: {float_id_db}
Cycle Number: {cycle}

Date and Time:
{time}

Location:
Latitude: {lat}
Longitude: {lon}

Depth / Pressure Measurements:
{json.dumps(pressure_values)}

Temperature Measurements:
{json.dumps(temperature_values)}

Salinity Measurements:
{json.dumps(salinity_values)}

This document represents an oceanographic profile collected by an ARGO
profiling float. The measurements describe how temperature and salinity
vary with depth at the recorded geographic location and time.
""".strip()

        return document

    # ---------------------------------------------------------
    # ADD PROFILE TO CHROMADB
    # ---------------------------------------------------------

    def add_profile_to_chromadb(
        self,
        profile_id_db,
        float_id_db,
        cycle,
        time,
        lat,
        lon,
        bgc_keys,
        pressure,
        temperature,
        salinity
    ):
        """
        Creates an embedding for an ARGO profile and stores it in ChromaDB.

        IMPORTANT:
        No LLM is called during ingestion.

        The LLM will be used later by the RAG backend when a user asks
        a question.
        """

        logging.info(
            "Adding profile to ChromaDB..."
        )

        # -----------------------------------------------------
        # Create structured document
        # -----------------------------------------------------

        document = self._create_profile_document(
            profile_id_db=profile_id_db,
            float_id_db=float_id_db,
            cycle=cycle,
            time=time,
            lat=lat,
            lon=lon,
            pressure=pressure,
            temperature=temperature,
            salinity=salinity
        )

        # -----------------------------------------------------
        # Generate embedding
        # -----------------------------------------------------

        embedding = self.embedding_model.encode(
            document
        ).tolist()

        # -----------------------------------------------------
        # Store in ChromaDB
        # -----------------------------------------------------

        document_id = f"profile_{profile_id_db}"

        self.chroma_collection.add(
            ids=[document_id],

            embeddings=[embedding],

            documents=[document],

            metadatas=[{
                "profile_id_sql": int(profile_id_db),
                "float_id_sql": int(float_id_db),
                "cycle_number": int(cycle),
                "latitude": float(lat),
                "longitude": float(lon),
                "date": time.strftime("%Y-%m-%d")
            }]
        )

        logging.info(
            f"Profile {profile_id_db} added to ChromaDB."
        )

    # ---------------------------------------------------------
    # DATA EXPLORER HELPERS
    # ---------------------------------------------------------

    def get_data_overview(self):
        """
        Retrieves global summary metrics for the dataset.
        """
        with self.mysql_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT 
                    (SELECT COUNT(*) FROM argo_floats) as total_floats,
                    COUNT(p.profile_id) as total_profiles,
                    MIN(p.profile_time) as min_date,
                    MAX(p.profile_time) as max_date,
                    MIN(p.latitude) as min_lat,
                    MAX(p.latitude) as max_lat,
                    MIN(p.longitude) as min_lon,
                    MAX(p.longitude) as max_lon
                FROM argo_profiles p
            """)).mappings().fetchone()

            projs_rows = conn.execute(text("SELECT DISTINCT project_name FROM argo_floats")).scalars().all()
            platforms_rows = conn.execute(text("SELECT DISTINCT platform_type FROM argo_floats")).scalars().all()

            proj_vals = [p for p in projs_rows if p is not None]
            plat_vals = [p for p in platforms_rows if p is not None]

            proj_recorded = len(proj_vals) > 0 and not all(p in ('N/A', 'NA', '') for p in proj_vals)
            plat_recorded = len(plat_vals) > 0 and not all(p in ('N/A', 'NA', '') for p in plat_vals)
            
            return {
                "total_floats": int(row["total_floats"] or 0),
                "total_profiles": int(row["total_profiles"] or 0),
                "date_range": {
                    "min": row["min_date"].isoformat() if row["min_date"] else None,
                    "max": row["max_date"].isoformat() if row["max_date"] else None
                },
                "geo_bounds": {
                    "min_latitude": float(row["min_lat"]) if row["min_lat"] is not None else None,
                    "max_latitude": float(row["max_lat"]) if row["max_lat"] is not None else None,
                    "min_longitude": float(row["min_lon"]) if row["min_lon"] is not None else None,
                    "max_longitude": float(row["max_lon"]) if row["max_lon"] is not None else None
                },
                "parameters": {
                    "pressure": True,
                    "temperature": True,
                    "salinity": True,
                    "bgc": False
                },
                "projects": {
                    "recorded": proj_recorded,
                    "values": proj_vals
                },
                "platform_types": {
                    "recorded": plat_recorded,
                    "values": plat_vals
                },
                "data_mode": "raw_unadjusted",
                "qc_flags": "unavailable",
                "version": "1.0"
            }

    def get_floats_paginated(self, page=1, limit=20, search=None, sort_by="wmo_number", sort_dir="asc"):
        """
        Retrieves paginated float records with profile counts and date ranges.
        """
        allowed_sorts = {
            "wmo_number": "f.wmo_number",
            "float_id": "f.float_id",
            "profile_count": "profile_count",
            "min_time": "min_time",
            "max_time": "max_time"
        }
        order_col = allowed_sorts.get(sort_by, "f.wmo_number")
        order_dir = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        
        where_clauses = []
        params = {}
        if search:
            where_clauses.append("CAST(f.wmo_number AS CHAR) LIKE :search")
            params["search"] = f"%{search}%"
            
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        
        if order_col == "f.float_id":
            order_clause = f"f.float_id {order_dir}"
        else:
            order_clause = f"{order_col} {order_dir}, f.float_id ASC"

        with self.mysql_engine.connect() as conn:
            count_sql = f"SELECT COUNT(*) FROM argo_floats f {where_sql}"
            total = conn.execute(text(count_sql), params).scalar() or 0
            
            offset = (page - 1) * limit
            params["limit"] = limit
            params["offset"] = offset
            
            data_sql = f"""
                SELECT 
                    f.float_id,
                    f.wmo_number,
                    f.project_name,
                    f.platform_type,
                    COUNT(p.profile_id) AS profile_count,
                    MIN(p.profile_time) AS min_time,
                    MAX(p.profile_time) AS max_time
                FROM argo_floats f
                LEFT JOIN argo_profiles p ON f.float_id = p.float_id
                {where_sql}
                GROUP BY f.float_id, f.wmo_number, f.project_name, f.platform_type
                ORDER BY {order_clause}
                LIMIT :limit OFFSET :offset
            """
            rows = conn.execute(text(data_sql), params).mappings().fetchall()
            
            floats = []
            for r in rows:
                floats.append({
                    "float_id": r["float_id"],
                    "wmo_number": r["wmo_number"],
                    "project_name": r["project_name"],
                    "platform_type": r["platform_type"],
                    "profile_count": int(r["profile_count"] or 0),
                    "min_time": r["min_time"].isoformat() if r["min_time"] else None,
                    "max_time": r["max_time"].isoformat() if r["max_time"] else None
                })
                
            total_pages = (total + limit - 1) // limit if total > 0 else 0
            return {
                "floats": floats,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "total_pages": total_pages
                }
            }

    def get_float_by_wmo(self, wmo_number):
        """
        Retrieves float metadata and full profile summary for a given WMO number.
        """
        with self.mysql_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT 
                    f.float_id, f.wmo_number, f.project_name, f.platform_type,
                    COUNT(p.profile_id) as profile_count,
                    MIN(p.profile_time) as min_time, MAX(p.profile_time) as max_time,
                    MIN(p.latitude) as min_lat, MAX(p.latitude) as max_lat,
                    MIN(p.longitude) as min_lon, MAX(p.longitude) as max_lon,
                    MIN(p.cycle_number) as min_cycle, MAX(p.cycle_number) as max_cycle
                FROM argo_floats f
                LEFT JOIN argo_profiles p ON f.float_id = p.float_id
                WHERE f.wmo_number = :wmo
                GROUP BY f.float_id, f.wmo_number, f.project_name, f.platform_type
            """), {"wmo": wmo_number}).mappings().fetchone()
            
            if not row:
                return None
                
            return {
                "float_id": row["float_id"],
                "wmo_number": row["wmo_number"],
                "project_name": row["project_name"],
                "platform_type": row["platform_type"],
                "profile_count": int(row["profile_count"] or 0),
                "date_range": {
                    "min_time": row["min_time"].isoformat() if row["min_time"] else None,
                    "max_time": row["max_time"].isoformat() if row["max_time"] else None
                },
                "geo_bounds": {
                    "min_latitude": float(row["min_lat"]) if row["min_lat"] is not None else None,
                    "max_latitude": float(row["max_lat"]) if row["max_lat"] is not None else None,
                    "min_longitude": float(row["min_lon"]) if row["min_lon"] is not None else None,
                    "max_longitude": float(row["max_lon"]) if row["max_lon"] is not None else None
                },
                "cycle_range": {
                    "min_cycle": int(row["min_cycle"]) if row["min_cycle"] is not None else None,
                    "max_cycle": int(row["max_cycle"]) if row["max_cycle"] is not None else None
                }
            }

    def _build_profile_filters(self, wmo_number=None, profile_id=None, cycle_number=None,
                               start_date=None, end_date=None, min_lat=None, max_lat=None,
                               min_lon=None, max_lon=None):
        where_clauses = []
        params = {}
        if wmo_number is not None:
            where_clauses.append("f.wmo_number = :wmo")
            params["wmo"] = wmo_number
        if profile_id is not None:
            where_clauses.append("p.profile_id = :pid")
            params["pid"] = profile_id
        if cycle_number is not None:
            where_clauses.append("p.cycle_number = :cycle")
            params["cycle"] = cycle_number
        if start_date is not None:
            where_clauses.append("p.profile_time >= :start_date")
            params["start_date"] = start_date
        if end_date is not None:
            where_clauses.append("p.profile_time <= :end_date")
            params["end_date"] = end_date
        if min_lat is not None:
            where_clauses.append("p.latitude >= :min_lat")
            params["min_lat"] = min_lat
        if max_lat is not None:
            where_clauses.append("p.latitude <= :max_lat")
            params["max_lat"] = max_lat
        if min_lon is not None:
            where_clauses.append("p.longitude >= :min_lon")
            params["min_lon"] = min_lon
        if max_lon is not None:
            where_clauses.append("p.longitude <= :max_lon")
            params["max_lon"] = max_lon
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        return where_sql, params

    def get_profiles_paginated(self, page=1, limit=20, wmo_number=None, profile_id=None,
                               cycle_number=None, start_date=None, end_date=None,
                               min_lat=None, max_lat=None, min_lon=None, max_lon=None,
                               sort_by="profile_time", sort_dir="desc"):
        """
        Retrieves paginated profiles without JSON measurement arrays.
        """
        allowed_sorts = {
            "profile_time": "p.profile_time",
            "profile_id": "p.profile_id",
            "cycle_number": "p.cycle_number",
            "latitude": "p.latitude",
            "longitude": "p.longitude",
            "wmo_number": "f.wmo_number"
        }
        order_col = allowed_sorts.get(sort_by, "p.profile_time")
        order_dir = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        if order_col == "p.profile_id":
            order_clause = f"p.profile_id {order_dir}"
        else:
            order_clause = f"{order_col} {order_dir}, p.profile_id ASC"
        
        where_sql, params = self._build_profile_filters(
            wmo_number=wmo_number, profile_id=profile_id, cycle_number=cycle_number,
            start_date=start_date, end_date=end_date, min_lat=min_lat, max_lat=max_lat,
            min_lon=min_lon, max_lon=max_lon
        )
        
        with self.mysql_engine.connect() as conn:
            count_sql = f"""
                SELECT COUNT(*)
                FROM argo_profiles p
                JOIN argo_floats f ON p.float_id = f.float_id
                {where_sql}
            """
            total = conn.execute(text(count_sql), params).scalar() or 0
            
            offset = (page - 1) * limit
            params["limit"] = limit
            params["offset"] = offset
            
            data_sql = f"""
                SELECT 
                    p.profile_id,
                    p.float_id,
                    f.wmo_number,
                    p.cycle_number,
                    p.profile_time,
                    p.latitude,
                    p.longitude,
                    JSON_LENGTH(p.pressure) AS level_count
                FROM argo_profiles p
                JOIN argo_floats f ON p.float_id = f.float_id
                {where_sql}
                ORDER BY {order_clause}
                LIMIT :limit OFFSET :offset
            """
            rows = conn.execute(text(data_sql), params).mappings().fetchall()
            
            profiles = []
            for r in rows:
                profiles.append({
                    "profile_id": r["profile_id"],
                    "float_id": r["float_id"],
                    "wmo_number": r["wmo_number"],
                    "cycle_number": r["cycle_number"],
                    "profile_time": r["profile_time"].isoformat() if r["profile_time"] else None,
                    "latitude": float(r["latitude"]) if r["latitude"] is not None else None,
                    "longitude": float(r["longitude"]) if r["longitude"] is not None else None,
                    "level_count": int(r["level_count"] or 0)
                })
                
            total_pages = (total + limit - 1) // limit if total > 0 else 0
            return {
                "profiles": profiles,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "total_pages": total_pages
                }
            }

    def get_profile_by_id(self, profile_id):
        """
        Retrieves a single profile including its measurement arrays.
        """
        with self.mysql_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT 
                    p.profile_id,
                    p.float_id,
                    f.wmo_number,
                    f.project_name,
                    f.platform_type,
                    p.cycle_number,
                    p.profile_time,
                    p.latitude,
                    p.longitude,
                    p.pressure,
                    p.temperature,
                    p.salinity
                FROM argo_profiles p
                JOIN argo_floats f ON p.float_id = f.float_id
                WHERE p.profile_id = :pid
            """), {"pid": profile_id}).mappings().fetchone()
            
            if not row:
                return None
                
            p_arr = json.loads(row["pressure"]) if row["pressure"] else []
            t_arr = json.loads(row["temperature"]) if row["temperature"] else []
            s_arr = json.loads(row["salinity"]) if row["salinity"] else []
            
            return {
                "profile_id": row["profile_id"],
                "float_id": row["float_id"],
                "wmo_number": row["wmo_number"],
                "project_name": row["project_name"],
                "platform_type": row["platform_type"],
                "cycle_number": row["cycle_number"],
                "profile_time": row["profile_time"].isoformat() if row["profile_time"] else None,
                "latitude": float(row["latitude"]) if row["latitude"] is not None else None,
                "longitude": float(row["longitude"]) if row["longitude"] is not None else None,
                "level_count": len(p_arr),
                "data_annotation": "Raw, unadjusted, no QC flags",
                "qc_available": False,
                "measurements": {
                    "pressure": p_arr,
                    "temperature": t_arr,
                    "salinity": s_arr
                }
            }

    def get_map_points(self, wmo_number=None, cycle_number=None, start_date=None,
                       end_date=None, min_lat=None, max_lat=None, min_lon=None,
                       max_lon=None, cap=5000):
        """
        Retrieves lightweight coordinates for spatial plotting, capped at cap.
        """
        where_sql, params = self._build_profile_filters(
            wmo_number=wmo_number, cycle_number=cycle_number, start_date=start_date,
            end_date=end_date, min_lat=min_lat, max_lat=max_lat, min_lon=min_lon,
            max_lon=max_lon
        )
        
        with self.mysql_engine.connect() as conn:
            count_sql = f"""
                SELECT COUNT(*)
                FROM argo_profiles p
                JOIN argo_floats f ON p.float_id = f.float_id
                {where_sql}
            """
            total = conn.execute(text(count_sql), params).scalar() or 0
            
            params["query_limit"] = cap + 1
            data_sql = f"""
                SELECT 
                    p.profile_id,
                    f.wmo_number,
                    p.cycle_number,
                    p.profile_time,
                    p.latitude,
                    p.longitude
                FROM argo_profiles p
                JOIN argo_floats f ON p.float_id = f.float_id
                {where_sql}
                ORDER BY p.profile_time ASC
                LIMIT :query_limit
            """
            rows = conn.execute(text(data_sql), params).mappings().fetchall()
            
            truncated = len(rows) > cap
            selected_rows = rows[:cap]
            
            points = []
            for r in selected_rows:
                points.append({
                    "profile_id": r["profile_id"],
                    "wmo_number": r["wmo_number"],
                    "cycle_number": r["cycle_number"],
                    "profile_time": r["profile_time"].isoformat() if r["profile_time"] else None,
                    "latitude": float(r["latitude"]) if r["latitude"] is not None else None,
                    "longitude": float(r["longitude"]) if r["longitude"] is not None else None
                })
                
            return {
                "points": points,
                "count": len(points),
                "total_available": total,
                "truncated": truncated
            }