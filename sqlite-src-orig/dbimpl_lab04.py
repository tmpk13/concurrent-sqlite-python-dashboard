import argparse
import json
import multiprocessing as mp
import sqlite3
import time
import random
import os


DEFAULT_NUMLOOPS = 5
DEFAULT_AVGWAIT = 1.0
DEFAULT_DBNAME = "lab04.db"

MAXCOURSES_PER_STUDENT = 4
MAXSTUDENTS_PER_COURSE = 10
MINADD = 5
MAXADD = 10
MINDEL = 0
MAXDEL = 3

DEPARTMENTS = ["CS", "Math", "EE", "Bio", "Chem", "Phys", "Econ"]
NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace",
         "Hank", "Ivy", "Jack", "Kara", "Leo", "Mona", "Nate", "Opal"]


def log(msg):
    print(f"{time.perf_counter_ns()} {msg}")
def log(msg):
    # Wall clock time to match modified SQLITE C time
    ns = time.time_ns()
    sec, frac = divmod(ns, 1_000_000_000)
    print(f"[{sec}.{frac:09d}] {msg}")

def get_conn(dbname):
    """Create connection with busy timeout and foreign keys enabled."""
    conn = sqlite3.connect(dbname, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def initialize_database(dbname, dbreset):
    conn = sqlite3.connect(dbname)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")

    if dbreset:
        log("Resetting database schema...")
        cur.execute("DROP TABLE IF EXISTS Enrollment;")
        cur.execute("DROP TABLE IF EXISTS Student;")
        cur.execute("DROP TABLE IF EXISTS Course;")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS Student(
            sid INTEGER PRIMARY KEY,
            name TEXT,
            department TEXT,
            gpa REAL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS Course(
            cid INTEGER PRIMARY KEY,
            name TEXT,
            department TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS Enrollment(
            sid INTEGER,
            cid INTEGER,
            FOREIGN KEY (sid) REFERENCES Student(sid),
            FOREIGN KEY (cid) REFERENCES Course(cid)
        );
    """)

    # Seed some initial courses so enroll has something to work with
    cur.execute("SELECT COUNT(*) FROM Course")
    if cur.fetchone()[0] == 0:
        for i in range(5):
            dept = random.choice(DEPARTMENTS)
            cur.execute("INSERT INTO Course(name, department) VALUES (?, ?)",
                        (f"{dept}-{random.randint(100,499)}", dept))

    # Seed some initial students
    cur.execute("SELECT COUNT(*) FROM Student")
    if cur.fetchone()[0] == 0:
        for i in range(10):
            name = random.choice(NAMES) + str(random.randint(1, 999))
            dept = random.choice(DEPARTMENTS)
            gpa = round(random.uniform(2.0, 4.0), 2)
            cur.execute("INSERT INTO Student(name, department, gpa) VALUES (?, ?, ?)",
                        (name, dept, gpa))

    conn.commit()
    conn.close()


def wait(avg_wait):
    if avg_wait > 0:
        delay = random.expovariate(1.0 / avg_wait)
        time.sleep(delay)



#
# CHECK
#
def check(numloops, avg_wait, dbname):
    pid = os.getpid()
    conn = get_conn(dbname)
    cur = conn.cursor()

    log(f"[CHECK agent {pid}] starting")

    for i in range(numloops):
        wait(avg_wait)
        log(f"[CHECK {pid} begin iter {i}]")

        try:
            cur.execute("BEGIN")

            # a. No duplicate enrollments
            cur.execute("""
                SELECT sid, cid, COUNT(*) as cnt
                FROM Enrollment GROUP BY sid, cid HAVING cnt > 1
            """)
            dupes = cur.fetchall()
            if dupes:
                log(f"[CHECK {pid}] FAIL: duplicate enrollments: {dupes}")
            else:
                log(f"[CHECK {pid}] OK: no duplicate enrollments")

            # b. Students enrolled in <= MAXCOURSES_PER_STUDENT courses
            cur.execute(f"""
                SELECT sid, COUNT(*) as cnt
                FROM Enrollment GROUP BY sid HAVING cnt > {MAXCOURSES_PER_STUDENT}
            """)
            over = cur.fetchall()
            if over:
                log(f"[CHECK {pid}] FAIL: students over max courses: {over}")
            else:
                log(f"[CHECK {pid}] OK: all students within course limit")

            # c. Courses have <= MAXSTUDENTS_PER_COURSE students
            cur.execute(f"""
                SELECT cid, COUNT(*) as cnt
                FROM Enrollment GROUP BY cid HAVING cnt > {MAXSTUDENTS_PER_COURSE}
            """)
            full = cur.fetchall()
            if full:
                log(f"[CHECK {pid}] FAIL: courses over max students: {full}")
            else:
                log(f"[CHECK {pid}] OK: all courses within student limit")

            conn.commit()

        except sqlite3.OperationalError as e:
            log(f"[CHECK {pid}] error: {e}, rolling back")
            conn.rollback()

        log(f"[CHECK {pid} end iter {i}]")

    conn.close()
    log(f"[CHECK agent {pid}] completed")



#
# REPORT
#
def report(numloops, avg_wait, dbname):
    pid = os.getpid()
    conn = get_conn(dbname)
    cur = conn.cursor()

    log(f"[REPORT agent {pid}] starting")

    for i in range(numloops):
        wait(avg_wait)
        log(f"[REPORT {pid} begin iter {i}]")

        try:
            cur.execute("BEGIN")

            cur.execute("SELECT COUNT(*) FROM Student")
            num_students = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM Course")
            num_courses = cur.fetchone()[0]

            log(f"[REPORT {pid}] Students: {num_students}, Courses: {num_courses}")

            cur.execute("""
                SELECT c.cid, c.name, COUNT(e.sid) as enrolled, AVG(s.gpa) as avg_gpa
                FROM Course c
                LEFT JOIN Enrollment e ON c.cid = e.cid
                LEFT JOIN Student s ON e.sid = s.sid
                GROUP BY c.cid, c.name
            """)
            for row in cur.fetchall():
                cid, cname, enrolled, avg_gpa = row
                avg_gpa_str = f"{avg_gpa:.2f}" if avg_gpa else "N/A"
                log(f"[REPORT {pid}]   Course {cid} ({cname}): "
                    f"{enrolled} students, avg GPA: {avg_gpa_str}")

            conn.commit()

        except sqlite3.OperationalError as e:
            log(f"[REPORT {pid}] error: {e}, rolling back")
            conn.rollback()

        log(f"[REPORT {pid} end iter {i}]")

    conn.close()
    log(f"[REPORT agent {pid}] completed")



#
# ADMIT
#
def admit(numloops, avg_wait, dbname):
    pid = os.getpid()
    conn = get_conn(dbname)
    cur = conn.cursor()

    log(f"[ADMIT agent {pid}] starting")

    for i in range(numloops):
        wait(avg_wait)
        log(f"[ADMIT {pid}, begin iter {i}]")

        try:
            cur.execute("BEGIN IMMEDIATE")

            # a. Add n new students
            n = random.randint(MINADD, MAXADD)
            new_sids = []
            for _ in range(n):
                name = random.choice(NAMES) + str(random.randint(1, 9999))
                dept = random.choice(DEPARTMENTS)
                gpa = round(random.uniform(2.0, 4.0), 2)
                cur.execute(
                    "INSERT INTO Student(name, department, gpa) VALUES (?, ?, ?)",
                    (name, dept, gpa))
                new_sids.append(cur.lastrowid)

            log(f"[ADMIT {pid}] added {n} students: {new_sids}")

            # b. Delete m existing students (not the newly added ones)
            m = random.randint(MINDEL, MAXDEL)
            if m > 0:
                placeholders = ",".join("?" for _ in new_sids)
                cur.execute(
                    f"SELECT sid FROM Student WHERE sid NOT IN ({placeholders})",
                    new_sids)
                existing = [row[0] for row in cur.fetchall()]
                to_delete = random.sample(existing, min(m, len(existing)))

                for sid in to_delete:
                    cur.execute("DELETE FROM Enrollment WHERE sid = ?", (sid,))
                    cur.execute("DELETE FROM Student WHERE sid = ?", (sid,))

                log(f"[ADMIT {pid}] deleted {len(to_delete)} students: {to_delete}")

            conn.commit()

        except sqlite3.OperationalError as e:
            log(f"[ADMIT {pid}] error: {e}, rolling back")
            conn.rollback()

        log(f"[ADMIT {pid}, end iter {i}]")

    conn.close()
    log(f"[ADMIT agent {pid}] completed")



#
# ENROLL
#
def enroll(numloops, avg_wait, dbname):
    pid = os.getpid()
    conn = get_conn(dbname)
    cur = conn.cursor()

    log(f"[ENROLL agent {pid}] starting")

    for i in range(numloops):
        wait(avg_wait)
        log(f"[ENROLL {pid} begin iter {i}]")

        try:
            cur.execute("BEGIN IMMEDIATE")

            cur.execute("SELECT sid FROM Student")
            all_students = [row[0] for row in cur.fetchall()]

            cur.execute("SELECT cid FROM Course")
            all_courses = [row[0] for row in cur.fetchall()]

            if not all_students or not all_courses:
                log(f"[ENROLL {pid}] no students or courses, skipping")
                conn.commit()
                continue

            n = random.randint(1, len(all_students))
            chosen = random.sample(all_students, n)

            enrolled_count = 0
            for sid in chosen:
                cid = random.choice(all_courses)

                # Check if already enrolled in this course
                cur.execute(
                    "SELECT 1 FROM Enrollment WHERE sid = ? AND cid = ?",
                    (sid, cid))
                if cur.fetchone():
                    continue  # already in this course

                # Check student's current enrollment count
                cur.execute(
                    "SELECT COUNT(*) FROM Enrollment WHERE sid = ?", (sid,))
                stu_count = cur.fetchone()[0]

                if stu_count >= MAXCOURSES_PER_STUDENT:
                    # Drop one random enrollment
                    cur.execute(
                        "SELECT rowid, cid FROM Enrollment WHERE sid = ?",
                        (sid,))
                    enrollments = cur.fetchall()
                    drop_rowid, drop_cid = random.choice(enrollments)
                    cur.execute(
                        "DELETE FROM Enrollment WHERE rowid = ?", (drop_rowid,))
                    log(f"[ENROLL {pid}] dropped student {sid} from course {drop_cid}")

                # Check course capacity
                cur.execute(
                    "SELECT COUNT(*) FROM Enrollment WHERE cid = ?", (cid,))
                course_count = cur.fetchone()[0]

                if course_count >= MAXSTUDENTS_PER_COURSE:
                    continue  # course full, skip

                cur.execute(
                    "INSERT INTO Enrollment(sid, cid) VALUES (?, ?)",
                    (sid, cid))
                enrolled_count += 1

            log(f"[ENROLL {pid}] enrolled {enrolled_count} out of {n} chosen")
            conn.commit()

        except sqlite3.OperationalError as e:
            log(f"[ENROLL {pid}] error: {e}, rolling back")
            conn.rollback()

        log(f"[ENROLL {pid} end iter {i}]")

    conn.close()
    log(f"[ENROLL agent {pid}] completed")


#
# OFFER
#
def offer(numloops, avg_wait, dbname):
    pid = os.getpid()
    conn = get_conn(dbname)
    cur = conn.cursor()

    log(f"[OFFER agent {pid}] starting")

    for i in range(numloops):
        wait(avg_wait)
        log(f"[OFFER {pid} begin iter {i}]")

        try:
            cur.execute("BEGIN IMMEDIATE")

            # a. Add one new course
            dept = random.choice(DEPARTMENTS)
            cname = f"{dept}-{random.randint(100, 499)}"
            cur.execute(
                "INSERT INTO Course(name, department) VALUES (?, ?)",
                (cname, dept))
            new_cid = cur.lastrowid
            log(f"[OFFER {pid}] added course {new_cid} ({cname})")

            # b. Delete one existing course (not the one just added)
            cur.execute("SELECT cid FROM Course WHERE cid != ?", (new_cid,))
            existing = [row[0] for row in cur.fetchall()]

            if existing:
                del_cid = random.choice(existing)
                cur.execute(
                    "DELETE FROM Enrollment WHERE cid = ?", (del_cid,))
                cur.execute(
                    "DELETE FROM Course WHERE cid = ?", (del_cid,))
                log(f"[OFFER {pid}] deleted course {del_cid}")

            conn.commit()

        except sqlite3.OperationalError as e:
            log(f"[OFFER {pid}] error: {e}, rolling back")
            conn.rollback()

        log(f"[OFFER {pid} end iter {i}]")

    conn.close()
    log(f"[OFFER agent {pid}] completed")


def main():
    parser = argparse.ArgumentParser(description="DBimpl Lab 04")
    parser.add_argument("--dbname", type=str, default=DEFAULT_DBNAME,
                        help="Database filename")
    parser.add_argument("--no-dbreset", dest="dbreset", action="store_false",
                        help="Do NOT drop and recreate tables")
    parser.set_defaults(dbreset=True)
    parser.add_argument("--check", nargs=2, type=float)
    parser.add_argument("--report", nargs=2, type=float)
    parser.add_argument("--admit", nargs=2, type=float)
    parser.add_argument("--enroll", nargs=2, type=float)
    parser.add_argument("--offer", nargs=2, type=float)
    args = parser.parse_args()

    initialize_database(args.dbname, args.dbreset)

    operation_map = {
        "check": check,
        "report": report,
        "admit": admit,
        "enroll": enroll,
        "offer": offer,
    }

    processes = []
    proc_names = []
    for name, func in operation_map.items():
        values = getattr(args, name)
        if values is None:
            continue
        numloops = int(values[0])
        avg_wait = float(values[1])
        if numloops > 0:
            p = mp.Process(target=func, args=(numloops, avg_wait, args.dbname))
            processes.append(p)
            proc_names.append(name.upper())
            p.start()

    # Write PID -> worker name mapping for the dashboard
    pid_map = {str(p.pid): n for p, n in zip(processes, proc_names)}
    map_path = os.path.join(os.path.dirname(os.path.abspath(args.dbname)), "pid_map.json")
    with open(map_path, "w") as f:
        json.dump(pid_map, f)
    log(f"Wrote PID map: {pid_map} -> {map_path}")

    for p in processes:
        p.join()

    log("\nAll agents and operations completed.")

if __name__ == "__main__":
    main()