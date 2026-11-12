# --- Database and File Path Configurations ---

# ARGO NetCDF files directory
# IMPORTANT: Update this path to the folder where you have stored the .nc files
ARGO_DATA_DIR = 'argo_data'

# MySQL Database Connection Details
# IMPORTANT: Update these with your MySQL server details
DB_USER = 'root'
DB_PASSWORD = '1234'
DB_HOST = 'localhost'
DB_PORT = '3306'
DB_NAME = 'argo_ocean_data_fresh'

# ChromaDB Configuration
CHROMA_PERSIST_DIR = 'chroma_db_storage_fresh'
CHROMA_COLLECTION_NAME = 'argo_float_profiles'
