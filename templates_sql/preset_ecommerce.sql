-- 预设课程 3：电商订单分析（customer/product/orders）
-- 练习点：多表 JOIN / 金额汇总 / TOP-N 查询

CREATE TABLE customer (
    cid       INT PRIMARY KEY,
    cname     VARCHAR(30) NOT NULL,
    city      VARCHAR(30)
);

CREATE TABLE product (
    pid       INT PRIMARY KEY,
    pname     VARCHAR(50) NOT NULL,
    price     DECIMAL(10,2) NOT NULL,
    stock     INT NOT NULL DEFAULT 0
);

CREATE TABLE orders (
    oid       INT AUTO_INCREMENT PRIMARY KEY,
    cid       INT NOT NULL,
    pid       INT NOT NULL,
    qty       INT NOT NULL,
    order_date DATE NOT NULL,
    FOREIGN KEY (cid) REFERENCES customer(cid),
    FOREIGN KEY (pid) REFERENCES product(pid)
);

INSERT INTO customer VALUES
(1, '王小明', '深圳'), (2, '刘小红', '广州'), (3, '陈小刚', '深圳'),
(4, '周小丽', '上海'), (5, '吴小强', '北京');

INSERT INTO product VALUES
(1, '机械键盘',   329.00, 50),
(2, '无线鼠标',   129.00, 120),
(3, '显示器27寸', 1099.00, 30),
(4, 'USB-C 扩展坞', 199.00, 80),
(5, '笔记本支架',  89.00, 100);

INSERT INTO orders (cid, pid, qty, order_date) VALUES
(1, 1, 1, '2026-10-01'),
(1, 2, 2, '2026-10-01'),
(2, 3, 1, '2026-10-03'),
(3, 4, 1, '2026-10-05'),
(3, 5, 2, '2026-10-05'),
(4, 3, 2, '2026-10-10'),
(5, 2, 1, '2026-10-12'),
(1, 3, 1, '2026-10-15'),
(2, 1, 1, '2026-10-18'),
(4, 5, 3, '2026-10-20');

-- 示例题：查询消费金额最高的前 3 名客户及其消费总额
-- SELECT c.cname, SUM(o.qty * p.price) AS total
-- FROM orders o JOIN customer c ON o.cid = c.cid
-- JOIN product p ON o.pid = p.pid
-- GROUP BY c.cname ORDER BY total DESC LIMIT 3;
