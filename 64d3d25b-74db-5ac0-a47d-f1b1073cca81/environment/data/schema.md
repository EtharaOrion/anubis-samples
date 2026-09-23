# Record shapes and serialization

This documents the shapes of the shipped input files and the shapes of the artifacts
you must emit. The derivation itself is in `rules.md`.

## Serialization

Every artifact whose name ends in `.jsonl` is one JSON object per line, each object
serialized with keys in sorted order and compact separators, one object per input
record, with a trailing newline after every line. Every artifact whose name ends in
`.json` is a single JSON value serialized with keys in sorted order, two space indent,
and a trailing newline. No artifact contains any floating point number; ratios are
carried as separate integer numerator and denominator fields, money as integer cents,
lengths as integer micrometres, time as integer seconds or integer day indices.

Two encoded fields follow the conventions in `rules.md`. An `orientation` field is the
integer index defined in stage 3 (0 base, 1 rotation, 2 and 3 the two tips), and a
rejected object carries orientation minus one and an empty string bay. The `weight` field
on a dependency edge is the tie break value defined in stage 10: an explicit rule edge
carries its edge priority, and a bay size or conservation edge carries the handling volume
in cubic micrometres of its from object.

## Input files

The shipped snapshot under the data directory holds these flat files. Columns are the
first line of each csv.

- `horizon_config.json` clearance unit, setup quantum, chain seed, the venue end and
  conservation end conventions.
- `object_master.csv` object_id, lender_id, material_code. A stale duplicate row for
  one object id with a conflicting lender is appended after the true row; the first
  occurrence wins.
- `object_dimensions.csv` object_id, width, height, depth, dim_unit.
- `object_mass.csv` object_id, mass, mass_unit.
- `object_value.csv` object_id, declared_value.
- `object_orientation.csv` object_id, tip_allowed, rotate_allowed.
- `object_version_map.csv` object_id, version.
- `material_rules.csv` material_code, conservation_class, max_lux, handling_class.
- `handling_class_rules.csv` handling_class, clearance_w, clearance_d, clearance_h,
  handle_margin, crew_class, setup_base. Margins are in millimetres.
- `gallery_bays.csv` bay_id, interior_w, interior_h, interior_d, lux_level, bay_priority.
  Interior dimensions are in millimetres.
- `bay_exceptions.csv` bay_id, blocked_class.
- `crate_catalog.csv` crate_class, inner_w, inner_h, inner_d, count, transport_mode.
  Interior dimensions are in millimetres.
- `crate_reservations.csv` crate_class, lender_id, reserved_count.
- `prior_state.csv` crate_class, consumed_count.
- `conservation_rules.csv` conservation_class, setup_start, setup_end, needs_dark.
- `venue_windows.csv` bay_id, access_start, access_end.
- `dependency_rules.csv` from_object, to_object, edge_priority.
- `crew_roster.csv` crew_class, avail_start, avail_end.
- `transport_rules.csv` lender_id, forbidden_mode, fallback_mode.
- `lender_master.csv` lender_id, insurance_rate_num, insurance_rate_den, pref_mode.
- `value_class_rules.csv` value_class, min_value, unschedulable_penalty, contention_penalty.
- `objective_order.json` the ordered objective component names and their senses.

## Output artifacts

All twenty four derived artifacts, plus the program, are written under the output
directory. The fields each record carries are below.

- `canonical_objects.jsonl` object_id, lender_id, material_code, version,
  declared_value_cents, mass_g.
- `normalized_geometry.jsonl` object_id, clearance_w, clearance_h, clearance_d,
  aspect_num, aspect_den.
- `admissibility_matrix.jsonl` object_id, orientations, tip_allowed, rotate_allowed.
- `bay_candidate_matrix.jsonl` object_id, bay_id, fits, orientation.
- `selected_placement.jsonl` object_id, bay_id, orientation, code.
- `rejected_placement.jsonl` object_id, code.
- `conservation_adjusted.jsonl` object_id, bay_id, orientation, code.
- `crate_candidate_matrix.jsonl` object_id, crate_class, residual.
- `crate_assignment.jsonl` object_id, crate_class, code, primary_class.
- `crate_capacity_ledger.jsonl` crate_class, catalog_count, prior_consumed, remaining.
- `crate_contention_report.jsonl` object_id, wanted_class, assigned_class.
- `transport_leg_manifest.jsonl` object_id, crate_class, mode, exception.
- `dependency_edges.jsonl` from, to, kind, weight.
- `cycle_resolution_audit.jsonl` from, to, kind, weight.
- `install_order.jsonl` position, object_id.
- `crew_requirement_ledger.jsonl` object_id, crew_class, setup_seconds.
- `install_window_table.jsonl` object_id, bay_id, window_start, window_end,
  required_days, code.
- `unschedulable_report.jsonl` object_id, code.
- `gallery_occupancy_map.jsonl` bay_id, object_count, objects.
- `deterministic_audit.jsonl` object_id, placed_bay, crate_class, displaced, scheduled,
  install_position, setup_seconds, insurance_cents.
- `penalty_summary.json` unschedulable_penalty, contention_penalty, cycles_broken,
  total_insurance_cents.
- `objective_tuple.json` objective, order.
- `integrity_manifest.json` a map from each hashed artifact name to its sha256 hex digest.
- `final_state.json` chain, repair_converged, cycle_edges_removed, final_cycle_count,
  displacement_count, unschedulable_count, placed_objects, total_setup_seconds,
  total_insurance_cents, objective.

## Row order

Where a rule does not fix it, the rows of an artifact follow the order object ids first
appear in `object_master.csv`. The exceptions are these. `bay_candidate_matrix` lists the
bays of each object in ascending `bay_id`. `crate_candidate_matrix` orders each object's
crates by ascending residual then ascending `crate_class`. `crate_candidate_matrix`,
`crate_assignment`, `crate_contention_report`, `rejected_placement` and
`unschedulable_report` are emitted in ascending `object_id`, and `rejected_placement` and
`unschedulable_report` break ties by `code`. `crate_capacity_ledger` is in ascending
`crate_class`. `gallery_occupancy_map` lists the bays in ascending `bay_id` and the objects
inside each bay in ascending `object_id`. `dependency_edges` is in ascending `from` then
`to`. `cycle_resolution_audit` lists the removed edges in the order they were removed.
`install_order` is in ascending install position.
