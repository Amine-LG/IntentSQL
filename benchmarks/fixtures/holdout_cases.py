"""Source of benchmarks/suites/holdout.json: the held-out hand-written suite.

Written on 2026-10-10 before IntentSQL was ever run on clinic.sqlite, and frozen
then.  Do not tune against these cases: run them only at milestones
(docs/EVALUATION.md) and report the results, failures included.  Changing a
case requires a new suite version, never an edit that makes a failure pass.
"""
DB = "clinic.sqlite"

FEE = "A.Fee"
COST = "P.Quantity * M.UnitCost"

CASES = [
    # basic
    ("h-01", "projection", "List all departments and the floor they are on.", "SELECT DeptName, Floor FROM Department"),
    ("h-02", "comparison", "Which doctors earn more than 400000?", "SELECT FullName FROM Doctor WHERE Salary > 400000"),
    ("h-03", "text_match", "Which medications have 'ine' at the end of their name?",
     "SELECT MedName FROM Medication WHERE MedName LIKE '%ine'"),
    ("h-04", "nulls", "How many patients have no insurance provider on file?",
     "SELECT COUNT(*) FROM Patient WHERE InsuranceProvider IS NULL"),
    ("h-05", "in_list", "How many patients live in Quincy or Medford?",
     "SELECT COUNT(*) FROM Patient WHERE City IN ('Quincy', 'Medford')"),
    ("h-06", "distinct", "What different medication categories are there?", "SELECT DISTINCT Category FROM Medication"),
    ("h-07", "ordering", "Show the five most expensive medications per unit, most expensive first.",
     "SELECT MedName FROM Medication ORDER BY UnitCost DESC LIMIT 5",
     ["SELECT MedName, UnitCost FROM Medication ORDER BY UnitCost DESC LIMIT 5"], True),
    ("h-08", "between", "How many appointments had a fee between 100 and 200?",
     "SELECT COUNT(*) FROM Appointment WHERE Fee BETWEEN 100 AND 200"),
    # joins
    ("h-09", "join", "Which doctors work in Cardiology?",
     "SELECT T1.FullName FROM Doctor T1 JOIN Department T2 ON T1.DeptID = T2.DeptID WHERE T2.DeptName = 'Cardiology'"),
    ("h-10", "multi_join", "Which patients were prescribed Tamoxifen? Give their last names.",
     "SELECT DISTINCT T1.LastName FROM Patient T1 JOIN Appointment T2 ON T1.PatientID = T2.PatientID "
     "JOIN Prescription T3 ON T2.ApptID = T3.ApptID JOIN Medication T4 ON T3.MedID = T4.MedID "
     "WHERE T4.MedName = 'Tamoxifen'",
     ["SELECT T1.LastName FROM Patient T1 WHERE T1.PatientID IN (SELECT T2.PatientID FROM Appointment T2 "
      "JOIN Prescription T3 ON T2.ApptID = T3.ApptID JOIN Medication T4 ON T3.MedID = T4.MedID "
      "WHERE T4.MedName = 'Tamoxifen')"]),
    ("h-11", "self_join", "List each doctor with the name of their supervisor.",
     "SELECT T1.FullName, T2.FullName FROM Doctor T1 JOIN Doctor T2 ON T1.SupervisorID = T2.DoctorID"),
    ("h-12", "self_join", "Which doctors earn more than their supervisor?",
     "SELECT T1.FullName FROM Doctor T1 JOIN Doctor T2 ON T1.SupervisorID = T2.DoctorID WHERE T1.Salary > T2.Salary"),
    ("h-13", "outer_join", "How many appointments has each doctor had, including doctors with none?",
     "SELECT T1.FullName, COUNT(T2.ApptID) FROM Doctor T1 LEFT JOIN Appointment T2 ON T1.DoctorID = T2.DoctorID "
     "GROUP BY T1.DoctorID"),
    # aggregation
    ("h-14", "aggregate", "What is the average appointment fee for each status?",
     "SELECT Status, AVG(Fee) FROM Appointment GROUP BY Status"),
    ("h-15", "having", "Which cities have more than 18 patients?",
     "SELECT City FROM Patient GROUP BY City HAVING COUNT(*) > 18"),
    ("h-16", "count_distinct", "How many different patients saw a doctor in 2024?",
     "SELECT COUNT(DISTINCT PatientID) FROM Appointment WHERE ApptDate LIKE '2024%'"),
    ("h-17", "group_join", "What is the total salary of the doctors in each department?",
     "SELECT T2.DeptName, SUM(T1.Salary) FROM Doctor T1 JOIN Department T2 ON T1.DeptID = T2.DeptID GROUP BY T2.DeptID"),
    ("h-18", "superlative", "Which department has the most doctors?",
     "SELECT T2.DeptName FROM Doctor T1 JOIN Department T2 ON T1.DeptID = T2.DeptID GROUP BY T2.DeptID "
     "ORDER BY COUNT(*) DESC LIMIT 1"),
    ("h-19", "nested_aggregate", "What is the average number of appointments per patient?",
     "SELECT COUNT(*) * 1.0 / (SELECT COUNT(*) FROM Patient) FROM Appointment",
     ["SELECT AVG(n) FROM (SELECT COUNT(*) AS n FROM Appointment GROUP BY PatientID)"]),
    ("h-20", "percentage", "What percentage of appointments were no-shows?",
     "SELECT 100.0 * SUM(Status = 'no-show') / COUNT(*) FROM Appointment"),
    # expressions
    ("h-21", "arithmetic", "What was the total medication cost of each prescription line? Show the medication name.",
     "SELECT M.MedName, P.Quantity * M.UnitCost FROM Prescription P JOIN Medication M ON P.MedID = M.MedID"),
    ("h-22", "arithmetic", "Which medication category has the highest total prescribed cost?",
     f"SELECT M.Category FROM Prescription P JOIN Medication M ON P.MedID = M.MedID GROUP BY M.Category "
     f"ORDER BY SUM({COST}) DESC LIMIT 1",
     [f"SELECT M.Category, SUM({COST}) FROM Prescription P JOIN Medication M ON P.MedID = M.MedID "
      f"GROUP BY M.Category ORDER BY SUM({COST}) DESC LIMIT 1"]),
    ("h-23", "column_compare", "Which doctors were hired before their supervisor?",
     "SELECT T1.FullName FROM Doctor T1 JOIN Doctor T2 ON T1.SupervisorID = T2.DoctorID WHERE T1.HiredOn < T2.HiredOn"),
    # advanced
    ("h-24", "not_exists", "Which patients have never had an appointment?",
     "SELECT FirstName, LastName FROM Patient WHERE PatientID NOT IN (SELECT PatientID FROM Appointment)",
     ["SELECT LastName FROM Patient WHERE PatientID NOT IN (SELECT PatientID FROM Appointment)",
      "SELECT PatientID FROM Patient WHERE PatientID NOT IN (SELECT PatientID FROM Appointment)"]),
    ("h-25", "not_exists", "Which doctors have never had a cancelled appointment?",
     "SELECT FullName FROM Doctor WHERE DoctorID NOT IN (SELECT DoctorID FROM Appointment WHERE Status = 'cancelled')"),
    ("h-26", "scalar_subquery", "Which medications cost more per unit than the average medication?",
     "SELECT MedName FROM Medication WHERE UnitCost > (SELECT AVG(UnitCost) FROM Medication)"),
    ("h-27", "correlated", "Which doctors earn more than the average salary in their department?",
     "SELECT T1.FullName FROM Doctor T1 WHERE T1.Salary > (SELECT AVG(T2.Salary) FROM Doctor T2 "
     "WHERE T2.DeptID = T1.DeptID)"),
    ("h-28", "set_operation", "Which cities have patients insured by both Aetna and Cigna?",
     "SELECT City FROM Patient WHERE InsuranceProvider = 'Aetna' INTERSECT SELECT City FROM Patient "
     "WHERE InsuranceProvider = 'Cigna'"),
    ("h-29", "top_per_group", "Who is the highest paid doctor in each department?",
     "SELECT T1.FullName FROM Doctor T1 WHERE T1.Salary = (SELECT MAX(T2.Salary) FROM Doctor T2 WHERE T2.DeptID = T1.DeptID)",
     ["SELECT T2.DeptName, T1.FullName FROM Doctor T1 JOIN Department T2 ON T1.DeptID = T2.DeptID WHERE T1.Salary = "
      "(SELECT MAX(T3.Salary) FROM Doctor T3 WHERE T3.DeptID = T1.DeptID)"]),
    ("h-30", "universal", "Which medication categories only contain medications costing under 1 per unit?",
     "SELECT DISTINCT Category FROM Medication EXCEPT SELECT Category FROM Medication WHERE UnitCost >= 1"),
    ("h-31", "vs_subset", "Which appointments had a lower fee than every cancelled appointment? Give their ids.",
     "SELECT ApptID FROM Appointment WHERE Fee < (SELECT MIN(Fee) FROM Appointment WHERE Status = 'cancelled')"),
    # dates
    ("h-32", "date_part", "How many appointments were there in each month of 2024?",
     "SELECT STRFTIME('%Y-%m', ApptDate), COUNT(*) FROM Appointment WHERE ApptDate LIKE '2024%' GROUP BY 1"),
    ("h-33", "date_range", "How many appointments took place in the second quarter of 2023?",
     "SELECT COUNT(*) FROM Appointment WHERE ApptDate >= '2023-04-01' AND ApptDate < '2023-07-01'"),
    ("h-34", "date_year", "Which doctors were hired before 2010?",
     "SELECT FullName FROM Doctor WHERE HiredOn < '2010-01-01'"),
    ("h-35", "date_month", "How many completed appointments were there in March 2024?",
     "SELECT COUNT(*) FROM Appointment WHERE Status = 'completed' AND ApptDate LIKE '2024-03%'"),
    # semantics
    ("h-36", "semantic", "What is the average fee of a completed appointment?",
     "SELECT AVG(Fee) FROM Appointment WHERE Status = 'completed'"),
    ("h-37", "semantic", "Which doctor saw the most different patients?",
     "SELECT T1.FullName FROM Doctor T1 JOIN Appointment T2 ON T1.DoctorID = T2.DoctorID GROUP BY T1.DoctorID "
     "ORDER BY COUNT(DISTINCT T2.PatientID) DESC LIMIT 1"),
    ("h-38", "semantic", "Which doctor had the most appointments?",
     "SELECT T1.FullName FROM Doctor T1 JOIN Appointment T2 ON T1.DoctorID = T2.DoctorID GROUP BY T1.DoctorID "
     "ORDER BY COUNT(*) DESC LIMIT 1"),
    ("h-39", "semantic", "How much did each patient pay in fees in total? Show last names.",
     "SELECT T1.LastName, SUM(T2.Fee) FROM Patient T1 JOIN Appointment T2 ON T1.PatientID = T2.PatientID "
     "GROUP BY T1.PatientID"),
    # paraphrases
    ("h-40", "paraphrase", "How many doctors does each department have?",
     "SELECT T2.DeptName, COUNT(*) FROM Doctor T1 JOIN Department T2 ON T1.DeptID = T2.DeptID GROUP BY T2.DeptID",
     None, False, "HP-1"),
    ("h-41", "paraphrase", "Count the physicians in every department.",
     "SELECT T2.DeptName, COUNT(*) FROM Doctor T1 JOIN Department T2 ON T1.DeptID = T2.DeptID GROUP BY T2.DeptID",
     None, False, "HP-1"),
    ("h-42", "paraphrase", "Number of doctors per department",
     "SELECT T2.DeptName, COUNT(*) FROM Doctor T1 JOIN Department T2 ON T1.DeptID = T2.DeptID GROUP BY T2.DeptID",
     None, False, "HP-1"),
    # unsupported: must be refused
    ("h-43", "unsupported", "Cancel all appointments scheduled for next week.", None),
    ("h-44", "unsupported", "Which patients will get sick next year?", None),
    ("h-45", "unsupported", "What is the capital of France?", None),
]


def suite() -> dict:
    cases = []
    for entry in CASES:
        case_id, category, question, gold = entry[:4]
        extra = entry[4] if len(entry) > 4 else None
        ordered = entry[5] if len(entry) > 5 else False
        group = entry[6] if len(entry) > 6 else None
        golds = ([gold] if gold else []) + (extra or [])
        cases.append({"id": case_id, "database": DB, "question": question, "gold_sql": golds,
                      "ordered": bool(ordered), "expect": "refuse" if gold is None else "answer",
                      "category": category, "group": group})
    return {"suite": "holdout", "frozen": "2026-10-10",
            "description": "Held-out hand-written suite on a schema unseen during development.",
            "cases": cases}
