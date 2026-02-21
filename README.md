# sql-heuristic-query-optimizer

# Project 01 — Heuristic Query Optimization
CS 5300 – Database Systems (Fall 2025)


## 1. Overview

This project implements a **heuristic query optimizer** for a single-block SQL query.

Given a text file that contains:

1. **Schema definitions** (tables, attributes, primary keys, unique keys)
2. **Exactly one SQL query**

the program:

1. Parses the schema and query.
2. Builds a **canonical relational algebra query tree**.
3. Applies a sequence of **heuristic optimization rules**.
4. Prints and saves the **query tree after each major transformation step**.
5. (Extra Credit) Generates a **refined SQL query** from the final optimized tree.
6. Outputs **Graphviz DOT** and **PNG** images of each query tree.

Main file:
- `heuristic_optimizer.py`

Inputs:
- `input1.txt`
- `input2.txt`
- `input3.txt`

Outputs will be saved in the `outputs/` directory.

---

## 2. Heuristic Optimization Rules

### Rule 1 — Cascade of Selections  
Breaks `A AND B AND C` into individual selections.

### Rule 2 — Push Selections Down  
Moves selections near relevant base tables.

### Rule 3 — Apply Most Selective Filters First  
Reorders cross products according to estimated selectivity.

### Rule 4 — Replace Cross Product + Selection → Join  
Turns `σ(cond)(R × S)` into `R ⨝_cond S`.

### Rule 5 — Push Projections Down  
Removes unnecessary attributes early.

---

## 3. Requirements

- Python 3.8+
- No external libraries required
- Optional: Graphviz installed for PNG output

---

## 4. Running the Program

### Default (runs all inputs):

```
python heuristic_optimizer.py
```

### Run custom files:

```
python heuristic_optimizer.py myfile.txt
```

---

## 5. Input Format

Each `.txt` file must include:

### Schema Example:

```
Employee(
  Fname, Minit, Lname, Ssn, Salary,
  PRIMARY KEY(Ssn)
);
```

### Query Example:

```
SELECT E.Lname
FROM Employee E, Works_On W
WHERE E.Ssn = W.Essn;
```

---

## 6. Output Format

Outputs are placed in:

```
outputs/
```

Each step generates:

- `.txt` file (ASCII tree)
- `.dot` file (Graphviz source)
- `.png` file (graphical tree)
- `.sql` file (refined SQL)

Example:

```
input1_step0_canonical.txt
input1_step0_canonical.dot
input1_step0_canonical.png
input1_refined_sql.sql
```

---

## 7. Notes

- `.png` files are created only if Graphviz is installed.
- `.dot` files can be manually converted:

```
dot -Tpng file.dot -o file.png
```

---

## 8. Author

Yaswanth Podapati  
CS 5300 — Fall 2025
