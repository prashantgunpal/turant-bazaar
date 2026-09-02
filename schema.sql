-- Turant Bazaar — Database Schema (MySQL / Aurora MySQL)

CREATE TABLE IF NOT EXISTS vendors (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    area VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS products (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    category VARCHAR(50),
    vendor_id INT,
    image_url TEXT,
    FOREIGN KEY (vendor_id) REFERENCES vendors(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_name VARCHAR(100),
    customer_phone VARCHAR(15),
    total_amount DECIMAL(10,2),
    status VARCHAR(20) DEFAULT 'placed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT,
    product_id INT,
    quantity INT,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- Sample data
INSERT INTO vendors (name, area) VALUES
('Sharma Kirana', 'Silampuriya'),
('Mayank Sweets', 'Sardarshahar Locality'),
('Jodhpur Road Mandi', 'Jodhpur Road');

INSERT INTO products (name, price, category, vendor_id, image_url) VALUES
('Amul Doodh (500ml)', 32.00, 'Daily', 1, ''),
('Besan Laddu (250g)', 90.00, 'Mithai', 2, ''),
('Taazi Tamatar (1kg)', 28.00, 'Sabzi', 3, ''),
('Bikaneri Papad', 55.00, 'Anaj', 1, '');
