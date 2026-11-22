import os
import sys

import mysql.connector

# Credentials come from the environment; no password is hard-coded.
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_HOST = os.environ.get("DB_HOST", "localhost")

if not DB_PASSWORD:
    sys.exit(
        "Set DB_PASSWORD (and optionally DB_USER / DB_HOST) before running "
        "this script."
    )

# Connect to MySQL server
conn = mysql.connector.connect(
    host=DB_HOST,
    user=DB_USER,
    password=DB_PASSWORD
)
cursor = conn.cursor()

# Create database
cursor.execute("DROP DATABASE IF EXISTS sales_db")
cursor.execute("CREATE DATABASE sales_db")
cursor.execute("USE sales_db")

# Create tables
cursor.execute("""
CREATE TABLE customers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100),
    region VARCHAR(50)
)
""")

cursor.execute("""
CREATE TABLE orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT,
    amount DECIMAL(10,2),
    order_date DATE,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
)
""")

# Insert sample customers
cursor.executemany("""
INSERT INTO customers (name, region) VALUES (%s, %s)
""", [
    ("Alice", "North"),
    ("Bob", "South"),
    ("Charlie", "East"),
    ("Diana", "West")
])

# Insert sample orders
cursor.executemany("""
INSERT INTO orders (customer_id, amount, order_date) VALUES (%s, %s, %s)
""", [
    (1, 120.50, "2025-01-05"),
    (2, 250.00, "2025-02-10"),
    (1, 300.75, "2025-03-15"),
    (3, 450.20, "2025-04-01"),
    (4, 180.00, "2025-05-12")
])

conn.commit()
cursor.close()
conn.close()
print("✅ MySQL sample DB created: sales_db with customers & orders")
