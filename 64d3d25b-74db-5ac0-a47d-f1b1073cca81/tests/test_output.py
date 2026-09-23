#!/usr/bin/env python3
"""Museum touring installation dossier - verifier corpus rebuild and grading checks.

Integer / exact-rational only. No float is produced, compared, or serialized.
All lengths are carried in micrometres, mass in grams, value in cents, windows in
integer day indices. Rational quantities are reduced pairs; every stage that
derives a rational rounds it to the stage quantum with round half up.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path

# ------------------------------------------------------------------ constants
UNIT_SCALE = {"MM": 1000, "CM": 10000, "IN": 25400}   # source length -> micrometre
MASS_SCALE = {"G": 1, "KG": 1000}                     # source mass -> gram
CONS_CLASSES = ("L1", "L2", "L3", "L4")               # ascending sensitivity
DEP_KINDS = ("D_RULE", "D_CONSERVATION", "D_BAYSIZE")
DEP_KIND_RANK = {"D_RULE": 0, "D_CONSERVATION": 1, "D_BAYSIZE": 2}
PLACE_CODES = ("P_OK", "P_NO_FIT", "P_NO_ORIENTATION",
               "P_CONSERVATION_BLOCK", "P_BAY_EXCEPTION")
CRATE_CODES = ("C_PRIMARY", "C_DISPLACED", "C_NO_CRATE")
SCHED_CODES = ("S_OK", "S_NO_PLACEMENT", "S_NO_CRATE", "S_NO_WINDOW", "S_SHORT_WINDOW")
SETUP_DIV = 700           # crew setup quantum (documented)
DAY_SECONDS = 3600        # install seconds available per window day (documented)
CHAIN_SEED = "0" * 64
MASK64 = (1 << 64) - 1


class Xorshift64:
    def __init__(self, seed):
        self.state = (seed & MASK64) or 0x9E3779B97F4A7C15

    def next(self):
        x = self.state
        x ^= (x << 13) & MASK64
        x ^= x >> 7
        x ^= (x << 17) & MASK64
        self.state = x
        return x

    def below(self, n):
        return self.next() % n

    def between(self, lo, hi):
        return lo + self.below(hi - lo + 1)

    def pick(self, items):
        return items[self.below(len(items))]


# -------------------------------------------------------------- render helpers
def render_json(payload):
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def render_jsonl(rows):
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows)


def csv_text(header, rows):
    out = [",".join(header)]
    for row in rows:
        out.append(",".join(str(row[c]) for c in header))
    return "\n".join(out) + "\n"


def round_half_up(num, den):
    """Nearest integer to num/den with exact halves rounding up. num,den >= 0."""
    if den == 0:
        return 0
    q, r = divmod(num, den)
    if 2 * r >= den:
        q += 1
    return q


def reduce_ratio(num, den):
    """Reduced (num, den); den 0 yields the defined sentinel 0/1."""
    if den == 0:
        return 0, 1
    g = math.gcd(num, den)
    if g == 0:
        return 0, 1
    return num // g, den // g


# ================================================================ GENERATOR
def build_model(seed, opts):
    rng = Xorshift64(seed)
    n_obj = opts["objects"]
    lenders = opts["lenders"]
    materials = opts["materials"]        # list of (code, cons_class, max_lux, handling_class)
    units = opts.get("units", ["MM"])

    objects = []
    for i in range(n_obj):
        oid = "OBJ%02d" % (i + 1)
        mat = materials[i % len(materials)]
        lender = lenders[i % len(lenders)]
        unit = units[i % len(units)]
        objects.append({
            "object_id": oid,
            "lender_id": lender,
            "material_code": mat[0],
            "width": rng.between(20, 70),
            "height": rng.between(20, 80),
            "depth": rng.between(20, 60),
            "dim_unit": unit,
            "mass": rng.between(30, 120),
            "mass_unit": "KG",
            "declared_value": rng.between(40, 200) * 1000,
            "tip_allowed": 1 if rng.below(2) else 0,
            "rotate_allowed": 1 if rng.below(3) else 0,
        })
    for ov in opts.get("object_overrides", []):
        for o in objects:
            if o["object_id"] == ov["object_id"]:
                o.update(ov)

    return {
        "objects": objects,
        "materials": list(materials) + list(opts.get("extra_materials", [])),
        "lenders": lenders,
        "bays": opts["bays"],
        "bay_exceptions": opts.get("bay_exceptions", []),
        "crates": opts["crates"],
        "crate_reservations": opts.get("crate_reservations", []),
        "prior_state": opts.get("prior_state", []),
        "handling_classes": opts["handling_classes"],
        "conservation_rules": opts["conservation_rules"],
        "venue_windows": opts["venue_windows"],
        "dependency_rules": opts.get("dependency_rules", []),
        "crew_roster": opts["crew_roster"],
        "transport_rules": opts.get("transport_rules", []),
        "lender_master": opts["lender_master"],
        "value_class_rules": opts["value_class_rules"],
        "objective_order": ["placed_objects", "unschedulable_penalty",
                            "contention_penalty", "cycles_broken", "total_residual"],
    }


def render_model(m):
    files = {}
    files["horizon_config.json"] = render_json({
        "clearance_unit": "MM", "setup_div": SETUP_DIV, "chain_seed": CHAIN_SEED,
        "venue_end_convention": "exclusive", "conservation_end_convention": "inclusive",
    })
    objs = m["objects"]
    master_rows = [{"object_id": o["object_id"], "lender_id": o["lender_id"],
                    "material_code": o["material_code"]} for o in objs]
    # a stale duplicate identity row for the first object, kept after the true row,
    # carrying a conflicting lender the first-occurrence rule discards
    dup = dict(master_rows[0])
    dup["lender_id"] = m["lenders"][(m["lenders"].index(dup["lender_id"]) + 1) % len(m["lenders"])]
    master_rows.append(dup)
    files["object_master.csv"] = csv_text(
        ["object_id", "lender_id", "material_code"], master_rows)
    files["object_dimensions.csv"] = csv_text(
        ["object_id", "width", "height", "depth", "dim_unit"],
        [{"object_id": o["object_id"], "width": o["width"], "height": o["height"],
          "depth": o["depth"], "dim_unit": o["dim_unit"]} for o in objs])
    files["object_mass.csv"] = csv_text(
        ["object_id", "mass", "mass_unit"],
        [{"object_id": o["object_id"], "mass": o["mass"], "mass_unit": o["mass_unit"]} for o in objs])
    files["object_value.csv"] = csv_text(
        ["object_id", "declared_value"],
        [{"object_id": o["object_id"], "declared_value": o["declared_value"]} for o in objs])
    files["object_orientation.csv"] = csv_text(
        ["object_id", "tip_allowed", "rotate_allowed"],
        [{"object_id": o["object_id"], "tip_allowed": o["tip_allowed"],
          "rotate_allowed": o["rotate_allowed"]} for o in objs])
    # version map carries two rows for the first object; its version is the max
    ver_rows = [{"object_id": o["object_id"], "version": 2} for o in objs]
    ver_rows.append({"object_id": objs[0]["object_id"], "version": 1})
    files["object_version_map.csv"] = csv_text(
        ["object_id", "version"],
        sorted(ver_rows, key=lambda r: (r["object_id"], r["version"])))

    files["material_rules.csv"] = csv_text(
        ["material_code", "conservation_class", "max_lux", "handling_class"],
        [{"material_code": mt[0], "conservation_class": mt[1], "max_lux": mt[2],
          "handling_class": mt[3]} for mt in m["materials"]])
    files["handling_class_rules.csv"] = csv_text(
        ["handling_class", "clearance_w", "clearance_d", "clearance_h", "handle_margin", "crew_class", "setup_base"],
        m["handling_classes"])
    files["gallery_bays.csv"] = csv_text(
        ["bay_id", "interior_w", "interior_h", "interior_d", "lux_level", "bay_priority"],
        m["bays"])
    files["bay_exceptions.csv"] = csv_text(
        ["bay_id", "blocked_class"], m["bay_exceptions"])
    files["crate_catalog.csv"] = csv_text(
        ["crate_class", "inner_w", "inner_h", "inner_d", "count", "transport_mode"],
        m["crates"])
    files["crate_reservations.csv"] = csv_text(
        ["crate_class", "lender_id", "reserved_count"], m["crate_reservations"])
    files["prior_state.csv"] = csv_text(
        ["crate_class", "consumed_count"], m["prior_state"])
    files["conservation_rules.csv"] = csv_text(
        ["conservation_class", "setup_start", "setup_end", "needs_dark"],
        m["conservation_rules"])
    files["venue_windows.csv"] = csv_text(
        ["bay_id", "access_start", "access_end"], m["venue_windows"])
    files["dependency_rules.csv"] = csv_text(
        ["from_object", "to_object", "edge_priority"], m["dependency_rules"])
    files["crew_roster.csv"] = csv_text(
        ["crew_class", "avail_start", "avail_end"], m["crew_roster"])
    files["transport_rules.csv"] = csv_text(
        ["lender_id", "forbidden_mode", "fallback_mode"], m["transport_rules"])
    files["lender_master.csv"] = csv_text(
        ["lender_id", "insurance_rate_num", "insurance_rate_den", "pref_mode"],
        m["lender_master"])
    files["value_class_rules.csv"] = csv_text(
        ["value_class", "min_value", "unschedulable_penalty", "contention_penalty"],
        m["value_class_rules"])
    files["objective_order.json"] = render_json({
        "order": m["objective_order"],
        "sense": ["max", "min", "min", "min", "min"],
    })
    return files


# ================================================================ PARSER
def _read_csv(path):
    p = Path(path)
    if not p.is_file():
        return []
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split(",")
    return [dict(zip(header, ln.split(","))) for ln in lines[1:]]


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse(data_dir):
    d = Path(data_dir)
    cfg = _read_json(d / "horizon_config.json")

    version = {}
    for r in _read_csv(d / "object_version_map.csv"):
        version[r["object_id"]] = max(version.get(r["object_id"], 0), int(r["version"]))

    objects = {}
    order = []
    for r in _read_csv(d / "object_master.csv"):
        if r["object_id"] in objects:
            continue
        objects[r["object_id"]] = {"object_id": r["object_id"],
                                   "lender_id": r["lender_id"],
                                   "material_code": r["material_code"],
                                   "version": version.get(r["object_id"], 1)}
        order.append(r["object_id"])
    for r in _read_csv(d / "object_dimensions.csv"):
        o = objects[r["object_id"]]
        o["width"] = int(r["width"]); o["height"] = int(r["height"])
        o["depth"] = int(r["depth"]); o["dim_unit"] = r["dim_unit"]
    for r in _read_csv(d / "object_mass.csv"):
        o = objects[r["object_id"]]
        o["mass"] = int(r["mass"]); o["mass_unit"] = r["mass_unit"]
    for r in _read_csv(d / "object_value.csv"):
        objects[r["object_id"]]["declared_value"] = int(r["declared_value"])
    for r in _read_csv(d / "object_orientation.csv"):
        o = objects[r["object_id"]]
        o["tip_allowed"] = int(r["tip_allowed"]); o["rotate_allowed"] = int(r["rotate_allowed"])

    materials = {}
    for r in _read_csv(d / "material_rules.csv"):
        materials[r["material_code"]] = {
            "conservation_class": r["conservation_class"], "max_lux": int(r["max_lux"]),
            "handling_class": r["handling_class"]}

    handling = {}
    for r in _read_csv(d / "handling_class_rules.csv"):
        handling[r["handling_class"]] = {
            "clearance_w": int(r["clearance_w"]), "clearance_d": int(r["clearance_d"]),
            "clearance_h": int(r["clearance_h"]), "handle_margin": int(r["handle_margin"]),
            "crew_class": r["crew_class"], "setup_base": int(r["setup_base"])}

    bays = []
    for r in _read_csv(d / "gallery_bays.csv"):
        bays.append({"bay_id": r["bay_id"], "interior_w": int(r["interior_w"]),
                     "interior_h": int(r["interior_h"]), "interior_d": int(r["interior_d"]),
                     "lux_level": int(r["lux_level"]), "bay_priority": int(r["bay_priority"])})
    bay_block = {}
    for r in _read_csv(d / "bay_exceptions.csv"):
        bay_block.setdefault(r["bay_id"], set()).add(r["blocked_class"])

    crates = []
    for r in _read_csv(d / "crate_catalog.csv"):
        crates.append({"crate_class": r["crate_class"], "inner_w": int(r["inner_w"]),
                       "inner_h": int(r["inner_h"]), "inner_d": int(r["inner_d"]),
                       "count": int(r["count"]), "transport_mode": r["transport_mode"]})
    reservations = [{"crate_class": r["crate_class"], "lender_id": r["lender_id"],
                     "reserved_count": int(r["reserved_count"])}
                    for r in _read_csv(d / "crate_reservations.csv")]
    prior = {r["crate_class"]: int(r["consumed_count"])
             for r in _read_csv(d / "prior_state.csv")}

    cons = {}
    for r in _read_csv(d / "conservation_rules.csv"):
        cons[r["conservation_class"]] = {"setup_start": int(r["setup_start"]),
                                         "setup_end": int(r["setup_end"]),
                                         "needs_dark": int(r["needs_dark"])}
    venue = {}
    for r in _read_csv(d / "venue_windows.csv"):
        venue[r["bay_id"]] = {"access_start": int(r["access_start"]),
                              "access_end": int(r["access_end"])}
    deps = [{"from_object": r["from_object"], "to_object": r["to_object"],
             "edge_priority": int(r["edge_priority"])}
            for r in _read_csv(d / "dependency_rules.csv")]
    crew = {}
    for r in _read_csv(d / "crew_roster.csv"):
        crew[r["crew_class"]] = {"avail_start": int(r["avail_start"]),
                                 "avail_end": int(r["avail_end"])}
    transport = {}
    for r in _read_csv(d / "transport_rules.csv"):
        transport[r["lender_id"]] = {"forbidden_mode": r["forbidden_mode"],
                                     "fallback_mode": r["fallback_mode"]}
    lender = {}
    for r in _read_csv(d / "lender_master.csv"):
        lender[r["lender_id"]] = {"insurance_rate_num": int(r["insurance_rate_num"]),
                                  "insurance_rate_den": int(r["insurance_rate_den"]),
                                  "pref_mode": r["pref_mode"]}
    value_classes = []
    for r in _read_csv(d / "value_class_rules.csv"):
        value_classes.append({"value_class": r["value_class"], "min_value": int(r["min_value"]),
                              "unschedulable_penalty": int(r["unschedulable_penalty"]),
                              "contention_penalty": int(r["contention_penalty"])})
    value_classes.sort(key=lambda x: -x["min_value"])
    objective = _read_json(d / "objective_order.json")

    return {
        "cfg": cfg, "objects": objects, "order": order, "materials": materials,
        "handling": handling, "bays": bays, "bay_block": bay_block, "crates": crates,
        "reservations": reservations, "prior": prior, "cons": cons, "venue": venue,
        "deps": deps, "crew": crew, "transport": transport, "lender": lender,
        "value_classes": value_classes, "objective": objective,
    }


# ================================================================ ENGINE
def _um_dims(P, oid):
    o = P["objects"][oid]
    s = UNIT_SCALE[o["dim_unit"]]
    return o["width"] * s, o["height"] * s, o["depth"] * s


def _mass_g(P, oid):
    o = P["objects"][oid]
    return o["mass"] * MASS_SCALE[o["mass_unit"]]


def _clearance_env(P, oid):
    o = P["objects"][oid]
    hc = P["handling"][P["materials"][o["material_code"]]["handling_class"]]
    w, h, d = _um_dims(P, oid)
    cw = w + 2 * hc["clearance_w"] * 1000
    ch = h + hc["clearance_h"] * 1000
    cd = d + 2 * hc["clearance_d"] * 1000
    return cw, ch, cd


def _handling_env(P, oid):
    o = P["objects"][oid]
    hc = P["handling"][P["materials"][o["material_code"]]["handling_class"]]
    w, h, d = _um_dims(P, oid)
    m = hc["handle_margin"] * 1000
    return w + 2 * m, h + 2 * m, d + 2 * m


def _orientations(P, oid):
    o = P["objects"][oid]
    oris = [0]
    if o["rotate_allowed"]:
        oris.append(1)
    if o["tip_allowed"]:
        oris += [2, 3]
    return oris


def _oriented_footprint(cw, ch, cd, ori):
    # returns (foot_w, foot_d, vertical)
    if ori == 0:
        return cw, cd, ch
    if ori == 1:
        return cd, cw, ch
    if ori == 2:
        return cw, ch, cd
    return ch, cw, cd


def _fits(P, oid, bay, ori):
    cw, ch, cd = _clearance_env(P, oid)
    fw, fd, vert = _oriented_footprint(cw, ch, cd, ori)
    BW = bay["interior_w"] * 1000
    BH = bay["interior_h"] * 1000
    BD = bay["interior_d"] * 1000
    # width axis inclusive, depth axis exclusive, vertical inclusive
    return fw <= BW and fd < BD and vert <= BH


def geometry(P):
    norm_rows = []
    admiss_rows = []
    cand_rows = []
    for oid in P["order"]:
        w, h, d = _um_dims(P, oid)
        cw, ch, cd = _clearance_env(P, oid)
        num, den = reduce_ratio(h, w)  # aspect ratio height/width; width 0 -> sentinel
        norm_rows.append({"object_id": oid, "clearance_w": cw, "clearance_h": ch,
                          "clearance_d": cd, "aspect_num": num, "aspect_den": den})
        oris = _orientations(P, oid)
        admiss_rows.append({"object_id": oid, "orientations": oris,
                            "tip_allowed": P["objects"][oid]["tip_allowed"],
                            "rotate_allowed": P["objects"][oid]["rotate_allowed"]})
        for bay in sorted(P["bays"], key=lambda b: b["bay_id"]):
            fit_oris = [ori for ori in oris if _fits(P, oid, bay, ori)]
            cand_rows.append({"object_id": oid, "bay_id": bay["bay_id"],
                              "fits": bool(fit_oris),
                              "orientation": fit_oris[0] if fit_oris else -1})
    return norm_rows, admiss_rows, cand_rows


def _bay_of(P, oid, conservation):
    o = P["objects"][oid]
    mat = P["materials"][o["material_code"]]
    oris = _orientations(P, oid)
    best = None
    for bay in sorted(P["bays"], key=lambda b: (b["bay_priority"], b["bay_id"])):
        fit_oris = [ori for ori in oris if _fits(P, oid, bay, ori)]
        if not fit_oris:
            continue
        if conservation:
            if mat["conservation_class"] in P["bay_block"].get(bay["bay_id"], set()):
                continue
            if bay["lux_level"] > mat["max_lux"]:
                continue
            if P["cons"][mat["conservation_class"]]["needs_dark"] and bay["lux_level"] > 0:
                continue
        best = (bay["bay_id"], fit_oris[0])
        break
    return best


def placement(P):
    """Stage 5 geometry-only, stage 6 conservation-adjusted."""
    sel_rows = []
    rej_rows = []
    cons_rows = []
    placed = {}  # oid -> (bay_id, ori) after conservation
    for oid in P["order"]:
        geo = _bay_of(P, oid, conservation=False)
        if geo is None:
            oris = _orientations(P, oid)
            code = "P_NO_ORIENTATION" if len(oris) == 1 else "P_NO_FIT"
            rej_rows.append({"object_id": oid, "code": code})
            sel_rows.append({"object_id": oid, "bay_id": "", "orientation": -1, "code": code})
            cons_rows.append({"object_id": oid, "bay_id": "", "orientation": -1, "code": code})
            continue
        sel_rows.append({"object_id": oid, "bay_id": geo[0], "orientation": geo[1], "code": "P_OK"})
        con = _bay_of(P, oid, conservation=True)
        if con is None:
            cons_rows.append({"object_id": oid, "bay_id": "", "orientation": -1,
                              "code": "P_CONSERVATION_BLOCK"})
            rej_rows.append({"object_id": oid, "code": "P_CONSERVATION_BLOCK"})
        else:
            cons_rows.append({"object_id": oid, "bay_id": con[0], "orientation": con[1], "code": "P_OK"})
            placed[oid] = con
    rej_rows.sort(key=lambda r: (r["object_id"], r["code"]))
    return sel_rows, rej_rows, cons_rows, placed


def _handling_vol(P, oid):
    w, h, d = _handling_env(P, oid)
    return w * h * d


def _crate_vol_um3(cr):
    return (cr["inner_w"] * 1000) * (cr["inner_h"] * 1000) * (cr["inner_d"] * 1000)


def _crate_fits(P, oid, cr):
    w, h, d = _handling_env(P, oid)
    od = sorted((w, h, d))
    cdim = sorted((cr["inner_w"] * 1000, cr["inner_h"] * 1000, cr["inner_d"] * 1000))
    return all(od[i] <= cdim[i] for i in range(3))


def crates(P, placed, order_seq):
    cand_rows = []
    # remaining capacity per class after prior state
    remaining = {}
    for cr in P["crates"]:
        remaining[cr["crate_class"]] = cr["count"] - P["prior"].get(cr["crate_class"], 0)
    reserved = {}
    for rv in P["reservations"]:
        reserved[(rv["crate_class"], rv["lender_id"])] = \
            reserved.get((rv["crate_class"], rv["lender_id"]), 0) + rv["reserved_count"]

    result = {}       # oid -> (chosen_class, code, primary_class)
    assigned = {}     # oid -> crate_class
    # stage 8 consumes shared crate capacity in installation order, so a later
    # object sees only what earlier installs left. Row output stays object sorted.
    for oid in order_seq:
        if oid not in placed:
            continue
        fitting = [cr for cr in P["crates"] if _crate_fits(P, oid, cr)]
        cand = sorted(fitting, key=lambda cr: (_crate_vol_um3(cr) - _handling_vol(P, oid),
                                               cr["crate_class"]))
        primary = cand[0]["crate_class"] if cand else ""
        lender = P["objects"][oid]["lender_id"]

        def priority_key(cr):
            has_reserved = 0 if reserved.get((cr["crate_class"], lender), 0) > 0 else 1
            return (has_reserved, _crate_vol_um3(cr) - _handling_vol(P, oid), cr["crate_class"])

        chosen = None
        for cr in sorted(fitting, key=priority_key):
            if remaining.get(cr["crate_class"], 0) > 0:
                chosen = cr["crate_class"]
                break
        if chosen is None:
            result[oid] = ("", "C_NO_CRATE", primary)
            continue
        remaining[chosen] -= 1
        if reserved.get((chosen, lender), 0) > 0:
            reserved[(chosen, lender)] -= 1
        code = "C_PRIMARY" if chosen == primary else "C_DISPLACED"
        result[oid] = (chosen, code, primary)
        assigned[oid] = chosen

    assign_rows = []
    contention_rows = []
    for oid in sorted(P["order"]):
        if oid not in placed:
            continue
        fitting = [cr for cr in P["crates"] if _crate_fits(P, oid, cr)]
        cand = sorted(fitting, key=lambda cr: (_crate_vol_um3(cr) - _handling_vol(P, oid),
                                               cr["crate_class"]))
        for cr in cand:
            cand_rows.append({"object_id": oid, "crate_class": cr["crate_class"],
                              "residual": _crate_vol_um3(cr) - _handling_vol(P, oid)})
        chosen, code, primary = result[oid]
        assign_rows.append({"object_id": oid, "crate_class": chosen, "code": code,
                            "primary_class": primary})
        if code == "C_DISPLACED":
            contention_rows.append({"object_id": oid, "wanted_class": primary,
                                    "assigned_class": chosen})

    ledger_rows = []
    for cr in sorted(P["crates"], key=lambda c: c["crate_class"]):
        cc = cr["crate_class"]
        ledger_rows.append({"crate_class": cc, "catalog_count": cr["count"],
                            "prior_consumed": P["prior"].get(cc, 0),
                            "remaining": remaining[cc]})
    return cand_rows, assign_rows, ledger_rows, contention_rows, assigned


def transport(P, assigned):
    rows = []
    crate_mode = {cr["crate_class"]: cr["transport_mode"] for cr in P["crates"]}
    for oid in P["order"]:
        if oid not in assigned:
            continue
        cc = assigned[oid]
        lender = P["objects"][oid]["lender_id"]
        mode = crate_mode[cc]
        exception = 0
        tr = P["transport"].get(lender)
        if tr and tr["forbidden_mode"] == mode:
            mode = tr["fallback_mode"]
            exception = 1
        rows.append({"object_id": oid, "crate_class": cc, "mode": mode, "exception": exception})
    return rows


def dependencies(P, placed):
    edges = []
    for r in P["deps"]:
        if r["from_object"] in placed or r["to_object"] in placed or True:
            edges.append({"from": r["from_object"], "to": r["to_object"],
                          "kind": "D_RULE", "weight": r["edge_priority"]})
    # bay-grouped edges among conservation-placed objects
    by_bay = {}
    for oid in P["order"]:
        if oid in placed:
            by_bay.setdefault(placed[oid][0], []).append(oid)
    for bay_id in sorted(by_bay):
        members = sorted(by_bay[bay_id])
        for i in range(len(members)):
            for j in range(len(members)):
                if i == j:
                    continue
                a, b = members[i], members[j]
                # bay-size: larger handling volume installed before smaller
                va, vb = _handling_vol(P, a), _handling_vol(P, b)
                if va > vb or (va == vb and a < b):
                    edges.append({"from": a, "to": b, "kind": "D_BAYSIZE", "weight": va})
                # conservation: more sensitive class installed before less sensitive
                ca = CONS_CLASSES.index(P["materials"][P["objects"][a]["material_code"]]["conservation_class"])
                cb = CONS_CLASSES.index(P["materials"][P["objects"][b]["material_code"]]["conservation_class"])
                if ca > cb:
                    edges.append({"from": a, "to": b, "kind": "D_CONSERVATION", "weight": _handling_vol(P, a)})
    # de-duplicate (keep the highest-precedence kind per (from,to))
    best = {}
    for e in edges:
        k = (e["from"], e["to"])
        if k not in best or DEP_KIND_RANK[e["kind"]] < DEP_KIND_RANK[best[k]["kind"]]:
            best[k] = e
    out = [best[k] for k in sorted(best)]
    return out


def _find_cycle(nodes, edges):
    adj = {n: [] for n in nodes}
    for e in edges:
        if e["from"] in adj and e["to"] in adj:
            adj[e["from"]].append(e)
    for n in adj:
        adj[n].sort(key=lambda e: (e["to"],))
    color = {n: 0 for n in nodes}  # 0 white 1 grey 2 black
    stack_edges = []

    def dfs(u):
        color[u] = 1
        for e in adj[u]:
            v = e["to"]
            if color[v] == 0:
                stack_edges.append(e)
                r = dfs(v)
                if r is not None:
                    return r
                stack_edges.pop()
            elif color[v] == 1:
                # found cycle: collect edges from where v is on the stack
                cyc = []
                started = False
                for se in stack_edges:
                    if se["from"] == v:
                        started = True
                    if started:
                        cyc.append(se)
                cyc.append(e)
                return cyc
        color[u] = 2
        return None

    for n in sorted(nodes):
        if color[n] == 0:
            r = dfs(n)
            if r is not None:
                return r
    return None


def resolve_cycles(P, edges):
    nodes = list(P["order"])
    live = list(edges)
    removed = []
    guard = 0
    while True:
        cyc = _find_cycle(nodes, live)
        if cyc is None:
            break
        guard += 1
        if guard > 10000:
            raise RuntimeError("cycle resolution did not converge")
        victim = max(cyc, key=lambda e: (DEP_KIND_RANK[e["kind"]], e["weight"], e["from"], e["to"]))
        removed.append({"from": victim["from"], "to": victim["to"], "kind": victim["kind"],
                        "weight": victim["weight"]})
        live = [e for e in live if not (e["from"] == victim["from"] and e["to"] == victim["to"]
                                        and e["kind"] == victim["kind"])]
    return live, removed


def _setup_seconds(P, oid):
    o = P["objects"][oid]
    hc = P["handling"][P["materials"][o["material_code"]]["handling_class"]]
    return round_half_up(hc["setup_base"] * _mass_g(P, oid), SETUP_DIV)


def install_order(P, live_edges):
    nodes = list(P["order"])
    indeg = {n: 0 for n in nodes}
    adj = {n: [] for n in nodes}
    for e in live_edges:
        adj[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    setup = {n: _setup_seconds(P, n) for n in nodes}
    order = []
    ready = [n for n in nodes if indeg[n] == 0]
    ready.sort(key=lambda n: (setup[n], n))
    while ready:
        u = ready.pop(0)
        order.append(u)
        for v in sorted(adj[u]):
            indeg[v] -= 1
            if indeg[v] == 0:
                ready.append(v)
        ready.sort(key=lambda n: (setup[n], n))
    return order


def crew_and_windows(P, placed, assigned):
    crew_rows = []
    window_rows = []
    unsched_rows = []
    scheduled = {}
    for oid in P["order"]:
        o = P["objects"][oid]
        mat = P["materials"][o["material_code"]]
        hc = P["handling"][mat["handling_class"]]
        crew_class = hc["crew_class"]
        setup = _setup_seconds(P, oid)
        crew_rows.append({"object_id": oid, "crew_class": crew_class, "setup_seconds": setup})
        if oid not in placed:
            unsched_rows.append({"object_id": oid, "code": "S_NO_PLACEMENT"})
            continue
        if oid not in assigned:
            unsched_rows.append({"object_id": oid, "code": "S_NO_CRATE"})
            continue
        bay_id = placed[oid][0]
        cr = P["cons"][mat["conservation_class"]]
        vw = P["venue"][bay_id]
        cw = P["crew"][crew_class]
        lo = max(cr["setup_start"], vw["access_start"], cw["avail_start"])
        hi = min(cr["setup_end"], vw["access_end"] - 1, cw["avail_end"])
        required_days = (setup + DAY_SECONDS - 1) // DAY_SECONDS
        if lo <= hi and required_days <= (hi - lo + 1):
            window_rows.append({"object_id": oid, "bay_id": bay_id, "window_start": lo,
                                "window_end": hi, "required_days": required_days, "code": "S_OK"})
            scheduled[oid] = (lo, hi)
        elif lo > hi:
            window_rows.append({"object_id": oid, "bay_id": bay_id, "window_start": -1,
                                "window_end": -1, "required_days": required_days, "code": "S_NO_WINDOW"})
            unsched_rows.append({"object_id": oid, "code": "S_NO_WINDOW"})
        else:
            window_rows.append({"object_id": oid, "bay_id": bay_id, "window_start": lo,
                                "window_end": hi, "required_days": required_days, "code": "S_SHORT_WINDOW"})
            unsched_rows.append({"object_id": oid, "code": "S_SHORT_WINDOW"})
    unsched_rows.sort(key=lambda r: (r["object_id"], r["code"]))
    return crew_rows, window_rows, unsched_rows, scheduled


def _value_class(P, value):
    for vc in P["value_classes"]:
        if value >= vc["min_value"]:
            return vc
    return P["value_classes"][-1]


def _insurance_cents(P, oid):
    o = P["objects"][oid]
    ld = P["lender"][o["lender_id"]]
    value_cents = o["declared_value"] * 100
    return round_half_up(value_cents * ld["insurance_rate_num"], ld["insurance_rate_den"])


def compute(P, program_bytes=b""):
    norm_rows, admiss_rows, cand_rows = geometry(P)
    sel_rows, rej_rows, cons_rows, placed = placement(P)
    edges = dependencies(P, placed)
    live_edges, removed = resolve_cycles(P, edges)
    order = install_order(P, live_edges)
    order_rows = [{"position": i, "object_id": oid} for i, oid in enumerate(order)]
    crate_cand, assign_rows, crate_ledger, contention_rows, assigned = crates(P, placed, order)
    transport_rows = transport(P, assigned)
    crew_rows, window_rows, unsched_rows, scheduled = crew_and_windows(P, placed, assigned)

    # gallery occupancy
    occ = {}
    for oid in P["order"]:
        if oid in placed:
            occ.setdefault(placed[oid][0], []).append(oid)
    gallery_rows = []
    for bay in sorted(P["bays"], key=lambda b: b["bay_id"]):
        members = sorted(occ.get(bay["bay_id"], []))
        gallery_rows.append({"bay_id": bay["bay_id"], "object_count": len(members),
                             "objects": members})

    # penalties and objective
    unsched_penalty = 0
    for r in unsched_rows:
        vc = _value_class(P, P["objects"][r["object_id"]]["declared_value"])
        unsched_penalty += vc["unschedulable_penalty"]
    contention_penalty = 0
    for r in contention_rows:
        vc = _value_class(P, P["objects"][r["object_id"]]["declared_value"])
        contention_penalty += vc["contention_penalty"]
    cycles_broken = len(removed)
    total_insurance = sum(_insurance_cents(P, oid) for oid in P["order"])
    residual = 0
    crate_by_class = {cr["crate_class"]: cr for cr in P["crates"]}
    for oid in P["order"]:
        if oid in assigned:
            residual += _crate_vol_um3(crate_by_class[assigned[oid]]) - _handling_vol(P, oid)
    placed_count = len(scheduled)
    objective = [placed_count, unsched_penalty, contention_penalty, cycles_broken, residual]

    total_setup = sum(_setup_seconds(P, oid) for oid in P["order"])

    # deterministic audit
    audit_rows = []
    for oid in P["order"]:
        audit_rows.append({
            "object_id": oid,
            "placed_bay": placed[oid][0] if oid in placed else "",
            "crate_class": assigned.get(oid, ""),
            "displaced": 1 if any(c["object_id"] == oid for c in contention_rows) else 0,
            "scheduled": 1 if oid in scheduled else 0,
            "install_position": order.index(oid),
            "setup_seconds": _setup_seconds(P, oid),
            "insurance_cents": _insurance_cents(P, oid),
        })

    art = {}
    art["canonical_objects.jsonl"] = render_jsonl([
        {"object_id": oid, "lender_id": P["objects"][oid]["lender_id"],
         "material_code": P["objects"][oid]["material_code"],
         "version": P["objects"][oid]["version"],
         "declared_value_cents": P["objects"][oid]["declared_value"] * 100,
         "mass_g": _mass_g(P, oid)} for oid in P["order"]])
    art["normalized_geometry.jsonl"] = render_jsonl(norm_rows)
    art["admissibility_matrix.jsonl"] = render_jsonl(admiss_rows)
    art["bay_candidate_matrix.jsonl"] = render_jsonl(cand_rows)
    art["selected_placement.jsonl"] = render_jsonl(sel_rows)
    art["rejected_placement.jsonl"] = render_jsonl(rej_rows)
    art["conservation_adjusted.jsonl"] = render_jsonl(cons_rows)
    art["crate_candidate_matrix.jsonl"] = render_jsonl(crate_cand)
    art["crate_assignment.jsonl"] = render_jsonl(assign_rows)
    art["crate_capacity_ledger.jsonl"] = render_jsonl(crate_ledger)
    art["crate_contention_report.jsonl"] = render_jsonl(contention_rows)
    art["transport_leg_manifest.jsonl"] = render_jsonl(transport_rows)
    art["dependency_edges.jsonl"] = render_jsonl(live_edges)
    art["cycle_resolution_audit.jsonl"] = render_jsonl(removed)
    art["install_order.jsonl"] = render_jsonl(order_rows)
    art["crew_requirement_ledger.jsonl"] = render_jsonl(crew_rows)
    art["install_window_table.jsonl"] = render_jsonl(window_rows)
    art["unschedulable_report.jsonl"] = render_jsonl(unsched_rows)
    art["gallery_occupancy_map.jsonl"] = render_jsonl(gallery_rows)
    art["deterministic_audit.jsonl"] = render_jsonl(audit_rows)
    art["penalty_summary.json"] = render_json({
        "unschedulable_penalty": unsched_penalty,
        "contention_penalty": contention_penalty,
        "cycles_broken": cycles_broken,
        "total_insurance_cents": total_insurance,
    })
    art["objective_tuple.json"] = render_json({"objective": objective,
                                               "order": P["objective"]["order"]})

    hashed = tuple(sorted(list(art.keys()) + ["plan.py"]))
    manifest = {}
    for name in sorted(art):
        manifest[name] = hashlib.sha256(art[name].encode("utf-8")).hexdigest()
    manifest["plan.py"] = hashlib.sha256(program_bytes).hexdigest()
    art["integrity_manifest.json"] = render_json(manifest)
    chain = CHAIN_SEED
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
    final_cycle = _find_cycle(list(P["order"]), live_edges)
    art["final_state.json"] = render_json({
        "chain": chain,
        "repair_converged": final_cycle is None,
        "cycle_edges_removed": len(removed),
        "final_cycle_count": 0 if final_cycle is None else 1,
        "displacement_count": len(contention_rows),
        "unschedulable_count": len(unsched_rows),
        "placed_objects": placed_count,
        "total_setup_seconds": total_setup,
        "total_insurance_cents": total_insurance,
        "objective": objective,
    })
    return art


def write_artifacts(art, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in art.items():
        (out / name).write_text(text, encoding="utf-8", newline="")


def run(data_dir, out_dir, program_bytes=b""):
    P = parse(data_dir)
    art = compute(P, program_bytes)
    write_artifacts(art, out_dir)
    return art

# ==========================================================================
# VERIFIER: corpora, reference, harness and graded tests.
# This module owns its corpora. It rebuilds the shipped sample tier from
# SAMPLE_SEED and generates three reseeded tiers and hand built fixtures at
# grade time in temporary directories. It never reads the agent data
# directory, which does not exist in the verifier container. The delivered
# program is only executed as a subprocess with a timeout.
# ==========================================================================
HANDLING_CLASSES = [
    {"handling_class": "H_LIGHT", "clearance_w": 5, "clearance_d": 5, "clearance_h": 8,
     "handle_margin": 3, "crew_class": "CREW_A", "setup_base": 40},
    {"handling_class": "H_STD", "clearance_w": 8, "clearance_d": 8, "clearance_h": 12,
     "handle_margin": 5, "crew_class": "CREW_B", "setup_base": 55},
    {"handling_class": "H_HEAVY", "clearance_w": 12, "clearance_d": 12, "clearance_h": 18,
     "handle_margin": 8, "crew_class": "CREW_C", "setup_base": 70},
]
MATERIALS = [
    ("MAT_OIL", "L3", 150, "H_STD"),
    ("MAT_BRONZE", "L1", 3000, "H_HEAVY"),
    ("MAT_PAPER", "L3", 50, "H_LIGHT"),
    ("MAT_CERAMIC", "L2", 300, "H_STD"),
    ("MAT_TEXTILE", "L3", 80, "H_LIGHT"),
]
LENDERS = ["LDR_ALPHA", "LDR_BETA", "LDR_GAMMA"]
LENDER_MASTER = [
    {"lender_id": "LDR_ALPHA", "insurance_rate_num": 3, "insurance_rate_den": 700, "pref_mode": "ROAD"},
    {"lender_id": "LDR_BETA", "insurance_rate_num": 5, "insurance_rate_den": 1100, "pref_mode": "AIR"},
    {"lender_id": "LDR_GAMMA", "insurance_rate_num": 2, "insurance_rate_den": 900, "pref_mode": "ROAD"},
]
VALUE_CLASS_RULES = [
    {"value_class": "VC_HIGH", "min_value": 120000, "unschedulable_penalty": 9, "contention_penalty": 4},
    {"value_class": "VC_MID", "min_value": 70000, "unschedulable_penalty": 5, "contention_penalty": 2},
    {"value_class": "VC_LOW", "min_value": 0, "unschedulable_penalty": 2, "contention_penalty": 1},
]
BAYS = [
    {"bay_id": "BAY_1", "interior_w": 2000, "interior_h": 2600, "interior_d": 1500,
     "lux_level": 200, "bay_priority": 1},
    {"bay_id": "BAY_2", "interior_w": 2400, "interior_h": 3000, "interior_d": 1800,
     "lux_level": 60, "bay_priority": 2},
    {"bay_id": "BAY_3", "interior_w": 3000, "interior_h": 3200, "interior_d": 2200,
     "lux_level": 20, "bay_priority": 3},
]
BAY_EXCEPTIONS = [{"bay_id": "BAY_2", "blocked_class": "L1"}]
CRATES = [
    {"crate_class": "CR_S", "inner_w": 900, "inner_h": 1100, "inner_d": 800,
     "count": 2, "transport_mode": "AIR"},
    {"crate_class": "CR_M", "inner_w": 1300, "inner_h": 1500, "inner_d": 1200,
     "count": 3, "transport_mode": "ROAD"},
    {"crate_class": "CR_L", "inner_w": 1900, "inner_h": 2000, "inner_d": 1600,
     "count": 24, "transport_mode": "ROAD"},
]
CRATE_RESERVATIONS = [{"crate_class": "CR_M", "lender_id": "LDR_BETA", "reserved_count": 1}]
PRIOR_STATE = [{"crate_class": "CR_S", "consumed_count": 1}]
CONSERVATION_RULES = [
    {"conservation_class": "L1", "setup_start": 0, "setup_end": 20, "needs_dark": 0},
    {"conservation_class": "L2", "setup_start": 2, "setup_end": 16, "needs_dark": 0},
    {"conservation_class": "L3", "setup_start": 4, "setup_end": 14, "needs_dark": 0},
    {"conservation_class": "L4", "setup_start": 5, "setup_end": 5, "needs_dark": 0},
]
EXTRA_MATERIALS = [("MAT_FRAGILE", "L4", 3000, "H_HEAVY")]
VENUE_WINDOWS = [
    {"bay_id": "BAY_1", "access_start": 1, "access_end": 12},
    {"bay_id": "BAY_2", "access_start": 2, "access_end": 15},
    {"bay_id": "BAY_3", "access_start": 3, "access_end": 18},
]
CREW_ROSTER = [
    {"crew_class": "CREW_A", "avail_start": 0, "avail_end": 20},
    {"crew_class": "CREW_B", "avail_start": 1, "avail_end": 16},
    {"crew_class": "CREW_C", "avail_start": 3, "avail_end": 14},
]
TRANSPORT_RULES = [{"lender_id": "LDR_BETA", "forbidden_mode": "AIR", "fallback_mode": "SEA"}]
DEPENDENCY_RULES = [
    {"from_object": "OBJ01", "to_object": "OBJ02", "edge_priority": 10},
    {"from_object": "OBJ02", "to_object": "OBJ03", "edge_priority": 12},
    {"from_object": "OBJ03", "to_object": "OBJ01", "edge_priority": 14},
    {"from_object": "OBJ05", "to_object": "OBJ06", "edge_priority": 8},
]


def _common_opts():
    return {
        "handling_classes": [dict(x) for x in HANDLING_CLASSES],
        "materials": list(MATERIALS),
        "extra_materials": list(EXTRA_MATERIALS),
        "lenders": list(LENDERS),
        "lender_master": [dict(x) for x in LENDER_MASTER],
        "value_class_rules": [dict(x) for x in VALUE_CLASS_RULES],
        "bays": [dict(x) for x in BAYS],
        "bay_exceptions": [dict(x) for x in BAY_EXCEPTIONS],
        "crates": [dict(x) for x in CRATES],
        "crate_reservations": [dict(x) for x in CRATE_RESERVATIONS],
        "prior_state": [dict(x) for x in PRIOR_STATE],
        "conservation_rules": [dict(x) for x in CONSERVATION_RULES],
        "venue_windows": [dict(x) for x in VENUE_WINDOWS],
        "crew_roster": [dict(x) for x in CREW_ROSTER],
        "transport_rules": [dict(x) for x in TRANSPORT_RULES],
        "dependency_rules": [dict(x) for x in DEPENDENCY_RULES],
        "units": ["MM", "MM", "CM", "MM", "MM"],
    }


OUT_ROOT = Path("/app/out")
PROGRAM_PATH = OUT_ROOT / "plan.py"
TRAINING_CORPUS_PATH = "/app/data"
RUN_TIMEOUT_SEC = 180
SUBMITTER_UID = 10001
SUBMITTER_GID = 10001

JSONL_ARTIFACTS = (
    "canonical_objects.jsonl", "normalized_geometry.jsonl", "admissibility_matrix.jsonl",
    "bay_candidate_matrix.jsonl", "selected_placement.jsonl", "rejected_placement.jsonl",
    "conservation_adjusted.jsonl", "crate_candidate_matrix.jsonl", "crate_assignment.jsonl",
    "crate_capacity_ledger.jsonl", "crate_contention_report.jsonl", "transport_leg_manifest.jsonl",
    "dependency_edges.jsonl", "cycle_resolution_audit.jsonl", "install_order.jsonl",
    "crew_requirement_ledger.jsonl", "install_window_table.jsonl", "unschedulable_report.jsonl",
    "gallery_occupancy_map.jsonl", "deterministic_audit.jsonl",
)
JSON_ARTIFACTS = ("penalty_summary.json", "objective_tuple.json",
                  "integrity_manifest.json", "final_state.json")
DECLARED_ARTIFACTS = tuple("/app/out/" + n for n in (("plan.py",) + JSONL_ARTIFACTS + JSON_ARTIFACTS))
MANIFEST_NAMES = tuple(sorted(JSONL_ARTIFACTS + ("penalty_summary.json", "objective_tuple.json") + ("plan.py",)))

def _planted(n):
    """Three planted objects that sit exactly on a rule decision boundary so the
    boundary and unit rules bite on every tier: a depth exclusive bay boundary
    object, an inclusive crate boundary object, and a centimetre object whose
    correct scale forces bay two and crate large while a wrong scale drops it into
    bay one and a small crate."""
    return [
        {"object_id": "OBJ%02d" % (n - 3), "material_code": "MAT_FRAGILE",
         "width": 20, "height": 20, "depth": 20, "dim_unit": "MM",
         "mass": 36005, "mass_unit": "G", "declared_value": 100000,
         "tip_allowed": 0, "rotate_allowed": 0},
        {"object_id": "OBJ%02d" % (n - 2), "material_code": "MAT_CERAMIC",
         "width": 199, "height": 10, "depth": 10, "dim_unit": "CM",
         "mass": 40, "mass_unit": "KG", "declared_value": 100000,
         "tip_allowed": 0, "rotate_allowed": 0},
        {"object_id": "OBJ%02d" % (n - 1), "material_code": "MAT_CERAMIC",
         "width": 100, "height": 100, "depth": 1484, "dim_unit": "MM",
         "mass": 40, "mass_unit": "KG", "declared_value": 100000,
         "tip_allowed": 0, "rotate_allowed": 0},
        {"object_id": "OBJ%02d" % n, "material_code": "MAT_CERAMIC",
         "width": 1490, "height": 100, "depth": 100, "dim_unit": "MM",
         "mass": 40, "mass_unit": "KG", "declared_value": 100000,
         "tip_allowed": 0, "rotate_allowed": 0},
    ]


def _tier(seed, n):
    return (seed, dict(_common_opts(), objects=n, object_overrides=_planted(n)))


SAMPLE_SEED = 7200000000000000019
SAMPLE_OPTS = dict(_common_opts(), objects=12, object_overrides=_planted(12))

# The reseeded tiers are each graded by exactly one whole document conjunction test
# (every artifact of the tier compared once), so no observable is paid for twice.
# A single wrong rule diverges the plan on every unseen corpus, so the count of
# tiers is what carries the cascade rather than repeated grading of one tier.
_RESEED_BASE = 5100000000000000091
_RESEED_TIERS = [("hidden_a", 5100000000000000091, 14), ("hidden_b", 6700000000000000123, 11)]
for _k in range(3, 21):
    _RESEED_TIERS.append(("tier_%02d" % _k, _RESEED_BASE + _k * 0x9E3779B97F4A7C15,
                          11 + (_k * 5) % 7))
RESEED_NAMES = [t[0] for t in _RESEED_TIERS]

CORPUS = {"sample": (SAMPLE_SEED, SAMPLE_OPTS)}
for _name, _seed, _n in _RESEED_TIERS:
    CORPUS[_name] = _tier(_seed, _n)


# ------------------------------------------------------------- fixtures
def _fx_vec_prod(pid, cls, hc):
    return (pid, cls, hc)


def _fx_base():
    common = _common_opts()
    objs = [
        {"object_id": "OBJ01", "lender_id": "LDR_ALPHA", "material_code": "MAT_CERAMIC",
         "width": 40, "height": 60, "depth": 40, "dim_unit": "MM", "mass": 40, "mass_unit": "KG",
         "declared_value": 100000, "tip_allowed": 1, "rotate_allowed": 1},
        {"object_id": "OBJ02", "lender_id": "LDR_GAMMA", "material_code": "MAT_PAPER",
         "width": 30, "height": 50, "depth": 30, "dim_unit": "MM", "mass": 30, "mass_unit": "KG",
         "declared_value": 80000, "tip_allowed": 1, "rotate_allowed": 1},
        {"object_id": "OBJ03", "lender_id": "LDR_ALPHA", "material_code": "MAT_OIL",
         "width": 35, "height": 55, "depth": 25, "dim_unit": "MM", "mass": 50, "mass_unit": "KG",
         "declared_value": 130000, "tip_allowed": 1, "rotate_allowed": 1},
    ]
    m = dict(common)
    m["objects"] = objs
    m["dependency_rules"] = []
    m["crate_reservations"] = []
    m["prior_state"] = []
    m["crates"] = [
        {"crate_class": "CR_S", "inner_w": 900, "inner_h": 1100, "inner_d": 800,
         "count": 5, "transport_mode": "ROAD"},
        {"crate_class": "CR_M", "inner_w": 1300, "inner_h": 1500, "inner_d": 1200,
         "count": 5, "transport_mode": "ROAD"},
        {"crate_class": "CR_L", "inner_w": 1900, "inner_h": 2000, "inner_d": 1600,
         "count": 5, "transport_mode": "ROAD"},
    ]
    m["bay_exceptions"] = []
    m["objective_order"] = ["placed_objects", "unschedulable_penalty",
                            "contention_penalty", "cycles_broken", "total_residual"]
    return m


def _fx_zero_width():
    m = _fx_base()
    m["objects"][0]["width"] = 0
    return m


def _fx_single_orientation():
    m = _fx_base()
    m["objects"][0]["tip_allowed"] = 0
    m["objects"][0]["rotate_allowed"] = 0
    return m


def _fx_no_placement():
    m = _fx_base()
    m["objects"][0]["width"] = 5000
    m["objects"][0]["height"] = 5000
    m["objects"][0]["depth"] = 5000
    m["objects"][0]["rotate_allowed"] = 0
    m["objects"][0]["tip_allowed"] = 0
    return m


def _fx_exact_bay_boundary():
    m = _fx_base()
    # clearance width of OBJ01 must equal BAY_1 interior width exactly (inclusive ok),
    # and clearance depth equal BAY_1 interior depth exactly (exclusive -> excluded).
    # BAY_1 interior_w 2000mm, interior_d 1500mm. H_STD clearance_w=8 (x2=16mm),
    # clearance_d=8 (x2=16mm). So object width 2000-16=1984mm, depth 1500-16=1484mm.
    m["objects"][0]["material_code"] = "MAT_CERAMIC"  # H_STD
    m["objects"][0]["width"] = 1984
    m["objects"][0]["depth"] = 1484
    m["objects"][0]["height"] = 100
    m["objects"][0]["rotate_allowed"] = 0
    m["objects"][0]["tip_allowed"] = 0
    return m


def _fx_crate_exhaustion():
    m = _fx_base()
    m["crates"] = [
        {"crate_class": "CR_S", "inner_w": 900, "inner_h": 1100, "inner_d": 800,
         "count": 1, "transport_mode": "ROAD"},
        {"crate_class": "CR_M", "inner_w": 1300, "inner_h": 1500, "inner_d": 1200,
         "count": 1, "transport_mode": "ROAD"},
        {"crate_class": "CR_L", "inner_w": 1900, "inner_h": 2000, "inner_d": 1600,
         "count": 0, "transport_mode": "ROAD"},
    ]
    return m


def _fx_conservation_overturn():
    m = _fx_base()
    # OBJ03 is MAT_OIL (L3, max_lux 150). BAY_1 lux 200 > 150 -> geometry picks BAY_1
    # first (priority 1) but conservation must overturn it to a darker bay.
    m["objects"][0]["material_code"] = "MAT_OIL"
    return m


def _fx_cycle_after_conservation():
    m = _fx_base()
    # two objects share one bay; the more sensitive one is smaller, so the bay-size
    # edge (big->small) and the conservation edge (sensitive->insensitive) oppose,
    # forming a cycle that only exists once conservation classes are read. Both
    # materials tolerate the bright bay so they co-locate in BAY_1.
    m["materials"] = [("MAT_BIG", "L1", 3000, "H_HEAVY"),
                      ("MAT_SMALL", "L3", 3000, "H_LIGHT"),
                      ("MAT_OIL", "L3", 150, "H_STD")]
    m["objects"][0]["material_code"] = "MAT_BIG"     # L1 insensitive, larger
    m["objects"][0]["width"] = 60; m["objects"][0]["height"] = 70; m["objects"][0]["depth"] = 50
    m["objects"][1]["material_code"] = "MAT_SMALL"   # L3 sensitive, smaller
    m["objects"][1]["width"] = 25; m["objects"][1]["height"] = 30; m["objects"][1]["depth"] = 20
    m["objects"][2]["material_code"] = "MAT_OIL"
    m["bay_exceptions"] = []
    return m


def _fx_halfway_rounding():
    m = _fx_base()
    # H_STD setup_base 55, SETUP_DIV 700, mass 70 grams -> 55*70=3850, 3850/700=5.5
    # exactly, so round half up gives 6 and a truncating reading gives 5.
    m["objects"][0]["material_code"] = "MAT_CERAMIC"
    m["objects"][0]["mass"] = 70
    m["objects"][0]["mass_unit"] = "G"
    return m


FIXTURES = {
    "fixture_zero_width": _fx_zero_width,
    "fixture_single_orientation": _fx_single_orientation,
    "fixture_no_placement": _fx_no_placement,
    "fixture_exact_bay_boundary": _fx_exact_bay_boundary,
    "fixture_crate_exhaustion": _fx_crate_exhaustion,
    "fixture_conservation_overturn": _fx_conservation_overturn,
    "fixture_cycle_after_conservation": _fx_cycle_after_conservation,
    "fixture_halfway_rounding": _fx_halfway_rounding,
}


# ------------------------------------------------------------- corpora + runs
_CORPUS_CACHE = {}
_STAGED = {}
_RUNS = {}
_EXPECTED = {}


def _corpus(name):
    if name not in _CORPUS_CACHE:
        if name in CORPUS:
            seed, opts = CORPUS[name]
            _CORPUS_CACHE[name] = render_model(build_model(seed, opts))
        else:
            _CORPUS_CACHE[name] = render_model(FIXTURES[name]())
    return _CORPUS_CACHE[name]


def _stage(name, label="data"):
    key = (name, label)
    if key not in _STAGED:
        root = Path(tempfile.mkdtemp(prefix="mu-corpus-"))
        root.chmod(0o711)
        target = root / label
        target.mkdir(parents=True, exist_ok=True)
        for filename, text in _corpus(name).items():
            (target / filename).write_text(text, encoding="utf-8", newline="")
        _STAGED[key] = target
    return _STAGED[key]


def _program_bytes():
    return PROGRAM_PATH.read_bytes()


def _snapshot_program():
    try:
        return PROGRAM_PATH.read_bytes()
    except OSError:
        return None


PROGRAM_SNAPSHOT = _snapshot_program()


def _program_source():
    return PROGRAM_PATH.read_text(encoding="utf-8", errors="replace")


def _program_code_text():
    source = _program_source()
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, IndentationError):
        return source
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstrings.add((body[0].value.lineno, body[0].value.col_offset))
    kept = []
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start in docstrings:
            continue
        kept.append(tok.string)
    return "\n".join(kept)


def _invoke_unguarded(data_dir, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.parent.chmod(0o711)
    os.chown(out_dir, SUBMITTER_UID, SUBMITTER_GID)
    process = subprocess.Popen(
        ["setpriv", "--reuid=submitter", "--regid=submitter", "--clear-groups",
         "--no-new-privs", sys.executable, str(PROGRAM_PATH), str(data_dir), str(out_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=RUN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    finally:
        if process.poll() is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _invoke(data_dir, out_dir):
    assert PROGRAM_PATH.exists(), "no delivered program at %s" % PROGRAM_PATH
    assert PROGRAM_PATH.read_bytes() == PROGRAM_SNAPSHOT, "delivered program changed before this run"
    completed = _invoke_unguarded(data_dir, out_dir)
    assert PROGRAM_PATH.read_bytes() == PROGRAM_SNAPSHOT, "run replaced the delivered program"
    return completed


def _run_on(name, label="run"):
    key = (name, label)
    if key not in _RUNS:
        data_dir = _stage(name)
        out_dir = Path(tempfile.mkdtemp(prefix="mu-out-")) / label
        completed = _invoke(data_dir, out_dir)
        assert completed.returncode == 0, (
            "delivered program exited %d on %s: %s"
            % (completed.returncode, name, (completed.stderr or "")[-500:]))
        _RUNS[key] = (data_dir, out_dir)
    return _RUNS[key]


def _expected(name):
    if name not in _EXPECTED:
        _EXPECTED[name] = compute(parse(_stage(name)), _program_bytes())
    return _EXPECTED[name]


def _delivered_text(basename):
    return (OUT_ROOT / basename).read_text(encoding="utf-8")


def _produced_text(out_dir, basename):
    return (out_dir / basename).read_text(encoding="utf-8")


def _jsonl_rows(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _write_corpus(destination):
    target = Path(destination)
    if target.name != "data":
        target = target / "data"
    target.mkdir(parents=True, exist_ok=True)
    for filename, text in _corpus("sample").items():
        (target / filename).write_text(text, encoding="utf-8", newline="")
    return target


# ==========================================================================
# Weight 1: structure a competent attempt earns for free.
# ==========================================================================
def test_all_declared_artifacts_exist():
    """POSITIVE: every declared artifact exists at its exact path."""
    missing = [p for p in DECLARED_ARTIFACTS if not Path(p).is_file()]
    assert not missing, "missing declared artifacts: %s" % missing


def test_json_artifacts_parse():
    """POSITIVE: every JSON artifact parses to a dict or list."""
    for basename in JSON_ARTIFACTS:
        parsed = json.loads(_delivered_text(basename))
        assert isinstance(parsed, (dict, list)) and parsed, basename


def test_jsonl_artifacts_parse_line_by_line():
    """POSITIVE: every JSONL artifact is one object per line."""
    for basename in JSONL_ARTIFACTS:
        lines = _delivered_text(basename).splitlines()
        expected = _jsonl_rows(_expected("sample")[basename])
        assert len(lines) == len(expected), basename
        for i, line in enumerate(lines, 1):
            assert line.strip(), "%s line %d blank" % (basename, i)
            assert isinstance(json.loads(line), dict), "%s line %d" % (basename, i)


def test_program_is_a_runnable_script():
    """POSITIVE: plan.py is valid Python."""
    raw = _program_bytes()
    assert raw, "plan.py is empty"
    compile(raw, "plan.py", "exec")


def test_jsonl_serialization_is_canonical():
    """POSITIVE: JSONL rows use sorted keys and compact separators."""
    for basename in JSONL_ARTIFACTS:
        lines = _delivered_text(basename).splitlines()
        assert len(lines) == len(_jsonl_rows(_expected("sample")[basename])), basename
        for line in lines:
            if not line.strip():
                continue
            obj = json.loads(line)
            assert line == json.dumps(obj, sort_keys=True, separators=(",", ":")), basename


def test_json_serialization_is_canonical():
    """POSITIVE: JSON artifacts use two space indent, sorted keys, trailing newline."""
    for basename in JSON_ARTIFACTS:
        text = _delivered_text(basename)
        obj = json.loads(text)
        assert obj, basename
        assert text == json.dumps(obj, sort_keys=True, indent=2) + "\n", basename


def test_geometry_ratios_are_reduced_fractions():
    """POSITIVE: every aspect ratio pair is reduced and uses the zero sentinel."""
    rows = _jsonl_rows(_delivered_text("normalized_geometry.jsonl"))
    assert rows
    for row in rows:
        num, den = row["aspect_num"], row["aspect_den"]
        assert den >= 1
        g = num
        b = den
        while b:
            g, b = b, g % b
        assert abs(g) == 1 or num == 0, row


def test_placement_codes_in_enum():
    """POSITIVE: every placement code is a documented code."""
    for basename in ("selected_placement.jsonl", "conservation_adjusted.jsonl", "rejected_placement.jsonl"):
        expected = _jsonl_rows(_expected("sample")[basename])
        rows = _jsonl_rows(_delivered_text(basename))
        assert len(rows) == len(expected), basename
        if expected:
            assert rows, basename
        for row in rows:
            assert row["code"] in PLACE_CODES, (basename, row)


def test_crate_codes_in_enum():
    """POSITIVE: every crate assignment code is a documented code."""
    rows = _jsonl_rows(_delivered_text("crate_assignment.jsonl"))
    assert rows
    for row in rows:
        assert row["code"] in CRATE_CODES, row


def test_integrity_manifest_covers_declared_set():
    """POSITIVE: the manifest names exactly the hashed artifact set."""
    manifest = json.loads(_delivered_text("integrity_manifest.json"))
    assert tuple(sorted(manifest)) == MANIFEST_NAMES, sorted(manifest)


def test_final_state_has_required_keys():
    """POSITIVE: final_state carries the folded chain and the repair summary."""
    state = json.loads(_delivered_text("final_state.json"))
    for key in ("chain", "repair_converged", "cycle_edges_removed", "final_cycle_count",
                "displacement_count", "unschedulable_count", "placed_objects", "objective"):
        assert key in state, key


def test_objective_tuple_has_five_components():
    """POSITIVE: the objective tuple lists the five ordered components."""
    obj = json.loads(_delivered_text("objective_tuple.json"))
    assert len(obj["objective"]) == 5
    assert len(obj["order"]) == 5


# ==========================================================================
# Weight 3: mid derivations on the visible tier plus cross artifact checks.
# ==========================================================================
def _assert_visible(basename):
    expected = _expected("sample")[basename]
    assert _delivered_text(basename) == expected, basename
    # The delivered program, not hand placed files, must reproduce the visible outcome.
    _, out_dir = _run_on("sample")
    assert _produced_text(out_dir, basename) == expected, ("program rerun on sample", basename)


def test_canonical_objects_match():
    """POSITIVE: canonical objects after version precedence and unit conversion match."""
    _assert_visible("canonical_objects.jsonl")


def test_normalized_geometry_match():
    """POSITIVE: normalized geometry in micrometres matches."""
    _assert_visible("normalized_geometry.jsonl")


def test_admissibility_matrix_match():
    """POSITIVE: the orientation admissibility matrix matches."""
    _assert_visible("admissibility_matrix.jsonl")


def test_bay_candidate_matrix_match():
    """POSITIVE: the bay candidate fit matrix matches."""
    _assert_visible("bay_candidate_matrix.jsonl")


def test_selected_placement_match():
    """POSITIVE: the geometry only primary placement matches."""
    _assert_visible("selected_placement.jsonl")


def test_rejected_placement_match():
    """POSITIVE: the rejected placement reasons match."""
    _, out_dir = _run_on("fixture_no_placement")
    assert _produced_text(out_dir, "rejected_placement.jsonl") == _expected("fixture_no_placement")["rejected_placement.jsonl"]
    assert _jsonl_rows(_expected("fixture_no_placement")["rejected_placement.jsonl"])


def test_conservation_adjusted_match():
    """POSITIVE: the conservation adjusted placement matches."""
    _assert_visible("conservation_adjusted.jsonl")


def test_crate_candidate_matrix_match():
    """POSITIVE: the crate candidate residual matrix matches."""
    _assert_visible("crate_candidate_matrix.jsonl")


def test_crate_capacity_ledger_match():
    """POSITIVE: the crate capacity ledger after prior state matches."""
    _assert_visible("crate_capacity_ledger.jsonl")


def test_transport_leg_manifest_match():
    """POSITIVE: the transport leg manifest with routing fallbacks matches."""
    _assert_visible("transport_leg_manifest.jsonl")


def test_dependency_edges_match():
    """POSITIVE: the resolved dependency edge list matches."""
    _assert_visible("dependency_edges.jsonl")


def test_crew_requirement_ledger_match():
    """POSITIVE: the crew and rounded setup ledger matches."""
    _assert_visible("crew_requirement_ledger.jsonl")


def test_install_window_table_match():
    """POSITIVE: the reconciled install window table matches."""
    _assert_visible("install_window_table.jsonl")


def test_gallery_occupancy_map_match():
    """POSITIVE: the gallery occupancy map matches."""
    _assert_visible("gallery_occupancy_map.jsonl")


def test_manifest_digests_match_files():
    """POSITIVE: each manifest digest equals the sha256 of the named artifact."""
    manifest = json.loads(_delivered_text("integrity_manifest.json"))
    assert tuple(sorted(manifest)) == MANIFEST_NAMES
    for name, digest in manifest.items():
        if name == "plan.py":
            continue
        actual = hashlib.sha256(_delivered_text(name).encode("utf-8")).hexdigest()
        assert actual == digest, name


def test_final_state_chain_folds_manifest():
    """POSITIVE: the final state chain folds the manifest digests in order."""
    manifest = json.loads(_delivered_text("integrity_manifest.json"))
    chain = CHAIN_SEED
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
    assert json.loads(_delivered_text("final_state.json"))["chain"] == chain


def test_scheduled_objects_have_window():
    """POSITIVE: every scheduled audit row carries an install window row."""
    windows = {r["object_id"]: r for r in _jsonl_rows(_delivered_text("install_window_table.jsonl"))}
    audit_rows = _jsonl_rows(_delivered_text("deterministic_audit.jsonl"))
    assert audit_rows
    assert any(r["scheduled"] == 1 for r in audit_rows)
    for row in audit_rows:
        if row["scheduled"] == 1:
            assert row["object_id"] in windows and windows[row["object_id"]]["code"] == "S_OK", row


def test_displaced_have_contention_row():
    """POSITIVE: every displaced crate assignment has a contention report row."""
    displaced = {r["object_id"] for r in _jsonl_rows(_delivered_text("crate_assignment.jsonl"))
                 if r["code"] == "C_DISPLACED"}
    reported = {r["object_id"] for r in _jsonl_rows(_delivered_text("crate_contention_report.jsonl"))}
    expected_displaced = {r["object_id"] for r in _jsonl_rows(_expected("sample")["crate_assignment.jsonl"])
                          if r["code"] == "C_DISPLACED"}
    assert expected_displaced
    assert displaced == expected_displaced
    assert displaced == reported, displaced ^ reported


def test_install_order_is_permutation():
    """POSITIVE: the install order lists every object exactly once."""
    order = [r["object_id"] for r in _jsonl_rows(_delivered_text("install_order.jsonl"))]
    canon = [r["object_id"] for r in _jsonl_rows(_delivered_text("canonical_objects.jsonl"))]
    assert canon
    assert sorted(order) == sorted(canon) and len(order) == len(set(order)), order


def test_occupancy_matches_placement():
    """POSITIVE: the gallery occupancy lists exactly the conservation placed objects per bay."""
    by_bay = {}
    for row in _jsonl_rows(_delivered_text("conservation_adjusted.jsonl")):
        if row["code"] == "P_OK":
            by_bay.setdefault(row["bay_id"], []).append(row["object_id"])
    assert by_bay
    for row in _jsonl_rows(_delivered_text("gallery_occupancy_map.jsonl")):
        assert sorted(by_bay.get(row["bay_id"], [])) == sorted(row["objects"]), row


# ==========================================================================
# Weight 5: outcome and completeness on the visible tier.
# ==========================================================================
def test_visible_crate_assignment_match():
    """POSITIVE: the stateful crate assignment matches whole document."""
    _assert_visible("crate_assignment.jsonl")


def test_visible_crate_contention_report_match():
    """POSITIVE: the crate contention report matches whole document."""
    _assert_visible("crate_contention_report.jsonl")


def test_visible_cycle_resolution_audit_match():
    """POSITIVE: the cycle resolution audit matches whole document."""
    _assert_visible("cycle_resolution_audit.jsonl")


def test_visible_install_order_match():
    """POSITIVE: the resolved install order matches whole document."""
    _assert_visible("install_order.jsonl")


def test_visible_unschedulable_report_match():
    """POSITIVE: the unschedulable report matches whole document."""
    _assert_visible("unschedulable_report.jsonl")


def test_visible_deterministic_audit_match():
    """POSITIVE: the per object audit trail matches whole document."""
    _assert_visible("deterministic_audit.jsonl")


def test_visible_penalty_summary_match():
    """POSITIVE: the priced penalty summary matches."""
    _assert_visible("penalty_summary.json")


def test_visible_objective_tuple_match():
    """POSITIVE: the lexicographic objective tuple matches."""
    _assert_visible("objective_tuple.json")


def test_visible_final_state_match():
    """POSITIVE: the folded final state matches."""
    _assert_visible("final_state.json")


def test_visible_integrity_manifest_match():
    """POSITIVE: the integrity manifest matches."""
    _assert_visible("integrity_manifest.json")


def test_visible_repair_actually_happened():
    """POSITIVE: the shipped horizon needed a cycle break and a crate displacement."""
    state = json.loads(_delivered_text("final_state.json"))
    assert state["cycle_edges_removed"] > 0, "shipped horizon should need a cycle break"
    assert state["final_cycle_count"] == 0
    assert state["repair_converged"] is True
    assert state["displacement_count"] > 0, "shipped horizon should need a crate displacement"


def test_objective_recomputed_from_raw_outputs():
    """POSITIVE: the objective recomputed from raw artifacts equals the published tuple."""
    scheduled = sum(1 for r in _jsonl_rows(_delivered_text("install_window_table.jsonl"))
                    if r["code"] == "S_OK")
    published = json.loads(_delivered_text("objective_tuple.json"))["objective"]
    assert published[0] == scheduled, (published, scheduled)
    removed = len(_jsonl_rows(_delivered_text("cycle_resolution_audit.jsonl")))
    assert published[3] == removed, (published, removed)


def test_conservation_overturned_a_placement():
    """POSITIVE: at least one object is moved or blocked by the conservation pass."""
    sel = {r["object_id"]: r for r in _jsonl_rows(_delivered_text("selected_placement.jsonl"))}
    changed = 0
    for row in _jsonl_rows(_delivered_text("conservation_adjusted.jsonl")):
        s = sel.get(row["object_id"], {})
        if s.get("code") == "P_OK" and (row["code"] != "P_OK" or row["bay_id"] != s.get("bay_id")):
            changed += 1
    assert changed >= 1, "conservation pass changed no placement"


# ==========================================================================
# Weight 5: reseeded tiers. The delivered program is rerun once per unseen
# corpus and the whole derived report for that corpus is compared in a single
# conjunction test, so every artifact of a tier is graded exactly once and no
# observable is paid for twice. One wrong rule diverges the plan on every
# corpus, so the cascade rides the count of tiers, not repeated grading.
# ==========================================================================
def _assert_reseed(name, basename):
    _, out_dir = _run_on(name)
    assert _produced_text(out_dir, basename) == _expected(name)[basename], (name, basename)


def _assert_reseed_fullset(name):
    _, out_dir = _run_on(name)
    expected = _expected(name)
    for basename in JSONL_ARTIFACTS + JSON_ARTIFACTS:
        assert _produced_text(out_dir, basename) == expected[basename], (name, basename)


def test_hidden_a_fullset_match():
    """POSITIVE: on tier hidden_a every derived artifact matches whole document."""
    _assert_reseed_fullset("hidden_a")

def test_hidden_b_fullset_match():
    """POSITIVE: on tier hidden_b every derived artifact matches whole document."""
    _assert_reseed_fullset("hidden_b")

def test_tier_03_fullset_match():
    """POSITIVE: on tier tier_03 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_03")

def test_tier_04_fullset_match():
    """POSITIVE: on tier tier_04 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_04")

def test_tier_05_fullset_match():
    """POSITIVE: on tier tier_05 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_05")

def test_tier_06_fullset_match():
    """POSITIVE: on tier tier_06 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_06")

def test_tier_07_fullset_match():
    """POSITIVE: on tier tier_07 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_07")

def test_tier_08_fullset_match():
    """POSITIVE: on tier tier_08 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_08")

def test_tier_09_fullset_match():
    """POSITIVE: on tier tier_09 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_09")

def test_tier_10_fullset_match():
    """POSITIVE: on tier tier_10 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_10")

def test_tier_11_fullset_match():
    """POSITIVE: on tier tier_11 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_11")

def test_tier_12_fullset_match():
    """POSITIVE: on tier tier_12 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_12")

def test_tier_13_fullset_match():
    """POSITIVE: on tier tier_13 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_13")

def test_tier_14_fullset_match():
    """POSITIVE: on tier tier_14 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_14")

def test_tier_15_fullset_match():
    """POSITIVE: on tier tier_15 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_15")

def test_tier_16_fullset_match():
    """POSITIVE: on tier tier_16 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_16")

def test_tier_17_fullset_match():
    """POSITIVE: on tier tier_17 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_17")

def test_tier_18_fullset_match():
    """POSITIVE: on tier tier_18 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_18")

def test_tier_19_fullset_match():
    """POSITIVE: on tier tier_19 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_19")

def test_tier_20_fullset_match():
    """POSITIVE: on tier tier_20 every derived artifact matches whole document."""
    _assert_reseed_fullset("tier_20")


# ==========================================================================
# Weight 5: hand built fixtures carrying legal but unshipped states.
# ==========================================================================
def test_fixture_zero_width_sentinel():
    """POSITIVE: a zero width object yields the defined zero denominator sentinel."""
    _, out_dir = _run_on("fixture_zero_width")
    expected = _expected("fixture_zero_width")
    rows = {r["object_id"]: r for r in _jsonl_rows(expected["normalized_geometry.jsonl"])}
    assert rows["OBJ01"]["aspect_num"] == 0 and rows["OBJ01"]["aspect_den"] == 1, rows["OBJ01"]
    assert _produced_text(out_dir, "normalized_geometry.jsonl") == expected["normalized_geometry.jsonl"]


def test_fixture_single_orientation():
    """POSITIVE: an object with rotation and tipping removed keeps one orientation."""
    _, out_dir = _run_on("fixture_single_orientation")
    expected = _expected("fixture_single_orientation")
    rows = {r["object_id"]: r for r in _jsonl_rows(expected["admissibility_matrix.jsonl"])}
    assert rows["OBJ01"]["orientations"] == [0], rows["OBJ01"]
    assert _produced_text(out_dir, "admissibility_matrix.jsonl") == expected["admissibility_matrix.jsonl"]


def test_fixture_exact_bay_boundary():
    """POSITIVE: a footprint equal to the inclusive width and the exclusive depth is excluded."""
    _, out_dir = _run_on("fixture_exact_bay_boundary")
    expected = _expected("fixture_exact_bay_boundary")
    cand = {(r["object_id"], r["bay_id"]): r
            for r in _jsonl_rows(expected["bay_candidate_matrix.jsonl"])}
    assert cand[("OBJ01", "BAY_1")]["fits"] is False, cand[("OBJ01", "BAY_1")]
    assert _produced_text(out_dir, "bay_candidate_matrix.jsonl") == expected["bay_candidate_matrix.jsonl"]


def test_fixture_crate_exhaustion():
    """POSITIVE: once every crate class is spent an object is left without a crate."""
    _, out_dir = _run_on("fixture_crate_exhaustion")
    expected = _expected("fixture_crate_exhaustion")
    codes = [r["code"] for r in _jsonl_rows(expected["crate_assignment.jsonl"])]
    assert "C_NO_CRATE" in codes, codes
    for basename in ("crate_assignment.jsonl", "unschedulable_report.jsonl"):
        assert _produced_text(out_dir, basename) == expected[basename], basename


def test_fixture_conservation_overturn():
    """POSITIVE: a geometrically valid bright bay placement is overturned by conservation."""
    _, out_dir = _run_on("fixture_conservation_overturn")
    expected = _expected("fixture_conservation_overturn")
    sel = {r["object_id"]: r for r in _jsonl_rows(expected["selected_placement.jsonl"])}
    con = {r["object_id"]: r for r in _jsonl_rows(expected["conservation_adjusted.jsonl"])}
    assert sel["OBJ01"]["bay_id"] == "BAY_1"
    assert con["OBJ01"]["bay_id"] != "BAY_1", con["OBJ01"]
    for basename in ("selected_placement.jsonl", "conservation_adjusted.jsonl"):
        assert _produced_text(out_dir, basename) == expected[basename], basename


def test_fixture_cycle_after_conservation():
    """POSITIVE: a conservation edge completes a cycle that the break rule resolves."""
    _, out_dir = _run_on("fixture_cycle_after_conservation")
    expected = _expected("fixture_cycle_after_conservation")
    removed = _jsonl_rows(expected["cycle_resolution_audit.jsonl"])
    assert any(r["kind"] == "D_BAYSIZE" for r in removed), removed
    for basename in ("dependency_edges.jsonl", "cycle_resolution_audit.jsonl", "install_order.jsonl"):
        assert _produced_text(out_dir, basename) == expected[basename], basename


def test_fixture_halfway_rounding():
    """POSITIVE: a setup value exactly halfway between quanta rounds up."""
    _, out_dir = _run_on("fixture_halfway_rounding")
    expected = _expected("fixture_halfway_rounding")
    rows = {r["object_id"]: r for r in _jsonl_rows(expected["crew_requirement_ledger.jsonl"])}
    assert rows["OBJ01"]["setup_seconds"] == 6, rows["OBJ01"]
    assert _produced_text(out_dir, "crew_requirement_ledger.jsonl") == expected["crew_requirement_ledger.jsonl"]


# ==========================================================================
# Negative probes. Polarity inverted: the assertion holds only when the cheat
# is present, so honest work makes these FAIL and no penalty applies.
# ==========================================================================
def test_negative_weight_hardcoded_sample_answers():
    """NEGATIVE: fires when the shipped answers are baked in and nothing derives."""
    expected = _expected("sample")
    for basename in ("crate_assignment.jsonl", "final_state.json", "cycle_resolution_audit.jsonl"):
        assert _delivered_text(basename) == expected[basename]
    visible = _delivered_text("final_state.json")
    replayed = PROGRAM_PATH.read_bytes() != PROGRAM_SNAPSHOT
    frozen = 0
    for name in ("hidden_a", "hidden_b"):
        out_dir = Path(tempfile.mkdtemp(prefix="mu-cheat-")) / "out"
        _invoke_unguarded(_stage(name), out_dir)
        produced = out_dir / "final_state.json"
        if produced.is_file() and produced.read_text(encoding="utf-8") == visible:
            frozen += 1
    assert replayed or frozen == 2, "the program produced corpus specific plans"


def test_negative_weight_reads_training_corpus_at_runtime():
    """NEGATIVE: fires when the delivered program pins the shipped data path as a code literal."""
    assert TRAINING_CORPUS_PATH in _program_code_text()


def test_negative_weight_filename_dependent_behaviour():
    """NEGATIVE: fires when the same bytes give different output under another directory
    name while each name stays stable across repeated runs. The within name stability
    requirement stops a nondeterministic program, which the determinism probe already
    penalises, from also tripping this one, so a single fault is never penalised twice."""
    first = _stage("hidden_a", "data")
    second = _stage("hidden_a", "loan_snapshot_2031")

    def _run(data_dir, tag):
        out_dir = Path(tempfile.mkdtemp(prefix="mu-name-")) / tag
        _invoke_unguarded(data_dir, out_dir)
        return {b: (out_dir / b).read_bytes() if (out_dir / b).is_file() else None
                for b in ("crate_assignment.jsonl", "final_state.json")}

    first_1, first_2 = _run(first, "data_a"), _run(first, "data_b")
    second_1, second_2 = _run(second, "name_a"), _run(second, "name_b")
    stable_within_name = first_1 == first_2 and second_1 == second_2
    differs_across_names = first_1 != second_1
    assert stable_within_name and differs_across_names, "output not filename dependent"


def test_negative_weight_nondeterministic_output():
    """NEGATIVE: fires when four runs on identical input disagree."""
    data_dir = _stage("fixture_crate_exhaustion")
    sigs = []
    for i in range(4):
        out_dir = Path(tempfile.mkdtemp(prefix="mu-det-")) / ("pass%d" % i)
        _invoke_unguarded(data_dir, out_dir)
        sigs.append({b: (out_dir / b).read_bytes() if (out_dir / b).is_file() else None
                     for b in MANIFEST_NAMES if b != "plan.py"})
    assert any(s != sigs[0] for s in sigs[1:]), "four runs produced identical bytes"


if __name__ == "__main__":
    _write_corpus(Path(sys.argv[1]))
