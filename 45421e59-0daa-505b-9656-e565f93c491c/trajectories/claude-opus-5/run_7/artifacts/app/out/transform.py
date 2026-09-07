#!/usr/bin/env python3
"""Incident triage reconstruction for the crash-reporter fleet.

Usage:  python3 transform.py [DATA_DIR] [OUT_DIR]

Reads a corpus directory holding the three shipped tiers (incident, control
slice, sample) plus the five reference tables, settles the nine withheld wire
parameters against the published control figures, then derives and writes the
whole triage set into the output directory.

Only the Python standard library is used.  All clock, byte and residency
arithmetic is carried in Python integers.
"""

import csv
import json
import os
import sys

# --------------------------------------------------------------------------
# fixed facts of the reporter and the report set
# --------------------------------------------------------------------------

TICKS_PER_MS = 1000
TICKS_PER_BUILD = 900000000          # a build index covers 900000000 ticks
SIG_HEX_WIDTH = 16
CHECK_MODULUS = 65536
SIGNATURE_FIELD_MODULUS = 1000000007
COUNT_BIAS_MODULUS = 9

RADIX_DIGITS = '0123456789abcdefghijklmnopqrstuvwxyz'
HEX_DIGITS = frozenset('0123456789abcdef')

# dump flag word
F_SPAN_DIFF2 = 0x001
F_ENTRY_ACC = 0x002
F_UNWIND_DRIFT = 0x004
F_ROTATED = 0x008
F_COUNT_BIAS = 0x010
F_ALT_RADIX = 0x020
F_ALIAS = 0x100
F_SPLIT_BASE = 0x800

# frame flag word
FF_INDEX_IN_SYMBOL = 0x040
FF_SIG_ROTATED = 0x080
FF_COARSE_ALLOC = 0x200
FF_SIG_RING = 0x400

REJECTION_KEYS = (
    'outside_support',
    'retired_build',
    'unprobed_frame',
    'unreported_dump',
    'unresolved_module',
)

CONTROL_KEYS = ('CTRL-A', 'CTRL-B', 'CTRL-C', 'CTRL-D', 'CTRL-E', 'CTRL-F',
                'CTRL-G', 'CTRL-H', 'CTRL-I', 'CTRL-J', 'CTRL-K')

# The candidate domain of each withheld wire parameter, as published with the
# control slice.  Keys are the internal parameter names; ``PARAM_LABELS`` maps
# them onto the names written into the derivation record.
CANDIDATE_DOMAINS = {
    'entry_clock_modulus': [1 << k for k in range(22, 35)],
    'drift_period': [48, 96, 192, 384, 768],
    'rotation_offset': list(range(0, 8)),
    'count_multiplier': list(range(0, 9)),
    'alternate_radix': list(range(11, 36)),
    'signature_offset': list(range(0, 8)),
    'coarse_unit': [64, 128, 256, 512, 1024],
    'ring_width': [4, 8, 12, 16],
    'base_shift_width': [8, 10, 12, 16],
}

# Each stage settles one or more parameters against a single control figure.
# The stages run in dependency order: a stage only needs parameters settled by
# itself or by an earlier stage.
SETTLEMENT_STAGES = (
    (('count_multiplier',), 'CTRL-A'),
    (('alternate_radix', 'base_shift_width'), 'CTRL-B'),
    (('rotation_offset',), 'CTRL-C'),
    (('entry_clock_modulus',), 'CTRL-D'),
    (('drift_period',), 'CTRL-E'),
    (('coarse_unit',), 'CTRL-F'),
    (('ring_width', 'signature_offset'), 'CTRL-G'),
)

# order in which parameters are reported in the derivation record
PARAM_REPORT_ORDER = tuple(sorted(CANDIDATE_DOMAINS))

TIER_FILES = {
    'incident': '',
    'calibration': 'calibration_',
    'sample': 'sample_',
}

ZONE_REPORT_PREFIX = 'region_'
SEGMENT_REPORT_PREFIX = 'segment_'


class WireError(ValueError):
    """A decoded field contradicts the reporter rules or a global invariant."""


# --------------------------------------------------------------------------
# reading the exports
# --------------------------------------------------------------------------

def read_dump_export(path):
    """Parse a pipe-delimited reporter export into a list of dump records."""
    dumps = []
    current = None
    with open(path, 'r', encoding='utf-8') as handle:
        for raw in handle:
            line = raw.rstrip('\n').rstrip('\r')
            if not line.strip() or line.startswith('#'):
                continue
            cols = line.split('|')
            if cols[0] == '@':
                if len(cols) != 8:
                    raise WireError('dump header does not carry seven columns')
                current = {
                    'dump_id': cols[1],
                    'module_column': cols[2],
                    'declared_count': int(cols[3]),
                    'base_field': cols[4],
                    'flags': int(cols[5], 16),
                    'key': int(cols[6]),
                    'check_total': int(cols[7], 16),
                    'lines': [],
                }
                dumps.append(current)
            else:
                if current is None:
                    raise WireError('frame line before any dump header')
                if len(cols) != 9:
                    raise WireError('frame line does not carry nine columns')
                current['lines'].append(cols)
    return dumps


def frame_check_total(cols):
    """Sum of the character codes of the eight preceding columns, joined."""
    return sum(ord(ch) for ch in '|'.join(cols[:8])) % CHECK_MODULUS


def parse_in_radix(text, radix):
    value = 0
    for ch in text:
        digit = RADIX_DIGITS.find(ch)
        if digit < 0 or digit >= radix:
            raise WireError('numeral digit outside the alternate radix')
        value = value * radix + digit
    return value


def rotate_right(seq, k):
    n = len(seq)
    k %= n
    if not k:
        return list(seq)
    return list(seq[n - k:]) + list(seq[:n - k])


# --------------------------------------------------------------------------
# decoding, stage by stage
#
# Each stage needs only the parameters settled up to that point, so the
# settlement search can drive the stages one control figure at a time.
# --------------------------------------------------------------------------

def stage_frame_counts(dumps, count_multiplier):
    """How many of a dump's lines are fault frames, checked against the totals."""
    counts = []
    for ordinal, dump in enumerate(dumps, start=1):
        count = dump['declared_count']
        if dump['flags'] & F_COUNT_BIAS:
            count -= (ordinal * count_multiplier) % COUNT_BIAS_MODULUS
        available = len(dump['lines'])
        if count < 1 or count > available:
            raise WireError('fault frame count outside the lines available')
        if (dump['flags'] & F_SPLIT_BASE) and count == available:
            raise WireError('split-base dump carries no trailer line')
        frames = dump['lines'][:count]
        for cols in frames:
            if frame_check_total(cols) != int(cols[8], 16):
                raise WireError('frame check total mismatch')
        total = sum(int(cols[8], 16) for cols in frames) % CHECK_MODULUS
        if total != dump['check_total']:
            raise WireError('dump check total mismatch')
        counts.append(count)
    return counts


def stage_logical_frames(dumps, counts, rotation_offset):
    """Un-rotate each dump's frame lines and recover authoritative indices."""
    per_dump = []
    for dump, count in zip(dumps, counts):
        frames = dump['lines'][:count]
        if dump['flags'] & F_ROTATED:
            frames = rotate_right(frames, (dump['key'] + rotation_offset) % count)
        records = []
        for position, cols in enumerate(frames):
            frame_flags = int(cols[6], 16)
            symbol_kind, _, tail = cols[7].rpartition(':')
            if frame_flags & FF_INDEX_IN_SYMBOL:
                frame_index = int(tail)
            else:
                frame_index = int(cols[0])
            records.append({
                'cols': cols,
                'frame_flags': frame_flags,
                'frame_index': frame_index,
                'symbol': symbol_kind,
                'source_dump': dump['dump_id'],
                'logical_position': position,
            })
        per_dump.append(records)
    return per_dump


def stage_resident_spans(dumps, per_dump):
    """Undo the second-order differencing of the resident span column."""
    for dump, records in zip(dumps, per_dump):
        written = [int(rec['cols'][3]) for rec in records]
        if dump['flags'] & F_SPAN_DIFF2:
            spans = [written[0]]
            if len(written) > 1:
                spans.append(spans[0] + written[1])
            for i in range(2, len(written)):
                spans.append(2 * spans[i - 1] - spans[i - 2] + written[i])
        else:
            spans = list(written)
        for rec, span in zip(records, spans):
            if span < 0:
                raise WireError('negative resident span')
            rec['resident_span'] = span


def stage_dump_bases(dumps, counts, alternate_radix, base_shift_width):
    """Recover each dump's base tick, joining split bases with their trailer."""
    bases = []
    for dump, count in zip(dumps, counts):
        field = dump['base_field']
        if dump['flags'] & F_ALT_RADIX:
            high = parse_in_radix(field, alternate_radix)
        else:
            high = int(field)
        if dump['flags'] & F_SPLIT_BASE:
            trailers = dump['lines'][count:]
            if not trailers:
                raise WireError('split-base dump carries no trailer line')
            low = int(trailers[0][0])
            if low < 0 or low >= (1 << base_shift_width):
                raise WireError('low part of the dump base does not fit')
            base = (high << base_shift_width) | low
        else:
            base = high
        if base < 0:
            raise WireError('negative dump base')
        bases.append(base)
    return bases


def stage_entry_stamps(dumps, per_dump, entry_clock_modulus):
    """Undo the running accumulation of the entry column."""
    for dump, records in zip(dumps, per_dump):
        written = [int(rec['cols'][1]) for rec in records]
        if dump['flags'] & F_ENTRY_ACC:
            stamps = [written[0] % entry_clock_modulus]
            for i in range(1, len(written)):
                stamps.append((written[i] - stamps[i - 1]) % entry_clock_modulus)
        else:
            stamps = list(written)
        for rec, stamp in zip(records, stamps):
            if not 0 <= stamp < entry_clock_modulus:
                raise WireError('entry stamp outside the wrapping clock')
            rec['enter_ns'] = stamp


def stage_capture_ticks(per_dump, bases, entry_clock_modulus):
    """Dump base plus the wrapping distance from the dump's first entry stamp."""
    for records, base in zip(per_dump, bases):
        first = records[0]['enter_ns']
        for rec in records:
            offset = (rec['enter_ns'] - first) % entry_clock_modulus
            rec['capture_tick'] = base + offset
            rec['build_index'] = rec['capture_tick'] // TICKS_PER_BUILD


def stage_allocations(per_dump, coarse_unit):
    """Expand coarse-unit allocation columns back into byte counts."""
    for records in per_dump:
        for rec in records:
            field = rec['cols'][4]
            if rec['frame_flags'] & FF_COARSE_ALLOC:
                if not field.startswith('~'):
                    raise WireError('coarse allocation column without a tilde')
                rec['bytes_allocated'] = int(field[1:]) * coarse_unit
            else:
                if field.startswith('~'):
                    raise WireError('tilde on a plain allocation column')
                rec['bytes_allocated'] = int(field)
            if rec['bytes_allocated'] < 0:
                raise WireError('negative allocated byte count')


def export_sequence(dumps, per_dump):
    """The export's fault frames in logical order, dump after dump.

    The running resident span total and the signature ring both advance over
    the export as a whole, in this order.
    """
    return [(dump, rec) for dump, records in zip(dumps, per_dump) for rec in records]


def stage_unwind_stamps(sequence, drift_period):
    """Remove the running resident span drift from the unwind column."""
    running = 0
    for dump, rec in sequence:
        written = int(rec['cols'][2])
        if dump['flags'] & F_UNWIND_DRIFT:
            rec['unwind_ns'] = written - (running % drift_period)
        else:
            rec['unwind_ns'] = written
        running += rec['resident_span']


def stage_signatures(sequence, ring_width, signature_offset):
    """Resolve ring references and un-rotate rotated fault signatures."""
    ring = [None] * ring_width
    write_position = 0
    for _dump, rec in sequence:
        field = rec['cols'][5]
        if rec['frame_flags'] & FF_SIG_RING:
            if not field.startswith('^'):
                raise WireError('ring reference column without a caret')
            distance = int(field[1:])
            signature = ring[(write_position - distance) % ring_width]
            if signature is None:
                raise WireError('ring reference reaches an unwritten slot')
        elif rec['frame_flags'] & FF_SIG_ROTATED:
            if len(field) != SIG_HEX_WIDTH:
                raise WireError('rotated signature is not sixteen characters')
            k = (rec['frame_index'] + signature_offset) % SIG_HEX_WIDTH
            signature = field[SIG_HEX_WIDTH - k:] + field[:SIG_HEX_WIDTH - k] if k else field
        else:
            signature = field
        if len(signature) != SIG_HEX_WIDTH or any(ch not in HEX_DIGITS for ch in signature):
            raise WireError('recovered signature is not sixteen hex characters')
        rec['fault_signature'] = signature
        ring[write_position] = signature
        write_position = (write_position + 1) % ring_width


def decode_export(path, params, upto=None):
    """Decode an export, running only the stages a control figure needs.

    ``upto`` names a control figure; the return value is that figure.  With
    ``upto`` left unset the whole export is decoded and the frame sequence is
    returned.
    """
    dumps = read_dump_export(path)
    counts = stage_frame_counts(dumps, params['count_multiplier'])
    if upto == 'CTRL-A':
        return sum(counts)
    if upto == 'CTRL-B':
        return sum(stage_dump_bases(dumps, counts, params['alternate_radix'],
                                    params['base_shift_width']))
    per_dump = stage_logical_frames(dumps, counts, params['rotation_offset'])
    if upto == 'CTRL-H':
        return sum(rec['frame_index'] for recs in per_dump for rec in recs)
    stage_resident_spans(dumps, per_dump)
    if upto == 'CTRL-C':
        return sum(rec['resident_span'] for recs in per_dump for rec in recs)
    if upto == 'CTRL-D':
        stage_entry_stamps(dumps, per_dump, params['entry_clock_modulus'])
        return sum(rec['enter_ns'] for recs in per_dump for rec in recs)
    if upto == 'CTRL-F':
        stage_allocations(per_dump, params['coarse_unit'])
        return sum(rec['bytes_allocated'] for recs in per_dump for rec in recs)
    sequence = export_sequence(dumps, per_dump)
    if upto == 'CTRL-E':
        stage_unwind_stamps(sequence, params['drift_period'])
        return sum(rec['unwind_ns'] for _d, rec in sequence)
    if upto == 'CTRL-G':
        stage_signatures(sequence, params['ring_width'], params['signature_offset'])
        return sum(int(rec['fault_signature'], 16)
                   for _d, rec in sequence) % SIGNATURE_FIELD_MODULUS

    bases = stage_dump_bases(dumps, counts, params['alternate_radix'],
                             params['base_shift_width'])
    stage_entry_stamps(dumps, per_dump, params['entry_clock_modulus'])
    stage_capture_ticks(per_dump, bases, params['entry_clock_modulus'])
    stage_allocations(per_dump, params['coarse_unit'])
    stage_unwind_stamps(sequence, params['drift_period'])
    stage_signatures(sequence, params['ring_width'], params['signature_offset'])
    for _dump, rec in sequence:
        if rec['unwind_ns'] > rec['enter_ns']:
            raise WireError('unwind stamp later than its entry stamp')
    return {'dumps': dumps, 'counts': counts, 'bases': bases,
            'per_dump': per_dump, 'sequence': sequence}


# --------------------------------------------------------------------------
# reference tables
# --------------------------------------------------------------------------

def load_reference_tables(data_dir):
    def rows(name):
        with open(os.path.join(data_dir, name), 'r', encoding='utf-8', newline='') as fh:
            return list(csv.DictReader(fh))

    registry = {}
    for row in rows('build_registry.csv'):
        registry.setdefault(row['module_tag'], []).append({
            'module_tag': row['module_tag'],
            'canonical_tag': row['canonical_tag'],
            'component_key': row['component_key'],
            'tier_index': int(row['tier_index']),
            'from_build': int(row['effective_from_build']),
            'to_build': int(row['effective_to_build']),
            'status': row['status'],
        })

    ladder = {}
    for row in rows('severity_ladder.csv'):
        ladder.setdefault(row['component_key'], {})[int(row['tier_index'])] = int(row['budget_ns'])

    windows = {}
    for row in rows('support_windows.csv'):
        windows[row['component_key']] = (int(row['open_tick']), int(row['close_tick']))

    host_zones = {}
    for row in rows('host_zones.csv'):
        host_zones[row['host_id']] = row['zone']

    tenant_segments = {}
    for row in rows('tenant_classes.csv'):
        tenant_segments[row['tenant_class']] = row['tenant_segment']

    return {
        'registry': registry,
        'ladder': ladder,
        'windows': windows,
        'host_zones': host_zones,
        'tenant_segments': tenant_segments,
        'zones': sorted(set(host_zones.values())),
        'segments': sorted(set(tenant_segments.values())),
    }


def resolve_module(registry, tag, build_index):
    """Follow the registry chain on one build index to its terminal row."""
    seen = set()
    current = tag
    while True:
        if current in seen:
            return None
        seen.add(current)
        covering = [row for row in registry.get(current, ())
                    if row['from_build'] <= build_index <= row['to_build']]
        if len(covering) != 1:
            return None
        row = covering[0]
        if row['canonical_tag'] == current:
            return row
        current = row['canonical_tag']


def load_probe(path):
    table = {}
    with open(path, 'r', encoding='utf-8', newline='') as fh:
        for row in csv.DictReader(fh):
            table[(row['module_tag'], int(row['frame_index']))] = (
                int(row['samples']), int(row['peak_ns']))
    return table


def load_uplink(path):
    table = {}
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            table[(obj['module_tag'], int(obj['frame_index']))] = obj
    return table


# --------------------------------------------------------------------------
# settling the withheld wire parameters against the control figures
# --------------------------------------------------------------------------

def settle_parameters(calibration_path, controls):
    """Enumerate each parameter's domain and select the surviving assignment.

    Returns ``(params, record)`` where ``record`` is the derivation record for
    ``candidate_search``: every candidate value carries a verdict, and every
    rejection names the control figure that falsified it.
    """
    falsifier = {}
    for names, control in SETTLEMENT_STAGES:
        for name in names:
            falsifier[name] = control

    survivors = [{}]
    for names, control in SETTLEMENT_STAGES:
        target = controls[control]
        nxt = []
        for partial in survivors:
            for combo in _product([CANDIDATE_DOMAINS[n] for n in names]):
                trial = dict(partial)
                trial.update(zip(names, combo))
                try:
                    if decode_export(calibration_path, trial, upto=control) == target:
                        nxt.append(trial)
                except (WireError, ValueError, IndexError, KeyError, ZeroDivisionError):
                    continue
        survivors = nxt
        if not survivors:
            raise WireError('no candidate assignment reproduces %s' % control)

    if len(survivors) != 1:
        raise WireError('the control figures leave the wire parameters ambiguous')
    params = survivors[0]

    parameters = []
    for name in PARAM_REPORT_ORDER:
        domain = CANDIDATE_DOMAINS[name]
        parameters.append({
            'name': name,
            'candidates': len(domain),
            'control': falsifier[name],
            'selected': params[name],
            'rejected': [{'value': value, 'control': falsifier[name]}
                         for value in domain if value != params[name]],
        })
    record = {
        'evaluations': sum(len(CANDIDATE_DOMAINS[n]) for n in PARAM_REPORT_ORDER),
        'parameters': parameters,
    }
    return params, record


def _product(pools):
    out = [()]
    for pool in pools:
        out = [item + (value,) for item in out for value in pool]
    return out


# --------------------------------------------------------------------------
# the fault ledger
# --------------------------------------------------------------------------

def build_ledger(data_dir, prefix, params, tables):
    """Decode a corpus, apply the acceptance conditions, tally the rejections."""
    decoded = decode_export(os.path.join(data_dir, prefix + 'fault_frames.psv'), params)
    probe = load_probe(os.path.join(data_dir, prefix + 'runtime_probe.csv'))
    uplink = load_uplink(os.path.join(data_dir, prefix + 'uplink_reports.jsonl'))

    registry = tables['registry']
    windows = tables['windows']
    rejections = {key: 0 for key in REJECTION_KEYS}
    accepted = []
    rejected_frames = 0

    for dump, rec in decoded['sequence']:
        row = resolve_module(registry, dump['module_column'], rec['build_index'])
        breaches = []
        if row is None:
            breaches.append('unresolved_module')
        elif row['status'] != 'supported':
            breaches.append('retired_build')
        else:
            canonical = row['canonical_tag']
            component = row['component_key']
            if component not in windows:
                raise WireError('component carries no support window')
            open_tick, close_tick = windows[component]
            if not open_tick < rec['capture_tick'] < close_tick:
                breaches.append('outside_support')
            key = (canonical, rec['frame_index'])
            if key not in probe:
                breaches.append('unprobed_frame')
            if key not in uplink:
                breaches.append('unreported_dump')
        for breach in breaches:
            rejections[breach] += 1
        if breaches:
            rejected_frames += 1
            continue

        samples, peak_ns = probe[(canonical, rec['frame_index'])]
        report = uplink[(canonical, rec['frame_index'])]
        host_id = report['host_id']
        tenant_class = report['tenant_class']
        if host_id not in tables['host_zones']:
            raise WireError('uplink names a host outside the host table')
        if tenant_class not in tables['tenant_segments']:
            raise WireError('uplink names a tenant class outside the tenant table')
        accepted.append({
            'module_tag': canonical,
            'component_key': row['component_key'],
            'declared_tier': row['tier_index'],
            'source_dump': rec['source_dump'],
            'frame_index': rec['frame_index'],
            'symbol': rec['symbol'],
            'capture_tick': rec['capture_tick'],
            'enter_ns': rec['enter_ns'],
            'unwind_ns': rec['unwind_ns'],
            'cost_ns': rec['resident_span'],
            'fault_signature': rec['fault_signature'],
            'bytes_allocated': rec['bytes_allocated'],
            'bytes_uploaded': int(report['bytes_uploaded']),
            'samples': samples,
            'peak_ns': peak_ns,
            'host_zone': tables['host_zones'][host_id],
            'tenant_segment': tables['tenant_segments'][tenant_class],
        })

    frame_count = len(decoded['sequence'])
    if len(accepted) + rejected_frames != frame_count:
        raise WireError('accepted and rejected frames do not close on the export')

    accepted.sort(key=lambda row: row['capture_tick'])
    for earlier, later in zip(accepted, accepted[1:]):
        if earlier['capture_tick'] == later['capture_tick']:
            raise WireError('two ledger rows share a capture tick')

    return {
        'accepted': accepted,
        'frame_count': frame_count,
        'rejected_count': rejected_frames,
        'rejections': rejections,
    }


# --------------------------------------------------------------------------
# quarantine settlement
# --------------------------------------------------------------------------

def module_allowances(modules, tiers, ladder):
    """A module's allowance: its own tier budget, capped by its envelope."""
    by_component = {}
    for tag, module in modules.items():
        by_component.setdefault(module['component_key'], []).append(tag)
    envelope = {}
    for component, tags in by_component.items():
        in_service = min(ladder[component][tiers[tag]] for tag in tags)
        envelope[component] = in_service * len(ladder[component])
    return {tag: min(ladder[module['component_key']][tiers[tag]],
                     envelope[module['component_key']])
            for tag, module in modules.items()}


def settle_tiers(accepted, ladder):
    """Take the highest tier assignment no module holds above its own evidence."""
    modules = {}
    for row in accepted:
        module = modules.setdefault(row['module_tag'], {
            'component_key': row['component_key'],
            'declared_tier': row['declared_tier'],
            'frames': [],
        })
        module['frames'].append(row)
        module['declared_tier'] = min(module['declared_tier'], row['declared_tier'])

    tiers = {tag: module['declared_tier'] for tag, module in modules.items()}
    for tag, module in modules.items():
        if tiers[tag] not in ladder[module['component_key']]:
            raise WireError('declared tier absent from the component ladder')

    passes = 0
    allowances = module_allowances(modules, tiers, ladder)
    while True:
        passes += 1
        allowances = module_allowances(modules, tiers, ladder)
        demote = []
        for tag, module in modules.items():
            within = sum(1 for row in module['frames']
                         if row['peak_ns'] <= allowances[tag])
            if tiers[tag] > 0 and (len(module['frames']) - within) > within:
                demote.append(tag)
        if not demote:
            break
        for tag in demote:
            tiers[tag] -= 1

    quarantined = []
    for tag, module in modules.items():
        within = sum(1 for row in module['frames'] if row['peak_ns'] <= allowances[tag])
        if tiers[tag] == 0 and (len(module['frames']) - within) > within:
            quarantined.append(tag)

    for row in accepted:
        tag = row['module_tag']
        row['effective_tier'] = tiers[tag]
        row['within_budget'] = row['peak_ns'] <= allowances[tag]

    return {
        'modules': modules,
        'tiers': tiers,
        'allowances': allowances,
        'passes': passes,
        'quarantined': sorted(quarantined),
    }


# --------------------------------------------------------------------------
# the roll-ups
# --------------------------------------------------------------------------

LEDGER_FIELDS = ('bytes_allocated', 'bytes_uploaded', 'capture_tick', 'component_key',
                 'cost_ns', 'effective_tier', 'enter_ns', 'fault_signature',
                 'frame_index', 'host_zone', 'module_tag', 'peak_ns', 'samples',
                 'source_dump', 'symbol', 'tenant_segment', 'unwind_ns',
                 'within_budget')


def ledger_rows(accepted):
    return [{field: row[field] for field in LEDGER_FIELDS} for row in accepted]


def _bucket_bytes(rows, key):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + row['bytes_uploaded']
    return out


def slice_report(rows, kind, name):
    """One deployment zone or tenant segment report."""
    report = {
        'accepted_count': len(rows),
        'by_component': _bucket_bytes(rows, 'component_key'),
        'modules': sorted({row['module_tag'] for row in rows}),
        'total_bytes_uploaded': sum(row['bytes_uploaded'] for row in rows),
        'total_cost_ns': sum(row['cost_ns'] for row in rows),
        'within_budget_count': sum(1 for row in rows if row['within_budget']),
    }
    if kind == 'zone':
        report['by_tenant_segment'] = _bucket_bytes(rows, 'tenant_segment')
        report['zone'] = name
    else:
        report['by_zone'] = _bucket_bytes(rows, 'host_zone')
        report['tenant_segment'] = name
    return report


def incident_summary(accepted, rejected_count):
    by_tier = {}
    for row in accepted:
        key = str(row['effective_tier'])
        by_tier[key] = by_tier.get(key, 0) + 1
    return {
        'accepted_count': len(accepted),
        'by_component': _bucket_bytes(accepted, 'component_key'),
        'by_effective_tier': by_tier,
        'by_tenant_segment': _bucket_bytes(accepted, 'tenant_segment'),
        'by_zone': _bucket_bytes(accepted, 'host_zone'),
        'rejected_count': rejected_count,
        'total_bytes_allocated': sum(row['bytes_allocated'] for row in accepted),
        'total_bytes_uploaded': sum(row['bytes_uploaded'] for row in accepted),
        'total_cost_ns': sum(row['cost_ns'] for row in accepted),
        'within_budget_count': sum(1 for row in accepted if row['within_budget']),
    }


def product_summary(settlement, ladder):
    modules = []
    for tag in sorted(settlement['modules']):
        module = settlement['modules'][tag]
        frames = module['frames']
        tier = settlement['tiers'][tag]
        allowance = settlement['allowances'][tag]
        modules.append({
            'accepted_count': len(frames),
            'allowance_ns': allowance,
            'budget_ns': ladder[module['component_key']][tier],
            'component_key': module['component_key'],
            'declared_tier': module['declared_tier'],
            'effective_tier': tier,
            'module_tag': tag,
            'net_samples': sum(row['samples'] for row in frames),
            'quarantined': tag in settlement['quarantined'],
            'total_bytes_uploaded': sum(row['bytes_uploaded'] for row in frames),
            'within_budget_count': sum(1 for row in frames if row['within_budget']),
        })
    return {'modules': modules}


def check_aggregate_closure(accepted, zone_reports, segment_reports):
    total_bytes = sum(row['bytes_uploaded'] for row in accepted)
    for reports in (zone_reports, segment_reports):
        if sum(r['total_bytes_uploaded'] for r in reports.values()) != total_bytes:
            raise WireError('slice byte totals do not close on the ledger')
        if sum(r['accepted_count'] for r in reports.values()) != len(accepted):
            raise WireError('slice accepted counts do not close on the ledger')


# --------------------------------------------------------------------------
# writing the triage set
# --------------------------------------------------------------------------

def write_report(out_dir, name, obj):
    with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(obj, indent=2, sort_keys=True))
        fh.write('\n')


def write_ledger(out_dir, name, rows):
    with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(',', ':')))
            fh.write('\n')


def triage_filenames(tables):
    names = ['normalized.jsonl', 'summary.json', 'product_summary.json',
             'data_quality.json']
    names += [ZONE_REPORT_PREFIX + zone + '.json' for zone in tables['zones']]
    names += [SEGMENT_REPORT_PREFIX + seg + '.json' for seg in tables['segments']]
    return names


def reset_output_dir(out_dir, tables):
    """Start the derivation from an empty output directory."""
    os.makedirs(out_dir, exist_ok=True)
    for name in triage_filenames(tables):
        path = os.path.join(out_dir, name)
        if os.path.exists(path):
            os.remove(path)


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

def derive(data_dir, prefix, params, tables):
    ledger = build_ledger(data_dir, prefix, params, tables)
    settlement = settle_tiers(ledger['accepted'], tables['ladder'])
    ledger['settlement'] = settlement
    return ledger


def calibration_figures(data_dir, params, tables):
    """Reproduce all eleven published control figures from the control slice."""
    path = os.path.join(data_dir, TIER_FILES['calibration'] + 'fault_frames.psv')
    decoded = decode_export(path, params)
    sequence = decoded['sequence']
    figures = {
        'CTRL-A': len(sequence),
        'CTRL-B': sum(decoded['bases']),
        'CTRL-C': sum(rec['resident_span'] for _d, rec in sequence),
        'CTRL-D': sum(rec['enter_ns'] for _d, rec in sequence),
        'CTRL-E': sum(rec['unwind_ns'] for _d, rec in sequence),
        'CTRL-F': sum(rec['bytes_allocated'] for _d, rec in sequence),
        'CTRL-G': sum(int(rec['fault_signature'], 16)
                      for _d, rec in sequence) % SIGNATURE_FIELD_MODULUS,
        'CTRL-H': sum(rec['frame_index'] for _d, rec in sequence),
    }
    control = derive(data_dir, TIER_FILES['calibration'], params, tables)
    accepted = control['accepted']
    figures['CTRL-I'] = len(accepted)
    figures['CTRL-J'] = len(control['settlement']['quarantined'])
    figures['CTRL-K'] = (sum(row['bytes_allocated'] for row in accepted)
                         - sum(row['bytes_uploaded'] for row in accepted))
    return figures


def main(argv):
    data_dir = argv[1] if len(argv) > 1 else '/app/data'
    out_dir = argv[2] if len(argv) > 2 else '/app/out'
    data_dir = os.path.abspath(data_dir)
    out_dir = os.path.abspath(out_dir)

    tables = load_reference_tables(data_dir)

    # --- rule 1: settle the withheld wire parameters against the published
    # control figures, before any figure is computed for the incident corpus.
    with open(os.path.join(data_dir, 'control-slice-figures.json'),
              'r', encoding='utf-8') as fh:
        controls = json.load(fh)
    calibration_path = os.path.join(data_dir, TIER_FILES['calibration'] + 'fault_frames.psv')
    params, search_record = settle_parameters(calibration_path, controls)

    # --- compare against every published control figure
    reproduced = calibration_figures(data_dir, params, tables)
    comparison = []
    for key in CONTROL_KEYS:
        if key not in controls:
            continue
        comparison.append({'control': key,
                           'published': controls[key],
                           'reproduced': reproduced[key]})
    mismatched = [c['control'] for c in comparison if c['published'] != c['reproduced']]
    if mismatched:
        raise WireError('settled parameters do not reproduce %s' % ', '.join(mismatched))

    # --- rule 4: with the parameter set settled, re-run the whole derivation
    # from an empty output directory.
    reset_output_dir(out_dir, tables)

    incident = derive(data_dir, TIER_FILES['incident'], params, tables)
    accepted = incident['accepted']
    settlement = incident['settlement']

    rows = ledger_rows(accepted)
    write_ledger(out_dir, 'normalized.jsonl', rows)

    zone_reports = {}
    for zone in tables['zones']:
        subset = [r for r in rows if r['host_zone'] == zone]
        zone_reports[zone] = slice_report(subset, 'zone', zone)
    segment_reports = {}
    for segment in tables['segments']:
        subset = [r for r in rows if r['tenant_segment'] == segment]
        segment_reports[segment] = slice_report(subset, 'segment', segment)
    check_aggregate_closure(rows, zone_reports, segment_reports)

    for zone, report in zone_reports.items():
        write_report(out_dir, ZONE_REPORT_PREFIX + zone + '.json', report)
    for segment, report in segment_reports.items():
        write_report(out_dir, SEGMENT_REPORT_PREFIX + segment + '.json', report)

    write_report(out_dir, 'summary.json',
                 incident_summary(rows, incident['rejected_count']))
    write_report(out_dir, 'product_summary.json',
                 product_summary(settlement, tables['ladder']))

    allocated = sum(row['bytes_allocated'] for row in rows)
    uploaded = sum(row['bytes_uploaded'] for row in rows)
    write_report(out_dir, 'data_quality.json', {
        'accepted_count': len(rows),
        'calibration_comparison': comparison,
        'candidate_search': search_record,
        'frame_count': incident['frame_count'],
        'reconciliation': {
            'allocated_bytes': allocated,
            'residual_bytes': allocated - uploaded,
            'uploaded_bytes': uploaded,
        },
        'rejections': dict(incident['rejections']),
        'settlement': {
            'passes': settlement['passes'],
            'quarantined_modules': settlement['quarantined'],
        },
    })
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
