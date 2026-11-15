import logging
import os
import werkzeug

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_restful import Resource, Api, reqparse

from database_manager import DatabaseManager
from data_processor import ArgoDataProcessor
from llmbackend import ArgoRAG, ChatRetrievalError
from explorer_api import create_explorer_blueprint


NC_STORAGE_PATH = "data"

app = Flask(__name__)
api = Api(app)
CORS(app)

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
# NetCDF file upload endpoint
# ---------------------------------------------------------

class NetCDFFileList(Resource):

    def post(self):
        """
        Receives and stores uploaded NetCDF files.
        """

        parser = reqparse.RequestParser()

        parser.add_argument(
            'files',
            type=werkzeug.datastructures.FileStorage,
            location='files',
            action='append'
        )

        args = parser.parse_args()

        uploaded_files = args.get('files')

        if not uploaded_files:
            return {
                'message': 'No files found in the request.'
            }, 400

        results = []

        for file in uploaded_files:

            filename = werkzeug.utils.secure_filename(
                file.filename
            )

            # Validate file type
            if not filename.endswith('.nc'):

                results.append({
                    "filename": filename,
                    "status": "error",
                    "detail": "Invalid file type. Only .nc files are accepted."
                })

                continue

            file_path = os.path.join(
                NC_STORAGE_PATH,
                filename
            )

            try:

                file.save(file_path)
                ingestion_result = data_processor.process_file(file_path)

                if ingestion_result["error"]:
                    raise RuntimeError(ingestion_result["error"])

                results.append({
                    "filename": filename,
                    "status": "success",
                    "stored_path": file_path,
                    "profiles_inserted": ingestion_result["inserted"],
                    "profiles_already_present": ingestion_result["skipped"],
                })

            except Exception as e:

                results.append({
                    "filename": filename,
                    "status": "error",
                    "detail": f"An error occurred during save: {str(e)}"
                })

        return {
            "upload_summary": results
        }, 201


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

        # Run the actual RAG pipeline
        response, context_data = (
            rag_chatbot.answer_question(user_query)
        )

        return jsonify({
            "response": response,
            "data": context_data
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

    app.run(
        debug=True,
        port=5000,
        host="0.0.0.0"
    )
