-- 预设课程 4：多表连接进阶（院系/学生/选课）
-- 练习点：INNER/LEFT JOIN / 子查询 / EXISTS / 计数字段

CREATE TABLE department (
    dept_id   INT PRIMARY KEY,
    dept_name VARCHAR(30) NOT NULL
);

CREATE TABLE student (
    sid       INT PRIMARY KEY,
    sname     VARCHAR(30) NOT NULL,
    gender    CHAR(1),
    dept_id   INT,
    FOREIGN KEY (dept_id) REFERENCES department(dept_id)
);

CREATE TABLE course (
    course_id INT PRIMARY KEY,
    cname     VARCHAR(50) NOT NULL,
    credit    DECIMAL(3,1) NOT NULL
);

CREATE TABLE enrollment (
    sid       INT NOT NULL,
    course_id INT NOT NULL,
    score     DECIMAL(5,1) NULL,
    PRIMARY KEY (sid, course_id),
    FOREIGN KEY (sid) REFERENCES student(sid),
    FOREIGN KEY (course_id) REFERENCES course(course_id)
);

INSERT INTO department VALUES (1, '计算机学院'), (2, '电子信息学院'), (3, '经济学院');

INSERT INTO student VALUES
(240101, '张伟', '男', 1), (240102, '李娜', '女', 1), (240103, '王强', '男', 1),
(240201, '刘洋', '男', 2), (240202, '陈静', '女', 2),
(240301, '杨帆', '男', 3), (240302, '赵磊', '男', 3), (240303, '孙悦', '女', 3);

INSERT INTO course VALUES
(101, '数据库原理', 3.5), (102, '数据结构', 4.0), (103, '操作系统', 3.0), (104, '宏观经济学', 2.5);

INSERT INTO enrollment VALUES
(240101, 101, 88), (240101, 102, 76), (240102, 101, 92), (240102, 103, 81),
(240103, 101, NULL), (240201, 102, 85), (240202, 102, 79), (240202, 103, 88),
(240301, 104, 90), (240302, 104, 65);

-- 示例题：查询没有选任何课程的学生（LEFT JOIN / NOT EXISTS 两种写法）
-- SELECT s.sname FROM student s
-- LEFT JOIN enrollment e ON s.sid = e.sid
-- WHERE e.sid IS NULL;
