import logging
import os
import werkzeug

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_restful import Resource, Api, reqparse
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from database_manager import DatabaseManager
from data_processor import ArgoDataProcessor
from conversation_context import extract_context, merge_followup
from llmbackend import ArgoRAG, ChatRetrievalError
from explorer_api import create_explorer_blueprint


# ---------------------------------------------------------
# Upload configuration
# ---------------------------------------------------------

def _env_int(name, default):
    """Reads an integer environment variable without crashing on bad input."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Folder that received NetCDF files are written to. A relative path is
# resolved against the working directory the server is started from.
NC_STORAGE_PATH = os.environ.get("NC_UPLOAD_DIR", "data")

# Total request size limit for uploads. Flask refuses anything larger with a
# 413 response instead of buffering an unbounded body.
MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 32)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

ALLOWED_UPLOAD_EXTENSIONS = (".nc",)

# Classic NetCDF (versions 1, 2 and 5) starts with a "CDF" magic number;
# NetCDF-4 files are HDF5 containers.
NETCDF_SIGNATURES = (
    b"CDF\x01",
    b"CDF\x02",
    b"CDF\x05",
    b"\x89HDF\r\n\x1a\n",
)

# The application records routing and retrieval decisions with
# ``logging.info``. Without a configured level those records are dropped, so
# the routing trail is enabled here (Phase 4B).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = Flask(__name__)
api = Api(app)
CORS(app)

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# ---------------------------------------------------------
# Ensure NetCDF storage directory exists
# ---------------------------------------------------------

os.makedirs(NC_STORAGE_PATH, exist_ok=True)


# ---------------------------------------------------------
# Initialize database and RAG backend
# ---------------------------------------------------------

db_manager = DatabaseManager()
db_manager.create_mysql_tables()
data_processor = ArgoDataProcessor(db_manager)
rag_chatbot = ArgoRAG(db_manager)

# ---------------------------------------------------------
# Register Data Explorer API
# ---------------------------------------------------------

app.register_blueprint(
    create_explorer_blueprint(db_manager),
    url_prefix="/api/data"
)


# ---------------------------------------------------------
# Upload validation helpers
# ---------------------------------------------------------

def _looks_like_netcdf(stream):
    """
    Checks the first bytes of an upload for a NetCDF/HDF5 signature.

    A renamed file (for example a .txt or an archive renamed to .nc) is
    rejected before it reaches the ingestion pipeline.
    """
    header = stream.read(8)
    stream.seek(0)
    return any(header.startswith(signature) for signature in NETCDF_SIGNATURES)


def _unique_upload_path(filename):
    """
    Returns a path inside NC_STORAGE_PATH that does not already exist.

    A repeated upload of the same name is stored as ``name_1.nc``,
    ``name_2.nc``, ... so an earlier file is never overwritten.
    """
    base, extension = os.path.splitext(filename)
    candidate = os.path.join(NC_STORAGE_PATH, filename)
    counter = 1

    while os.path.exists(candidate):
        candidate = os.path.join(NC_STORAGE_PATH, f"{base}_{counter}{extension}")
        counter += 1

    return candidate


# ---------------------------------------------------------
# NetCDF file upload endpoint
# ---------------------------------------------------------

class NetCDFFileList(Resource):

    def post(self):
        """
        Receives and stores uploaded NetCDF files.

        Each file is validated before it is saved: usable filename, .nc
        extension and a NetCDF/HDF5 file signature. Files that pass are stored
        under a unique name, so an upload can never overwrite an existing file.
        The response status is 201 only when every file was ingested, 400 when
        none was, and 207 for a mixed result.
        """

        parser = reqparse.RequestParser()

        parser.add_argument(
            'files',
            type=werkzeug.datastructures.FileStorage,
            location='files',
            action='append'
        )

        args = parser.parse_args()

        uploaded_files = [
            file for file in (args.get('files') or [])
            if file is not None and file.filename
        ]

        if not uploaded_files:

            return {
                'error': 'No files found in the request. '
                         'Attach at least one NetCDF (.nc) file.'
            }, 400

        results = []

        for file in uploaded_files:

            filename = secure_filename(
                file.filename
            )

            if not filename:

                results.append({
                    "filename": str(file.filename),
                    "status": "error",
                    "detail": "The uploaded file name is not usable."
                })

                continue

            # Validate file type
            if not filename.lower().endswith(ALLOWED_UPLOAD_EXTENSIONS):

                results.append({
                    "filename": filename,
                    "status": "error",
                    "detail": "Invalid file type. Only .nc files are accepted."
                })

                continue

            # Validate file content before anything is written to disk
            if not _looks_like_netcdf(file.stream):

                results.append({
                    "filename": filename,
                    "status": "error",
                    "detail": "The file is not a NetCDF file "
                              "(unrecognised file signature)."
                })

                continue

            # Never overwrite a previously uploaded file of the same name
            file_path = _unique_upload_path(filename)

            try:

                file.save(file_path)
                ingestion_result = data_processor.process_file(file_path)

                if ingestion_result["error"]:
                    raise RuntimeError(ingestion_result["error"])

                results.append({
                    "filename": os.path.basename(file_path),
                    "status": "success",
                    "profiles_inserted": ingestion_result["inserted"],
                    "profiles_already_present": ingestion_result["skipped"],
                })

            except Exception:

                # Full detail (including the database error) stays in the
                # server log. The client receives no paths, SQL or credentials.
                logging.exception(
                    "Failed to ingest uploaded file: %s", filename
                )

                results.append({
                    "filename": os.path.basename(file_path),
                    "status": "error",
                    "detail": "The file could not be processed and was not "
                              "added to the dataset."
                })

        ingested = sum(1 for entry in results if entry["status"] == "success")

        if ingested == 0:
            status_code = 400
        elif ingested == len(results):
            status_code = 201
        else:
            status_code = 207

        return {
            "upload_summary": results,
            "files_received": len(results),
            "files_ingested": ingested,
        }, status_code

# ---------------------------------------------------------
# Upload size limit response
# ---------------------------------------------------------

@app.errorhandler(RequestEntityTooLarge)
def handle_upload_too_large(error):

    return jsonify({
        "error": "Upload is too large. The maximum request size is "
                 f"{MAX_UPLOAD_MB} MB."
    }), 413


# ---------------------------------------------------------
# Register /files endpoint
# ---------------------------------------------------------

api.add_resource(
    NetCDFFileList,
    '/files'
)


# ---------------------------------------------------------
# RAG question endpoint
# ---------------------------------------------------------

@app.route('/get-question', methods=['POST'])
def query():

    try:

        data = request.get_json()

        if not data:
            return jsonify({
                "error": "Request body is empty."
            }), 400

        user_query = data.get("prompt")

        if not user_query:
            return jsonify({
                "error": "No prompt was provided."
            }), 400

        # Phase 4D: a validated filter object from the previous structured
        # answer lets a follow-up replace or add a constraint.  The merged
        # question is parsed and validated again, so context can never inject
        # SQL or bypass the supported-query boundary.
        followup = merge_followup(data.get("context"), user_query)

        if followup is not None and followup.clarification:

            return jsonify({
                "response": followup.clarification,
                "data": [],
                "context": data.get("context")
            })

        effective_query = (
            followup.question
            if followup is not None and followup.question
            else user_query
        )

        # Run the actual RAG pipeline
        response, context_data = (
            rag_chatbot.answer_question(effective_query)
        )

        # Only structured filters survive to the next turn; a semantic or
        # conversational answer resets the context instead of leaking it.
        return jsonify({
            "response": response,
            "data": context_data,
            "context": extract_context(effective_query)
        })

    except ChatRetrievalError as e:

        logging.error(f"Chat retrieval failed: {e}")

        return jsonify({
            "error": "The ARGO data service could not complete this query. "
                     "This is a retrieval failure, not an empty result; "
                     "please try again."
        }), 503

    except Exception:

        # Full detail is logged server-side only. SQL, credentials and
        # internal paths are never returned to the browser.
        logging.exception("Chat query failed")

        return jsonify({
            "error": "The question could not be processed due to an "
                     "internal error."
        }), 500


# ---------------------------------------------------------
# Start Flask server
# ---------------------------------------------------------

if __name__ == '__main__':

    # Debug mode is off unless it is explicitly requested for local
    # development with FLASK_DEBUG=1. It must never be enabled for a
    # demonstration or any shared deployment, because the Werkzeug debugger
    # allows arbitrary code execution from the browser.
    DEBUG_ENABLED = os.environ.get("FLASK_DEBUG", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )

    if DEBUG_ENABLED:

        logging.warning(
            "Flask debug mode is ENABLED (FLASK_DEBUG=%s). "
            "Local development only.",
            os.environ.get("FLASK_DEBUG"),
        )

    app.run(
        debug=DEBUG_ENABLED,
        port=_env_int("FLASK_PORT", 5000),
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        use_reloader=DEBUG_ENABLED,
    )
