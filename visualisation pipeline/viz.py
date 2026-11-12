import mysql.connector
import chromadb
import ollama
import json
from decimal import Decimal
 
# --- MySQL connection ---
def run_mysql_query(query):
    conn = mysql.connector.connect(
            host="localhost",
            user="root",
            password="shyam123",
            database="sales_db")
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(query)
        results = cursor.fetchall()

        # Convert Decimal → float
        for row in results:
            for k, v in row.items():
                if isinstance(v, Decimal):
                    row[k] = float(v)

        return {"query": query, "results": results}
    except Exception as e:
        return {"error": str(e), "query": query}
    finally:
        cursor.close()
        conn.close()

# --- ChromaDB connection ---
client = chromadb.PersistentClient(path="./chromadb_store")
collection = client.get_collection("research_papers")

def query_chromadb(user_query):
    try:
        results = collection.query(
            query_texts=[user_query],
            n_results=2
        )
        return {"query": user_query, "result": results}
    except Exception as e:
        return {"query": user_query, "error": str(e)}

# --- Use Ollama (Gemma2 LLM) to decide ---
def ask_llm(user_prompt):
    response = ollama.chat(
        model="gemma2",
        messages=[{"role": "user", "content": f"""
You are an assistant that decides whether to query MySQL or ChromaDB.

Databases available:
1. MySQL: sales_db (tables: customers(id, name, region), orders(id, customer_id, amount, order_date))
2. ChromaDB: research_papers (text documents about AI, climate, quantum, ocean, etc.)

Task: Based on the user query, output ONLY a JSON in this format:
{{
  "db": "mysql" or "chromadb",
  "query": "SQL query or search text"
}}

User query: {user_prompt}
        """}]
    )
    return response["message"]["content"]

# --- Pipeline ---
import re

def process(user_prompt):
    llm_output = ask_llm(user_prompt)

    # --- Sanitize LLM output ---
    cleaned = llm_output.strip()

    # Remove ```json or ``` fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"```[a-zA-Z]*", "", cleaned)  # remove ```json or ```
        cleaned = cleaned.replace("```", "").strip()

    try:
        parsed = json.loads(cleaned)
    except Exception as e:
        return {"error": f"LLM did not return valid JSON: {e}", "raw": llm_output}

    if parsed["db"] == "mysql":
        return run_mysql_query(parsed["query"])
    elif parsed["db"] == "chromadb":
        return query_chromadb(parsed["query"])
    else:
        return {"error": "Unknown DB target", "raw": parsed}


# --- Test queries ---
if __name__ == "__main__":
    tests = [
        "Show me total sales by region",
        "Find papers about quantum computing"
    ]
    for t in tests:
        print(json.dumps(process(t), indent=2))
