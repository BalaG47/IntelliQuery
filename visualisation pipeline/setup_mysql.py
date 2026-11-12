import mysql.connector

# Connect to MySQL server
conn = mysql.connector.connect(
    host="localhost",
    user="root",
    password="shyam123"  # change if needed
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
