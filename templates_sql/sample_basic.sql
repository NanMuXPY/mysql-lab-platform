-- 实验模板：基础三表样例（student / course / score）
-- 交接文档第九节第 1 条：没有真实模板前，用这个跑通全流程
-- 注意：容器只在数据卷为空时执行本文件一次

CREATE TABLE student (
  sno      CHAR(10) PRIMARY KEY COMMENT '学号',
  sname    VARCHAR(20) NOT NULL COMMENT '姓名',
  gender   ENUM('M','F') NOT NULL DEFAULT 'M',
  birthday DATE,
  major    VARCHAR(30) COMMENT '专业'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE course (
  cno      CHAR(6) PRIMARY KEY COMMENT '课程号',
  cname    VARCHAR(40) NOT NULL COMMENT '课程名',
  credit   DECIMAL(3,1) NOT NULL DEFAULT 2.0,
  teacher  VARCHAR(20)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE score (
  sno     CHAR(10) NOT NULL,
  cno     CHAR(6) NOT NULL,
  grade   DECIMAL(5,1) CHECK (grade BETWEEN 0 AND 100),
  PRIMARY KEY (sno, cno),
  FOREIGN KEY (sno) REFERENCES student(sno),
  FOREIGN KEY (cno) REFERENCES course(cno)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO student VALUES
('240101','张三','M','2006-03-12','云计算技术应用'),
('240102','李四','F','2006-07-25','云计算技术应用'),
('240103','王五','M','2006-01-08','大数据技术');

INSERT INTO course VALUES
('C00001','MySQL数据库','3.0','陈老师'),
('C00002','Linux操作系统','2.5','林老师'),
('C00003','Python程序设计','3.0','黄老师');

INSERT INTO score VALUES
('240101','C00001',88.0),('240101','C00002',76.5),('240101','C00003',92.0),
('240102','C00001',65.0),('240102','C00002',81.0),
('240103','C00001',59.5),('240103','C00003',73.0);
