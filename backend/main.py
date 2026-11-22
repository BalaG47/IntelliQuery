import os
import logging
import warnings

# Import configurations and classes from other files
from config import (
    ARGO_DATA_DIR,
    ConfigurationError,
    require_database_config,
)
from database_manager import DatabaseManager
from data_processor import ArgoDataProcessor

def main():
    """
    Main execution function to orchestrate the data ingestion pipeline.
    """
    # Suppress warnings for cleaner output
    warnings.filterwarnings("ignore", category=UserWarning)
    
    # Set up logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    logging.info("Starting ARGO Data Ingestion Pipeline.")
    
    # --- Pre-run Checks ---
    if not os.path.isdir(ARGO_DATA_DIR):
        logging.error(
            "ARGO_DATA_DIR does not exist: %s", ARGO_DATA_DIR
        )
        logging.error(
            "Set ARGO_DATA_DIR in backend/.env (see backend/.env.example)."
        )
        return

    try:
        require_database_config()
    except ConfigurationError as config_error:
        logging.error(str(config_error))
        return
        
    # --- Pipeline Execution ---
    try:
        # 1. Initialize the manager for database operations
        db_manager = DatabaseManager()
        
        # 2. Prepare database tables
        db_manager.create_mysql_tables()
        
        # 3. Initialize the processor with the database manager
        processor = ArgoDataProcessor(db_manager)
        
        # 4. Start the ingestion process
        processor.process_and_ingest()
        
        logging.info("ARGO Data Ingestion Pipeline finished successfully.")

    except Exception as e:
        logging.error(f"An unexpected error occurred during the pipeline execution: {e}", exc_info=True)


if __name__ == "__main__":
    main()