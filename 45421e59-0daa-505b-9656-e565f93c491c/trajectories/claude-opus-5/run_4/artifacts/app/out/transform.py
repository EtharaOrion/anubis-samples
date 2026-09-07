#!/usr/bin/env python3
"""Reconstruct a fault ledger and its roll-ups from a crash-reporter corpus.

Usage:  python3 transform.py [DATA_DIR] [OUT_DIR]

Defaults to /app/data and /app/out.  Standard library only.  All clock, byte
and residency arithmetic is carried in Python integers.
"""

import csv
import json
import os
import sys

# ---------------------------------------------------------------------------
# Fixed fleet constants (documented, not corpus-derived)
# ---------------------------------------------------------------------------

TICKS_PER_BUILD = 900000000          # reporter clock: 1000 ticks/ms, 900e6/build
SIG_LEN = 16                         # fault signature width, hex characters
CHECK_MOD = 65536                    # check totals are mod 2**16
SIG_SUM_MOD = 1000000007             # CTRL-G reduction
HEXDIGITS = set('0123456789abcdef')

# Withheld wire parameters, in settlement order, with the published control
# figure that pins each one.
PARAM_ORDER = [
    ('count_multiplier',    'CTRL-A', list(range(0, 9))),
    ('rotation_offset',     'CTRL-C', list(range(0, 8))),
    ('base_shift_width',    'CTRL-B', [8, 10, 12, 16]),
    ('alternate_radix',     'CTRL-B', list(range(11, 36))),
    ('entry_clock_modulus', 'CTRL-D', [1 << e for e in range(22, 35)]),
    ('drift_period',        'CTRL-E', [48, 96, 192, 384, 768]),
    ('coarse_unit',         'CTRL-F', [64, 128, 256, 512, 1024]),
    ('ring_width',          'CTRL-G', [4, 8, 12, 16]),
    ('signature_offset',    'CTRL-G', list(range(0, 8))),
]

CONTROLS = ['CTRL-A', 'CTRL-B', 'CTRL-C', 'CTRL-D', 'CTRL-E', 'CTRL-F',
            'CTRL-G', 'CTRL-H', 'CTRL-I', 'CTRL-J', 'CTRL-K']

REJECTION_KINDS = ['outside_support', 'retired_build', 'unprobed_frame',
                   'unreported_dump', 'unresolved_module']

# Dump flag word
F_SPANDIFF = 0x001      # resident span column carries second-order differences
F_ENTRYSUM = 0x002      # entry column carries running pairwise sums
F_UNWDRIFT = 0x004      # unwind column carries a resident-span drift offset
F_ROTATE = 0x008        # fault frame lines written rotated left
F_COUNTBIAS = 0x010     # declared count carries an ordinal bias
F_ALTRADIX = 0x020      # base field written in the alternate radix
F_ALIAS = 0x100         # module column carries a registry alias
F_SPLITBASE = 0x800     # base field carries only the high part of the base

# Frame flag word
G_PLACEHOLDER = 0x040   # frame index column is a dump-local placeholder
G_SIGROT = 0x080        # fault signature column written rotated left
G_COARSE = 0x200        # allocation column written in coarse units
G_RING = 0x400          # fault signature column carries a ring distance


class WireError(Exception):
    """A candidate parameter assignment cannot decode the export at all."""


# ---------------------------------------------------------------------------
# Reading the shipped materials
# ---------------------------------------------------------------------------

def read_rows(path):
    with open(path, newline='', encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


def read_jsonl(path):
    out = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def read_dumps(path):
    """Parse a pipe-delimited reporter export into raw dump records."""
    dumps = []
    cur = None
    with open(path, encoding='utf-8') as fh:
        for raw in fh:
            line = raw.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            cols = line.split('|')
            if cols[0] == '@':
                cur = {
                    'dump_id': cols[1],
                    'module': cols[2],
                    'declared': int(cols[3]),
                    'base_field': cols[4],
                    'flags': int(cols[5], 16),
                    'key': int(cols[6]),
                    'check': int(cols[7], 16),
                    'lines': [],
                }
                dumps.append(cur)
            else:
                if cur is None:
                    raise WireError('frame line before any dump header')
                cur['lines'].append(cols)
    return dumps


# ---------------------------------------------------------------------------
# Wire decoding
# ---------------------------------------------------------------------------

def frame_check_total(cols):
    """Sum of the character codes of the eight leading columns, joined by '|'."""
    return sum(ord(c) for c in '|'.join(cols[:8])) % CHECK_MOD


def parse_radix(text, radix):
    value = 0
    for ch in text:
        if '0' <= ch <= '9':
            digit = ord(ch) - 48
        elif 'a' <= ch <= 'z':
            digit = ord(ch) - 87
        else:
            return None
        if digit >= radix:
            return None
        value = value * radix + digit
    return value


def rotate_right(text, amount):
    amount %= len(text)
    return text[-amount:] + text[:-amount] if amount else text


def fault_frame_counts(dumps, count_multiplier):
    """How many of each dump's lines are fault frames (the rest are trailers)."""
    counts = []
    for ordinal, dump in enumerate(dumps, start=1):
        n = dump['declared']
        if dump['flags'] & F_COUNTBIAS:
            n -= (ordinal * count_multiplier) % 9
        if n < 1 or n > len(dump['lines']):
            raise WireError('implausible fault frame count')
        if (dump['flags'] & F_SPLITBASE) and n >= len(dump['lines']):
            raise WireError('split-base dump without a trailer line')
        counts.append(n)
    return counts


def verify_check_totals(dumps, counts):
    """Cross-check the shipped dump and frame totals against the columns they cover."""
    for dump, n in zip(dumps, counts):
        total = 0
        for cols in dump['lines'][:n]:
            if len(cols) != 9:
                raise WireError('fault frame line is not nine columns')
            declared = int(cols[8], 16)
            if frame_check_total(cols) != declared:
                raise WireError('frame check total mismatch')
            total += declared
        if total % CHECK_MOD != dump['check']:
            raise WireError('dump check total mismatch')


def authoritative_index(cols):
    if int(cols[6], 16) & G_PLACEHOLDER:
        return int(cols[7].rsplit(':', 1)[1])
    return int(cols[0])


def logical_frames(dump, n, rotation_offset):
    """The dump's fault frame lines restored to logical order."""
    frames = dump['lines'][:n]
    if dump['flags'] & F_ROTATE:
        return rotate_right_list(frames, (dump['key'] + rotation_offset) % n)
    return frames


def rotate_right_list(items, amount):
    amount %= len(items)
    return items[-amount:] + items[:-amount] if amount else items


def resident_spans(dump, frames):
    """Recover resident spans, undoing second-order differencing when set."""
    raw = [int(cols[3]) for cols in frames]
    if not (dump['flags'] & F_SPANDIFF):
        return raw
    spans = [raw[0]]
    if len(raw) > 1:
        step = raw[1]
        spans.append(spans[-1] + step)
        for i in range(2, len(raw)):
            step += raw[i]
            spans.append(spans[-1] + step)
    return spans


def entry_stamps(dump, frames, modulus):
    """Recover entry stamps, undoing the running pairwise sum when set."""
    raw = [int(cols[1]) for cols in frames]
    if not (dump['flags'] & F_ENTRYSUM):
        return raw
    stamps = [raw[0]]
    for i in range(1, len(raw)):
        stamps.append((raw[i] - stamps[-1]) % modulus)
    return stamps


def recover_base(dump, n, radix, shift):
    """Recover a dump's base tick from its base field (and trailer, when split)."""
    field = dump['base_field']
    if dump['flags'] & F_ALTRADIX:
        high = parse_radix(field, radix)
        if high is None:
            raise WireError('base field is not a numeral in the alternate radix')
    else:
        try:
            high = int(field)
        except ValueError:
            raise WireError('base field is not a decimal numeral')
    if dump['flags'] & F_SPLITBASE:
        low = int(dump['lines'][n][0])
        if low < 0 or low >= (1 << shift):
            raise WireError('low part of the dump base overflows the shift width')
        base = (high << shift) | low
    else:
        base = high
    if base < 0:
        raise WireError('negative dump base')
    return base


def decode_export(dumps, params, verify=True):
    """Decode a whole reporter export under one parameter assignment.

    The running resident-span total and the signature ring both advance over
    the export as a whole, in dump order and logical frame order within a dump.
    """
    counts = fault_frame_counts(dumps, params['count_multiplier'])
    if verify:
        verify_check_totals(dumps, counts)

    modulus = params['entry_clock_modulus']
    period = params['drift_period']
    unit = params['coarse_unit']
    width = params['ring_width']
    sig_off = params['signature_offset']

    ring = [None] * width
    ring_pos = 0
    running_span = 0

    frames = []
    bases = []
    for dump, n in zip(dumps, counts):
        base = recover_base(dump, n, params['alternate_radix'],
                            params['base_shift_width'])
        bases.append(base)
        ordered = logical_frames(dump, n, params['rotation_offset'])
        spans = resident_spans(dump, ordered)
        stamps = entry_stamps(dump, ordered, modulus)
        first_stamp = stamps[0]
        for i, cols in enumerate(ordered):
            gflags = int(cols[6], 16)
            index = authoritative_index(cols)

            unwind = int(cols[2])
            if dump['flags'] & F_UNWDRIFT:
                unwind -= running_span % period

            alloc_col = cols[4]
            if gflags & G_COARSE:
                if not alloc_col.startswith('~'):
                    raise WireError('coarse allocation column lacks its tilde')
                allocated = int(alloc_col[1:]) * unit
            else:
                allocated = int(alloc_col)

            sig_col = cols[5]
            if gflags & G_RING:
                if not sig_col.startswith('^'):
                    raise WireError('ring signature column lacks its caret')
                distance = int(sig_col[1:])
                signature = ring[(ring_pos - distance) % width]
                if signature is None:
                    raise WireError('ring slot never written')
            elif gflags & G_SIGROT:
                signature = rotate_right(sig_col, (index + sig_off) % SIG_LEN)
            else:
                signature = sig_col
            if len(signature) != SIG_LEN or any(c not in HEXDIGITS for c in signature):
                raise WireError('recovered signature is not sixteen hex digits')

            ring[ring_pos] = signature
            ring_pos = (ring_pos + 1) % width

            capture = base + ((stamps[i] - first_stamp) % modulus)
            frames.append({
                'source_dump': dump['dump_id'],
                'header_tag': dump['module'],
                'frame_index': index,
                'enter_ns': stamps[i],
                'unwind_ns': unwind,
                'cost_ns': spans[i],
                'bytes_allocated': allocated,
                'fault_signature': signature,
                'symbol': cols[7].rsplit(':', 1)[0],
                'capture_tick': capture,
            })
            running_span += spans[i]

    return {'frames': frames, 'bases': bases, 'counts': counts}


# ---------------------------------------------------------------------------
# Control figures
# ---------------------------------------------------------------------------

def wire_figures(decoded):
    """The eight wire-level control figures, from a decoded export."""
    frames = decoded['frames']
    return {
        'CTRL-A': len(frames),
        'CTRL-B': sum(decoded['bases']),
        'CTRL-C': sum(f['cost_ns'] for f in frames),
        'CTRL-D': sum(f['enter_ns'] for f in frames),
        'CTRL-E': sum(f['unwind_ns'] for f in frames),
        'CTRL-F': sum(f['bytes_allocated'] for f in frames),
        'CTRL-G': sum(int(f['fault_signature'], 16) for f in frames) % SIG_SUM_MOD,
        'CTRL-H': sum(f['frame_index'] for f in frames),
    }


def control_figure(control, dumps, params):
    """Reproduce one wire-level control figure using only the parameters that
    bear on it, so a figure can be evaluated before the rest are settled."""
    counts = fault_frame_counts(dumps, params['count_multiplier'])
    if control == 'CTRL-A':
        return sum(counts)
    if control == 'CTRL-B':
        return sum(recover_base(d, n, params['alternate_radix'],
                                params['base_shift_width'])
                   for d, n in zip(dumps, counts))

    total = 0
    running_span = 0
    ring_pos = 0
    ring = None
    if control == 'CTRL-G':
        ring = [None] * params['ring_width']

    for dump, n in zip(dumps, counts):
        ordered = logical_frames(dump, n, params['rotation_offset'])
        if control in ('CTRL-C', 'CTRL-E'):
            spans = resident_spans(dump, ordered)
            if control == 'CTRL-C':
                total += sum(spans)
                continue
            for i, cols in enumerate(ordered):
                unwind = int(cols[2])
                if dump['flags'] & F_UNWDRIFT:
                    unwind -= running_span % params['drift_period']
                total += unwind
                running_span += spans[i]
            continue
        if control == 'CTRL-D':
            total += sum(entry_stamps(dump, ordered, params['entry_clock_modulus']))
            continue
        for cols in ordered:
            gflags = int(cols[6], 16)
            if control == 'CTRL-H':
                total += authoritative_index(cols)
            elif control == 'CTRL-F':
                if gflags & G_COARSE:
                    total += int(cols[4][1:]) * params['coarse_unit']
                else:
                    total += int(cols[4])
            elif control == 'CTRL-G':
                width = params['ring_width']
                sig_col = cols[5]
                if gflags & G_RING:
                    signature = ring[(ring_pos - int(sig_col[1:])) % width]
                    if signature is None:
                        raise WireError('ring slot never written')
                elif gflags & G_SIGROT:
                    signature = rotate_right(
                        sig_col,
                        (authoritative_index(cols) + params['signature_offset'])
                        % SIG_LEN)
                else:
                    signature = sig_col
                if len(signature) != SIG_LEN or \
                        any(c not in HEXDIGITS for c in signature):
                    raise WireError('recovered signature is not sixteen hex digits')
                ring[ring_pos] = signature
                ring_pos = (ring_pos + 1) % width
                total += int(signature, 16)
    if control == 'CTRL-G':
        total %= SIG_SUM_MOD
    return total


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------

class Registry:
    def __init__(self, rows):
        self.by_tag = {}
        for row in rows:
            self.by_tag.setdefault(row['module_tag'], []).append({
                'canonical': row['canonical_tag'],
                'component': row['component_key'],
                'tier': int(row['tier_index']),
                'lo': int(row['effective_from_build']),
                'hi': int(row['effective_to_build']),
                'status': row['status'],
            })

    def row_for(self, tag, build):
        for row in self.by_tag.get(tag, ()):
            if row['lo'] <= build <= row['hi']:
                return row
        return None

    def resolve(self, tag, build):
        """Follow the canonical chain to a terminal row, or return None."""
        seen = set()
        current = tag
        while True:
            if current in seen:
                return None
            seen.add(current)
            row = self.row_for(current, build)
            if row is None:
                return None
            if row['canonical'] == current:
                return {'module_tag': current, 'component_key': row['component'],
                        'declared_tier': row['tier'], 'status': row['status']}
            current = row['canonical']


# ---------------------------------------------------------------------------
# Attribution and quarantine settlement
# ---------------------------------------------------------------------------

def build_ledger(decoded, tables):
    """Apply the acceptance conditions; return accepted frames and rejection tallies."""
    registry = tables['registry']
    windows = tables['windows']
    probe = tables['probe']
    uplink = tables['uplink']
    zones = tables['zones']
    segments = tables['segments']

    rejections = {kind: 0 for kind in REJECTION_KINDS}
    accepted = []

    for frame in decoded['frames']:
        build = frame['capture_tick'] // TICKS_PER_BUILD
        resolved = registry.resolve(frame['header_tag'], build)
        if resolved is None:
            rejections['unresolved_module'] += 1
            continue

        ok = True
        carried = resolved['status'] == 'supported'
        if not carried:
            rejections['retired_build'] += 1
            ok = False

        window = windows.get(resolved['component_key'])
        if window is None or not (window[0] < frame['capture_tick'] < window[1]):
            rejections['outside_support'] += 1
            ok = False

        # The side exports only carry facts for still-carried modules, so a
        # frame already rejected as a retired build is not additionally counted
        # as unprobed or unreported.
        facts = report = None
        if carried:
            key = (resolved['module_tag'], frame['frame_index'])
            facts = probe.get(key)
            if facts is None:
                rejections['unprobed_frame'] += 1
                ok = False
            report = uplink.get(key)
            if report is None:
                rejections['unreported_dump'] += 1
                ok = False

        if not ok:
            continue

        accepted.append({
            'bytes_allocated': frame['bytes_allocated'],
            'bytes_uploaded': report['bytes_uploaded'],
            'capture_tick': frame['capture_tick'],
            'component_key': resolved['component_key'],
            'cost_ns': frame['cost_ns'],
            'declared_tier': resolved['declared_tier'],
            'enter_ns': frame['enter_ns'],
            'fault_signature': frame['fault_signature'],
            'frame_index': frame['frame_index'],
            'host_zone': zones[report['host_id']],
            'module_tag': resolved['module_tag'],
            'peak_ns': facts['peak_ns'],
            'samples': facts['samples'],
            'source_dump': frame['source_dump'],
            'symbol': frame['symbol'],
            'tenant_segment': segments[report['tenant_class']],
            'unwind_ns': frame['unwind_ns'],
        })

    accepted.sort(key=lambda r: r['capture_tick'])
    return accepted, rejections


def settle(accepted, ladders):
    """Demote effective tiers until no module above tier zero is over budget.

    Returns (effective_tier by module, allowance by module, pass count,
    quarantined module tags).
    """
    modules = {}
    for row in accepted:
        info = modules.setdefault(row['module_tag'], {
            'component': row['component_key'],
            'declared': row['declared_tier'],
            'peaks': [],
        })
        info['peaks'].append(row['peak_ns'])

    tiers = {tag: info['declared'] for tag, info in modules.items()}

    def allowances(tiers):
        envelope = {}
        for tag, info in modules.items():
            comp = info['component']
            budget = ladders[comp][tiers[tag]]
            if comp not in envelope or budget < envelope[comp]:
                envelope[comp] = budget
        envelope = {c: b * len(ladders[c]) for c, b in envelope.items()}
        return {tag: min(ladders[info['component']][tiers[tag]],
                         envelope[info['component']])
                for tag, info in modules.items()}

    def tally(allow):
        out = {}
        for tag, info in modules.items():
            within = sum(1 for p in info['peaks'] if p <= allow[tag])
            out[tag] = (within, len(info['peaks']) - within)
        return out

    passes = 0
    while True:
        passes += 1
        allow = allowances(tiers)
        counts = tally(allow)
        demote = [tag for tag in modules
                  if tiers[tag] > 0 and counts[tag][1] > counts[tag][0]]
        if not demote:
            break
        for tag in demote:
            tiers[tag] -= 1

    quarantined = sorted(tag for tag in modules
                         if tiers[tag] == 0 and counts[tag][1] > counts[tag][0])
    return tiers, allow, passes, quarantined, modules


# ---------------------------------------------------------------------------
# Corpus derivation
# ---------------------------------------------------------------------------

def derive(dumps, probe_rows, uplink_rows, tables, params):
    decoded = decode_export(dumps, params)
    ctx = dict(tables)
    ctx['probe'] = {(r['module_tag'], int(r['frame_index'])):
                    {'samples': int(r['samples']), 'peak_ns': int(r['peak_ns'])}
                    for r in probe_rows}
    ctx['uplink'] = {(r['module_tag'], int(r['frame_index'])): r for r in uplink_rows}

    accepted, rejections = build_ledger(decoded, ctx)
    tiers, allow, passes, quarantined, modules = settle(accepted, tables['ladders'])

    for row in accepted:
        row['effective_tier'] = tiers[row['module_tag']]
        row['within_budget'] = row['peak_ns'] <= allow[row['module_tag']]

    return {
        'decoded': decoded,
        'accepted': accepted,
        'rejections': rejections,
        'tiers': tiers,
        'allowances': allow,
        'passes': passes,
        'quarantined': quarantined,
        'modules': modules,
    }


def corpus_figures(result):
    """The three corpus-level control figures."""
    accepted = result['accepted']
    allocated = sum(r['bytes_allocated'] for r in accepted)
    uploaded = sum(r['bytes_uploaded'] for r in accepted)
    return {
        'CTRL-I': len(accepted),
        'CTRL-J': len(result['quarantined']),
        'CTRL-K': allocated - uploaded,
    }


# ---------------------------------------------------------------------------
# Parameter settlement against the published control figures
# ---------------------------------------------------------------------------

def settle_parameters(data_dir, tables, published):
    """Enumerate each withheld parameter's domain and record a verdict for every
    candidate before selecting that parameter's value."""
    dumps = read_dumps(os.path.join(data_dir, 'calibration_fault_frames.psv'))
    probe_rows = read_rows(os.path.join(data_dir, 'calibration_runtime_probe.csv'))
    uplink_rows = read_jsonl(os.path.join(data_dir, 'calibration_uplink_reports.jsonl'))

    chosen = {}
    records = []
    evaluations = 0
    domains = dict((n, d) for n, _, d in PARAM_ORDER)

    def matches(control, trial):
        params = dict(chosen)
        params.update(trial)
        try:
            return control_figure(control, dumps, params) == published[control]
        except (WireError, ValueError, KeyError, IndexError, ZeroDivisionError):
            return False

    # Parameters pinned by CTRL-B and CTRL-G are settled as a pair: the outer
    # one is fixed by asking whether any inner candidate can reproduce the
    # figure, then the inner one is settled against the same figure.
    paired_inner = {'base_shift_width': 'alternate_radix',
                    'ring_width': 'signature_offset'}

    for name, control, domain in PARAM_ORDER:
        inner = paired_inner.get(name)
        rejected = []
        selected = None
        for value in domain:
            evaluations += 1
            if inner is not None:
                ok = any(matches(control, {name: value, inner: iv})
                         for iv in domains[inner])
            else:
                ok = matches(control, {name: value})
            if ok and selected is None:
                selected = value
            elif not ok:
                rejected.append({'value': value, 'control': control})
        if selected is None:
            raise WireError('no candidate reproduces %s for %s' % (control, name))
        chosen[name] = selected
        records.append({'name': name, 'candidates': len(domain), 'control': control,
                        'selected': selected, 'rejected': rejected})

    return chosen, {'evaluations': evaluations, 'parameters': records}, \
        (dumps, probe_rows, uplink_rows)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def write_json(path, obj):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write('\n')


def write_jsonl(path, rows):
    with open(path, 'w', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(',', ':')))
            fh.write('\n')


LEDGER_FIELDS = ['bytes_allocated', 'bytes_uploaded', 'capture_tick',
                 'component_key', 'cost_ns', 'effective_tier', 'enter_ns',
                 'fault_signature', 'frame_index', 'host_zone', 'module_tag',
                 'peak_ns', 'samples', 'source_dump', 'symbol',
                 'tenant_segment', 'unwind_ns', 'within_budget']


def bucket_bytes(rows, key):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + row['bytes_uploaded']
    return out


def slice_report(rows, dimension, name, other):
    return {
        'accepted_count': len(rows),
        'by_component': bucket_bytes(rows, 'component_key'),
        other[0]: bucket_bytes(rows, other[1]),
        'modules': sorted({r['module_tag'] for r in rows}),
        dimension: name,
        'total_bytes_uploaded': sum(r['bytes_uploaded'] for r in rows),
        'total_cost_ns': sum(r['cost_ns'] for r in rows),
        'within_budget_count': sum(1 for r in rows if r['within_budget']),
    }


def emit(out_dir, result, tables, quality):
    accepted = result['accepted']

    write_jsonl(os.path.join(out_dir, 'normalized.jsonl'),
                [{k: row[k] for k in LEDGER_FIELDS} for row in accepted])

    by_tier = {}
    for row in accepted:
        k = str(row['effective_tier'])
        by_tier[k] = by_tier.get(k, 0) + 1

    write_json(os.path.join(out_dir, 'summary.json'), {
        'accepted_count': len(accepted),
        'by_component': bucket_bytes(accepted, 'component_key'),
        'by_effective_tier': by_tier,
        'by_tenant_segment': bucket_bytes(accepted, 'tenant_segment'),
        'by_zone': bucket_bytes(accepted, 'host_zone'),
        'rejected_count': quality['frame_count'] - len(accepted),
        'total_bytes_allocated': sum(r['bytes_allocated'] for r in accepted),
        'total_bytes_uploaded': sum(r['bytes_uploaded'] for r in accepted),
        'total_cost_ns': sum(r['cost_ns'] for r in accepted),
        'within_budget_count': sum(1 for r in accepted if r['within_budget']),
    })

    modules = []
    for tag in sorted(result['modules']):
        rows = [r for r in accepted if r['module_tag'] == tag]
        info = result['modules'][tag]
        tier = result['tiers'][tag]
        modules.append({
            'accepted_count': len(rows),
            'allowance_ns': result['allowances'][tag],
            'budget_ns': tables['ladders'][info['component']][tier],
            'component_key': info['component'],
            'declared_tier': info['declared'],
            'effective_tier': tier,
            'module_tag': tag,
            'net_samples': sum(r['samples'] for r in rows),
            'quarantined': tag in result['quarantined'],
            'total_bytes_uploaded': sum(r['bytes_uploaded'] for r in rows),
            'within_budget_count': sum(1 for r in rows if r['within_budget']),
        })
    write_json(os.path.join(out_dir, 'product_summary.json'), {'modules': modules})

    for zone in tables['zone_names']:
        rows = [r for r in accepted if r['host_zone'] == zone]
        write_json(os.path.join(out_dir, 'region_%s.json' % zone),
                   slice_report(rows, 'zone', zone,
                                ('by_tenant_segment', 'tenant_segment')))

    for segment in tables['segment_names']:
        rows = [r for r in accepted if r['tenant_segment'] == segment]
        write_json(os.path.join(out_dir, 'segment_%s.json' % segment),
                   slice_report(rows, 'tenant_segment', segment,
                                ('by_zone', 'host_zone')))

    write_json(os.path.join(out_dir, 'data_quality.json'), quality)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def load_tables(data_dir):
    registry = Registry(read_rows(os.path.join(data_dir, 'build_registry.csv')))

    ladders = {}
    for row in read_rows(os.path.join(data_dir, 'severity_ladder.csv')):
        ladders.setdefault(row['component_key'], {})[int(row['tier_index'])] = \
            int(row['budget_ns'])

    windows = {row['component_key']: (int(row['open_tick']), int(row['close_tick']))
               for row in read_rows(os.path.join(data_dir, 'support_windows.csv'))}

    zone_rows = read_rows(os.path.join(data_dir, 'host_zones.csv'))
    zones = {row['host_id']: row['zone'] for row in zone_rows}

    seg_rows = read_rows(os.path.join(data_dir, 'tenant_classes.csv'))
    segments = {row['tenant_class']: row['tenant_segment'] for row in seg_rows}

    return {
        'registry': registry,
        'ladders': ladders,
        'windows': windows,
        'zones': zones,
        'segments': segments,
        'zone_names': sorted({row['zone'] for row in zone_rows}),
        'segment_names': sorted({row['tenant_segment'] for row in seg_rows}),
    }


def main(argv):
    data_dir = argv[1] if len(argv) > 1 else '/app/data'
    out_dir = argv[2] if len(argv) > 2 else '/app/out'
    os.makedirs(out_dir, exist_ok=True)

    tables = load_tables(data_dir)
    with open(os.path.join(data_dir, 'control-slice-figures.json'),
              encoding='utf-8') as fh:
        published = json.load(fh)

    # Rule 1: settle the withheld wire parameters against the published control
    # figures before computing any figure for the incident corpus.
    params, search, calib = settle_parameters(data_dir, tables, published)

    # Rule 4: with the parameter set settled, re-run the whole derivation.
    calib_dumps, calib_probe, calib_uplink = calib
    calib_result = derive(calib_dumps, calib_probe, calib_uplink, tables, params)
    reproduced = wire_figures(calib_result['decoded'])
    reproduced.update(corpus_figures(calib_result))
    comparison = [{'control': c, 'published': published[c],
                   'reproduced': reproduced[c]} for c in CONTROLS if c in published]

    dumps = read_dumps(os.path.join(data_dir, 'fault_frames.psv'))
    probe_rows = read_rows(os.path.join(data_dir, 'runtime_probe.csv'))
    uplink_rows = read_jsonl(os.path.join(data_dir, 'uplink_reports.jsonl'))
    result = derive(dumps, probe_rows, uplink_rows, tables, params)

    accepted = result['accepted']
    allocated = sum(r['bytes_allocated'] for r in accepted)
    uploaded = sum(r['bytes_uploaded'] for r in accepted)

    quality = {
        'accepted_count': len(accepted),
        'calibration_comparison': comparison,
        'candidate_search': search,
        'frame_count': len(result['decoded']['frames']),
        'reconciliation': {
            'allocated_bytes': allocated,
            'residual_bytes': allocated - uploaded,
            'uploaded_bytes': uploaded,
        },
        'rejections': dict(result['rejections']),
        'settlement': {
            'passes': result['passes'],
            'quarantined_modules': result['quarantined'],
        },
    }

    emit(out_dir, result, tables, quality)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
