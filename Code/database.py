import re
import sys
from copy import deepcopy

# ============================================================
# FAST PRECOMPILED REGEXES
# ============================================================
# These are reused everywhere — precompile once for efficiency.
_SPACE_RE = re.compile(r"\s+")
_QUALIFIED_ATTR_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")
_SCHEMA_LINE_RE = re.compile(r"([A-Za-z_]\w*)\s*\((.*)\)\s*;?\s*$", re.IGNORECASE)


# ============================================================
# BASIC STRING UTILITIES
# ============================================================
def normalize_space(s: str):
    """Convert any messy SQL whitespace into single-spaced clean text."""
    return _SPACE_RE.sub(" ", s.strip())


def split_and(expr: str):
    """
    Split a WHERE clause by top-level ANDs.
    Key point: respects parentheses → so (A AND B) AND C works correctly.
    """
    expr = expr.strip()
    if not expr:
        return []
    parts, buf, depth = [], [], 0
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        # Track parentheses depth to avoid splitting inside (...) blocks
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)

        # Only split AND when depth == 0 (not inside parentheses)
        if depth == 0 and expr[i:i + 3].upper() == "AND" and \
           (i == 0 or not expr[i - 1].isalnum()) and \
           (i + 3 == n or not expr[i + 3].isalnum()):
            parts.append(normalize_space("".join(buf)))
            buf = []
            i += 3
            continue

        buf.append(ch)
        i += 1

    if buf:
        parts.append(normalize_space("".join(buf)))
    return [p for p in parts if p]


def extract_qualified_attrs(expr: str):
    """Return list of alias.attr pairs appearing in the expression."""
    if not expr:
        return []
    return _QUALIFIED_ATTR_RE.findall(expr)


# ============================================================
# INPUT PARSING (SCHEMA + SQL)
# ============================================================
def load_input_file(path: str):
    """
    Load the input .txt file:
    - First section: schema lines
    - Last line(s): SQL query
    Comments starting with '--' are ignored.
    """
    with open(path, "r") as f:
        lines = [ln.rstrip("\n") for ln in f]

    lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("--")]

    # Find first SELECT — that marks beginning of SQL query
    i = 0
    while i < len(lines) and not lines[i].strip().upper().startswith("SELECT"):
        i += 1
    return lines[:i], " ".join(lines[i:])


def parse_schema(schema_lines):
    """
    Very small schema parser.
    Extracts:
      - attributes
      - PRIMARY KEY columns
      - UNIQUE groups
    """
    schema = {}
    for ln in schema_lines:
        ln = ln.strip()
        if not ln:
            continue

        m = _SCHEMA_LINE_RE.match(ln)
        if not m:
            continue

        rel = m.group(1).upper()
        body = m.group(2)
        parts = [p.strip() for p in body.split(",") if p.strip()]

        attrs, pk, uniques = [], [], []
        for p in parts:
            up = p.upper()
            if up.startswith("PRIMARY KEY"):
                cols = re.search(r"\((.*)\)", p).group(1)
                pk = [c.strip() for c in cols.split(",")]
            elif up.startswith("UNIQUE"):
                cols = re.search(r"\((.*)\)", p).group(1)
                uniques.append([c.strip() for c in cols.split(",")])
            else:
                attrs.append(p.split()[0])  # attribute name only

        schema[rel] = {"attrs": attrs, "pk": pk, "unique": uniques}
    return schema


# ============================================================
# SQL QUERY CLAUSE EXTRACTION
# ============================================================
def parse_sql(query: str):
    """
    Extract SELECT, FROM, WHERE, GROUP BY, HAVING, ORDER BY.
    Uses regex slicing — clean and reliable for this project.
    """
    q = normalize_space(query.strip().rstrip(";").replace("\n", " "))

    def grab(pattern):
        m = re.search(pattern, q, re.IGNORECASE | re.DOTALL)
        return normalize_space(m.group(1)) if m else None

    return {
        "SELECT": grab(r"SELECT(.*?)FROM") or "",
        "FROM": grab(r"FROM(.*?)(WHERE|GROUP BY|HAVING|ORDER BY|$)") or "",
        "WHERE": grab(r"WHERE(.*?)(GROUP BY|HAVING|ORDER BY|$)"),
        "GROUPBY": grab(r"GROUP BY(.*?)(HAVING|ORDER BY|$)"),
        "HAVING": grab(r"HAVING(.*?)(ORDER BY|$)"),
        "ORDERBY": grab(r"ORDER BY(.*)$"),
    }


def attach_order_by(tree, parsed):
    """Wrap ORDER BY on top of final tree."""
    order_clause = parsed.get("ORDERBY")
    return {"type": "ORDER_BY", "attributes": order_clause, "child": tree} if order_clause else tree


# ============================================================
# FROM → CANONICAL BASE TREE
# ============================================================
# We separately parse:
#   - Comma joins  → × tree
#   - INNER JOIN chain (Example 2)
#   - LEFT OUTER JOIN (Example 4)
# Each returned as a tree of REL, ×, ⨝, or ⟕ nodes.

def parse_left_outer_join(from_clause: str):
    """Match:  A X LEFT OUTER JOIN B Y ON condition"""
    pattern = re.compile(
        r"""
        (?P<lbase>[A-Za-z_]\w*)\s+
        (?:(?:AS\s+)?(?P<lalias>[A-Za-z_]\w*))\s+
        LEFT\s+OUTER\s+JOIN\s+
        (?P<rbase>[A-Za-z_]\w*)\s+
        (?:(?:AS\s+)?(?P<ralias>[A-Za-z_]\w*))\s+
        ON\s+
        (?P<on>.+)
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    m = pattern.fullmatch(from_clause.strip())
    if not m:
        return None
    return (
        m.group("lbase"),
        m.group("lalias"),
        m.group("rbase"),
        m.group("ralias"),
        normalize_space(m.group("on")),
    )


def parse_inner_join_chain(from_clause: str):
    """
    This recognises only the 3-way INNER JOIN chain pattern used in Example 2.
    Not a full SQL join parser — intentionally simple for the project.
    """
    pattern = re.compile(
        r"""
        (?P<b1>[A-Za-z_]\w*)\s+(?P<a1>[A-Za-z_]\w*)\s+
        INNER\s+JOIN\s+
        (?P<b2>[A-Za-z_]\w*)\s+(?P<a2>[A-Za-z_]\w*)\s+
        ON\s+(?P<on1>.+?)\s+
        INNER\s+JOIN\s+
        (?P<b3>[A-Za-z_]\w*)\s+(?P<a3>[A-Za-z_]\w*)\s+
        ON\s+(?P<on2>.+)
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    m = pattern.fullmatch(from_clause.strip())
    if not m:
        return None
    bases = [(m.group("b1"), m.group("a1")),
             (m.group("b2"), m.group("a2")),
             (m.group("b3"), m.group("a3"))]
    ons = [normalize_space(m.group("on1")), normalize_space(m.group("on2"))]
    return bases, ons


def parse_from_items(from_clause: str):
    """Extract (base, alias) pairs for any FROM structure."""
    loj = parse_left_outer_join(from_clause)
    if loj:
        return [(loj[0], loj[1]), (loj[2], loj[3])]

    ij = parse_inner_join_chain(from_clause)
    if ij:
        return ij[0]  # bases

    items = []
    for raw in [x.strip() for x in from_clause.split(",") if x.strip()]:
        parts = raw.split()
        if len(parts) >= 3 and parts[-2].upper() == "AS":
            items.append((parts[0], parts[-1]))
        elif len(parts) >= 2:
            items.append((parts[0], parts[-1]))
        else:
            items.append((parts[0], parts[0]))
    return items


def build_from_base(from_clause: str):
    """
    Build the **base FROM tree** — BEFORE any heuristic rules.
    """
    from_items = parse_from_items(from_clause)
    alias_map = {alias: base.upper() for (base, alias) in from_items}

    # LEFT OUTER JOIN special case
    loj = parse_left_outer_join(from_clause)
    if loj:
        _, lalias, _, ralias, oncond = loj
        L = {"type": "REL", "base": alias_map[lalias], "alias": lalias}
        R = {"type": "REL", "base": alias_map[ralias], "alias": ralias}
        return {"type": "⟕", "condition": oncond, "children": [L, R]}, alias_map

    # Explicit INNER JOIN chain
    ij = parse_inner_join_chain(from_clause)
    if ij:
        bases, ons = ij
        rels = [{"type": "REL", "base": b.upper(), "alias": a} for (b, a) in bases]
        n = {"type": "⨝", "condition": ons[0], "children": [rels[0], rels[1]]}
        n = {"type": "⨝", "condition": ons[1], "children": [n, rels[2]]}
        return n, alias_map

    # Otherwise: comma joins → left-deep ×
    rels = [{"type": "REL", "base": b.upper(), "alias": a} for (b, a) in from_items]
    if len(rels) == 1:
        return rels[0], alias_map

    node = {"type": "×", "children": [rels[0], rels[1]]}
    for r in rels[2:]:
        node = {"type": "×", "children": [node, r]}
    return node, alias_map


# ============================================================
# CANONICAL QUERY TREE
# ============================================================
def build_canonical_tree(parsed, alias_map):
    """
    Canonical form described in Hudson & Garcia-Molina:
    π → σ → FROM-tree → (GROUP BY → HAVING optional)
    """
    base, _ = build_from_base(parsed["FROM"])
    node = base
    if parsed["WHERE"]:
        node = {"type": "σ", "condition": parsed["WHERE"], "child": node}
    if parsed["GROUPBY"]:
        node = {"type": "GROUP_BY", "attributes": parsed["GROUPBY"], "child": node}
    if parsed["HAVING"]:
        node = {"type": "HAVING", "condition": parsed["HAVING"], "child": node}
    return {"type": "π", "attributes": parsed["SELECT"], "child": node}


# ============================================================
# HEURISTIC RULES — HELPERS
# ============================================================
def get_aliases(node):
    """
    Return set of relation aliases under this node.
    Used for finding lowest placement of selection predicates.
    """
    if isinstance(node, dict):
        t = node.get("type")
        if t == "REL":
            return {node["alias"]}
        if t in ("×", "⨝", "⟕"):
            out = set()
            for ch in node["children"]:
                out |= get_aliases(ch)
            return out
        if t in ("σ", "π", "π_rel", "GROUP_BY", "HAVING", "ORDER_BY"):
            return get_aliases(node["child"])
    return set()


def attr_is_pk_or_unique(rel_alias, attr_name, alias_map, schema):
    """Check if alias.attr sits on PK/Unique index → allow high selectivity."""
    base = alias_map.get(rel_alias.upper()) or alias_map.get(rel_alias)
    if not base:
        return False
    info = schema.get(base)
    if not info:
        return False
    if attr_name in info["pk"]:
        return True
    return any(attr_name in grp for grp in info["unique"])


def estimate_selectivity(cond, alias_map, schema):
    """
    A simple heuristic:
       PK equality → most selective
       equality → moderate
       ranges → less selective
       others → weak
    """
    attrs = extract_qualified_attrs(cond)
    if len(attrs) == 1:
        a, attr = attrs[0]
        if re.search(r"=\s*('[^']*'|\d+)", cond):
            return 1 if attr_is_pk_or_unique(a, attr, alias_map, schema) else 2
        if re.search(r"[<>]=?\s*('[^']*'|\d+)", cond):
            return 3
    return 4

def add_local_sigma(node, cond, alias):
    """
    Attach a local selection σ[cond] as low as possible
    in the subtree that contains the given alias.
    """
    if alias not in get_aliases(node):
        return node

    if isinstance(node, dict):
        t = node.get("type")

        # Leaf: relation node
        if t == "REL":
            # Only wrap if this is the correct alias
            if node["alias"] == alias:
                return {"type": "σ", "condition": cond, "child": node}
            return node

        # Binary operators: ×, ⨝, ⟕
        if t in ("×", "⨝", "⟕"):
            new_children = []
            for ch in node.get("children", []):
                if alias in get_aliases(ch):
                    new_children.append(add_local_sigma(ch, cond, alias))
                else:
                    new_children.append(ch)
            node["children"] = new_children
            return node

        # Unary operators: σ, π, GROUP_BY, HAVING, π_rel, ORDER_BY
        if t in ("σ", "π", "π_rel", "GROUP_BY", "HAVING", "ORDER_BY"):
            node["child"] = add_local_sigma(node["child"], cond, alias)
            return node

    return node


def add_join_sigma(node, cond, aliases_needed):
    """
    Place a join selection σ[cond] as low as possible,
    but at or above the lowest common ancestor of all needed aliases.
    """
    node_aliases = get_aliases(node)
    # If this node's subtree doesn't even contain all required aliases, skip.
    if not aliases_needed.issubset(node_aliases):
        return node

    if isinstance(node, dict) and node.get("type") in ("×", "⨝", "⟕"):
        children = node.get("children", [])
        child_aliases = [get_aliases(ch) for ch in children]

        placed_deeper = False
        new_children = []

        # Try to push into one of the children if that child alone covers all aliases_needed.
        for ch, aset in zip(children, child_aliases):
            if aliases_needed.issubset(aset):
                new_children.append(add_join_sigma(ch, cond, aliases_needed))
                placed_deeper = True
            else:
                new_children.append(ch)

        if placed_deeper:
            node["children"] = new_children
            return node

        # Otherwise, this node is the LCA → wrap it with σ.
        return {"type": "σ", "condition": cond, "child": node}

    if isinstance(node, dict) and node.get("type") in ("σ", "π", "π_rel", "GROUP_BY", "HAVING", "ORDER_BY"):
        node["child"] = add_join_sigma(node["child"], cond, aliases_needed)
        return node

    return node



# ============================================================
# RULE 1 & RULE 2: Selection Cascade + Selection Pushdown
# ============================================================
def apply_rule1_2(parsed, alias_map, schema):
    """
    Rule 1: Break WHERE into individual conjuncts.
    Rule 2: Push each σ down as far as legally allowed.
    Special-cases:
      - OR inside WHERE → cannot safely push (so we keep whole σ).
      - No WHERE → nothing to do.
    """
    base, _ = build_from_base(parsed["FROM"])
    where_cond = parsed["WHERE"]

    # CASE 1: OR present → unsafe, keep WHERE as single σ
    if where_cond and " OR " in where_cond.upper():
        out = {"type": "σ", "condition": where_cond, "child": base}
        # groupby/having propagate above
        if parsed["GROUPBY"]:
            out = {"type": "GROUP_BY", "attributes": parsed["GROUPBY"], "child": out}
        if parsed["HAVING"]:
            out = {"type": "HAVING", "condition": parsed["HAVING"], "child": out}
        return {"type": "π", "attributes": parsed["SELECT"], "child": out}

    # CASE 2: no WHERE at all
    if not where_cond:
        out = base
        if parsed["GROUPBY"]:
            out = {"type": "GROUP_BY", "attributes": parsed["GROUPBY"], "child": out}
        if parsed["HAVING"]:
            out = {"type": "HAVING", "condition": parsed["HAVING"], "child": out}
        return {"type": "π", "attributes": parsed["SELECT"], "child": out}

    # CASE 3: AND-only WHERE → we can split
    conjuncts = split_and(where_cond)
    join_conds, local_conds = [], []

    for c in conjuncts:
        (join_conds if len(extract_qualified_attrs(c)) >= 2 else local_conds).append(c)

    # Push local selections first
    for cond in local_conds:
        attrs = extract_qualified_attrs(cond)
        alias = attrs[0][0] if attrs else cond.split(".")[0]
        base = add_local_sigma(base, cond, alias)

    # Push join selections
    for cond in join_conds:
        a1, a2 = extract_qualified_attrs(cond)[:2]
        base = add_join_sigma(base, cond, {a1[0], a2[0]})

    out = base
    if parsed["GROUPBY"]:
        out = {"type": "GROUP_BY", "attributes": parsed["GROUPBY"], "child": out}
    if parsed["HAVING"]:
        out = {"type": "HAVING", "condition": parsed["HAVING"], "child": out}
    return {"type": "π", "attributes": parsed["SELECT"], "child": out}


# ============================================================
# RULE 3: Reorder × based on join selectivity
# ============================================================
def apply_rule3(tree12, parsed, alias_map, schema):
    """
    Rule 3: reorder joins to evaluate the more selective join first.
    Only applies to:
      - 3-way comma joins
      - AND-only WHERE
      - exactly 2 join predicates
    """
    from_clause = parsed["FROM"]
    where_clause = parsed["WHERE"]

    # Conditions where Rule 3 is NOT applicable
    if not where_clause or " OR " in where_clause.upper():
        return tree12
    if parse_left_outer_join(from_clause) or parse_inner_join_chain(from_clause):
        return tree12

    from_items = parse_from_items(from_clause)
    if len(from_items) != 3:
        return tree12

    # Identify join vs local conditions
    conj = split_and(where_clause)
    join_conds, local = [], {a: [] for a in alias_map.keys()}
    for c in conj:
        attrs = extract_qualified_attrs(c)
        if len(attrs) >= 2:
            join_conds.append(c)
        elif len(attrs) == 1:
            local[attrs[0][0]].append(c)
    if len(join_conds) != 2:
        return tree12

    # Compute how selective each alias is (min selectivity score)
    alias_score = {}
    for a in alias_map.keys():
        alias_score[a] = min((estimate_selectivity(c, alias_map, schema) for c in local[a]),
                             default=100)

    # Pair join predicates with their selectivity
    info = []
    for jc in join_conds:
        a1, a2 = extract_qualified_attrs(jc)[:2]
        sc = min(alias_score[a1[0]], alias_score[a2[0]])
        info.append({"cond": jc, "aliases": {a1[0], a2[0]}, "score": sc})
    info.sort(key=lambda d: d["score"])
    j1, j2 = info

    aliases_all = set(alias_map.keys())
    third = list(aliases_all - j1["aliases"])
    if len(third) != 1:
        return tree12
    third = third[0]

    def build_rel(a):
        """Wrap relation with its local σ's ordered by selectivity."""
        base_rel = {"type": "REL", "base": alias_map[a], "alias": a}
        conds = sorted(local[a], key=lambda c: estimate_selectivity(c, alias_map, schema))
        node = base_rel
        for c in reversed(conds):
            node = {"type": "σ", "condition": c, "child": node}
        return node

    nodes = {a: build_rel(a) for a in alias_map.keys()}

    # First join
    a1, a2 = list(j1["aliases"])
    J1 = {"type": "σ", "condition": j1["cond"],
          "child": {"type": "×", "children": [nodes[a1], nodes[a2]]}}

    # Second join above that
    J2 = {"type": "σ", "condition": j2["cond"],
          "child": {"type": "×", "children": [J1, nodes[third]]}}

    return {"type": "π", "attributes": parsed["SELECT"], "child": J2}


# ============================================================
# RULE 4: Convert σ-over-× into ⨝
# ============================================================
def apply_rule4(tree):
    """
    If σ(condition) is directly above a × node AND the condition joins
    two different aliases → convert to real join node ⨝.
    """
    t = deepcopy(tree)

    def rec(n):
        if isinstance(n, dict):
            tp = n.get("type")
            if tp == "σ":
                child = rec(n["child"])
                attrs = extract_qualified_attrs(n["condition"])
                if isinstance(child, dict) and child.get("type") == "×" and len(attrs) >= 2:
                    return {"type": "⨝", "condition": n["condition"],
                            "children": child["children"]}
                return {"type": "σ", "condition": n["condition"], "child": child}
            if tp in ("×", "⨝", "⟕"):
                n["children"] = [rec(c) for c in n["children"]]
            elif tp in ("π", "π_rel", "GROUP_BY", "HAVING", "ORDER_BY"):
                n["child"] = rec(n["child"])
        return n

    return rec(t)


# ============================================================
# RULE 5: Projection Pushdown
# ============================================================
def compute_needed_attributes(parsed, alias_map, from_clause):
    """
    Determine which attributes each alias actually needs to retain.
    Based on:
      - SELECT
      - GROUP BY
      - HAVING
      - JOIN predicates (but NOT local σ)
    If SELECT *, disable projection pushdown.
    """
    sel = parsed["SELECT"].strip()
    if sel == "*":
        return {a: None for a in alias_map.keys()}

    needed = {a: set() for a in alias_map.keys()}
    sources = [sel, parsed["GROUPBY"] or "", parsed["HAVING"] or ""]

    for c in split_and(parsed["WHERE"] or ""):
        if len(extract_qualified_attrs(c)) >= 2:
            sources.append(c)

    # FROM join ON conditions
    loj = parse_left_outer_join(from_clause)
    if loj:
        sources.append(loj[4])
    ij = parse_inner_join_chain(from_clause)
    if ij:
        sources.extend(ij[1])

    for src in sources:
        for a, attr in extract_qualified_attrs(src):
            needed[a].add(attr)

    return needed


def apply_rule5(tree, parsed, alias_map, schema):
    """
    Apply projection pushdown:
      - If a relation has local σ, π_rel goes above the σ-chain.
      - If a relation has no σ, π_rel attaches directly above REL.
      - Needed[] determines which attributes to project.
    """
    needed = compute_needed_attributes(parsed, alias_map, parsed["FROM"])

    def collect_local_sigmas(n):
        """Collect aliases having at least one local selection."""
        out = set()
        if isinstance(n, dict):
            if n.get("type") == "σ":
                attrs = extract_qualified_attrs(n.get("condition", ""))
                if len(attrs) == 1:
                    out.add(attrs[0][0])
            for v in n.values():
                if isinstance(v, dict):
                    out |= collect_local_sigmas(v)
                elif isinstance(v, list):
                    for c in v:
                        if isinstance(c, dict):
                            out |= collect_local_sigmas(c)
        return out

    alias_sigma = collect_local_sigmas(tree)
    t = deepcopy(tree)

    def push(n):
        if not isinstance(n, dict):
            return n
        tp = n.get("type")

        if tp in ("×", "⨝", "⟕"):
            n["children"] = [push(c) for c in n["children"]]
            return n

        if tp in ("σ", "π", "GROUP_BY", "HAVING", "π_rel", "ORDER_BY"):
            n["child"] = push(n["child"])
            aliases = get_aliases(n)
            if len(aliases) == 1:  # only one relation in subtree
                a = next(iter(aliases))
                if a in alias_sigma and tp != "π_rel":
                    req = needed.get(a)
                    if req:
                        attrs = ", ".join(f"{a}.{r}" for r in sorted(req))
                        return {"type": "π_rel", "attributes": attrs, "child": n}
            return n

        if tp == "REL":
            a = n["alias"]
            if a not in alias_sigma:
                req = needed.get(a)
                if req:
                    attrs = ", ".join(f"{a}.{r}" for r in sorted(req))
                    return {"type": "π_rel", "attributes": attrs, "child": n}
            return n

        return n

    return push(t)

def collect_conditions_for_where(node):
    """
    Traverse the final optimized tree and collect predicates
    that belong in the WHERE clause.

    - σ nodes: their condition goes into WHERE
    - ⨝ nodes: their join condition goes into WHERE
    - ⟕ nodes (LEFT OUTER JOIN): we do NOT pull their condition
      into WHERE, because that belongs in the ON clause.
    """
    conditions = []

    def rec(n):
        if not isinstance(n, dict):
            return
        t = n.get("type")

        if t == "σ":
            cond = n.get("condition")
            if cond:
                conditions.append(cond)
            rec(n.get("child"))

        elif t == "⨝":
            cond = n.get("condition")
            if cond:
                conditions.append(cond)
            for ch in n.get("children", []):
                rec(ch)

        elif t == "⟕":
            # LEFT OUTER JOIN: keep its condition in ON, not WHERE
            for ch in n.get("children", []):
                rec(ch)

        elif t == "×":
            for ch in n.get("children", []):
                rec(ch)

        elif t in ("π", "π_rel", "GROUP_BY", "HAVING", "ORDER_BY"):
            rec(n.get("child"))

        # REL and others: nothing to collect directly

    rec(node)
    # Filter out any empty/None just in case
    return [c for c in conditions if c]


def generate_refined_sql(final_core_tree, parsed, alias_map):
    """
    Convert the final optimized *core* tree back into a runnable SQL query.

    - SELECT: from parsed["SELECT"]
    - FROM:   reuse parsed["FROM"] (logical from-clause unchanged)
    - WHERE:  rebuilt from σ and ⨝ conditions in the optimized tree
    - GROUP BY / HAVING / ORDER BY: reused from parsed
    """
    select_clause = (parsed["SELECT"] or "").strip() or "*"
    from_clause = parsed["FROM"].strip()

    # Collect WHERE predicates from the optimized core tree (no ORDER_BY wrapper)
    where_conds = collect_conditions_for_where(final_core_tree)
    where_clause = " AND ".join(where_conds) if where_conds else None

    groupby_clause = (parsed["GROUPBY"] or "").strip() or None
    having_clause = (parsed["HAVING"] or "").strip() or None
    orderby_clause = (parsed["ORDERBY"] or "").strip() or None

    parts = []
    parts.append("SELECT " + select_clause)
    parts.append("FROM " + from_clause)
    if where_clause:
        parts.append("WHERE " + where_clause)
    if groupby_clause:
        parts.append("GROUP BY " + groupby_clause)
    if having_clause:
        parts.append("HAVING " + having_clause)
    if orderby_clause:
        parts.append("ORDER BY " + orderby_clause)

    return "\n".join(parts) + ";"



# ============================================================
# TREE PRINTER
# ============================================================
def print_tree(node, indent=0, is_last=True):
    """Pretty ASCII rendering of the operator tree."""
    prefix = "└── " if is_last else "├── "
    if not isinstance(node, dict):
        print(" " * indent + prefix + str(node))
        return

    t = node.get("type")
    if t in ("π", "π_rel"):
        print(" " * indent + prefix + f"π ({node['attributes']})")
        print_tree(node["child"], indent + 4, True)

    elif t == "σ":
        print(" " * indent + prefix + f"σ [{node['condition']}]")
        print_tree(node["child"], indent + 4, True)

    elif t in ("×", "⨝", "⟕"):
        cond = f" [{node.get('condition')}]" if node.get("condition") else ""
        print(" " * indent + prefix + f"{t}{cond}")
        ch = node["children"]
        for i, c in enumerate(ch):
            print_tree(c, indent + 4, i == len(ch) - 1)

    elif t == "GROUP_BY":
        print(" " * indent + prefix + f"GROUP BY ({node['attributes']})")
        print_tree(node["child"], indent + 4, True)

    elif t == "HAVING":
        print(" " * indent + prefix + f"HAVING [{node['condition']}]")
        print_tree(node["child"], indent + 4, True)

    elif t == "REL":
        print(" " * indent + prefix + node["base"])

    elif t == "ORDER_BY":
        print(" " * indent + prefix + f"ORDER BY ({node['attributes']})")
        print_tree(node["child"], indent + 4, True)

    else:
        print(" " * indent + prefix + str(node))


# ============================================================
# MAIN DRIVER
# ============================================================
def is_left_outer_join(parsed):
    return " LEFT OUTER JOIN " in parsed["FROM"].upper()


def main():
    if len(sys.argv) != 2:
        print("Usage: python db.py <inputfile>")
        sys.exit(1)

    infile = sys.argv[1]
    schema_lines, sql_query = load_input_file(infile)
    schema = parse_schema(schema_lines)
    parsed = parse_sql(sql_query)

    base_tree, alias_map = build_from_base(parsed["FROM"])

    # Apply each rule in sequence (your logic preserved)
    canonical_core = build_canonical_tree(parsed, alias_map)
    r12_core = apply_rule1_2(parsed, alias_map, schema)
    r3_core = apply_rule3(r12_core, parsed, alias_map, schema)
    r4_core = apply_rule4(r3_core)
    r5_core = apply_rule5(r4_core, parsed, alias_map, schema)

    # Attach ORDER BY wrapper
    canonical = attach_order_by(canonical_core, parsed)
    r12 = attach_order_by(r12_core, parsed)
    r3 = attach_order_by(r3_core, parsed)
    r4 = attach_order_by(r4_core, parsed)
    r5 = attach_order_by(r5_core, parsed)

    # Utility flags
    where_upper = (parsed["WHERE"] or "").upper()
    has_or = " OR " in where_upper
    has_lo = is_left_outer_join(parsed)

    # Print sections (same behavior as your code)
    print("CANONICAL QUERY TREE")
    print_tree(canonical)

    print("\nAFTER RULE 1 & RULE 2 (selection cascade + pushdown)")
    if has_or or has_lo:
        print("Not applicable.")
    print_tree(r12)

    print("\nAFTER RULE 3 (reorder by selectivity)")
    if has_or or has_lo:
        print("Not applicable.")
    print_tree(r3)

    print("\nAFTER RULE 4 (× + σ(join) → ⨝)")
    if has_or or has_lo:
        print("Not applicable.")
    print_tree(r4)

    print("\nAFTER RULE 5 (projection pushdown – FINAL OPTIMIZED TREE)")
    if parsed["SELECT"].strip() == "*" and has_or:
        print("Not applicable.")
    print_tree(r5)

    #generate refined SQL query from final optimized core tree
    refined_sql = generate_refined_sql(r5_core, parsed, alias_map)
    print("\nREFINED SQL QUERY")
    print(refined_sql)


if __name__ == "__main__":
    main()

