-- 预设课程 1：单表查询与聚合（emp/dept 经典员工部门模型）
-- 练习点：SELECT / WHERE / ORDER BY / 聚合函数 / GROUP BY / HAVING

CREATE TABLE dept (
    deptno   INT PRIMARY KEY,
    dname    VARCHAR(30) NOT NULL,
    loc      VARCHAR(50)
);

CREATE TABLE emp (
    empno    INT PRIMARY KEY,
    ename    VARCHAR(30) NOT NULL,
    job      VARCHAR(30),
    hiredate DATE,
    salary   DECIMAL(10,2) NOT NULL,
    deptno   INT NOT NULL,
    FOREIGN KEY (deptno) REFERENCES dept(deptno)
);

INSERT INTO dept VALUES (10, '财务部', '深圳'), (20, '研发部', '广州'), (30, '销售部', '深圳'), (40, '人事部', '北京');

INSERT INTO emp VALUES
(1001, '张伟',   '经理',   '2019-03-15', 12000.00, 10),
(1002, '李娜',   '会计',   '2020-07-01',  8000.00, 10),
(1003, '王强',   '工程师', '2021-01-10', 15000.00, 20),
(1004, '刘洋',   '工程师', '2022-05-20', 14000.00, 20),
(1005, '陈静',   '实习生', '2024-09-01',  5000.00, 20),
(1006, '杨帆',   '销售',   '2020-11-11',  7500.00, 30),
(1007, '赵磊',   '销售',   '2021-08-15',  7200.00, 30),
(1008, '黄敏',   '专员',   '2023-04-01',  6800.00, 40);

-- 示例题：查询每个部门的平均工资，只显示平均工资大于 7000 的部门
-- SELECT d.dname, ROUND(AVG(e.salary),2) AS avg_salary
-- FROM emp e JOIN dept d ON e.deptno = d.deptno
-- GROUP BY d.dname HAVING avg_salary > 7000 ORDER BY avg_salary DESC;
