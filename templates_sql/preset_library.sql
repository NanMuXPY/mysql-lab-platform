-- 预设课程 2：图书借阅管理（book/reader/borrow）
-- 练习点：外键约束 / 多表连接 / 日期计算 / 子查询

CREATE TABLE book (
    book_id    INT AUTO_INCREMENT PRIMARY KEY,
    title      VARCHAR(100) NOT NULL,
    author     VARCHAR(50),
    category   VARCHAR(30),
    stock      INT NOT NULL DEFAULT 1
);

CREATE TABLE reader (
    reader_id  INT PRIMARY KEY,
    rname      VARCHAR(30) NOT NULL,
    phone      VARCHAR(20),
    reg_date   DATE NOT NULL
);

CREATE TABLE borrow (
    borrow_id  INT AUTO_INCREMENT PRIMARY KEY,
    book_id    INT NOT NULL,
    reader_id  INT NOT NULL,
    borrow_date DATE NOT NULL,
    return_date DATE NULL,
    FOREIGN KEY (book_id) REFERENCES book(book_id),
    FOREIGN KEY (reader_id) REFERENCES reader(reader_id)
);

INSERT INTO book (title, author, category, stock) VALUES
('数据库系统概论', '王珊', '教材', 5),
('MySQL 必知必会', 'Ben Forta', '教材', 3),
('三体', '刘慈欣', '小说', 8),
('活着', '余华', '小说', 6),
('算法导论', 'Thomas Cormen', '教材', 2);

INSERT INTO reader VALUES
(1, '张三', '13800000001', '2025-09-01'),
(2, '李四', '13800000002', '2025-09-03'),
(3, '王五', '13800000003', '2025-10-10');

INSERT INTO borrow (book_id, reader_id, borrow_date, return_date) VALUES
(1, 1, '2026-10-01', '2026-10-15'),
(1, 2, '2026-10-05', NULL),
(3, 1, '2026-10-20', NULL),
(4, 3, '2026-10-22', NULL);

-- 示例题：查询当前所有未归还的借阅记录（含书名和读者姓名）
-- SELECT b.borrow_id, bk.title, r.rname, b.borrow_date
-- FROM borrow b JOIN book bk ON b.book_id = bk.book_id
-- JOIN reader r ON b.reader_id = r.reader_id
-- WHERE b.return_date IS NULL;
