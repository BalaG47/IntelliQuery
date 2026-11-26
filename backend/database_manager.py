import chromadb
import json
import logging

from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from sentence_transformers import SentenceTransformer

from config import (
    DB_USER,
    DB_PASSWORD,
    DB_HOST,
    DB_PORT,
    DB_NAME,
    CHROMA_PERSIST_DIR,
    CHROMA_COLLECTION_NAME,
    require_database_config
)


def _redact(message):
    """
    Removes the configured password from a message before it is logged.

    SQLAlchemy includes the full connection URL in some connection errors, so
    a failure to connect must never be logged verbatim.
    """

    if DB_PASSWORD and DB_PASSWORD in message:
        return message.replace(DB_PASSWORD, "***")

    return message


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

        # Fails with an explicit ConfigurationError when DB_USER, DB_PASSWORD
        # or DB_NAME is missing, instead of attempting a default credential.
        require_database_config()

        try:
            connection_url = (
                f"mysql+mysqlconnector://"
                f"{quote_plus(DB_USER)}:{quote_plus(DB_PASSWORD)}@"
                f"{DB_HOST}:{DB_PORT}/{DB_NAME}"
            )

            engine = create_engine(connection_url)

            with engine.connect():
                logging.info(
                    f"Successfully connected to MySQL database: '{DB_NAME}'"
                )

            return engine

        except Exception as e:
            logging.error(
                f"Failed to connect to MySQL: {_redact(str(e))}"
            )
            logging.error(
                "Please ensure MySQL is running and that DB_HOST, DB_PORT and "
                "DB_NAME in backend/.env are correct."
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
        cycle_number,
        direction,
        profile_time
    ):
        """
        Checks whether this logical profile already exists.

        Identity is (float_id, cycle_number, direction, profile_time). The core
        and BGC products of one physical profile share it, and one cycle may
        legitimately hold both an ascending and a descending profile.
        profile_index_in_file is provenance, never identity.
        """

        return self.get_profile_id(
            float_id, cycle_number, direction, profile_time
        ) is not None

    def get_profile_id(
        self,
        float_id,
        cycle_number,
        direction,
        profile_time
    ):
        """
        Returns the ``profile_id`` of a stored logical profile, or None.

        The caller uses the id to verify that the profile's vector exists: a
        row can be committed before its vector is written, so an existing row
        is not proof of a complete record.
        """

        with self.mysql_engine.connect() as conn:

            row = conn.execute(
                text("""
                    SELECT profile_id
                    FROM argo_profiles
                    WHERE float_id = :fid
                    AND cycle_number = :cn
                    AND direction = :dir
                    AND profile_time = :pt
                """),
                {
                    "fid": float_id,
                    "cn": cycle_number,
                    "dir": direction,
                    "pt": profile_time
                }
            ).fetchone()

        return None if row is None else int(row[0])

    # ---------------------------------------------------------
    # INSERT PROFILE
    # ---------------------------------------------------------

    # Columns the ingestion payload writes to. They are created by
    # backend/migrations/V2__add_qc_adjusted_provenance_and_bgc_columns.sql; a
    # database built only by create_mysql_tables() lacks them, and filtering the
    # payload by the visible columns would drop QC, adjusted, calibration and
    # BGC data with no error at all.
    REQUIRED_PROFILE_COLUMNS = frozenset({
        "float_id", "cycle_number", "direction", "profile_time",
        "latitude", "longitude",
        "pressure", "temperature", "salinity", "bgc_params",
        "source_file", "source_file_sha256", "profile_index_in_file",
        "source_metadata", "data_mode", "profile_pres_qc", "profile_temp_qc",
        "profile_psal_qc", "pressure_qc", "pressure_adjusted",
        "pressure_adjusted_error", "pressure_adjusted_qc", "temperature_qc",
        "temperature_adjusted", "temperature_adjusted_error",
        "temperature_adjusted_qc", "salinity_qc", "salinity_adjusted",
        "salinity_adjusted_error", "salinity_adjusted_qc", "scientific_calib",
        "parameter_data_mode", "bgc_qc", "bgc_adjusted", "bgc_adjusted_error",
        "bgc_adjusted_qc", "bgc_pressure", "bgc_source_metadata",
    })

    # One logical Argo profile is identified by float, cycle, direction and
    # observation time. The D/R records of one profile are processing variants
    # and the SD/SR records are its BGC measurements, so neither the N_PROF
    # index nor the product can be part of the identity.
    PROFILE_IDENTITY_KEY = (
        "float_id", "cycle_number", "direction", "profile_time"
    )

    #: Values of the ``product_type`` vector metadata key. They describe the
    #: stored profile content (see _build_profile_metadata), not the source
    #: product file name.
    PRODUCT_TYPE_CORE = "core"
    PRODUCT_TYPE_CORE_BGC = "core_bgc"

    _UNCACHED = object()

    def _get_profile_table_columns(self):
        """
        Returns the real argo_profiles column names (cached per manager).

        A failed inspection is raised, never replaced by a guessed fallback: the
        old fallback looked exactly like a pre-migration schema, so ingestion
        silently discarded the columns it did not list.
        """
        if getattr(self, "_cached_profile_cols", None) is None:
            try:
                with self.mysql_engine.connect() as conn:
                    rows = conn.execute(
                        text("SHOW COLUMNS FROM argo_profiles")
                    ).fetchall()
            except Exception as e:
                raise RuntimeError(
                    "Could not inspect the argo_profiles schema, so no profile "
                    f"was written: {_redact(str(e))}. Check the database "
                    "connection, then re-run ingestion."
                ) from e
            self._cached_profile_cols = {row[0] for row in rows}
        return self._cached_profile_cols

    def _get_profile_unique_keys(self):
        """
        Returns every UNIQUE index on argo_profiles as a tuple of column tuples.

        A failed inspection is raised rather than treated as "no constraint",
        because the constraints decide whether a profile can be stored at all.
        """
        cached = getattr(self, "_cached_profile_unique_keys", self._UNCACHED)
        if cached is not self._UNCACHED:
            return cached
        try:
            with self.mysql_engine.connect() as conn:
                rows = conn.execute(
                    text("SHOW INDEX FROM argo_profiles")
                ).mappings().fetchall()
        except Exception as e:
            raise RuntimeError(
                "Could not inspect the argo_profiles indexes, so no profile was "
                f"written: {_redact(str(e))}. Check the database connection, "
                "then re-run ingestion."
            ) from e

        indexes = {}
        for row in rows:
            if row["Key_name"] == "PRIMARY" or int(row["Non_unique"]) != 0:
                continue
            indexes.setdefault(row["Key_name"], []).append(
                (int(row["Seq_in_index"]), row["Column_name"])
            )
        self._cached_profile_unique_keys = tuple(
            tuple(column for _, column in sorted(columns))
            for columns in indexes.values()
        )
        return self._cached_profile_unique_keys

    def _validate_profile_schema(self, payload_keys):
        """
        Refuses to write a profile into a database that would lose its data.
        """
        columns = self._get_profile_table_columns()

        missing = sorted(self.REQUIRED_PROFILE_COLUMNS - columns)
        if missing:
            raise RuntimeError(
                "argo_profiles is missing required column(s):                 "+ ", ".join(missing)
                + ". Apply backend/migrations/"
                "V2__add_qc_adjusted_provenance_and_bgc_columns.sql, "
                "V3__profile_identity_and_optional_position.sql and "
                "V4__logical_profile_identity_and_bgc_axis.sql before "
                "ingesting; refusing to discard that data silently."
            )

        unknown = sorted(set(payload_keys) - columns)
        if unknown:
            raise RuntimeError(
                "insert_profile received field(s) that argo_profiles does not "
                "have: " + ", ".join(unknown)
                + ". Add a migration for them instead of dropping the values."
            )

        unique_keys = self._get_profile_unique_keys()
        identity = self.PROFILE_IDENTITY_KEY
        if identity not in unique_keys:
            raise RuntimeError(
                "argo_profiles has no UNIQUE KEY " + str(identity) + " (found "
                + str(unique_keys) + "). Apply backend/migrations/"
                "V4__logical_profile_identity_and_bgc_axis.sql; without it, the "
                "core and BGC products of one profile cannot share a row."
            )

        # A unique key that omits part of the identity distinguishes fewer
        # profiles than the model does, so it would reject a legitimate second
        # row (an ascending/descending pair, or a merged BGC record).
        identity_columns = set(identity)
        narrower = [
            key for key in unique_keys
            if key != identity and not identity_columns <= set(key)
        ]
        if narrower:
            raise RuntimeError(
                "argo_profiles still has a UNIQUE KEY narrower than the logical "
                "identity: " + str(narrower[0]) + ". Every unique key must "
                "include " + str(identity) + ", otherwise a legitimate profile "
                "is rejected. Drop it as described in backend/migrations/"
                "V4__logical_profile_identity_and_bgc_axis.sql."
            )

    def insert_profile(self, profile_data):
        """
        Inserts a new ARGO profile into MySQL.

        The schema is validated first: every column the payload writes must
        exist, and profile identity must be
        (float_id, cycle_number, direction, profile_time). Re-ingesting a
        profile that is already stored is prevented by the caller's existence
        check and, as a backstop, by that unique key.
        """

        logging.info("Inserting profile into MySQL...")

        self._validate_profile_schema(profile_data.keys())

        insert_keys = list(profile_data.keys())
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
        salinity,
        direction=None,
        bgc_keys=None
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

        # A profile without a GPS fix is still a real profile; say so instead of
        # printing "None" as though it were a coordinate.
        latitude_text = "unavailable (no position fix)" if lat is None else lat
        longitude_text = "unavailable (no position fix)" if lon is None else lon

        direction_text = {"A": "ascending", "D": "descending"}.get(
            direction, "not recorded")
        bgc_text = ", ".join(sorted(bgc_keys)) if bgc_keys else "none"

        document = f"""
ARGO Oceanographic Profile

Profile ID: {profile_id_db}
Float WMO ID: {float_id_db}
Cycle Number: {cycle}
Profile Direction: {direction_text}

Date and Time:
{time}

Location:
Latitude: {latitude_text}
Longitude: {longitude_text}

Depth / Pressure Measurements:
{json.dumps(pressure_values)}

Temperature Measurements:
{json.dumps(temperature_values)}

Salinity Measurements:
{json.dumps(salinity_values)}

Bio-geochemical Parameters (stored with their own pressure axis):
{bgc_text}

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
        salinity,
        direction=None
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

        document_id, document, metadata = self._build_profile_vector_payload(
            profile_id_db=profile_id_db,
            float_id_db=float_id_db,
            cycle=cycle,
            time=time,
            lat=lat,
            lon=lon,
            bgc_keys=bgc_keys,
            pressure=pressure,
            temperature=temperature,
            salinity=salinity,
            direction=direction
        )

        # -----------------------------------------------------
        # Generate embedding
        # -----------------------------------------------------

        embedding = self._embed_document(document)

        # -----------------------------------------------------
        # Store in ChromaDB
        # -----------------------------------------------------

        self.chroma_collection.add(
            ids=[document_id],

            embeddings=[embedding],

            documents=[document],

            metadatas=[metadata]
        )

        logging.info(
            f"Profile {profile_id_db} added to ChromaDB."
        )

    # ---------------------------------------------------------
    # PROFILE VECTOR PAYLOAD AND METADATA
    # ---------------------------------------------------------

    @staticmethod
    def _bgc_parameter_names(bgc_keys):
        """
        Sorted, de-duplicated BGC parameter names of one profile.

        Only parameters actually stored with the profile reach this function
        (the ingestion and reconciliation paths pass the keys of the row's
        ``bgc_params``), never the whole parameter catalogue.
        """

        if bgc_keys is None:
            return ()

        if isinstance(bgc_keys, dict):
            bgc_keys = bgc_keys.keys()

        return tuple(sorted({str(key) for key in bgc_keys if key}))

    def _build_profile_metadata(
        self,
        profile_id_db,
        float_id_db,
        cycle,
        time,
        lat,
        lon,
        bgc_keys=None,
        direction=None
    ):
        """
        Builds the ChromaDB metadata for one profile.

        ChromaDB metadata values must be scalars (``bool``, ``int``, ``float``
        or ``str``). ``bgc_parameters`` is therefore a sorted comma-separated
        string: a list is not usable in a ``where`` filter, and the names are
        still readable. ``bgc_parameter_count`` gives the number of names for
        range filters.

        ``product_type`` describes the stored profile content, not the source
        file name: ``core_bgc`` when the profile carries at least one BGC
        parameter, ``core`` when it carries none.
        """

        parameters = self._bgc_parameter_names(bgc_keys)

        metadata = {
            "profile_id_sql": int(profile_id_db),
            "float_id_sql": int(float_id_db),
            "cycle_number": int(cycle),
            "date": time.strftime("%Y-%m-%d"),
            "has_bgc": bool(parameters),
            "product_type": (
                self.PRODUCT_TYPE_CORE_BGC if parameters
                else self.PRODUCT_TYPE_CORE
            ),
            "bgc_parameters": ",".join(parameters),
            "bgc_parameter_count": len(parameters)
        }

        # ChromaDB metadata cannot store None, and 0, 0 would be an invented
        # position. A profile without a fix is recorded by the missing keys.
        if lat is not None and lon is not None:
            metadata["latitude"] = float(lat)
            metadata["longitude"] = float(lon)

        # ChromaDB metadata cannot store None. An unknown direction is omitted
        # rather than stored as a value it does not have.
        if direction in ("A", "D"):
            metadata["direction"] = direction

        return metadata

    def _build_profile_vector_payload(
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
        salinity,
        direction=None
    ):
        """
        Document id, document text and metadata for one profile.

        Deterministic: the same stored row always yields the same id, text and
        metadata, which is what lets a missing vector be rebuilt without
        creating a duplicate.
        """

        document = self._create_profile_document(
            profile_id_db=profile_id_db,
            float_id_db=float_id_db,
            cycle=cycle,
            time=time,
            lat=lat,
            lon=lon,
            pressure=pressure,
            temperature=temperature,
            salinity=salinity,
            direction=direction,
            bgc_keys=list(self._bgc_parameter_names(bgc_keys)) or None
        )

        metadata = self._build_profile_metadata(
            profile_id_db=profile_id_db,
            float_id_db=float_id_db,
            cycle=cycle,
            time=time,
            lat=lat,
            lon=lon,
            bgc_keys=bgc_keys,
            direction=direction
        )

        return f"profile_{int(profile_id_db)}", document, metadata

    def _embed_document(self, document):
        return self.embedding_model.encode(document).tolist()

    # ---------------------------------------------------------
    # VECTOR RECONCILIATION
    # ---------------------------------------------------------

    def get_profile_vector_record(self, profile_id_db):
        """
        Returns the stored values a profile's vector is rebuilt from, or None.

        Only these columns are read: the vector is derived from the committed
        row, and no measurement or identity is ever written back.
        """

        with self.mysql_engine.connect() as conn:

            row = conn.execute(
                text("""
                    SELECT p.profile_id, p.float_id, f.wmo_number,
                           p.cycle_number, p.profile_time, p.direction,
                           p.latitude, p.longitude, p.pressure, p.temperature,
                           p.salinity, p.bgc_params
                    FROM argo_profiles p
                    JOIN argo_floats f ON f.float_id = p.float_id
                    WHERE p.profile_id = :pid
                """),
                {"pid": profile_id_db}
            ).mappings().fetchone()

        if row is None:
            return None

        def decoded(value):
            # A JSON column may come back as text or already decoded.
            if value is None or value == "":
                return None

            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8")

            if isinstance(value, str):
                try:
                    return json.loads(value)
                except ValueError:
                    return None

            return value

        return {
            "profile_id": int(row["profile_id"]),
            "float_id": int(row["float_id"]),
            "wmo_number": int(row["wmo_number"]),
            "cycle_number": int(row["cycle_number"]),
            "profile_time": row["profile_time"],
            "direction": row["direction"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "pressure": decoded(row["pressure"]),
            "temperature": decoded(row["temperature"]),
            "salinity": decoded(row["salinity"]),
            "bgc_params": decoded(row["bgc_params"]) or {}
        }

    def add_profile_from_row(self, profile_id_db):
        """
        Writes the vector of a just-inserted profile from its stored row.

        The row is read back instead of reusing the ingestion payload, so the
        vector is built from exactly the values a later reconciliation reads:
        the WMO number rather than the internal float id, and the FLOAT
        position columns rather than the full-precision source coordinates.
        Without that, every reconciliation would rewrite an unchanged vector.
        """

        record = self.get_profile_vector_record(profile_id_db)

        if record is None:
            raise ValueError(
                f"profile {profile_id_db} is not stored, so its vector cannot "
                "be built from the relational record"
            )

        self.add_profile_to_chromadb(
            record["profile_id"],
            record["wmo_number"],
            record["cycle_number"],
            record["profile_time"],
            record["latitude"],
            record["longitude"],
            list(record["bgc_params"]),
            record["pressure"],
            record["temperature"],
            record["salinity"],
            direction=record["direction"]
        )

    def sync_profile_vector(self, profile_id_db):
        """
        Makes the vector of a stored profile exist and match its row.

        Returns ``unchanged``, ``recreated`` or ``updated``. The relational row
        is authoritative: the document, metadata and embedding are rebuilt from
        it. A vector that already matches is not rewritten, and the
        deterministic id keeps a repair from creating a duplicate. A failure is
        raised, so the run reports it and the next run retries.
        """

        record = self.get_profile_vector_record(profile_id_db)

        if record is None:
            raise ValueError(
                f"profile {profile_id_db} is not stored, so its vector cannot "
                "be rebuilt from the relational record"
            )

        document_id, document, metadata = self._build_profile_vector_payload(
            profile_id_db=record["profile_id"],
            float_id_db=record["wmo_number"],
            cycle=record["cycle_number"],
            time=record["profile_time"],
            lat=record["latitude"],
            lon=record["longitude"],
            bgc_keys=record["bgc_params"],
            pressure=record["pressure"],
            temperature=record["temperature"],
            salinity=record["salinity"],
            direction=record["direction"]
        )

        existing = self.chroma_collection.get(
            ids=[document_id], include=["documents", "metadatas"]
        )
        existing_ids = (existing or {}).get("ids") or []

        if not existing_ids:

            self.add_profile_from_row(profile_id_db)

            logging.info(
                f"Profile {profile_id_db}: ChromaDB vector recreated.")

            return "recreated"

        current_document = (existing.get("documents") or [None])[0]
        current_metadata = (existing.get("metadatas") or [None])[0]

        if (current_document == document
                and dict(current_metadata or {}) == metadata):
            return "unchanged"

        self.chroma_collection.update(
            ids=[document_id],
            embeddings=[self._embed_document(document)],
            documents=[document],
            metadatas=[metadata]
        )

        logging.info(f"Profile {profile_id_db}: ChromaDB vector updated.")

        return "updated"

    # ---------------------------------------------------------
    # DATA EXPLORER HELPERS
    # ---------------------------------------------------------

    def get_data_overview(self):
        """
        Retrieves global summary metrics for the dataset, including Core/BGC breakdown.
        """
        from bgc_calculations import BGC_PARAMETER_UNITS

        with self.mysql_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT 
                    (SELECT COUNT(*) FROM argo_floats) as total_floats,
                    COUNT(p.profile_id) as total_profiles,
                    SUM(CASE WHEN p.bgc_params IS NOT NULL AND JSON_LENGTH(p.bgc_params) > 0 THEN 1 ELSE 0 END) as bgc_profiles,
                    SUM(CASE WHEN p.bgc_params IS NULL OR JSON_LENGTH(p.bgc_params) = 0 THEN 1 ELSE 0 END) as core_only_profiles,
                    SUM(CASE WHEN p.direction = 'A' THEN 1 ELSE 0 END) as asc_profiles,
                    SUM(CASE WHEN p.direction = 'D' THEN 1 ELSE 0 END) as desc_profiles,
                    MIN(p.profile_time) as min_date,
                    MAX(p.profile_time) as max_date,
                    MIN(p.latitude) as min_lat,
                    MAX(p.latitude) as max_lat,
                    MIN(p.longitude) as min_lon,
                    MAX(p.longitude) as max_lon
                FROM argo_profiles p
            """)).mappings().fetchone()

            bgc_key_rows = conn.execute(text(
                "SELECT JSON_KEYS(bgc_params) AS keys_json "
                "FROM argo_profiles WHERE bgc_params IS NOT NULL AND JSON_LENGTH(bgc_params) > 0"
            )).scalars().all()

            param_counts = {}
            for raw_keys in bgc_key_rows:
                keys = json.loads(raw_keys) if isinstance(raw_keys, str) else raw_keys
                if isinstance(keys, list):
                    for k in keys:
                        param_counts[k] = param_counts.get(k, 0) + 1

            available_bgc_parameters = [
                {
                    "parameter": param,
                    "unit": BGC_PARAMETER_UNITS.get(param, "unknown"),
                    "profile_count": count,
                }
                for param, count in sorted(param_counts.items())
            ]

            projs_rows = conn.execute(text("SELECT DISTINCT project_name FROM argo_floats")).scalars().all()
            platforms_rows = conn.execute(text("SELECT DISTINCT platform_type FROM argo_floats")).scalars().all()

            proj_vals = [p for p in projs_rows if p is not None]
            plat_vals = [p for p in platforms_rows if p is not None]

            proj_recorded = len(proj_vals) > 0 and not all(p in ('N/A', 'NA', '') for p in proj_vals)
            plat_recorded = len(plat_vals) > 0 and not all(p in ('N/A', 'NA', '') for p in plat_vals)

            total_profiles = int(row["total_profiles"] or 0)
            bgc_profiles = int(row["bgc_profiles"] or 0)
            core_only_profiles = int(row["core_only_profiles"] or 0)

            return {
                "total_floats": int(row["total_floats"] or 0),
                "total_profiles": total_profiles,
                "bgc_profiles": bgc_profiles,
                "core_only_profiles": core_only_profiles,
                "direction_counts": {
                    "A": int(row["asc_profiles"] or 0),
                    "D": int(row["desc_profiles"] or 0),
                },
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
                    "bgc": bgc_profiles > 0,
                    "bgc_parameters": [item["parameter"] for item in available_bgc_parameters],
                },
                "available_bgc_parameters": available_bgc_parameters,
                "projects": {
                    "recorded": proj_recorded,
                    "values": proj_vals
                },
                "platform_types": {
                    "recorded": plat_recorded,
                    "values": plat_vals
                },
                "data_mode": "raw_and_adjusted",
                "qc_flags": "available_per_profile",
                "version": "1.1"
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
                               min_lon=None, max_lon=None, profile_type=None,
                               parameter=None, direction=None):
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
        if profile_type == "bgc":
            where_clauses.append("p.bgc_params IS NOT NULL AND JSON_LENGTH(p.bgc_params) > 0")
        elif profile_type == "core":
            where_clauses.append("(p.bgc_params IS NULL OR JSON_LENGTH(p.bgc_params) = 0)")
        if parameter is not None:
            if parameter in ("TEMP", "PSAL", "PRES"):
                col = {"TEMP": "temperature", "PSAL": "salinity", "PRES": "pressure"}[parameter]
                where_clauses.append(f"p.{col} IS NOT NULL AND JSON_LENGTH(p.{col}) > 0")
            else:
                where_clauses.append("JSON_CONTAINS_PATH(p.bgc_params, 'one', :param_json_path)")
                params["param_json_path"] = f"$.{parameter}"
        if direction is not None:
            where_clauses.append("p.direction = :direction")
            params["direction"] = direction
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        return where_sql, params

    def get_profiles_paginated(self, page=1, limit=20, wmo_number=None, profile_id=None,
                               cycle_number=None, start_date=None, end_date=None,
                               min_lat=None, max_lat=None, min_lon=None, max_lon=None,
                               profile_type=None, parameter=None, direction=None,
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
            "wmo_number": "f.wmo_number",
            "direction": "p.direction",
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
            min_lon=min_lon, max_lon=max_lon, profile_type=profile_type,
            parameter=parameter, direction=direction,
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
                    p.direction,
                    p.data_mode,
                    p.profile_time,
                    p.latitude,
                    p.longitude,
                    JSON_LENGTH(p.pressure) AS level_count,
                    JSON_KEYS(p.bgc_params) AS bgc_param_keys
                FROM argo_profiles p
                JOIN argo_floats f ON p.float_id = f.float_id
                {where_sql}
                ORDER BY {order_clause}
                LIMIT :limit OFFSET :offset
            """
            rows = conn.execute(text(data_sql), params).mappings().fetchall()
            
            profiles = []
            for r in rows:
                raw_keys = r["bgc_param_keys"]
                bgc_keys = json.loads(raw_keys) if isinstance(raw_keys, str) else (raw_keys or [])
                bgc_keys = sorted(str(k) for k in bgc_keys) if isinstance(bgc_keys, list) else []
                has_bgc = len(bgc_keys) > 0
                profiles.append({
                    "profile_id": r["profile_id"],
                    "float_id": r["float_id"],
                    "wmo_number": r["wmo_number"],
                    "cycle_number": r["cycle_number"],
                    "direction": r["direction"],
                    "data_mode": r["data_mode"],
                    "has_bgc": has_bgc,
                    "profile_type": "BGC" if has_bgc else "Core",
                    "bgc_parameters": bgc_keys,
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
        Retrieves a single profile including core and BGC measurement arrays.
        Core pressure and BGC pressure are returned separately and never merged.
        """
        from bgc_calculations import BGC_PARAMETER_UNITS, extract_valid_bgc_series

        with self.mysql_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT 
                    p.profile_id,
                    p.float_id,
                    f.wmo_number,
                    f.project_name,
                    f.platform_type,
                    p.cycle_number,
                    p.direction,
                    p.data_mode,
                    p.parameter_data_mode,
                    p.profile_pres_qc,
                    p.profile_temp_qc,
                    p.profile_psal_qc,
                    p.profile_time,
                    p.latitude,
                    p.longitude,
                    p.pressure,
                    p.pressure_qc,
                    p.temperature,
                    p.temperature_qc,
                    p.salinity,
                    p.salinity_qc,
                    p.bgc_pressure,
                    p.bgc_params,
                    p.bgc_qc,
                    p.bgc_adjusted,
                    p.bgc_adjusted_qc,
                    p.bgc_source_metadata
                FROM argo_profiles p
                JOIN argo_floats f ON p.float_id = f.float_id
                WHERE p.profile_id = :pid
            """), {"pid": profile_id}).mappings().fetchone()
            
            if not row:
                return None
                
            p_arr = json.loads(row["pressure"]) if isinstance(row["pressure"], str) else (row["pressure"] or [])
            t_arr = json.loads(row["temperature"]) if isinstance(row["temperature"], str) else (row["temperature"] or [])
            s_arr = json.loads(row["salinity"]) if isinstance(row["salinity"], str) else (row["salinity"] or [])
            p_qc = json.loads(row["pressure_qc"]) if isinstance(row["pressure_qc"], str) else (row["pressure_qc"] or [])
            t_qc = json.loads(row["temperature_qc"]) if isinstance(row["temperature_qc"], str) else (row["temperature_qc"] or [])
            s_qc = json.loads(row["salinity_qc"]) if isinstance(row["salinity_qc"], str) else (row["salinity_qc"] or [])

            bgc_pres = json.loads(row["bgc_pressure"]) if isinstance(row["bgc_pressure"], str) else (row["bgc_pressure"] or [])
            bgc_params_dict = json.loads(row["bgc_params"]) if isinstance(row["bgc_params"], str) else (row["bgc_params"] or {})
            bgc_qc_dict = json.loads(row["bgc_qc"]) if isinstance(row["bgc_qc"], str) else (row["bgc_qc"] or {})
            bgc_adj_dict = json.loads(row["bgc_adjusted"]) if isinstance(row["bgc_adjusted"], str) else (row["bgc_adjusted"] or {})
            bgc_adj_qc_dict = json.loads(row["bgc_adjusted_qc"]) if isinstance(row["bgc_adjusted_qc"], str) else (row["bgc_adjusted_qc"] or {})
            param_modes = json.loads(row["parameter_data_mode"]) if isinstance(row["parameter_data_mode"], str) else (row["parameter_data_mode"] or {})
            bgc_src_meta = json.loads(row["bgc_source_metadata"]) if isinstance(row["bgc_source_metadata"], str) else row["bgc_source_metadata"]

            bgc_parameters_detail = {}
            bgc_keys = sorted(bgc_params_dict.keys()) if isinstance(bgc_params_dict, dict) else []
            row_dict = dict(row)
            for param_key in bgc_keys:
                raw_vals = bgc_params_dict.get(param_key) or []
                _, _, counts, warnings = extract_valid_bgc_series(row_dict, param_key)
                bgc_parameters_detail[param_key] = {
                    "values": raw_vals,
                    "qc": bgc_qc_dict.get(param_key) if isinstance(bgc_qc_dict, dict) else None,
                    "adjusted": bgc_adj_dict.get(param_key) if isinstance(bgc_adj_dict, dict) else None,
                    "adjusted_qc": bgc_adj_qc_dict.get(param_key) if isinstance(bgc_adj_qc_dict, dict) else None,
                    "unit": BGC_PARAMETER_UNITS.get(param_key, "unknown"),
                    "data_mode": param_modes.get(param_key) if isinstance(param_modes, dict) else row["data_mode"],
                    "valid_count": counts["valid"],
                    "missing_count": counts["missing"],
                    "total_count": counts["total"],
                    "warnings": warnings,
                }

            has_bgc = len(bgc_keys) > 0
            available_parameters = ["PRES", "TEMP", "PSAL"] + bgc_keys
            core_qc_available = bool(p_qc or t_qc or s_qc or row["profile_temp_qc"] or row["profile_psal_qc"])
            bgc_qc_available = bool(isinstance(bgc_qc_dict, dict) and any(bgc_qc_dict.values()))

            return {
                "profile_id": row["profile_id"],
                "float_id": row["float_id"],
                "wmo_number": row["wmo_number"],
                "project_name": row["project_name"],
                "platform_type": row["platform_type"],
                "cycle_number": row["cycle_number"],
                "direction": row["direction"],
                "data_mode": row["data_mode"],
                "has_bgc": has_bgc,
                "profile_type": "BGC" if has_bgc else "Core",
                "available_parameters": available_parameters,
                "bgc_parameters": bgc_keys,
                "profile_time": row["profile_time"].isoformat() if row["profile_time"] else None,
                "latitude": float(row["latitude"]) if row["latitude"] is not None else None,
                "longitude": float(row["longitude"]) if row["longitude"] is not None else None,
                "level_count": len(p_arr),
                "bgc_level_count": len(bgc_pres) if isinstance(bgc_pres, list) else 0,
                "data_annotation": (
                    f"Data mode: {row['data_mode'] or 'R'}; "
                    "BGC pressure is independent from core pressure"
                    if has_bgc
                    else f"Data mode: {row['data_mode'] or 'R'}; Core profile"
                ),
                "qc_available": core_qc_available or bgc_qc_available,
                "core_qc": {
                    "profile_pres_qc": row["profile_pres_qc"],
                    "profile_temp_qc": row["profile_temp_qc"],
                    "profile_psal_qc": row["profile_psal_qc"],
                    "pressure_qc": p_qc,
                    "temperature_qc": t_qc,
                    "salinity_qc": s_qc,
                },
                "measurements": {
                    "pressure": p_arr,
                    "temperature": t_arr,
                    "salinity": s_arr
                },
                "bgc_measurements": {
                    "bgc_pressure": bgc_pres,
                    "parameters": bgc_parameters_detail,
                    "source_metadata": bgc_src_meta,
                } if has_bgc else None,
            }

    # ---------------------------------------------------------
    # SCIENTIFIC INSIGHTS HELPERS (Phase 4B)
    # ---------------------------------------------------------

    def get_insights_summary(self):
        """
        Returns dataset composition, float distribution, and validated BGC parameter
        statistics computed via bgc_calculations.py.
        """
        from bgc_calculations import (
            BGC_PARAMETER_DESCRIPTIONS,
            BGC_PARAMETER_UNITS,
            calculate_bgc_statistics,
        )

        overview = self.get_data_overview()

        with self.mysql_engine.connect() as conn:
            float_rows = conn.execute(text("""
                SELECT
                    f.wmo_number,
                    COUNT(p.profile_id) AS profile_count,
                    SUM(CASE WHEN p.bgc_params IS NOT NULL AND JSON_LENGTH(p.bgc_params) > 0 THEN 1 ELSE 0 END) AS bgc_profile_count,
                    SUM(CASE WHEN p.bgc_params IS NULL OR JSON_LENGTH(p.bgc_params) = 0 THEN 1 ELSE 0 END) AS core_profile_count
                FROM argo_floats f
                LEFT JOIN argo_profiles p ON f.float_id = p.float_id
                GROUP BY f.float_id, f.wmo_number
                ORDER BY profile_count DESC, f.wmo_number ASC
            """)).mappings().fetchall()

            float_distribution = [
                {
                    "wmo_number": int(r["wmo_number"]),
                    "profile_count": int(r["profile_count"] or 0),
                    "bgc_profile_count": int(r["bgc_profile_count"] or 0),
                    "core_profile_count": int(r["core_profile_count"] or 0),
                }
                for r in float_rows
            ]

            bgc_rows = conn.execute(text("""
                SELECT
                    p.profile_id,
                    f.wmo_number,
                    p.cycle_number,
                    p.direction,
                    p.data_mode,
                    p.bgc_pressure,
                    p.bgc_params,
                    p.bgc_qc,
                    p.bgc_adjusted,
                    p.bgc_adjusted_qc,
                    p.bgc_source_metadata
                FROM argo_profiles p
                JOIN argo_floats f ON p.float_id = f.float_id
                WHERE p.bgc_params IS NOT NULL AND JSON_LENGTH(p.bgc_params) > 0
            """)).mappings().fetchall()

        bgc_row_dicts = [dict(r) for r in bgc_rows]
        target_params = [
            "DOXY",
            "CHLA",
            "BBP700",
            "NITRATE",
            "PH_IN_SITU_TOTAL",
            "CHLA_FLUORESCENCE",
        ]

        parameter_stats = []
        for param_key in target_params:
            matching_rows = []
            wmo_set = set()
            for r in bgc_row_dicts:
                raw_p = r.get("bgc_params")
                parsed_p = json.loads(raw_p) if isinstance(raw_p, str) else raw_p
                if isinstance(parsed_p, dict) and param_key in parsed_p:
                    matching_rows.append(r)
                    if r.get("wmo_number") is not None:
                        wmo_set.add(r["wmo_number"])

            if not matching_rows:
                parameter_stats.append({
                    "parameter": param_key,
                    "description": BGC_PARAMETER_DESCRIPTIONS.get(param_key, param_key),
                    "unit": BGC_PARAMETER_UNITS.get(param_key, "unknown"),
                    "profile_count": 0,
                    "float_count": 0,
                    "valid_count": 0,
                    "missing_count": 0,
                    "minimum": None,
                    "maximum": None,
                    "mean": None,
                    "median": None,
                    "std_dev": None,
                    "qc_policy": "all_finite_with_qc_reported",
                    "aggregation_scope": "dataset_wide_unbinned",
                    "warnings": [f"No profiles contain {param_key}."],
                })
                continue

            calc = calculate_bgc_statistics(
                matching_rows,
                param_key,
                scope="result_set",
                aggregation_type="SUMMARY",
            )
            stat_dict = calc.to_dict()
            stat_dict["description"] = BGC_PARAMETER_DESCRIPTIONS.get(param_key, param_key)
            stat_dict["profile_count"] = len(matching_rows)
            stat_dict["float_count"] = len(wmo_set)
            stat_dict["aggregation_scope"] = "dataset_wide_unbinned"
            parameter_stats.append(stat_dict)

        return {
            "composition": {
                "total_floats": overview["total_floats"],
                "total_profiles": overview["total_profiles"],
                "bgc_profiles": overview["bgc_profiles"],
                "core_only_profiles": overview["core_only_profiles"],
                "direction_counts": overview["direction_counts"],
            },
            "float_distribution": float_distribution,
            "bgc_parameter_statistics": parameter_stats,
            "aggregation_note": (
                "Dataset-wide statistics aggregate all valid observations across matching "
                "profiles without depth binning; global means are not representative of "
                "individual profiles or specific depth layers."
            ),
        }

    def get_insights_depth_profile(self, parameter, bin_size_dbar=50, wmo_number=None):
        """
        Computes explicitly documented fixed-width pressure-binned statistics for a
        BGC parameter using bgc_pressure exclusively.
        """
        import statistics
        from bgc_calculations import (
            BGC_PARAMETER_UNITS,
            QC_POLICY_ALL_FINITE,
            extract_valid_bgc_series,
        )

        where_parts = [
            "p.bgc_params IS NOT NULL",
            "JSON_CONTAINS_PATH(p.bgc_params, 'one', :param_json_path)",
        ]
        params = {"param_json_path": f"$.{parameter}", "scan_limit": 500}
        if wmo_number is not None:
            where_parts.append("f.wmo_number = :wmo")
            params["wmo"] = wmo_number

        sql = f"""
            SELECT
                p.profile_id,
                f.wmo_number,
                p.bgc_pressure,
                p.bgc_params,
                p.bgc_qc,
                p.bgc_source_metadata
            FROM argo_profiles p
            JOIN argo_floats f ON p.float_id = f.float_id
            WHERE {' AND '.join(where_parts)}
            ORDER BY p.profile_id ASC
            LIMIT :scan_limit
        """

        with self.mysql_engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().fetchall()

        bins = {}
        bin_profiles = {}
        profiles_with_valid_pressure = 0
        warnings = []

        for r in rows:
            row_dict = dict(r)
            vals, pressures, _, row_warnings = extract_valid_bgc_series(row_dict, parameter)
            for w in row_warnings:
                if w not in warnings:
                    warnings.append(w)

            any_paired = False
            for val, pres in zip(vals, pressures):
                if pres is None or pres < 0:
                    continue
                any_paired = True
                bin_idx = int(pres // bin_size_dbar)
                bins.setdefault(bin_idx, []).append(val)
                bin_profiles.setdefault(bin_idx, set()).add(row_dict["profile_id"])
            if any_paired:
                profiles_with_valid_pressure += 1

        binned_levels = []
        for bin_idx in sorted(bins.keys()):
            b_vals = bins[bin_idx]
            b_start = bin_idx * bin_size_dbar
            b_end = b_start + bin_size_dbar
            binned_levels.append({
                "bin_start_dbar": b_start,
                "bin_end_dbar": b_end,
                "bin_mid_dbar": b_start + (bin_size_dbar / 2.0),
                "observation_count": len(b_vals),
                "profile_count": len(bin_profiles[bin_idx]),
                "mean": round(statistics.fmean(b_vals), 4),
                "minimum": round(min(b_vals), 4),
                "maximum": round(max(b_vals), 4),
            })

        return {
            "parameter": parameter,
            "unit": BGC_PARAMETER_UNITS.get(parameter, "unknown"),
            "wmo_number": wmo_number,
            "bin_size_dbar": bin_size_dbar,
            "binning_method": "fixed_width_pressure_binning",
            "pressure_axis": "bgc_pressure",
            "qc_policy": QC_POLICY_ALL_FINITE,
            "profiles_scanned": len(rows),
            "profiles_with_valid_data": profiles_with_valid_pressure,
            "bins": binned_levels,
            "warnings": warnings,
        }

    def get_insights_compare(self, profile_ids, parameter):
        """
        Retrieves separate, unmerged profile series for side-by-side comparison.
        Uses bgc_pressure for BGC parameters and core pressure for TEMP/PSAL.
        """
        from bgc_calculations import (
            BGC_PARAMETER_UNITS,
            calculate_bgc_statistics,
            extract_valid_bgc_series,
        )

        is_core = parameter in ("TEMP", "PSAL")
        unit = {"TEMP": "°C", "PSAL": "PSU"}.get(
            parameter, BGC_PARAMETER_UNITS.get(parameter, "unknown")
        )
        pressure_axis = "pressure" if is_core else "bgc_pressure"

        placeholders = ", ".join(f":pid_{i}" for i in range(len(profile_ids)))
        bind_params = {f"pid_{i}": pid for i, pid in enumerate(profile_ids)}

        sql = f"""
            SELECT
                p.profile_id,
                f.wmo_number,
                p.cycle_number,
                p.direction,
                p.data_mode,
                p.profile_time,
                p.pressure,
                p.temperature,
                p.salinity,
                p.bgc_pressure,
                p.bgc_params,
                p.bgc_qc,
                p.bgc_source_metadata
            FROM argo_profiles p
            JOIN argo_floats f ON p.float_id = f.float_id
            WHERE p.profile_id IN ({placeholders})
            ORDER BY p.profile_id ASC
        """

        with self.mysql_engine.connect() as conn:
            rows = conn.execute(text(sql), bind_params).mappings().fetchall()

        rows_by_id = {r["profile_id"]: dict(r) for r in rows}
        series_list = []

        for pid in profile_ids:
            row = rows_by_id.get(pid)
            if not row:
                series_list.append({
                    "profile_id": pid,
                    "available": False,
                    "reason": f"Profile {pid} not found.",
                })
                continue

            if is_core:
                p_arr = json.loads(row["pressure"]) if isinstance(row["pressure"], str) else (row["pressure"] or [])
                col = "temperature" if parameter == "TEMP" else "salinity"
                v_arr = json.loads(row[col]) if isinstance(row[col], str) else (row[col] or [])
                paired_v, paired_p = [], []
                missing_cnt = 0
                for v, p in zip(v_arr, p_arr):
                    if v is None or p is None:
                        missing_cnt += 1
                        continue
                    paired_v.append(float(v))
                    paired_p.append(float(p))
                series_list.append({
                    "profile_id": pid,
                    "wmo_number": row["wmo_number"],
                    "cycle_number": row["cycle_number"],
                    "direction": row["direction"],
                    "profile_time": row["profile_time"].isoformat() if row["profile_time"] else None,
                    "available": len(paired_v) > 0,
                    "reason": None if paired_v else f"No valid {parameter} values in profile {pid}.",
                    "unit": unit,
                    "pressure_axis": pressure_axis,
                    "values": paired_v,
                    "pressures": paired_p,
                    "valid_count": len(paired_v),
                    "missing_count": missing_cnt,
                    "warnings": [],
                })
            else:
                vals, pressures, counts, warnings = extract_valid_bgc_series(row, parameter)
                calc = calculate_bgc_statistics([row], parameter)
                paired_v, paired_p = [], []
                for v, p in zip(vals, pressures):
                    if p is not None:
                        paired_v.append(v)
                        paired_p.append(p)
                available = len(paired_v) > 0
                reason = None
                if not available:
                    reason = (
                        f"{parameter} is not available with valid bgc_pressure in profile {pid}."
                    )
                series_list.append({
                    "profile_id": pid,
                    "wmo_number": row["wmo_number"],
                    "cycle_number": row["cycle_number"],
                    "direction": row["direction"],
                    "profile_time": row["profile_time"].isoformat() if row["profile_time"] else None,
                    "available": available,
                    "reason": reason,
                    "unit": unit,
                    "pressure_axis": pressure_axis,
                    "values": paired_v,
                    "pressures": paired_p,
                    "valid_count": counts["valid"],
                    "missing_count": counts["missing"],
                    "stats": calc.to_dict() if counts["valid"] > 0 else None,
                    "warnings": warnings,
                })

        return {
            "parameter": parameter,
            "unit": unit,
            "pressure_axis": pressure_axis,
            "grid_note": (
                "Each profile is plotted on its own native pressure grid; "
                "no cross-profile interpolation or grid merging is performed."
            ),
            "profiles": series_list,
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