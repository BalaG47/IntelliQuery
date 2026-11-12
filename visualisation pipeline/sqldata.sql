CREATE DATABASE sales_db;
USE sales_db;

CREATE TABLE customers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(50),
    region VARCHAR(50)
);

CREATE TABLE sales (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT,
    product VARCHAR(50),
    amount DECIMAL(10,2),
    sale_date DATE,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

INSERT INTO customers (name, region) VALUES
('Alice', 'North'),
('Bob', 'South'),
('Charlie', 'East'),
('Diana', 'West');

INSERT INTO sales (customer_id, product, amount, sale_date) VALUES
(1, 'Laptop', 1200.50, '2025-01-15'),
(2, 'Phone', 800.00, '2025-02-10'),
(3, 'Tablet', 400.00, '2025-03-05'),
(4, 'Laptop', 1300.00, '2025-03-12'),
(1, 'Phone', 750.00, '2025-04-20');
