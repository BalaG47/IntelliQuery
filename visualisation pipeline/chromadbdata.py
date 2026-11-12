import chromadb

# Initialize ChromaDB client
client = chromadb.PersistentClient(path="./chromadb_store")

# Delete old collection if exists
try:
    client.delete_collection("research_papers")
except:
    pass

# Create collection
collection = client.create_collection("research_papers")

# Add some dummy documents
docs = [
    "AI is transforming healthcare with predictive analytics.",
    "Climate change is accelerating ice melting in Antarctica.",
    "Quantum computing promises exponential speed-ups for simulations.",
    "Ocean ecosystems are vital for carbon absorption."
]

collection.add(
    documents=docs,
    ids=[f"doc{i}" for i in range(len(docs))]
)

print("✅ ChromaDB collection 'research_papers' created with sample documents")
