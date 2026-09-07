#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Everything is read from DATA_DIR: the policy documents, the entrant registry,
the division topology, the licence classes, the calibration controls published
with the control slice and the sealed captures themselves.  Only the standard
library is used, nothing is installed, no network is reached and nothing
outside the two directories handed over on the command line is touched.

The three quantities that govern how the replay guards resolve, the season
budget and the promotion threshold are never written down.  Each is drawn from
a roster published in the material and is pinned here against the control
figures published with the control slice; every rejected candidate is recorded
together with the control figure that killed it.
"""

import collections
import csv
import hashlib
import itertools
import json
import math
import os
import re
import struct
import sys
from math import gcd

M32 = 0xFFFFFFFF

GUARDS = ['keystream', 'chain', 'delta2', 'driftkey', 'segperm', 'backref',
          'endian', 'countbias', 'shadow', 'rotate', 'alias', 'escape']

TIERS = ['champion', 'contender', 'qualifier', 'provisional']

KIND_NAMES = collections.OrderedDict(
    [('deploy', 1), ('output', 2), ('eliminate', 3), ('disband', 4),
     ('concede', 5), ('transfer', 6), ('close', 8)])

UNMASK_ORDERS = ['keystream_then_rotate', 'rotate_then_keystream']
DRIFT_SOURCES = ['decoded', 'raw']

BRACKET_CONSTRAINTS = [
    ('prominence', 'at least one member sits in a division whose ladder weight '
                   'reaches the prominence figure'),
    ('both_segments', 'both entrant segments appear among the members'),
    ('standing_floor', 'every member holds at least the standing points floor'),
    ('distinct_conferences', 'no two members share a conference'),
    ('ranked_entrant', 'at least one member is a ranked entrant'),
    ('ladder_weight_floor', 'the ladder weights of the members sum to at least '
                            'the combined ladder weight floor'),
    ('no_overflow', 'no member carries any overflow upkeep'),
    ('champion_present', 'at least one member holds the champion tier'),
    ('gross_floor', 'the gross output of the members sums to at least the '
                    'combined gross output floor'),
    ('no_handover', 'no member has handed its entry to another squad'),
]


class DecodeError(Exception):
    pass


def u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def rot_left(b, k):
    n = len(b)
    if n == 0:
        return b
    k %= n
    return b[k:] + b[:k] if k else b


def rot_right(b, k):
    n = len(b)
    if n == 0:
        return b
    k %= n
    return b[n - k:] + b[:n - k] if k else b


_ks_cache = {}


def keystream(engine_tag, match_id, salt, ordinal, width):
    """SHA-256 of '<tag>:<match>:<salt>:<ordinal>' truncated to the blob width."""
    key = (engine_tag, match_id, salt, ordinal, width)
    d = _ks_cache.get(key)
    if d is None:
        d = hashlib.sha256(('%s:%d:%d:%d' % key[:4]).encode('ascii')).digest()
        _ks_cache[key] = d
    return d[:width]


# --------------------------------------------------------------------------
# captures
# --------------------------------------------------------------------------

def read_envelope(raw):
    if len(raw) < 36 or raw[0:4] != b'RJNL':
        raise DecodeError('not a journal envelope')
    endian = bool(struct.unpack_from('<H', raw, 6)[0] & (1 << GUARDS.index('endian')))
    e = dict(envelope_version=raw[4], reserved=raw[5],
             guard_register=struct.unpack_from('<H', raw, 6)[0],
             nonce=struct.unpack_from('>I' if endian else '<I', raw, 8)[0],
             declared_segment_count=u32(raw, 12), declared_record_count=u32(raw, 16),
             season_base_tick=u32(raw, 20), capture_identifier=u32(raw, 24),
             declared_roster_size=u32(raw, 28), envelope_checksum=u32(raw, 32))
    e['guards_active'] = [n for i, n in enumerate(GUARDS) if e['guard_register'] & (1 << i)]
    e['envelope_checksum_agrees'] = e['envelope_checksum'] == sum(raw[:32]) % (2 ** 32)
    return e


def read_segments(raw):
    off, segs = 36, []
    while off < len(raw):
        if raw[off:off + 4] != b'SEG0':
            raise DecodeError('bad segment marker at offset %d' % off)
        pw = u32(raw, off + 20)
        segs.append(dict(slot=u32(raw, off + 4), match_id=u32(raw, off + 8),
                         declared_count=u32(raw, off + 12), salt=u32(raw, off + 16),
                         payload_width=u32(raw, off + 20),
                         payload_checksum=u32(raw, off + 24),
                         payload=raw[off + 28:off + 28 + pw], file_offset=off))
        off += 28 + pw
    return segs


def permutation_step(nonce, nseg):
    """Smallest integer at least two more than (nonce>>8) mod nseg, coprime with nseg."""
    k = (nonce >> 8) % nseg + 2
    while gcd(k, nseg) != 1:
        k += 1
    return k


def decode_capture(raw, engine_tag, window_depth, sentinel, unmask_order,
                   drift_source, roster_dict=None):
    """Decode one sealed capture into its logical record stream.

    unmask_order  'keystream_then_rotate' or 'rotate_then_keystream'
    drift_source  'decoded' or 'raw' -- which value field feeds the drift key
    roster_dict   corpus roster dictionary used by the alias and escape guards;
                  when None a local dictionary is bootstrapped as the capture
                  is read, which is what the reference capture needs.
    """
    env = read_envelope(raw)
    reg = env['guard_register']
    on = lambda n: bool(reg & (1 << GUARDS.index(n)))
    bias = (sum(engine_tag.encode('ascii')) % 11) if on('countbias') else 0
    segs = read_segments(raw)
    for s in segs:
        s['count'] = s['declared_count'] - bias

    n = len(segs)
    by_slot = {s['slot']: s for s in segs}
    if sorted(by_slot) != list(range(n)):
        raise DecodeError('segment slots are not a permutation of 0..%d' % (n - 1))
    if on('segperm'):
        step = permutation_step(env['nonce'], n)
        ordered = [None] * n
        for i in range(n):
            ordered[(env['nonce'] + i * step) % n] = by_slot[i]
    else:
        step, ordered = None, [by_slot[i] for i in range(n)]

    window, records = [], []
    local_dict = list(roster_dict) if roster_dict is not None else []
    prev_tick = None      # decoded tick of the previous record of the capture
    drift_sum = 0         # running sum of value fields over logically earlier segments
    ordinal_capture = 0
    diag = dict(bias=bias, permutation_step=step, tick_regressions=0,
                payload_checksum_mismatches=0, filler_width_mismatches=0,
                escape_records=0, escape_pairs=0,
                declared_segments=env['declared_segment_count'] - bias,
                declared_records=env['declared_record_count'] - bias,
                declared_roster=env['declared_roster_size'] - bias)

    for logical_index, seg in enumerate(ordered):
        eff = (seg['salt'] ^ drift_sum) & M32 if on('driftkey') else seg['salt']
        seg['effective_salt'] = eff
        if sum(seg['payload']) % (2 ** 32) != seg['payload_checksum']:
            diag['payload_checksum_mismatches'] += 1
        p, o, seg_values = seg['payload'], 0, []
        prev_diff, prev_tick_seg = 0, 0
        for i in range(seg['count']):
            if o + 2 > len(p):
                raise DecodeError('payload overrun in segment %d' % logical_index)
            visible, shadow = p[o], p[o + 1]
            width = (shadow ^ (eff & 0xFF)) if on('shadow') else visible
            if width <= 0 or o + 2 + width > len(p):
                raise DecodeError('implausible frame width %d in segment %d record %d'
                                  % (width, logical_index, i))
            blob = bytes(p[o + 2:o + 2 + width])
            o += 2 + width

            if on('keystream') and on('rotate'):
                ks = keystream(engine_tag, seg['match_id'], eff, i, width)
                turn = (eff + ordinal_capture) % width
                if unmask_order == 'keystream_then_rotate':
                    blob = bytes(a ^ b for a, b in zip(blob, ks))
                    blob = rot_right(blob, turn)
                else:
                    blob = rot_right(blob, turn)
                    blob = bytes(a ^ b for a, b in zip(blob, ks))
            elif on('keystream'):
                ks = keystream(engine_tag, seg['match_id'], eff, i, width)
                blob = bytes(a ^ b for a, b in zip(blob, ks))
            elif on('rotate'):
                blob = rot_right(blob, (eff + ordinal_capture) % width)

            pairs, bo = [], 0
            if on('escape'):
                while bo + 1 < len(blob) and blob[bo] == sentinel and len(pairs) < 2:
                    directive = blob[bo + 1]
                    pairs.append((directive >> 4, directive & 0xF))
                    bo += 2

            kind, mode = blob[bo], blob[bo + 1]
            bo += 2
            if mode == 0:
                attr = blob[bo:bo + 3].decode('ascii')
                bo += 3
            elif mode in (1, 2):
                attr = blob[bo]
                bo += 1
            else:
                raise DecodeError('unknown attribution mode %d' % mode)
            unit = struct.unpack_from('<H', blob, bo)[0]
            stored_tick = struct.unpack_from('<I', blob, bo + 2)[0]
            stored_value = struct.unpack_from('<I', blob, bo + 6)[0]

            # delta2: second order difference, both differences running from zero
            if on('delta2'):
                diff = (prev_diff + stored_tick) & M32
                tick = (prev_tick_seg + diff) & M32
                prev_diff, prev_tick_seg = diff, tick
            else:
                prev_diff, tick, prev_tick_seg = 0, stored_tick, stored_tick
            if i > 0 and tick < prev_tick_seg:
                diag['tick_regressions'] += 1

            # chain: value masked against the tick of the immediately preceding record
            if on('chain'):
                value = stored_value ^ (env['nonce'] if prev_tick is None else prev_tick)
            else:
                value = stored_value
            prev_tick = tick

            # attribution as written on the wire, before any escape directive
            if mode == 0:
                wire_key = attr
            elif mode == 1:
                if on('backref'):
                    if attr >= len(window):
                        raise DecodeError('back reference %d outside a window of %d'
                                          % (attr, len(window)))
                    wire_key = window[attr]
                else:
                    wire_key = attr
            else:
                if on('alias'):
                    if attr >= len(local_dict):
                        raise DecodeError('alias position %d outside the roster dictionary'
                                          % attr)
                    wire_key = local_dict[attr]
                else:
                    wire_key = attr

            if wire_key not in local_dict:
                local_dict.append(wire_key)

            if on('backref'):
                if wire_key in window:
                    window.remove(wire_key)
                window.append(wire_key)
                if len(window) > window_depth:
                    window.pop(0)

            key = wire_key
            for op, operand in pairs:
                # the corpus dictionary heads the working list, so positions 0..len-1 keep
                # the meaning the reference capture fixed; keys met only on the wire of this
                # capture are appended behind them, which is what lets an escape advance
                # speak about a key the reference capture never met
                if op == 1:
                    if operand >= len(local_dict):
                        raise DecodeError('escape redirect outside the roster dictionary')
                    key = local_dict[operand]
                elif op == 2:
                    if key not in local_dict:
                        raise DecodeError('escape advance on a key outside the roster dictionary')
                    key = local_dict[(local_dict.index(key) + operand) % len(local_dict)]
                else:
                    raise DecodeError('unknown escape operation %d' % op)

            records.append(dict(kind=kind, attribution_mode=mode,
                                logical_segment=logical_index, match_id=seg['match_id'],
                                record_ordinal=ordinal_capture, ordinal_in_segment=i,
                                squad_key=key, wire_key=wire_key, unit=unit, tick=tick,
                                value=value, escape_directives=pairs))
            seg_values.append(value if drift_source == 'decoded' else stored_value)
            ordinal_capture += 1
            if pairs:
                diag['escape_records'] += 1
                diag['escape_pairs'] += len(pairs)

        drift_sum = (drift_sum + sum(seg_values)) & M32
        # the trailing filler is three bytes more than the written salt modulo five
        if seg['payload_width'] - o != 3 + (seg['salt'] % 5):
            diag['filler_width_mismatches'] += 1

    diag['segments_recovered'] = len(ordered)
    diag['records_recovered'] = len(records)
    return records, diag, env


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------

def parse_roster(text, *keywords):
    for line in text.splitlines():
        m = re.match(r'^\s*(.*?roster)\s*:\s*(.+)$', line, re.I)
        if not m:
            continue
        label, rest = m.group(1).lower(), m.group(2)
        if all(k in label for k in keywords):
            items = [x.group(1).strip() for x in re.finditer(r'`([^`]+)`', rest)]
            if items:
                return items
    return None


def parse_scalar(text, *keywords):
    for line in text.splitlines():
        m = re.match(r'^\s*([^:`]+):\s*`?([^`\n]+?)`?\s*$', line)
        if not m:
            continue
        label = m.group(1).strip().lower()
        if all(k in label for k in keywords):
            return m.group(2).strip()
    return None


def parse_controls(text):
    out = collections.OrderedDict()
    for line in text.splitlines():
        m = re.match(r'^\s*-\s*([Cc]\d+)\s+(.+?)\s*:\s*(\d+)\s*$', line)
        if m:
            out[m.group(1)] = dict(label=m.group(2).strip().lower(), published=int(m.group(3)))
    return out


def read_csv(path):
    with open(path, 'r', encoding='utf-8', newline='') as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------
# ownership and settlement
# --------------------------------------------------------------------------

def follow(handovers, start):
    """Follow one chain; returns (endpoint, cycle) with cycle None when open."""
    seen, cur = [], start
    while cur in handovers and cur not in seen:
        seen.append(cur)
        cur = handovers[cur]
    if cur in seen:
        cyc = seen[seen.index(cur):]
        return min(cyc), cyc
    return cur, None


def resolve_owners(handovers, universe):
    return {s: follow(handovers, s)[0] for s in universe}


def sweep_settlement(handovers, universe):
    """Repeated sweeps.  A closed set is folded onto its least key so that the
    sweeps converge; the count includes the sweep that changed nothing."""
    collapsed = dict(handovers)
    for s in list(handovers):
        _, cyc = follow(handovers, s)
        if cyc:
            rep = min(cyc)
            for member in cyc:
                collapsed[member] = rep
    owner = {s: s for s in universe}
    sweeps = 0
    while True:
        new = {s: collapsed.get(o, o) for s, o in owner.items()}
        sweeps += 1
        if new == owner:
            return owner, sweeps
        owner = new


def promotion_tier(score, threshold):
    if score >= 3 * threshold:
        return 'champion'
    if score >= 2 * threshold:
        return 'contender'
    if score >= threshold:
        return 'qualifier'
    return 'provisional'


def settle(records, ladder_weight, levy_rate, budget, threshold):
    """Score, tier and rank one capture.  Records arrive in logical order."""
    by_segment = collections.OrderedDict()
    for r in records:
        by_segment.setdefault(r['logical_segment'], []).append(r)
    matches = []
    for seg_index, rr in by_segment.items():
        close_tick, eliminations, concessions = None, {}, {}
        for r in rr:
            if r['kind'] == 8:
                close_tick = r['tick']
            elif r['kind'] == 3:
                eliminations.setdefault((r['squad_key'], r['unit']), r['tick'])
            elif r['kind'] == 5:
                concessions.setdefault(r['squad_key'], r['tick'])
        stints, open_stints = [], {}
        gross, voided = collections.Counter(), collections.Counter()
        for r in rr:
            kind, s, u, t = r['kind'], r['squad_key'], r['unit'], r['tick']
            if kind == 1:
                open_stints[(s, u)] = (t, r['value'])
            elif kind == 2:
                if s in concessions and t >= concessions[s]:
                    voided[s] += r['value']
                elif (s, u) in eliminations and t >= eliminations[(s, u)]:
                    voided[s] += r['value']
                else:
                    gross[s] += r['value']
            elif kind == 4:
                if (s, u) in open_stints:
                    start, rate = open_stints.pop((s, u))
                    stints.append((s, start, rate, t))
        for (s, u), (start, rate) in open_stints.items():
            stints.append((s, start, rate, close_tick))
        raw = collections.Counter()
        for s, start, rate, end in stints:
            raw[s] += rate * (end - start)
        matches.append(dict(logical_segment=seg_index, match_id=rr[0]['match_id'],
                            close_tick=close_tick, gross=gross, voided=voided, raw=raw,
                            eliminations=eliminations, concessions=concessions,
                            participants=sorted({r['squad_key'] for r in rr}),
                            escape_records=sum(1 for r in rr if r['escape_directives'])))

    squads = sorted({r['squad_key'] for r in records})
    remaining = {s: budget for s in squads}
    for m in matches:
        m['charged'], m['overflow'] = {}, {}
        for s in m['participants']:
            r0 = m['raw'].get(s, 0)
            c = min(r0, remaining[s])
            m['charged'][s], m['overflow'][s] = c, r0 - c
            remaining[s] -= c

    for m in matches:
        m['match_score'] = {s: m['gross'].get(s, 0) * ladder_weight(s)
                            - m['charged'].get(s, 0) * levy_rate(s)
                            for s in m['participants']}
        best = None
        for s in sorted(m['match_score']):
            if s not in m['concessions'] and (best is None
                                             or m['match_score'][s] > m['match_score'][best]):
                best = s
        winners = [] if best is None else [
            s for s in sorted(m['match_score'])
            if s not in m['concessions'] and m['match_score'][s] == m['match_score'][best]]
        m['winners'] = winners
        m['points'] = {}
        for s in m['participants']:
            if s in m['concessions']:
                m['points'][s] = 0
            elif len(winners) == 1 and s == winners[0]:
                m['points'][s] = 3
            elif s in winners:
                m['points'][s] = 1
            else:
                m['points'][s] = 0

    agg = {s: dict(squad_key=s, matches=0, gross_output=0, voided_output=0, raw_upkeep=0,
                   charged_upkeep=0, overflow_upkeep=0, season_score=0, match_awards=0,
                   streak_credit=0, results=[]) for s in squads}
    for m in matches:
        for s in m['participants']:
            a = agg[s]
            a['matches'] += 1
            a['gross_output'] += m['gross'].get(s, 0)
            a['voided_output'] += m['voided'].get(s, 0)
            a['raw_upkeep'] += m['raw'].get(s, 0)
            a['charged_upkeep'] += m['charged'].get(s, 0)
            a['overflow_upkeep'] += m['overflow'].get(s, 0)
            a['season_score'] += m['match_score'][s]
            a['match_awards'] += m['points'][s]
            a['results'].append('win' if len(m['winners']) == 1 and m['winners'][0] == s
                                else ('shared' if s in m['winners'] else 'other'))
    for a in agg.values():
        run = credit = 0
        for res in a['results']:
            run = run + 1 if res == 'win' else 0
            if run > 1:
                credit += 2
        a['streak_credit'] = credit
        a['standing_points'] = a['match_awards'] + credit
        a['tier'] = promotion_tier(a['season_score'], threshold)
    return dict(matches=matches, squads=agg)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: %s DATA_DIR OUT_DIR\n' % argv[0])
        return 2
    data_dir, out_dir = map(os.path.abspath, argv[1:3])
    if not os.path.isdir(data_dir):
        sys.stderr.write('data directory %s does not exist\n' % data_dir)
        return 2
    os.makedirs(out_dir, exist_ok=True)

    # ---- documents -------------------------------------------------------
    docs = {n: open(os.path.join(data_dir, n), 'r', encoding='utf-8').read()
            for n in sorted(os.listdir(data_dir))
            if os.path.isfile(os.path.join(data_dir, n)) and n.endswith('.md')}
    all_md = '\n'.join(docs.values())

    def roster(keywords, cast=str, what=''):
        items = parse_roster(all_md, *keywords)
        if items is None:
            raise SystemExit('the published %s roster was not found in the material' % what)
        return [cast(x) for x in items]

    def scalar(keywords, what=''):
        v = parse_scalar(all_md, *keywords)
        if v is None:
            raise SystemExit('the published %s was not found in the material' % what)
        return int(v)

    engine_tags = roster(['engine tag'], what='engine tag')
    window_depths = roster(['window depth'], int, 'window depth')
    sentinels = roster(['sentinel'], lambda x: int(x, 0), 'sentinel')
    budgets = roster(['budget cap'], int, 'budget cap')
    thresholds = roster(['promotion threshold'], int, 'promotion threshold')
    eligibility_floor = scalar(['eligibility', 'floor'], 'eligibility match floor')
    prominence = scalar(['prominence'], 'ladder weight prominence')
    standing_floor = scalar(['standing points floor'], 'standing points floor')
    weight_floor = scalar(['combined ladder weight'], 'combined ladder weight floor')
    gross_floor = scalar(['combined gross output'], 'combined gross output floor')
    review_weight = scalar(['review weight'], 'review weight')
    docket_size = scalar(['docket size'], 'docket size')
    docket_cap = scalar(['docket conference cap'], 'docket conference cap')

    controls_doc = next((t for t in docs.values() if re.search(r'^- C\d+', t, re.M)), '')
    control_figures = parse_controls(controls_doc)
    if not control_figures:
        raise SystemExit('no published calibration controls were found')

    registry = read_csv(os.path.join(data_dir, 'entrant-registry.csv'))
    topology = read_csv(os.path.join(data_dir, 'division-topology.csv'))
    licences = read_csv(os.path.join(data_dir, 'licence-classes.csv'))
    divisions = {r['division']: r for r in topology}
    licence_classes = {r['licence_class']: r for r in licences}
    entry_of = {r['squad_key']: r for r in registry}
    conferences = list(dict.fromkeys(r['conference'] for r in topology))
    segments = list(dict.fromkeys(r['segment'] for r in licences))

    # ---- captures, told apart by the capture identifier they carry --------
    raw, roles = {}, {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, 'rb') as fh:
            blob = fh.read()
        if blob[:4] == b'RJNL':
            raw[name] = blob
            roles[name] = {1: 'reference', 2: 'control', 3: 'season'}.get(
                read_envelope(blob)['capture_identifier'] % 10, 'unclassified')
    by_role = collections.defaultdict(list)
    for name in sorted(raw):
        by_role[roles[name]].append(name)
    if not all(by_role[r] for r in ('reference', 'control', 'season')):
        raise SystemExit('the material must carry a reference capture (identifier ending 1), '
                         'a control slice (ending 2) and the sealed season (ending 3)')
    ref_name, ctl_name, sea_name = (by_role[r][0] for r in ('reference', 'control', 'season'))

    def decode(name, tag, depth, sentinel, order, drift, dictionary=None):
        return decode_capture(raw[name], tag, depth, sentinel, order, drift, dictionary)

    def weight_of(s):
        e = entry_of.get(s)
        return int(divisions[e['division']]['ladder_weight']) if e else 1

    def levy_of(s):
        e = entry_of.get(s)
        return int(licence_classes[e['licence_class']]['levy_rate']) if e else 1

    def conference_of(s):
        e = entry_of.get(s)
        return divisions[e['division']]['conference'] if e else None

    def segment_of(s):
        e = entry_of.get(s)
        return licence_classes[e['licence_class']]['segment'] if e else None

    def is_ranked(s):
        e = entry_of.get(s)
        return bool(e) and e['is_ranked'].strip().lower() == 'yes'

    # ---- calibration -----------------------------------------------------
    label_map = {'matches': 'matches opened', 'records': 'records recovered',
                 'squads': 'distinct squads', 'gross': 'gross output',
                 'voided': 'voided output', 'raw': 'raw upkeep',
                 'charged': 'charged upkeep', 'overflow': 'overflow upkeep',
                 'champion': 'champion entrants', 'contender': 'contender entrants',
                 'qualifier': 'qualifier entrants', 'provisional': 'provisional entrants'}
    control_keys = {}
    for fig, info in control_figures.items():
        for k, lab in label_map.items():
            if info['label'] == lab:
                control_keys[k] = fig
    # whatever figures the slice publishes are the figures that pin the reading
    needed = [k for k in label_map if k in control_keys]
    guard_figures = [k for k in ('matches', 'records', 'squads', 'gross', 'voided', 'raw')
                     if k in control_keys]
    budget_figures = [k for k in ('charged', 'overflow') if k in control_keys]
    tier_figures = [k for k in ('champion', 'contender', 'qualifier', 'provisional')
                    if k in control_keys]
    if len(guard_figures) < 3 or not budget_figures or not tier_figures:
        raise SystemExit('the published controls are too thin to pin the unstated quantities')

    grid = {}
    for tag in engine_tags:
        for depth in window_depths:
            for sentinel in sentinels:
                for order in UNMASK_ORDERS:
                    for drift in DRIFT_SOURCES:
                        k = (tag, depth, sentinel, order, drift)
                        try:
                            ref_recs, _, _ = decode(ref_name, *k)
                            dictionary = list(dict.fromkeys(r['wire_key'] for r in ref_recs))
                            recs, diag, _ = decode(ctl_name, *k, dictionary)
                            st = settle(recs, weight_of, levy_of, max(budgets), max(thresholds))
                            a = st['squads']
                            grid[k] = dict(
                                dictionary=dictionary, records=recs,
                                control_values=dict(
                                    matches=len(st['matches']),
                                    records=diag['records_recovered'],
                                    squads=len({r['squad_key'] for r in recs}),
                                    gross=sum(x['gross_output'] for x in a.values()),
                                    voided=sum(x['voided_output'] for x in a.values()),
                                    raw=sum(x['raw_upkeep'] for x in a.values())),
                                per_squad_raw={s: x['raw_upkeep'] for s, x in a.items()},
                                budget_grid={}, score_grid={}, threshold_grid={})
                        except (DecodeError, UnicodeDecodeError, struct.error):
                            grid[k] = None
    live = [k for k, v in grid.items() if v is not None]
    if not live:
        raise SystemExit('no reading of the material decodes the control slice')
    for k in live:
        cell = grid[k]
        for b in budgets:
            charged = sum(min(v, b) for v in cell['per_squad_raw'].values())
            cell['budget_grid'][b] = (charged, sum(cell['per_squad_raw'].values()) - charged)
            # the season score carries charged upkeep, so the tier counts of a
            # threshold candidate can only be judged against the budget it is
            # paired with, never against scores settled at another budget
            cell['score_grid'][b] = {
                s: x['season_score'] for s, x in
                settle(cell['records'], weight_of, levy_of, b, max(thresholds))['squads'].items()}
            cell['threshold_grid'][b] = {t: collections.Counter(
                promotion_tier(v, t) for v in cell['score_grid'][b].values())
                for t in thresholds}

    def reproduced(k, budget, threshold):
        cell = grid[k]
        if cell is None:
            return set()
        ok = {key for key in ('matches', 'records', 'squads', 'gross', 'voided', 'raw')
              if key in control_keys
              and cell['control_values'][key] == control_figures[control_keys[key]]['published']}
        c, o = cell['budget_grid'][budget]
        if 'charged' in control_keys and c == control_figures[control_keys['charged']]['published']:
            ok.add('charged')
        if 'overflow' in control_keys and o == control_figures[control_keys['overflow']]['published']:
            ok.add('overflow')
        tc = cell['threshold_grid'][budget][threshold]
        ok |= {key for key in ('champion', 'contender', 'qualifier', 'provisional')
               if key in control_keys
               and tc.get(key, 0) == control_figures[control_keys[key]]['published']}
        return ok

    full = set(needed)
    readings = [dict(engine_tag=k[0], window_depth=k[1], sentinel=k[2], unmask_order=k[3],
                     drift_source=k[4], season_budget=b, promotion_threshold=t)
                for k in live for b in budgets for t in thresholds
                if reproduced(k, b, t) == full]
    if not readings:
        raise SystemExit('no reading of the material reproduces the published control figures')
    if len(readings) > 1:
        sys.stderr.write('warning: %d readings reproduce the controls; taking the first\n'
                         % len(readings))
    pinned = readings[0]
    dkey = (pinned['engine_tag'], pinned['window_depth'], pinned['sentinel'],
            pinned['unmask_order'], pinned['drift_source'])
    budget, threshold = pinned['season_budget'], pinned['promotion_threshold']
    dictionary = grid[dkey]['dictionary']

    def show(name, v):
        return '0x%02X' % v if name == 'sentinel' else v

    dims = [('engine_tag', 'engine tag', 0, engine_tags),
            ('window_depth', 'window depth', 1, window_depths),
            ('sentinel', 'sentinel', 2, sentinels),
            ('season_budget', 'budget cap', None, budgets),
            ('promotion_threshold', 'promotion threshold', None, thresholds)]
    spaces = collections.OrderedDict()
    for name, label, idx, space in dims:
        rows = []
        for cand in space:
            accepted = sum(1 for r in readings if r[name] == cand)
            completions = [k for k in grid
                           if idx is None or k[idx] == cand] if idx is not None else list(grid)
            decodable = sum(1 for k in completions if grid[k] is not None)
            falsifier = None
            if not accepted:
                for key in needed:                      # earliest figure that no completion hits
                    reachable = False
                    for other in live:
                        if idx is not None and other[idx] != cand:
                            continue
                        for b in budgets:
                            if name == 'season_budget' and b != cand:
                                continue
                            for t in thresholds:
                                if name == 'promotion_threshold' and t != cand:
                                    continue
                                if key in reproduced(other, b, t):
                                    reachable = True
                                    break
                            if reachable:
                                break
                    if not reachable:
                        falsifier = '%s (%s)' % (control_keys[key],
                                                 control_figures[control_keys[key]]['label'])
                        break
            rows.append(dict(value=show(name, cand),
                             verdict='accepted' if accepted else 'rejected',
                             readings_that_accept_it=accepted,
                             decodable_completions=decodable,
                             falsifier=falsifier,
                             note=(None if decodable else
                                   'no completion of the remaining quantities decodes the '
                                   'control slice at all, so no control figure can be reproduced')))
        spaces[name] = dict(
            drawn_from='the published %s roster' % label,
            candidate_space=[r['value'] for r in rows], candidates=rows,
            pinned=show(name, pinned[name]))

    # ---- reference transcript cross-check --------------------------------
    transcript_check = None
    for jname in sorted(os.listdir(data_dir)):
        if not jname.endswith('.jsonl'):
            continue
        expected = [json.loads(l) for l in
                    open(os.path.join(data_dir, jname), 'r', encoding='utf-8')
                    if l.strip()]
        try:
            got, _, _ = decode(ref_name, *dkey)
        except (DecodeError, struct.error):
            continue
        if len(got) != len(expected) or [r['match_id'] for r in got] != [e['match_id']
                                                                        for e in expected]:
            continue
        diffs = sum(1 for a, b in zip(got, expected)
                    if any(a[f] != b[f] for f in ('kind', 'logical_segment', 'match_id',
                                                  'record_ordinal', 'squad_key', 'tick',
                                                  'unit', 'value')))
        transcript_check = dict(transcript=jname, capture=ref_name,
                                records_compared=len(expected), mismatches=diffs,
                                byte_exact_agreement=diffs == 0)
        break

    # ---- decode and settle every capture ---------------------------------
    decoded = {}
    for name in raw:
        recs, diag, env = decode(name, *dkey,
                                 None if name == ref_name else dictionary)
        decoded[name] = dict(records=recs, diag=diag, env=env)

    srecs = decoded[sea_name]['records']
    settlement = settle(srecs, weight_of, levy_of, budget, threshold)
    smatches, sagg = settlement['matches'], settlement['squads']

    handovers, handover_events = {}, []
    for r in srecs:
        if r['kind'] == 6:
            receiver = dictionary[r['value'] % len(dictionary)]
            handovers[r['squad_key']] = receiver
            handover_events.append(dict(record_ordinal=r['record_ordinal'],
                                        match_id=r['match_id'], from_squad=r['squad_key'],
                                        to_squad=receiver))
    universe = set(sagg) | set(handovers)
    swept_owner, sweeps = sweep_settlement(handovers, universe)
    effective_owner = resolve_owners(handovers, universe)
    if swept_owner != effective_owner:
        raise SystemExit('the sweep settlement and the chain settlement disagree')
    closed_sets = sorted({tuple(sorted(follow(handovers, s)[1])) for s in universe
                          if follow(handovers, s)[1] is not None})

    ladder = sorted(sagg.values(), key=lambda a: (-a['season_score'], a['squad_key']))
    for i, a in enumerate(ladder, 1):
        a['rank'] = i

    # ---- entrant entries (absent entrants are listed and contribute nothing)
    def entry_row(s, a=None):
        e = entry_of[s]
        row = collections.OrderedDict(
            [(k, e[k]) for k in ('entrant_id', 'squad_key', 'division', 'licence_class')]
            + [('is_ranked', e['is_ranked']),
               ('conference', divisions[e['division']]['conference']),
               ('segment', licence_classes[e['licence_class']]['segment'])])
        a = a or dict(matches=0, gross_output=0, voided_output=0, raw_upkeep=0,
                      charged_upkeep=0, overflow_upkeep=0, season_score=0, match_awards=0,
                      streak_credit=0, standing_points=0,
                      tier=promotion_tier(0, threshold))
        row.update((k, a[k]) for k in ('matches', 'gross_output', 'voided_output',
                                       'raw_upkeep', 'charged_upkeep', 'overflow_upkeep',
                                       'season_score', 'match_awards', 'streak_credit',
                                       'standing_points', 'tier'))
        row['effective_owner'] = effective_owner.get(s, s)
        row['is_own_effective_owner'] = effective_owner.get(s, s) == s
        return row

    rows_by_squad = {s: entry_row(s, sagg[s]) for s in sagg if s in entry_of}
    absent_entrants = sorted(s for s in entry_of if s not in sagg)
    for s in absent_entrants:
        rows_by_squad[s] = entry_row(s)
    all_rows = sorted(rows_by_squad.values(), key=lambda r: r['squad_key'])

    def group_report(kind, name, members):
        tc = collections.Counter(e['tier'] for e in members)
        return collections.OrderedDict([
            ('group_kind', kind), ('group_name', name), ('entrants', len(members)),
            ('divisions', sorted({e['division'] for e in members})),
            ('entries', members),
            ('gross_output', sum(e['gross_output'] for e in members)),
            ('voided_output', sum(e['voided_output'] for e in members)),
            ('charged_upkeep', sum(e['charged_upkeep'] for e in members)),
            ('overflow_upkeep', sum(e['overflow_upkeep'] for e in members)),
            ('season_score', sum(e['season_score'] for e in members)),
            ('standing_points', sum(e['standing_points'] for e in members)),
            ('tier_counts', collections.OrderedDict((t, tc.get(t, 0)) for t in TIERS))])

    conference_reports = {c: group_report('conference', c,
                                          [e for e in all_rows if e['conference'] == c])
                          for c in conferences}
    segment_reports = {sg: group_report('segment', sg,
                                        [e for e in all_rows if e['segment'] == sg])
                       for sg in segments}

    # ---- finals bracket --------------------------------------------------
    handed_away = set(handovers)
    candidates = [s for s in sagg
                  if effective_owner.get(s, s) == s and sagg[s]['matches'] >= eligibility_floor]

    def constraint_values(combo):
        confs = [conference_of(s) for s in combo if conference_of(s) is not None]
        segs = {segment_of(s) for s in combo if segment_of(s) is not None}
        return collections.OrderedDict([
            ('prominence', any(weight_of(s) >= prominence for s in combo)),
            ('both_segments', segs >= set(segments)),
            ('standing_floor', all(sagg[s]['standing_points'] >= standing_floor for s in combo)),
            ('distinct_conferences', len(confs) == len(set(confs))),
            ('ranked_entrant', any(is_ranked(s) for s in combo)),
            ('ladder_weight_floor', sum(weight_of(s) for s in combo) >= weight_floor),
            ('no_overflow', all(sagg[s]['overflow_upkeep'] == 0 for s in combo)),
            ('champion_present', any(sagg[s]['tier'] == 'champion' for s in combo)),
            ('gross_floor', sum(sagg[s]['gross_output'] for s in combo) >= gross_floor),
            ('no_handover', all(s not in handed_away for s in combo))])

    bracket_space = list(itertools.combinations(sorted(candidates), 3))
    scored = [(sum(v.values()), sum(sagg[s]['charged_upkeep'] for s in combo), combo, v)
              for combo, v in ((c, constraint_values(c)) for c in bracket_space)]
    best_count = max((s[0] for s in scored), default=0)
    finalists = sorted([s for s in scored if s[0] == best_count], key=lambda s: (s[1], s[2]))
    elected = finalists[0] if finalists else None

    # ---- review docket ---------------------------------------------------
    for m in smatches:
        registered = {s: m['gross'].get(s, 0) for s in m['participants'] if s in entry_of}
        m['leading_squad'] = (sorted(registered, key=lambda s: (-registered[s], s))[0]
                              if registered else None)
        m['voided_total'] = sum(m['voided'].values())
        m['review_index'] = m['voided_total'] + review_weight * m['escape_records']
        m['conference'] = conference_of(m['leading_squad']) if m['leading_squad'] else None

    def docket_summary(m):
        return collections.OrderedDict([
            ('match_id', m['match_id']), ('conference', m['conference']),
            ('leading_squad', m['leading_squad']), ('voided_output', m['voided_total']),
            ('escape_directive_records', m['escape_records']),
            ('review_index', m['review_index'])])

    pool = sorted(smatches, key=lambda m: -m['review_index'])
    best = [None]

    def search(start, picks, per_conf, total):
        if len(picks) == docket_size:
            ids = tuple(sorted(m['match_id'] for m in picks))
            if best[0] is None or total > best[0][0] or (total == best[0][0] and ids < best[0][1]):
                best[0] = (total, ids)
            return
        room = docket_size - len(picks)
        # bound: filling every remaining seat with the largest indices still on the table
        ceiling = total + sum(m['review_index'] for m in pool[start:start + room])
        if best[0] is not None and ceiling < best[0][0]:
            return
        for j in range(start, len(pool)):
            m = pool[j]
            c = m['conference']
            if per_conf[c] >= docket_cap:
                continue
            if len(pool) - j < room:
                break
            picks.append(m)
            per_conf[c] += 1
            search(j + 1, picks, per_conf, total + m['review_index'])
            picks.pop()
            per_conf[c] -= 1

    search(0, [], collections.Counter(), 0)
    docket_total, docket_ids = best[0] if best[0] else (0, ())
    docket_matches = [m for m in sorted(smatches, key=lambda m: m['match_id'])
                      if m['match_id'] in docket_ids]
    docket_spread = collections.OrderedDict(
        sorted(collections.Counter(m['conference'] for m in docket_matches).items(),
               key=lambda kv: (kv[0] is None, kv[0] or '')))

    # ---- data quality ----------------------------------------------------
    # ordered by role rather than by file name, so the record reads the same
    # whichever names the captures were shipped under
    role_order = {'reference': 0, 'control': 1, 'season': 2, 'unclassified': 3}
    capture_order = sorted(raw, key=lambda n: (role_order[roles[n]],
                                                decoded[n]['env']['capture_identifier'], n))
    capture_quality = collections.OrderedDict()
    for name in capture_order:
        d, recs = decoded[name], decoded[name]['records']
        diag, env = d['diag'], d['env']
        st = settle(recs, weight_of, levy_of, budget, threshold)
        a = st['squads']
        seen = {r['squad_key'] for r in recs}
        registered = seen & set(entry_of)
        ledger = collections.OrderedDict(
            (n, sum(1 for r in recs if r['kind'] == k)) for n, k in KIND_NAMES.items())
        capture_quality[name] = collections.OrderedDict([
            ('role', roles[name]), ('capture_identifier', env['capture_identifier']),
            ('guards_active', env['guards_active']),
            ('envelope_checksum_agrees', env['envelope_checksum_agrees']),
            ('declared_segment_count', diag['declared_segments']),
            ('segments_recovered', diag['segments_recovered']),
            ('segment_count_residual', diag['segments_recovered'] - diag['declared_segments']),
            ('declared_record_count', diag['declared_records']),
            ('records_recovered', diag['records_recovered']),
            ('record_count_residual', diag['records_recovered'] - diag['declared_records']),
            ('declared_roster_size', diag['declared_roster']),
            ('registered_squads_recovered', len(registered)),
            ('roster_size_residual', len(registered) - diag['declared_roster']),
            ('distinct_squads_recovered', len(seen)),
            ('unregistered_squads', sorted(seen - set(entry_of))),
            ('payload_checksum_mismatches', diag['payload_checksum_mismatches']),
            ('filler_width_mismatches', diag['filler_width_mismatches']),
            ('tick_regressions', diag['tick_regressions']),
            ('escape_directive_records', diag['escape_records']),
            ('kind_ledger', ledger),
            ('kind_ledger_residual', len(recs) - sum(ledger.values())),
            ('gross_output', sum(x['gross_output'] for x in a.values())),
            ('voided_output', sum(x['voided_output'] for x in a.values())),
            ('raw_upkeep', sum(x['raw_upkeep'] for x in a.values())),
            ('charged_upkeep', sum(x['charged_upkeep'] for x in a.values())),
            ('overflow_upkeep', sum(x['overflow_upkeep'] for x in a.values()))])

    season_gross = sum(x['gross_output'] for x in sagg.values())
    conference_gross = sum(r['gross_output'] for r in conference_reports.values())
    unregistered_squads = sorted(set(sagg) - set(entry_of))
    output_reported = sum(r['value'] for r in srecs if r['kind'] == 2)
    season_ledger = collections.OrderedDict(
        (n, sum(1 for r in srecs if r['kind'] == k)) for n, k in KIND_NAMES.items())

    season_quality = collections.OrderedDict([
        ('attribution_residual', season_gross - conference_gross),
        ('attribution_residual_explanation',
         'season gross output less the sum of the conference gross outputs: the output settled '
         'for squads that belong to no conference'),
        ('squads_behind_the_attribution_residual', unregistered_squads),
        ('output_ledger_residual',
         (season_gross + sum(x['voided_output'] for x in sagg.values())) - output_reported),
        ('upkeep_ledger_residual',
         (sum(x['charged_upkeep'] for x in sagg.values())
          + sum(x['overflow_upkeep'] for x in sagg.values()))
         - sum(x['raw_upkeep'] for x in sagg.values())),
        ('kind_ledger', season_ledger),
        ('kind_ledger_residual', len(srecs) - sum(season_ledger.values())),
        ('tick_regressions', decoded[sea_name]['diag']['tick_regressions']),
        ('unregistered_squads', unregistered_squads),
        ('absent_entrants', absent_entrants),
        ('reference_transcript_cross_check', transcript_check)])

    findings = []
    for name, q in capture_quality.items():
        for field, text in (
                ('segment_count_residual',
                 'declared segment count disagrees with the segments recovered'),
                ('record_count_residual',
                 'declared record count disagrees with the records recovered'),
                ('roster_size_residual',
                 'declared roster size disagrees with the registered squads recovered'),
                ('payload_checksum_mismatches',
                 'segment payload checksums disagree with the payload bytes'),
                ('filler_width_mismatches',
                 'trailing filler width disagrees with the written segment salt'),
                ('tick_regressions', 'ticks move backwards inside a segment'),
                ('kind_ledger_residual',
                 'recovered records carry event kinds outside the named ledger')):
            if q[field]:
                findings.append(collections.OrderedDict(
                    [('capture', name), ('role', q['role']), ('finding', text),
                     ('observed', q[field])]))
        if q['unregistered_squads']:
            findings.append(collections.OrderedDict(
                [('capture', name), ('role', q['role']),
                 ('finding', 'squads appear on the wire without an entry in the registry'),
                 ('observed', q['unregistered_squads'])]))
    if season_quality['attribution_residual']:
        findings.append(collections.OrderedDict(
            [('capture', sea_name), ('role', 'season'),
             ('finding', 'season gross output exceeds the sum of the conference gross outputs'),
             ('observed', season_quality['attribution_residual'])]))
    if absent_entrants:
        findings.append(collections.OrderedDict(
            [('capture', sea_name), ('role', 'season'),
             ('finding', 'entrants appear in the registry but in no capture'),
             ('observed', absent_entrants)]))
    if transcript_check and not transcript_check['byte_exact_agreement']:
        findings.append(collections.OrderedDict(
            [('capture', transcript_check['capture']), ('role', 'reference'),
             ('finding', 'the reference capture disagrees with its labelled transcript'),
             ('observed', transcript_check['mismatches'])]))

    data_quality = collections.OrderedDict([
        ('captures', capture_quality), ('season_reconciliation', season_quality),
        ('findings', findings)])

    # ---- policy audit ----------------------------------------------------
    cell = grid[dkey]
    calibration_rows = []
    for k in needed:
        if k in ('charged', 'overflow'):
            got = cell['budget_grid'][budget][0 if k == 'charged' else 1]
        elif k in ('champion', 'contender', 'qualifier', 'provisional'):
            got = cell['threshold_grid'][budget][threshold].get(k, 0)
        else:
            got = cell['control_values'][k]
        published = control_figures[control_keys[k]]['published']
        calibration_rows.append(collections.OrderedDict(
            [('figure', control_keys[k]), ('label', control_figures[control_keys[k]]['label']),
             ('published', published), ('reproduced', got), ('agrees', published == got)]))

    audit = collections.OrderedDict([
        ('method',
         'each unstated quantity is drawn from the roster published beside it and pinned '
         'against the control figures published with the control slice; a candidate is '
         'rejected by the earliest control figure that no completion of the remaining '
         'quantities reproduces'),
        ('unstated_quantities', spaces),
        ('decoder_resolution', collections.OrderedDict([
            ('unmask_order', pinned['unmask_order']),
            ('drift_key_source', pinned['drift_source']),
            ('note', 'the order in which the keystream and rotate guards are undone, and the '
                     'value field that feeds the drift key, are not written down either; both '
                     'were resolved against the same control figures')])),
        ('attribution_settlement', collections.OrderedDict([
            ('passes', sweeps),
            ('entry_handovers', handover_events),
            ('effective_owners', {k: v for k, v in sorted(effective_owner.items()) if k != v}),
            ('closed_sets', [list(c) for c in closed_sets])])),
        ('bracket', collections.OrderedDict([
            ('candidate_space_size', len(bracket_space)),
            ('best_satisfied_constraint_count', best_count),
            ('constraints', [collections.OrderedDict([('name', n), ('requirement', d)])
                             for n, d in BRACKET_CONSTRAINTS]),
            ('elected', list(elected[2]) if elected else []),
            ('satisfied_constraint_count', elected[0] if elected else 0),
            ('total_charged_upkeep', elected[1] if elected else 0),
            ('constraint_values', elected[3] if elected else collections.OrderedDict()),
            ('unsatisfied_constraints',
             [n for n, v in (elected[3] if elected else collections.OrderedDict()).items()
              if not v]),
            ('candidates_at_best_count', len(finalists)),
            ('tie_break', 'least total charged upkeep, then the member keys sorting first')])),
        ('review_docket', collections.OrderedDict([
            ('candidate_space_size', math.comb(len(smatches), docket_size)),
            ('docket_size', docket_size), ('conference_cap', docket_cap),
            ('total_review_index', docket_total),
            ('conference_spread', docket_spread),
            ('matches', [docket_summary(m) for m in docket_matches]),
            ('tie_break', 'the match identifiers sorting first')])),
        ('calibration', collections.OrderedDict([
            ('population', ctl_name), ('rows', calibration_rows),
            ('all_agree', all(r['agrees'] for r in calibration_rows))]))])

    # ---- summary ---------------------------------------------------------
    def ladder_row(a):
        s = a['squad_key']
        return collections.OrderedDict(
            [('rank', a['rank']), ('squad_key', s),
             ('entrant_id', entry_of[s]['entrant_id'] if s in entry_of else None),
             ('registered', s in entry_of),
             ('division', entry_of[s]['division'] if s in entry_of else None),
             ('conference', conference_of(s)),
             ('licence_class', entry_of[s]['licence_class'] if s in entry_of else None),
             ('segment', segment_of(s)),
             ('is_ranked', is_ranked(s)),
             ('effective_owner', effective_owner.get(s, s)),
             ('is_own_effective_owner', effective_owner.get(s, s) == s)]
            + [(k, a[k]) for k in ('matches', 'gross_output', 'voided_output', 'raw_upkeep',
                                   'charged_upkeep', 'overflow_upkeep', 'season_score',
                                   'match_awards', 'streak_credit', 'standing_points', 'tier')])

    summary = collections.OrderedDict([
        ('captures', collections.OrderedDict(
            (n, collections.OrderedDict([('role', roles[n]),
                                         ('capture_identifier', decoded[n]['env']['capture_identifier']),
                                         ('guards_active', decoded[n]['env']['guards_active'])]))
            for n in capture_order)),
        ('pinned_quantities', collections.OrderedDict([
            ('engine_tag', pinned['engine_tag']), ('window_depth', pinned['window_depth']),
            ('sentinel', show('sentinel', pinned['sentinel'])),
            ('season_budget', budget), ('promotion_threshold', threshold)])),
        ('roster_dictionary', dictionary),
        ('season', collections.OrderedDict([
            ('capture', sea_name),
            ('capture_identifier', decoded[sea_name]['env']['capture_identifier']),
            ('matches', len(smatches)), ('records_recovered', len(srecs)),
            ('squads', len(sagg)),
            ('registered_squads', len(set(sagg) & set(entry_of))),
            ('unregistered_squads', unregistered_squads),
            ('absent_entrants', absent_entrants),
            ('gross_output', season_gross),
            ('voided_output', sum(x['voided_output'] for x in sagg.values())),
            ('raw_upkeep', sum(x['raw_upkeep'] for x in sagg.values())),
            ('charged_upkeep', sum(x['charged_upkeep'] for x in sagg.values())),
            ('overflow_upkeep', sum(x['overflow_upkeep'] for x in sagg.values())),
            ('tier_counts', collections.OrderedDict(
                (t, sum(1 for x in sagg.values() if x['tier'] == t)) for t in TIERS))])),
        ('entry_handovers', collections.OrderedDict([
            ('settlement_passes', sweeps), ('handovers', handover_events),
            ('effective_owners', {k: v for k, v in sorted(effective_owner.items()) if k != v})])),
        ('ladder', [ladder_row(a) for a in ladder]),
        ('finals_bracket', collections.OrderedDict([
            ('members', list(elected[2]) if elected else []),
            ('satisfied_constraint_count', elected[0] if elected else 0),
            ('total_charged_upkeep', elected[1] if elected else 0),
            ('constraint_values', elected[3] if elected else collections.OrderedDict()),
            ('unsatisfied_constraints',
             [n for n, v in (elected[3] if elected else collections.OrderedDict()).items()
              if not v])])),
        ('review_docket', collections.OrderedDict([
            ('match_ids', list(docket_ids)), ('total_review_index', docket_total),
            ('conference_spread', docket_spread),
            ('matches', [docket_summary(m) for m in docket_matches])]))])

    def dump(name, obj):
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, indent=2)
            fh.write('\n')

    dump('summary.json', summary)
    dump('data_quality.json', data_quality)
    dump('policy_audit.json', audit)
    for c, rep in conference_reports.items():
        dump('region_%s.json' % c, rep)
    for sg, rep in segment_reports.items():
        dump('segment_%s.json' % sg, rep)

    sys.stderr.write('settled %d matches over %d squads from %s\n'
                     % (len(smatches), len(sagg), sea_name))
    sys.stderr.write('bracket %s (%d/%d constraints); docket %s (review index %d)\n'
                     % (list(elected[2]) if elected else [], elected[0] if elected else 0,
                        len(BRACKET_CONSTRAINTS), list(docket_ids), docket_total))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
