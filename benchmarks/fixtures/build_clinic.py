"""Build benchmarks/fixtures/clinic.sqlite for the held-out suite (benchmarks/suites/holdout.json).

A schema never used during development, with different naming conventions
(CamelCase identifiers), an undeclared self-reference (Doctor.SupervisorID),
undeclared foreign keys (Appointment.PatientID), nullable attributes and a
composite-key line table.  Deterministic.

    python benchmarks/fixtures/build_clinic.py
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).with_name("clinic.sqlite")

DDL = """
CREATE TABLE Department (DeptID INTEGER PRIMARY KEY, DeptName TEXT NOT NULL, Floor INTEGER);
CREATE TABLE Doctor (DoctorID INTEGER PRIMARY KEY, FullName TEXT NOT NULL, Specialty TEXT,
                     DeptID INTEGER REFERENCES Department(DeptID), SupervisorID INTEGER,
                     HiredOn DATE, Salary REAL);
CREATE TABLE Patient (PatientID INTEGER PRIMARY KEY, FirstName TEXT, LastName TEXT, BirthDate DATE,
                      City TEXT, InsuranceProvider TEXT);
CREATE TABLE Appointment (ApptID INTEGER PRIMARY KEY, PatientID INTEGER, DoctorID INTEGER
                          REFERENCES Doctor(DoctorID), ApptDate DATE, Status TEXT, Fee REAL);
CREATE TABLE Medication (MedID INTEGER PRIMARY KEY, MedName TEXT NOT NULL, Category TEXT, UnitCost REAL);
CREATE TABLE Prescription (ApptID INTEGER REFERENCES Appointment(ApptID),
                           MedID INTEGER REFERENCES Medication(MedID), Quantity INTEGER,
                           PRIMARY KEY (ApptID, MedID));
"""

DEPARTMENTS = ["Cardiology", "Pediatrics", "Oncology", "Neurology", "Orthopedics", "Dermatology",
               "Radiology", "Emergency"]
SPECIALTIES = {"Cardiology": ["Cardiologist", "Electrophysiologist"], "Pediatrics": ["Pediatrician"],
               "Oncology": ["Oncologist", "Hematologist"], "Neurology": ["Neurologist"],
               "Orthopedics": ["Orthopedic Surgeon", "Sports Medicine"], "Dermatology": ["Dermatologist"],
               "Radiology": ["Radiologist"], "Emergency": ["Emergency Physician"]}
FIRST = ["Maya", "Liam", "Sofia", "Noah", "Emma", "Lucas", "Chloe", "Ethan", "Zoe", "Omar", "Ines",
         "Hugo", "Lea", "Adam", "Nora", "Jules", "Clara", "Sam", "Rita", "Paul"]
LAST = ["Dubois", "Moreau", "Laurent", "Garcia", "Martin", "Bernard", "Petit", "Roux", "Fontaine",
        "Mercier", "Blanc", "Guerin", "Faure", "Andre", "Lopez", "Muller", "Henry", "Perrin"]
CITIES = ["Boston", "Cambridge", "Somerville", "Quincy", "Newton", "Brookline", "Medford"]
INSURERS = ["BlueShield", "Aetna", "Cigna", "UnitedHealth", None]
MEDS = [("Atorvastatin", "Cardiac", 0.45), ("Lisinopril", "Cardiac", 0.30), ("Metoprolol", "Cardiac", 0.38),
        ("Amoxicillin", "Antibiotic", 0.25), ("Azithromycin", "Antibiotic", 1.10), ("Cephalexin", "Antibiotic", 0.55),
        ("Ibuprofen", "Analgesic", 0.08), ("Acetaminophen", "Analgesic", 0.05), ("Tramadol", "Analgesic", 0.70),
        ("Sertraline", "Psychiatric", 0.60), ("Gabapentin", "Neurologic", 0.40), ("Levetiracetam", "Neurologic", 1.30),
        ("Methotrexate", "Oncologic", 2.40), ("Tamoxifen", "Oncologic", 1.80), ("Prednisone", "Steroid", 0.20),
        ("Hydrocortisone", "Steroid", 0.35), ("Cetirizine", "Antihistamine", 0.12), ("Loratadine", "Antihistamine", 0.15),
        ("Insulin Glargine", "Endocrine", 9.50), ("Metformin", "Endocrine", 0.10)]
STATUSES = ["completed"] * 6 + ["cancelled", "no-show", "scheduled"]


def build() -> Path:
    rng = random.Random(20261009)
    if OUT.exists():
        OUT.unlink()
    conn = sqlite3.connect(OUT)
    conn.executescript(DDL)
    for i, name in enumerate(DEPARTMENTS, start=1):
        conn.execute("INSERT INTO Department VALUES (?, ?, ?)", (i, name, rng.randint(1, 6)))
    doctors = []
    for i in range(1, 31):
        dept = (i - 1) % len(DEPARTMENTS) + 1
        name = f"Dr. {rng.choice(FIRST)} {rng.choice(LAST)}"
        supervisor = None if i <= 8 else dept          # the first doctor of each department supervises it
        hired = date(2005, 1, 1) + timedelta(days=rng.randint(0, 6800))
        salary = round(rng.uniform(150_000, 420_000), -3)
        specialty = rng.choice(SPECIALTIES[DEPARTMENTS[dept - 1]])
        doctors.append(i)
        conn.execute("INSERT INTO Doctor VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (i, name, specialty, dept, supervisor, hired.isoformat(), salary))
    for i in range(1, 121):
        birth = date(1940, 1, 1) + timedelta(days=rng.randint(0, 29000))
        conn.execute("INSERT INTO Patient VALUES (?, ?, ?, ?, ?, ?)",
                     (i, rng.choice(FIRST), rng.choice(LAST), birth.isoformat(), rng.choice(CITIES),
                      rng.choice(INSURERS)))
    for i, (name, category, cost) in enumerate(MEDS, start=1):
        conn.execute("INSERT INTO Medication VALUES (?, ?, ?, ?)", (i, name, category, cost))
    busy_doctors = doctors[:27]                       # three doctors have no appointments
    for appt in range(1, 601):
        patient = rng.randint(1, 110)                 # ten patients never had an appointment
        day = date(2023, 1, 2) + timedelta(days=rng.randint(0, 729))
        status = rng.choice(STATUSES)
        fee = round(rng.choice([80, 120, 150, 200, 250, 320]) * rng.uniform(0.9, 1.1), 2)
        conn.execute("INSERT INTO Appointment VALUES (?, ?, ?, ?, ?, ?)",
                     (appt, patient, rng.choice(busy_doctors), day.isoformat(), status, fee))
        if status == "completed":
            for med in rng.sample(range(1, len(MEDS) + 1), rng.choice([0, 1, 1, 2, 3])):
                conn.execute("INSERT INTO Prescription VALUES (?, ?, ?)", (appt, med, rng.choice([7, 10, 14, 30, 60, 90])))
    conn.commit()
    conn.close()
    return OUT


if __name__ == "__main__":
    print(build())
