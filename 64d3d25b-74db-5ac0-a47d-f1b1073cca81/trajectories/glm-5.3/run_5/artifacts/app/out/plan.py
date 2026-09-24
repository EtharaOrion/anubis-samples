#!/usr/bin/env python3
"""Installation dossier derivation program.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Derives the twenty four dossier artifacts described in rules.md, schema.md,
constants.md and reason_codes.md found in the data directory handed to it.
All arithmetic is integer or exact rational; no float is ever produced,
compared or written.
"""

import csv
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

LENGTH_FACTORS = {"MM": 1000, "CM": 10000, "IN": 25400}
MASS_FACTORS = {"G": 1, "KG": 1000}
SECONDS_PER_DAY = 3600

# ascending removal precedence: bay size first, then conservation, then rule
KIND_RANK_REMOVE = {"D_BAYSIZE": 0, "D_CONSERVATION": 1, "D_RULE": 2}
# ascending keep precedence: an explicit rule outranks conservation outranks bay size
KIND_RANK_KEEP = {"D_RULE": 0, "D_CONSERVATION": 1, "D_BAYSIZE": 2}

JSONL_ARTIFACTS = [
    "admissibility_matrix.jsonl",
    "bay_candidate_matrix.jsonl",
    "canonical_objects.jsonl",
    "conservation_adjusted.jsonl",
    "crate_assignment.jsonl",
    "crate_capacity_ledger.jsonl",
    "crate_candidate_matrix.jsonl",
    "crate_contention_report.jsonl",
    "crew_requirement_ledger.jsonl",
    "cycle_resolution_audit.jsonl",
    "dependency_edges.jsonl",
    "deterministic_audit.jsonl",
    "gallery_occupancy_map.jsonl",
    "install_order.jsonl",
    "install_window_table.jsonl",
    "normalized_geometry.jsonl",
    "rejected_placement.jsonl",
    "selected_placement.jsonl",
    "transport_leg_manifest.jsonl",
    "unschedulable_report.jsonl",
]


def rhu(num, den):
    """Round num/den half up. A zero denominator yields the sentinel zero."""
    if den == 0:
        return 0
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return q


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
                for row in csv.DictReader(fh)]


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def to_int(text):
    text = (text or "").strip()
    return int(text) if text else 0


def reduced_pair(num, den):
    if den == 0:
        return 0, 1
    a, b = abs(num), abs(den)
    while b:
        a, b = b, a % b
    g = a or 1
    num, den = num // g, den // g
    if den < 0:
        num, den = -num, -den
    return num, den


def sensitivity_rank(code, fallback):
    m = re.match(r"^(\D+)(\d+)$", code.strip())
    return int(m.group(2)) if m else fallback


def strongly_connected_components(nodes, adj):
    """Iterative Kosaraju over sorted nodes; adjacency maps node -> set."""
    visited = set()
    order = []
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(adj.get(start, ()))))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(sorted(adj.get(nxt, ())))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()
    reverse = defaultdict(set)
    for u, vs in adj.items():
        for v in vs:
            reverse[v].add(u)
    comp = {}
    cid = 0
    for start in reversed(order):
        if start in comp:
            continue
        comp[start] = cid
        stack = [start]
        while stack:
            u = stack.pop()
            for w in reverse.get(u, ()):
                if w not in comp:
                    comp[w] = cid
                    stack.append(w)
        cid += 1
    return comp


def cyclic_edges(edges):
    """The subset of `edges` that lies on some directed cycle, in given order."""
    adj = defaultdict(set)
    for u, v, _k, _w in edges:
        adj[u].add(v)
    comp = strongly_connected_components(sorted(adj), adj)
    sizes = defaultdict(int)
    for node, cid in comp.items():
        sizes[cid] += 1
    out = []
    for edge in edges:
        u, v = edge[0], edge[1]
        if u == v or (comp[u] == comp[v] and sizes[comp[u]] > 1):
            out.append(edge)
    return out


def main(data_dir, out_dir):
    data_dir = os.path.abspath(data_dir)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    horizon = read_json(os.path.join(data_dir, "horizon_config.json"))
    objective_order = read_json(os.path.join(data_dir, "objective_order.json"))
    setup_div = to_int(str(horizon.get("setup_div", ""))) or 700
    chain_seed = str(horizon.get("chain_seed", "0" * 64))
    venue_end_shift = 1 if str(horizon.get("venue_end_convention")) == "exclusive" else 0
    cons_end_shift = 0 if str(horizon.get("conservation_end_convention")) == "inclusive" else 1

    master_rows = read_csv(os.path.join(data_dir, "object_master.csv"))
    dims_rows = read_csv(os.path.join(data_dir, "object_dimensions.csv"))
    mass_rows = read_csv(os.path.join(data_dir, "object_mass.csv"))
    value_rows = read_csv(os.path.join(data_dir, "object_value.csv"))
    orient_rows = read_csv(os.path.join(data_dir, "object_orientation.csv"))
    version_rows = read_csv(os.path.join(data_dir, "object_version_map.csv"))
    material_rows = read_csv(os.path.join(data_dir, "material_rules.csv"))
    handling_rows = read_csv(os.path.join(data_dir, "handling_class_rules.csv"))
    bay_rows = read_csv(os.path.join(data_dir, "gallery_bays.csv"))
    exception_rows = read_csv(os.path.join(data_dir, "bay_exceptions.csv"))
    crate_rows = read_csv(os.path.join(data_dir, "crate_catalog.csv"))
    reservation_rows = read_csv(os.path.join(data_dir, "crate_reservations.csv"))
    prior_rows = read_csv(os.path.join(data_dir, "prior_state.csv"))
    cons_rows = read_csv(os.path.join(data_dir, "conservation_rules.csv"))
    venue_rows = read_csv(os.path.join(data_dir, "venue_windows.csv"))
    dep_rows = read_csv(os.path.join(data_dir, "dependency_rules.csv"))
    crew_rows = read_csv(os.path.join(data_dir, "crew_roster.csv"))
    transport_rows = read_csv(os.path.join(data_dir, "transport_rules.csv"))
    lender_rows = read_csv(os.path.join(data_dir, "lender_master.csv"))
    value_class_rows = read_csv(os.path.join(data_dir, "value_class_rules.csv"))

    # ---- reference tables ----------------------------------------------
    material = {r["material_code"]: r for r in material_rows}
    handling = {}
    for r in handling_rows:
        handling[r["handling_class"]] = {
            "cw": to_int(r["clearance_w"]) * 1000,
            "cd": to_int(r["clearance_d"]) * 1000,
            "ch": to_int(r["clearance_h"]) * 1000,
            "margin": to_int(r["handle_margin"]) * 1000,
            "crew_class": r["crew_class"],
            "setup_base": to_int(r["setup_base"]),
        }
    bays = {}
    for r in bay_rows:
        bays[r["bay_id"]] = {
            "w": to_int(r["interior_w"]) * 1000,
            "h": to_int(r["interior_h"]) * 1000,
            "d": to_int(r["interior_d"]) * 1000,
            "lux": to_int(r["lux_level"]),
            "priority": to_int(r["bay_priority"]),
        }
    blocked = defaultdict(set)
    for r in exception_rows:
        blocked[r["bay_id"]].add(r["blocked_class"])
    crates = {}
    for r in crate_rows:
        iw, ih, id_ = (to_int(r["inner_w"]), to_int(r["inner_h"]), to_int(r["inner_d"]))
        crates[r["crate_class"]] = {
            "dims": sorted([iw * 1000, ih * 1000, id_ * 1000]),
            "volume": iw * ih * id_ * 1000 * 1000 * 1000,
            "count": to_int(r["count"]),
            "mode": r["transport_mode"],
        }
    reservations = defaultdict(int)
    for r in reservation_rows:
        reservations[(r["crate_class"], r["lender_id"])] += to_int(r["reserved_count"])
    prior = defaultdict(int)
    for r in prior_rows:
        prior[r["crate_class"]] += to_int(r["consumed_count"])
    conservation = {}
    for idx, r in enumerate(cons_rows):
        conservation[r["conservation_class"]] = {
            "start": to_int(r["setup_start"]),
            "end": to_int(r["setup_end"]),
            "needs_dark": to_int(r["needs_dark"]) != 0,
            "sensitivity": sensitivity_rank(r["conservation_class"], idx),
        }
    venue = {}
    for r in venue_rows:
        venue.setdefault(r["bay_id"], (to_int(r["access_start"]), to_int(r["access_end"])))
    crew = {}
    for r in crew_rows:
        crew.setdefault(r["crew_class"], (to_int(r["avail_start"]), to_int(r["avail_end"])))
    transport_rule = {r["lender_id"]: (r["forbidden_mode"], r["fallback_mode"])
                      for r in transport_rows}
    lender = {r["lender_id"]: {"num": to_int(r["insurance_rate_num"]),
                               "den": to_int(r["insurance_rate_den"])}
              for r in lender_rows}
    value_classes = sorted(
        [{"value_class": r["value_class"], "min_value": to_int(r["min_value"]) * 100,
          "unschedulable_penalty": to_int(r["unschedulable_penalty"]),
          "contention_penalty": to_int(r["contention_penalty"])}
         for r in value_class_rows],
        key=lambda c: c["min_value"], reverse=True)

    # ---- stage 1 canonical identity and units --------------------------
    objects = []
    master = {}
    for r in master_rows:
        oid = r["object_id"]
        if oid in master:
            continue
        master[oid] = (r["lender_id"], r["material_code"])
        objects.append(oid)

    first_row = {}
    for rows, key in ((dims_rows, "dims"), (mass_rows, "mass"), (value_rows, "value"),
                      (orient_rows, "orient")):
        seen = {}
        for r in rows:
            if r["object_id"] not in seen:
                seen[r["object_id"]] = r
        first_row[key] = seen
    version = {}
    for r in version_rows:
        oid, v = r["object_id"], to_int(r["version"])
        version[oid] = max(version.get(oid, v), v)

    canonical = {}
    for oid in objects:
        lender_id, mat_code = master[oid]
        d = first_row["dims"].get(oid, {})
        unit = LENGTH_FACTORS.get(d.get("dim_unit", "MM"), 1000)
        m = first_row["mass"].get(oid, {})
        canonical[oid] = {
            "object_id": oid,
            "lender_id": lender_id,
            "material_code": mat_code,
            "version": version.get(oid, 0),
            "declared_value_cents": to_int(first_row["value"].get(oid, {}).get("declared_value")) * 100,
            "mass_g": to_int(m.get("mass")) * MASS_FACTORS.get(m.get("mass_unit", "G"), 1),
            "w": to_int(d.get("width")) * unit,
            "h": to_int(d.get("height")) * unit,
            "d": to_int(d.get("depth")) * unit,
        }

    # ---- stage 2 normalized geometry ------------------------------------
    geometry = {}
    for oid in objects:
        c = canonical[oid]
        hc = handling[material[c["material_code"]]["handling_class"]]
        num, den = reduced_pair(c["h"], c["w"])
        geometry[oid] = {
            "clearance_w": c["w"] + 2 * hc["cw"],
            "clearance_h": c["h"] + hc["ch"],
            "clearance_d": c["d"] + 2 * hc["cd"],
            "aspect_num": num, "aspect_den": den,
            "handle": sorted([c["w"] + 2 * hc["margin"], c["h"] + 2 * hc["margin"],
                              c["d"] + 2 * hc["margin"]]),
            "handling": hc,
        }
    handling_volume = {o: geometry[o]["handle"][0] * geometry[o]["handle"][1]
                          * geometry[o]["handle"][2] for o in objects}

    # ---- stage 3 orientation admissibility ------------------------------
    admissible = {}
    for oid in objects:
        o = first_row["orient"].get(oid, {})
        tip = to_int(o.get("tip_allowed")) == 1
        rot = to_int(o.get("rotate_allowed")) == 1
        lst = [0] + ([1] if rot else []) + ([2, 3] if tip else [])
        admissible[oid] = {"orientations": lst, "tip_allowed": tip, "rotate_allowed": rot}

    def oriented(oid, idx):
        g = geometry[oid]
        cw, ch, cd = g["clearance_w"], g["clearance_h"], g["clearance_d"]
        return ((cw, cd, ch), (cd, cw, ch), (cw, ch, cd), (ch, cw, cd))[idx]

    # ---- stage 4 bay candidate matrix -----------------------------------
    bay_order = sorted(bays, key=lambda b: (bays[b]["priority"], b))
    bay_matrix = []
    fits_in = {}
    for oid in objects:
        per_bay = {}
        for bay_id in sorted(bays):
            best = None
            bay = bays[bay_id]
            for idx in admissible[oid]["orientations"]:
                fw, fd, fh = oriented(oid, idx)
                if fw <= bay["w"] and fd < bay["d"] and fh <= bay["h"]:
                    best = idx
                    break
            per_bay[bay_id] = best
            bay_matrix.append({"object_id": oid, "bay_id": bay_id,
                               "fits": best is not None,
                               "orientation": -1 if best is None else best})
        fits_in[oid] = per_bay

    # ---- stage 5 selected placement (geometry only) ---------------------
    selected = {}
    rejected = []
    for oid in objects:
        chosen = None
        for bay_id in bay_order:
            if fits_in[oid][bay_id] is not None:
                chosen = (bay_id, fits_in[oid][bay_id])
                break
        if chosen:
            selected[oid] = {"bay_id": chosen[0], "orientation": chosen[1], "code": "P_OK"}
        else:
            code = "P_NO_ORIENTATION" if admissible[oid]["orientations"] == [0] else "P_NO_FIT"
            selected[oid] = {"bay_id": "", "orientation": -1, "code": code}
            rejected.append({"object_id": oid, "code": code})

    # ---- stage 6 conservation adjustment --------------------------------
    def cons_ok(oid, bay_id):
        mat = material[canonical[oid]["material_code"]]
        rule = conservation[mat["conservation_class"]]
        bay = bays[bay_id]
        if bay["lux"] > to_int(mat["max_lux"]):
            return False
        if mat["conservation_class"] in blocked[bay_id]:
            return False
        if rule["needs_dark"] and bay["lux"] > 0:
            return False
        return True

    placed = {}
    for oid in objects:
        chosen = None
        geo_fit = False
        for bay_id in bay_order:
            if fits_in[oid][bay_id] is not None:
                geo_fit = True
                if cons_ok(oid, bay_id):
                    chosen = (bay_id, fits_in[oid][bay_id])
                    break
        if chosen:
            placed[oid] = {"bay_id": chosen[0], "orientation": chosen[1], "code": "P_OK"}
        elif geo_fit:
            placed[oid] = {"bay_id": "", "orientation": -1, "code": "P_CONSERVATION_BLOCK"}
        else:
            placed[oid] = {"bay_id": "", "orientation": -1, "code": selected[oid]["code"]}

    # ---- stage 13 prerequisites: crew class and setup seconds -----------
    crew_of, setup = {}, {}
    for oid in objects:
        hc = geometry[oid]["handling"]
        crew_of[oid] = hc["crew_class"]
        setup[oid] = rhu(hc["setup_base"] * canonical[oid]["mass_g"], setup_div)

    # ---- stage 7 crate candidates ---------------------------------------
    crate_fit, primary_class = {}, {}
    for oid in objects:
        hs = geometry[oid]["handle"]
        entries = sorted((crates[cc]["volume"] - handling_volume[oid], cc)
                         for cc in sorted(crates)
                         if hs[0] <= crates[cc]["dims"][0]
                         and hs[1] <= crates[cc]["dims"][1]
                         and hs[2] <= crates[cc]["dims"][2])
        crate_fit[oid] = entries
        primary_class[oid] = entries[0][1] if entries else ""

    # ---- stage 10 dependency edges --------------------------------------
    bay_members = defaultdict(list)
    for oid in objects:
        if placed[oid]["bay_id"]:
            bay_members[placed[oid]["bay_id"]].append(oid)

    edge_map = {}

    def add_edge(u, v, kind, weight):
        rank = KIND_RANK_KEEP[kind]
        prev = edge_map.get((u, v))
        if prev is None or rank < prev[0]:
            edge_map[(u, v)] = (rank, kind, weight)

    for r in dep_rows:
        add_edge(r["from_object"], r["to_object"], "D_RULE", to_int(r["edge_priority"]))
    for bay_id in sorted(bay_members):
        members = sorted(bay_members[bay_id])
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                va, vb = handling_volume[a], handling_volume[b]
                if va > vb:
                    add_edge(a, b, "D_BAYSIZE", va)
                elif vb > va:
                    add_edge(b, a, "D_BAYSIZE", vb)
                else:
                    add_edge(a, b, "D_BAYSIZE", va)
                ca = conservation[material[canonical[a]["material_code"]]["conservation_class"]]["sensitivity"]
                cb = conservation[material[canonical[b]["material_code"]]["conservation_class"]]["sensitivity"]
                if ca > cb:
                    add_edge(a, b, "D_CONSERVATION", handling_volume[a])
                elif cb > ca:
                    add_edge(b, a, "D_CONSERVATION", handling_volume[b])

    edges = sorted(((u, v, kind, w) for (u, v), (_r, kind, w) in edge_map.items()),
                   key=lambda e: (e[0], e[1]))

    # ---- stage 11 cycle resolution --------------------------------------
    live = list(edges)
    removed_edges = []
    while True:
        candidates = cyclic_edges(live)
        if not candidates:
            break
        candidates.sort(key=lambda e: (KIND_RANK_REMOVE[e[2]], e[3], e[0], e[1]))
        drop = candidates[0]
        removed_edges.append(drop)
        live.remove(drop)
    final_cycle_count = len(cyclic_edges(live))

    preds = defaultdict(set)
    for u, v, _k, _w in live:
        preds[v].add(u)

    # ---- stage 12 install order -----------------------------------------
    install_pos, order_rows = {}, []
    remaining = list(objects)
    installed = set()
    position = 0
    while remaining:
        ready = [o for o in remaining if not (preds[o] - installed)]
        if not ready:
            ready = sorted(remaining, key=lambda o: (setup[o], o))
        ready.sort(key=lambda o: (setup[o], o))
        pick = ready[0]
        remaining.remove(pick)
        installed.add(pick)
        position += 1
        install_pos[pick] = position
        order_rows.append({"position": position, "object_id": pick})

    # ---- stage 8 shared crate capacity ----------------------------------
    available = {cc: crates[cc]["count"] - prior[cc] for cc in crates}
    open_res = dict(reservations)
    assigned = {}
    for row in order_rows:
        oid = row["object_id"]
        primary = primary_class[oid]
        if not placed[oid]["bay_id"]:
            assigned[oid] = {"crate_class": "", "code": "C_NO_CRATE", "primary": primary}
            continue
        lender_id = canonical[oid]["lender_id"]
        options = []
        for vol, cc in crate_fit[oid]:
            if available[cc] > 0:
                rank = 0 if open_res.get((cc, lender_id), 0) > 0 else 1
                options.append((rank, vol, cc))
        if not options:
            assigned[oid] = {"crate_class": "", "code": "C_NO_CRATE", "primary": primary}
            continue
        options.sort()
        cc = options[0][2]
        available[cc] -= 1
        key = (cc, lender_id)
        if open_res.get(key, 0) > 0:
            open_res[key] -= 1
        assigned[oid] = {"crate_class": cc,
                         "code": "C_PRIMARY" if cc == primary else "C_DISPLACED",
                         "primary": primary}

    # ---- stage 9 transport legs -----------------------------------------
    transport = {}
    for oid in objects:
        cc = assigned[oid]["crate_class"]
        if not cc:
            continue
        mode, exc = crates[cc]["mode"], False
        rule = transport_rule.get(canonical[oid]["lender_id"])
        if rule and mode == rule[0]:
            mode, exc = rule[1], True
        transport[oid] = (mode, exc)

    # ---- stage 13 install windows ---------------------------------------
    windows, unschedulable = {}, []
    for oid in objects:
        bay_id = placed[oid]["bay_id"]
        required_days = -(-setup[oid] // SECONDS_PER_DAY)
        if not bay_id:
            windows[oid] = {"bay_id": "", "window_start": 0, "window_end": 0,
                            "required_days": required_days, "code": "S_NO_PLACEMENT"}
            unschedulable.append({"object_id": oid, "code": "S_NO_PLACEMENT"})
            continue
        rule = conservation[material[canonical[oid]["material_code"]]["conservation_class"]]
        v_start, v_end = venue.get(bay_id, (0, 0))
        c_start, c_end = crew.get(crew_of[oid], (0, 0))
        low = max(rule["start"], v_start, c_start)
        high = min(rule["end"] - cons_end_shift, v_end - venue_end_shift, c_end)
        days_available = high - low + 1
        required_days = -(-setup[oid] // SECONDS_PER_DAY)
        if assigned[oid]["crate_class"] == "":
            code = "S_NO_CRATE"
        elif days_available <= 0:
            code = "S_NO_WINDOW"
        elif required_days > days_available:
            code = "S_SHORT_WINDOW"
        else:
            code = "S_OK"
        windows[oid] = {"bay_id": bay_id, "window_start": low, "window_end": high,
                        "required_days": required_days, "code": code}
        if code != "S_OK":
            unschedulable.append({"object_id": oid, "code": code})
    scheduled = {o for o in objects if windows[o]["code"] == "S_OK"}

    # ---- stage 14 dossier -----------------------------------------------
    def value_class_of(oid):
        v = canonical[oid]["declared_value_cents"]
        for vc in value_classes:
            if v >= vc["min_value"]:
                return vc
        return value_classes[-1]

    def insurance_of(oid):
        c = canonical[oid]
        rate = lender[c["lender_id"]]
        return rhu(c["declared_value_cents"] * rate["num"], rate["den"])

    insurance = {o: insurance_of(o) for o in objects}
    unschedulable_penalty = sum(value_class_of(r["object_id"])["unschedulable_penalty"]
                                for r in unschedulable)
    displaced = [o for o in objects if assigned[o]["code"] == "C_DISPLACED"]
    contention_penalty = sum(value_class_of(o)["contention_penalty"] for o in displaced)
    fit_map = {o: {cc: vol for vol, cc in crate_fit[o]} for o in objects}
    total_residual = sum(fit_map[o][assigned[o]["crate_class"]]
                         for o in objects if assigned[o]["crate_class"])
    objective = [len(scheduled), unschedulable_penalty, contention_penalty,
                 len(removed_edges), total_residual]

    # ---- serialization ---------------------------------------------------
    def w_jsonl(name, rows):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    def w_json(name, value):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(value, sort_keys=True, indent=2) + "\n")

    w_jsonl("canonical_objects.jsonl",
            [{k: canonical[o][k] for k in ("object_id", "lender_id", "material_code", "version",
                                           "declared_value_cents", "mass_g")} for o in objects])
    w_jsonl("normalized_geometry.jsonl",
            [dict({"object_id": o}, **{k: geometry[o][k] for k in
                                       ("clearance_w", "clearance_h", "clearance_d",
                                        "aspect_num", "aspect_den")})
             for o in objects])
    w_jsonl("admissibility_matrix.jsonl",
            [{"object_id": o, "orientations": admissible[o]["orientations"],
              "tip_allowed": admissible[o]["tip_allowed"],
              "rotate_allowed": admissible[o]["rotate_allowed"]} for o in objects])
    w_jsonl("bay_candidate_matrix.jsonl", bay_matrix)
    w_jsonl("selected_placement.jsonl",
            [{"object_id": o, "bay_id": selected[o]["bay_id"],
              "orientation": selected[o]["orientation"], "code": selected[o]["code"]}
             for o in objects])
    w_jsonl("rejected_placement.jsonl",
            sorted(rejected, key=lambda r: (r["object_id"], r["code"])))
    w_jsonl("conservation_adjusted.jsonl",
            [{"object_id": o, "bay_id": placed[o]["bay_id"],
              "orientation": placed[o]["orientation"], "code": placed[o]["code"]}
             for o in objects])
    w_jsonl("crate_candidate_matrix.jsonl",
            [{"object_id": o, "crate_class": cc, "residual": vol}
             for o in sorted(objects) for vol, cc in crate_fit[o]])
    w_jsonl("crate_assignment.jsonl",
            [{"object_id": o, "crate_class": assigned[o]["crate_class"],
              "code": assigned[o]["code"], "primary_class": assigned[o]["primary"]}
             for o in sorted(objects)])
    w_jsonl("crate_capacity_ledger.jsonl",
            [{"crate_class": cc, "catalog_count": crates[cc]["count"],
              "prior_consumed": prior[cc], "remaining": available[cc]}
             for cc in sorted(crates)])
    w_jsonl("crate_contention_report.jsonl",
            [{"object_id": o, "wanted_class": primary_class[o],
              "assigned_class": assigned[o]["crate_class"]} for o in sorted(displaced)])
    w_jsonl("transport_leg_manifest.jsonl",
            [{"object_id": o, "crate_class": assigned[o]["crate_class"],
              "mode": transport[o][0], "exception": transport[o][1]}
             for o in objects if o in transport])
    w_jsonl("dependency_edges.jsonl",
            [{"from": u, "to": v, "kind": k, "weight": w} for u, v, k, w in edges])
    w_jsonl("cycle_resolution_audit.jsonl",
            [{"from": u, "to": v, "kind": k, "weight": w} for u, v, k, w in removed_edges])
    w_jsonl("install_order.jsonl", order_rows)
    w_jsonl("crew_requirement_ledger.jsonl",
            [{"object_id": o, "crew_class": crew_of[o], "setup_seconds": setup[o]}
             for o in objects])
    w_jsonl("install_window_table.jsonl",
            [{"object_id": o, "bay_id": windows[o]["bay_id"],
              "window_start": windows[o]["window_start"],
              "window_end": windows[o]["window_end"],
              "required_days": windows[o]["required_days"], "code": windows[o]["code"]}
             for o in objects])
    w_jsonl("unschedulable_report.jsonl",
            sorted(unschedulable, key=lambda r: (r["object_id"], r["code"])))
    w_jsonl("gallery_occupancy_map.jsonl",
            [{"bay_id": b, "object_count": len(bay_members[b]),
              "objects": sorted(bay_members[b])} for b in sorted(bays)])
    w_jsonl("deterministic_audit.jsonl",
            [{"object_id": o, "placed_bay": placed[o]["bay_id"],
              "crate_class": assigned[o]["crate_class"],
              "displaced": assigned[o]["code"] == "C_DISPLACED",
              "scheduled": o in scheduled, "install_position": install_pos[o],
              "setup_seconds": setup[o], "insurance_cents": insurance[o]}
             for o in objects])

    w_json("penalty_summary.json", {
        "unschedulable_penalty": unschedulable_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": len(removed_edges),
        "total_insurance_cents": sum(insurance.values()),
    })
    w_json("objective_tuple.json", {"objective": objective, "order": objective_order["order"]})

    program_path = os.path.abspath(__file__)
    manifest = {}
    for name in JSONL_ARTIFACTS + ["penalty_summary.json", "objective_tuple.json", "plan.py"]:
        src = program_path if name == "plan.py" else os.path.join(out_dir, name)
        with open(src, "rb") as fh:
            manifest[name] = hashlib.sha256(fh.read()).hexdigest()
    w_json("integrity_manifest.json", manifest)

    chain = chain_seed
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
    w_json("final_state.json", {
        "chain": chain,
        "repair_converged": final_cycle_count == 0,
        "cycle_edges_removed": len(removed_edges),
        "final_cycle_count": final_cycle_count,
        "displacement_count": len(displaced),
        "unschedulable_count": len(unschedulable),
        "placed_objects": len(scheduled),
        "total_setup_seconds": sum(setup.values()),
        "total_insurance_cents": sum(insurance.values()),
        "objective": objective,
    })


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
