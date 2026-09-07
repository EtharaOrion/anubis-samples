#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Reads only DATA_DIR, writes only OUT_DIR.  Standard library only.
"""

import csv
import hashlib
import itertools
import json
import os
import re
import struct
import sys
from math import gcd

M32 = 1 << 32

# ---------------------------------------------------------------- guard bits
GUARD_NAMES = ['keystream', 'chain', 'delta2', 'driftkey', 'segperm', 'backref',
               'endian', 'countbias', 'shadow', 'rotate', 'alias', 'escape']
KEYSTREAM, CHAIN, DELTA2, DRIFTKEY, SEGPERM, BACKREF = 0, 1, 2, 3, 4, 5
ENDIAN, COUNTBIAS, SHADOW, ROTATE, ALIAS, ESCAPE = 6, 7, 8, 9, 10, 11

KIND_NAMES = {1: 'deploy', 2: 'output', 3: 'eliminate', 4: 'disband',
              5: 'concede', 6: 'transfer', 8: 'close'}

BODY_WIDTH = {0: 15, 1: 13, 2: 13}   # kind+mode+attr+unit(2)+tick(4)+value(4)


class Bad(Exception):
    """A capture does not decode under the offered reading."""

    def __init__(self, message, progress=0):
        Exception.__init__(self, message)
        self.progress = progress            # records recovered before giving up


# ---------------------------------------------------------------- documents
def read_text(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read()


def scan_docs(data_dir):
    """Concatenate every markdown document shipped with the corpus."""
    blob = []
    for name in sorted(os.listdir(data_dir)):
        if name.lower().endswith(('.md', '.txt')):
            blob.append(read_text(os.path.join(data_dir, name)))
    return '\n'.join(blob)


def find_roster(docs, label):
    """`Label roster: `a`, `b`, ...` -> ['a', 'b', ...]."""
    for line in docs.splitlines():
        if line.strip().lower().startswith(label.lower()):
            head, _, tail = line.partition(':')
            if head.strip().lower().rstrip('s') != label.lower().rstrip('s'):
                continue
            items = re.findall(r'`([^`]+)`', tail)
            if items:
                return items
    raise Bad('roster not found: %s' % label)


def find_scalar(docs, label, default=None):
    """`Label: `7`` -> 7."""
    for line in docs.splitlines():
        head, sep, tail = line.partition(':')
        if not sep:
            continue
        if head.strip().lower() == label.lower():
            m = re.search(r'`?(-?\d+)`?', tail)
            if m:
                return int(m.group(1))
    if default is not None:
        return default
    raise Bad('scalar not found: %s' % label)


def as_int(text):
    t = text.strip()
    return int(t, 16) if t.lower().startswith('0x') else int(t)


# ---------------------------------------------------------------- envelopes
def read_envelope(data):
    if data[:4] != b'RJNL':
        raise Bad('envelope marker')
    e = {'version': data[4], 'reserved': data[5]}
    e['guard_register'] = struct.unpack('<H', data[6:8])[0]
    e['nonce_le'] = struct.unpack('<I', data[8:12])[0]
    e['nonce_be'] = struct.unpack('>I', data[8:12])[0]
    (e['declared_segments'], e['declared_records'], e['base_tick'],
     e['capture_id'], e['declared_roster'], e['checksum']) = struct.unpack('<6I', data[12:36])
    e['checksum_computed'] = sum(data[:32]) % M32
    e['checksum_ok'] = e['checksum'] == e['checksum_computed']
    return e


def parse_segments(data):
    off, segs = 36, []
    while off < len(data):
        if data[off:off + 4] != b'SEG0':
            raise Bad('segment marker')
        if off + 28 > len(data):
            raise Bad('segment envelope truncated')
        slot, mid, rc, salt, pw, pck = struct.unpack('<6I', data[off + 4:off + 28])
        off += 28
        pay = data[off:off + pw]
        if len(pay) != pw:
            raise Bad('segment payload truncated')
        off += pw
        segs.append({'slot': slot, 'match_id': mid, 'declared_records': rc,
                     'salt': salt, 'payload_width': pw, 'payload_checksum': pck,
                     'payload': pay,
                     'checksum_computed': sum(pay) % M32,
                     'checksum_ok': (sum(pay) % M32) == pck})
    return segs


def perm_step(nonce, n):
    """Smallest integer at least two more than (nonce >> 8) mod n, coprime with n."""
    s = ((nonce >> 8) % n) + 2
    while gcd(s, n) != 1:
        s += 1
    return s


def rot_right(b, k):
    k %= len(b)
    return b if k == 0 else b[-k:] + b[:-k]


# ---------------------------------------------------------------- decoding
def decode_capture(data, tag, depth, sentinel, roster_dict):
    """Recover the logically ordered record stream of one capture.

    `roster_dict` is the corpus roster dictionary (or None while it is being
    built from the reference capture).
    """
    env = read_envelope(data)
    g = env['guard_register']
    on = lambda bit: bool(g >> bit & 1)

    nonce = env['nonce_be'] if on(ENDIAN) else env['nonce_le']
    bias = (sum(tag.encode()) % 11) if on(COUNTBIAS) else 0

    segs = parse_segments(data)
    n = len(segs)
    if n == 0:
        raise Bad('no segments')

    if on(SEGPERM):
        step = perm_step(nonce, n)
        logical_of_slot = [(nonce + i * step) % n for i in range(n)]
    else:
        step = 1
        logical_of_slot = list(range(n))
    order = [None] * n
    for i, lp in enumerate(logical_of_slot):
        if order[lp] is not None:
            raise Bad('segment permutation collides')
        order[lp] = segs[i]

    recs = []
    running = 0            # drift key: value sum of logically earlier segments
    prev_tick = None       # chain: tick of the previously decoded record
    cap_ord = 0
    window = []            # back reference: least recent first
    backwards = 0

    for lp, seg in enumerate(order):
        rc = seg['declared_records'] - bias
        if rc < 0:
            raise Bad('negative segment record count')
        seg['logical_position'] = lp
        seg['recovered_records'] = rc
        eff_salt = (seg['salt'] ^ running) if on(DRIFTKEY) else seg['salt']
        seg['effective_salt'] = eff_salt
        pay = seg['payload']
        p, prev_t, prev_d1, seg_sum = 0, 0, 0, 0

        for ri in range(rc):
            if p + 2 > len(pay):
                raise Bad('frame header past payload', len(recs))
            width = (pay[p + 1] ^ (eff_salt & 0xFF)) if on(SHADOW) else pay[p]
            if width < 13 or p + 2 + width > len(pay):
                raise Bad('frame width', len(recs))
            blob = pay[p + 2:p + 2 + width]
            p += 2 + width

            if on(KEYSTREAM):
                ks = hashlib.sha256(('%s:%d:%d:%d' % (tag, seg['match_id'], eff_salt, ri))
                                    .encode()).digest()[:width]
                blob = bytes(x ^ y for x, y in zip(blob, ks))
            if on(ROTATE):
                blob = rot_right(blob, (eff_salt + cap_ord) % width)

            directives = []
            q = 0
            if on(ESCAPE):
                while len(directives) < 2 and q + 1 < len(blob) and blob[q] == sentinel:
                    directives.append(blob[q + 1])
                    q += 2
            body = blob[q:]
            if len(body) < 13:
                raise Bad('body too short', len(recs))
            kind, mode = body[0], body[1]
            if kind not in KIND_NAMES:
                raise Bad('event kind %d' % kind, len(recs))
            if mode not in BODY_WIDTH or len(body) != BODY_WIDTH[mode]:
                raise Bad('attribution mode %d' % mode, len(recs))
            if mode == 0:
                raw = body[2:5]
                if not all(32 <= c < 127 for c in raw):
                    raise Bad('attribution key not printable', len(recs))
                wire = raw.decode('ascii')
                z = 5
            else:
                pos = body[2]
                if mode == 1:
                    if pos >= len(window):
                        raise Bad('back reference out of range', len(recs))
                    wire = window[pos]
                else:
                    if not roster_dict or pos >= len(roster_dict):
                        raise Bad('alias out of range', len(recs))
                    wire = roster_dict[pos]
                z = 3
            unit = struct.unpack('<H', body[z:z + 2])[0]
            t_raw = struct.unpack('<I', body[z + 2:z + 6])[0]
            v_raw = struct.unpack('<I', body[z + 6:z + 10])[0]

            if on(DELTA2):
                d1 = (prev_d1 + t_raw) % M32
                tick = (prev_t + d1) % M32
                if d1 >= (M32 >> 1):
                    backwards += 1
                prev_d1, prev_t = d1, tick
            else:
                tick = t_raw
                if tick < prev_t:
                    backwards += 1
                prev_t = tick

            if on(CHAIN):
                value = v_raw ^ (nonce if prev_tick is None else prev_tick)
            else:
                value = v_raw
            prev_tick = tick

            # the rolling window is refreshed from the wire key, before escapes
            if wire in window:
                window.remove(wire)
            window.append(wire)
            while len(window) > depth:
                window.pop(0)

            # directive pairs resolve innermost first
            eff = wire
            for d in reversed(directives):
                op, operand = d >> 4, d & 0xF
                if not roster_dict:
                    continue
                if op == 1:
                    if operand < len(roster_dict):
                        eff = roster_dict[operand]
                elif op == 2 and eff in roster_dict:
                    eff = roster_dict[(roster_dict.index(eff) + operand) % len(roster_dict)]

            seg_sum = (seg_sum + value) % M32
            recs.append({'kind': kind, 'mode': mode, 'wire_key': wire, 'squad_key': eff,
                         'unit': unit, 'tick': tick, 'value': value,
                         'match_id': seg['match_id'], 'logical_segment': lp,
                         'record_ordinal': cap_ord, 'segment_ordinal': ri,
                         'escapes': len(directives)})
            cap_ord += 1
        running = (running + seg_sum) % M32

    return {'envelope': env, 'segments': order, 'records': recs, 'bias': bias,
            'nonce': nonce, 'step': step, 'backward_ticks': backwards,
            'guard_on': [GUARD_NAMES[i] for i in range(12) if on(i)]}


def roster_dictionary(reference):
    """Distinct wire keys of the reference capture in logical order."""
    seen, out = set(), []
    for r in reference['records']:
        if r['wire_key'] not in seen:
            seen.add(r['wire_key'])
            out.append(r['wire_key'])
    return out


# ---------------------------------------------------------------- registries
def load_registry(data_dir):
    reg, div, lic = {}, {}, {}
    with open(os.path.join(data_dir, 'entrant-registry.csv')) as fh:
        for row in csv.DictReader(fh):
            reg[row['squad_key']] = {
                'entrant_id': row['entrant_id'],
                'division': row['division'],
                'licence_class': row['licence_class'],
                'is_ranked': row['is_ranked'].strip().lower() in ('yes', 'true', '1', 'y'),
            }
    with open(os.path.join(data_dir, 'division-topology.csv')) as fh:
        for row in csv.DictReader(fh):
            div[row['division']] = {'conference': row['conference'],
                                    'ladder_weight': int(row['ladder_weight'])}
    with open(os.path.join(data_dir, 'licence-classes.csv')) as fh:
        for row in csv.DictReader(fh):
            lic[row['licence_class']] = {'segment': row['segment'],
                                         'levy_rate': int(row['levy_rate'])}
    return reg, div, lic


class Book(object):
    """Ladder weights, levy rates, conferences and segments for any squad key."""

    def __init__(self, reg, div, lic):
        self.reg, self.div, self.lic = reg, div, lic
        self.conferences = sorted({d['conference'] for d in div.values()})
        self.segments = sorted({l['segment'] for l in lic.values()})

    def registered(self, key):
        return key in self.reg

    def weight(self, key):
        e = self.reg.get(key)
        return self.div[e['division']]['ladder_weight'] if e and e['division'] in self.div else 1

    def levy(self, key):
        e = self.reg.get(key)
        return self.lic[e['licence_class']]['levy_rate'] if e and e['licence_class'] in self.lic else 1

    def conference(self, key):
        e = self.reg.get(key)
        return self.div[e['division']]['conference'] if e and e['division'] in self.div else None

    def segment(self, key):
        e = self.reg.get(key)
        return self.lic[e['licence_class']]['segment'] if e and e['licence_class'] in self.lic else None

    def division(self, key):
        e = self.reg.get(key)
        return e['division'] if e else None

    def licence(self, key):
        e = self.reg.get(key)
        return e['licence_class'] if e else None

    def entrant_id(self, key):
        e = self.reg.get(key)
        return e['entrant_id'] if e else None

    def ranked(self, key):
        e = self.reg.get(key)
        return bool(e['is_ranked']) if e else False


# ---------------------------------------------------------------- settlement
def match_tables(decoded):
    """Per match: gross, voided, raw upkeep, participants, escapes, close tick."""
    by_match = {}
    for seg in decoded['segments']:
        by_match[seg['match_id']] = {'logical': seg['logical_position'], 'records': []}
    for r in decoded['records']:
        by_match[r['match_id']]['records'].append(r)

    matches = []
    for mid in sorted(by_match, key=lambda m: by_match[m]['logical']):
        recs = by_match[mid]['records']
        closes = [r['tick'] for r in recs if r['kind'] == 8]
        close_tick = max(closes) if closes else (max((r['tick'] for r in recs), default=0))
        elim, disband, concede, deploy = {}, {}, {}, {}
        participants = set()
        for r in recs:
            s, u = r['squad_key'], r['unit']
            participants.add(s)
            if r['kind'] == 1:
                if (s, u) not in deploy or r['tick'] < deploy[(s, u)][0]:
                    deploy[(s, u)] = (r['tick'], r['value'])
            elif r['kind'] == 3:
                elim[(s, u)] = min(elim.get((s, u), r['tick']), r['tick'])
            elif r['kind'] == 4:
                disband[(s, u)] = min(disband.get((s, u), r['tick']), r['tick'])
            elif r['kind'] == 5:
                concede[s] = min(concede.get(s, r['tick']), r['tick'])
        gross, voided, raw = {}, {}, {}
        for r in recs:
            if r['kind'] != 2:
                continue
            s, k = r['squad_key'], (r['squad_key'], r['unit'])
            withdrawn = ((k in elim and r['tick'] >= elim[k]) or
                         (s in concede and r['tick'] >= concede[s]))
            tgt = voided if withdrawn else gross
            tgt[s] = tgt.get(s, 0) + r['value']
        for (s, u), (dt, rate) in deploy.items():
            leave = disband.get((s, u), close_tick)
            raw[s] = raw.get(s, 0) + max(0, leave - dt) * rate
        matches.append({
            'match_id': mid,
            'logical': by_match[mid]['logical'],
            'close_tick': close_tick,
            'participants': participants,
            'conceded': set(concede),
            'gross': gross, 'voided': voided, 'raw': raw,
            'escape_records': sum(1 for r in recs if r['escapes']),
            'records': len(recs),
        })
    return matches


def settle_owners(records, roster_dict):
    """Effective owner of every squad, the sweep count and the hand overs.

    A closed set of hand overs settles on the member whose key sorts first;
    resolving those sets first is what lets the sweeps reach a fixpoint.
    """
    hands = {}
    for r in records:                       # last hand over in logical order stands
        if r['kind'] == 6 and roster_dict:
            hands[r['squad_key']] = roster_dict[r['value'] % len(roster_dict)]
    keys = sorted({r['squad_key'] for r in records} | set(hands) |
                  set(hands.values()))

    step = dict(hands)
    rings = []
    for k in keys:                          # every closed set of hand overs
        seen, cur = [], k
        while cur in step and cur not in seen:
            seen.append(cur)
            cur = step[cur]
        if cur in seen:
            ring = seen[seen.index(cur):]
            if sorted(ring) not in [sorted(r) for r in rings]:
                rings.append(ring)
    for ring in rings:
        first = min(ring)
        for member in ring:
            step[member] = first
        step.pop(first, None)

    owner = {k: k for k in keys}
    passes = 0
    while True:
        passes += 1
        nxt = {k: step.get(owner[k], owner[k]) for k in keys}
        if nxt == owner:
            break
        owner = nxt
        if passes > len(keys) + 4:          # cannot happen once rings are closed
            break
    return owner, passes, hands, rings


def score_season(matches, book, budget, threshold):
    """Per squad season figures, following the written policies in order."""
    keys = sorted({s for m in matches for s in m['participants']})
    stats = {k: {'squad_key': k, 'gross_output': 0, 'voided_output': 0,
                 'raw_upkeep': 0, 'charged_upkeep': 0, 'overflow_upkeep': 0,
                 'season_score': 0, 'matches': 0, 'match_awards': 0,
                 'streak_credit': 0, 'standing_points': 0} for k in keys}
    remaining = {k: budget for k in keys}
    per_match_score = {k: [] for k in keys}

    for m in matches:                                   # logical order
        charged = {}
        for s in sorted(m['participants']):
            raw = m['raw'].get(s, 0)
            take = min(remaining[s], raw)
            charged[s] = take
            remaining[s] -= take
            st = stats[s]
            st['raw_upkeep'] += raw
            st['charged_upkeep'] += take
            st['overflow_upkeep'] += raw - take
            st['gross_output'] += m['gross'].get(s, 0)
            st['voided_output'] += m['voided'].get(s, 0)
            st['matches'] += 1
        m['charged'] = charged
        m['score'] = {s: m['gross'].get(s, 0) * book.weight(s) - charged[s] * book.levy(s)
                      for s in m['participants']}
        for s in m['participants']:
            stats[s]['season_score'] += m['score'][s]

        eligible = [s for s in m['participants'] if s not in m['conceded']]
        if eligible:
            top = max(m['score'][s] for s in eligible)
            winners = [s for s in eligible if m['score'][s] == top]
            award = 3 if len(winners) == 1 else 1
            for s in winners:
                stats[s]['match_awards'] += award
            m['winners'] = sorted(winners)
            m['outright'] = len(winners) == 1
        else:
            m['winners'] = []
            m['outright'] = False
        for s in m['participants']:
            per_match_score[s].append(m['outright'] and s in m['winners'])

    for k in keys:
        run, credit = 0, 0
        for won in per_match_score[k]:
            if won:
                run += 1
                if run > 1:
                    credit += 2
            else:
                run = 0
        stats[k]['streak_credit'] = credit
        stats[k]['standing_points'] = stats[k]['match_awards'] + credit
        s = stats[k]['season_score']
        stats[k]['tier'] = ('champion' if s >= 3 * threshold else
                            'contender' if s >= 2 * threshold else
                            'qualifier' if s >= threshold else 'provisional')
    return stats


# ---------------------------------------------------------------- calibration
FIELD_BY_FIGURE = {
    'matches opened': 'matches_opened',
    'records recovered': 'records_recovered',
    'distinct squads': 'distinct_squads',
    'gross output': 'gross_output',
    'voided output': 'voided_output',
    'raw upkeep': 'raw_upkeep',
    'charged upkeep': 'charged_upkeep',
    'overflow upkeep': 'overflow_upkeep',
    'champion entrants': 'champion',
    'contender entrants': 'contender',
    'qualifier entrants': 'qualifier',
    'provisional entrants': 'provisional',
}
GUARD_FIELDS = ('matches_opened', 'records_recovered', 'distinct_squads',
                'gross_output', 'voided_output', 'raw_upkeep')
BUDGET_FIELDS = ('charged_upkeep', 'overflow_upkeep')
TIER_FIELDS = ('champion', 'contender', 'qualifier', 'provisional')


def parse_controls(docs):
    """`- C4 gross output: 6228` -> [('C4', 'gross output', 'gross_output', 6228)].

    The figures are keyed by what they name, not by the order they were listed.
    """
    out = []
    for line in docs.splitlines():
        m = re.match(r'[-*]?\s*(C\d+)\s+([^:]+):\s*(-?\d+)\s*$', line.strip())
        if m:
            figure = m.group(2).strip()
            out.append((m.group(1), figure, FIELD_BY_FIGURE.get(figure.lower()),
                        int(m.group(3))))
    out.sort(key=lambda r: int(r[0][1:]))
    return out


def control_reading(decoded, book, budget=None, threshold=None):
    """Reproduce the control figures from one decoding of the control slice."""
    matches = match_tables(decoded)
    got = {
        'matches_opened': len(matches),
        'records_recovered': len(decoded['records']),
        'distinct_squads': len({r['squad_key'] for r in decoded['records']}),
        'gross_output': sum(sum(m['gross'].values()) for m in matches),
        'voided_output': sum(sum(m['voided'].values()) for m in matches),
        'raw_upkeep': sum(sum(m['raw'].values()) for m in matches),
    }
    if budget is not None:
        stats = score_season(matches, book, budget,
                             threshold if threshold is not None else 1)
        got['charged_upkeep'] = sum(s['charged_upkeep'] for s in stats.values())
        got['overflow_upkeep'] = sum(s['overflow_upkeep'] for s in stats.values())
        if threshold is not None:
            for tier in ('champion', 'contender', 'qualifier', 'provisional'):
                got[tier] = sum(1 for s in stats.values() if s['tier'] == tier)
    return got


def control_check(published, got, fields):
    """First published figure drawn from `fields` that the reading fails.

    Returns (rank, text) so that competing falsifiers can be ordered by how far
    into the published list a reading survived, or None when nothing fails.
    """
    for rank, (cid, figure, field, value) in enumerate(published):
        if field not in fields:
            continue
        if field not in got or got[field] != value:
            return (rank, '%s %s (published %s, reproduced %s)'
                    % (cid, figure, value, got.get(field, 'nothing')))
    return None


# ---------------------------------------------------------------- bracket
def bracket_constraints(members, stats, book, hands, cfg):
    w = [book.weight(k) for k in members]
    confs = [book.conference(k) for k in members if book.conference(k)]
    segs = {book.segment(k) for k in members if book.segment(k)}
    return [
        ('member in a prominent division', any(x >= cfg['prominence'] for x in w)),
        ('both entrant segments present', set(book.segments) <= segs),
        ('every member meets the standing points floor',
         all(stats[k]['standing_points'] >= cfg['standing_floor'] for k in members)),
        ('no two members share a conference', len(confs) == len(set(confs))),
        ('a ranked entrant is present', any(book.ranked(k) for k in members)),
        ('combined ladder weight floor', sum(w) >= cfg['weight_floor']),
        ('no member carries overflow upkeep',
         all(stats[k]['overflow_upkeep'] == 0 for k in members)),
        ('a champion tier member is present',
         any(stats[k]['tier'] == 'champion' for k in members)),
        ('combined gross output floor',
         sum(stats[k]['gross_output'] for k in members) >= cfg['gross_floor']),
        ('no member has handed its entry away',
         all(k not in hands for k in members)),
    ]


def elect_bracket(stats, book, owners, hands, cfg):
    pool = sorted(k for k in stats
                  if owners.get(k, k) == k and stats[k]['matches'] >= cfg['match_floor'])
    best = None
    space = 0
    best_count = 0
    for members in itertools.combinations(pool, 3):
        space += 1
        res = bracket_constraints(members, stats, book, hands, cfg)
        met = sum(1 for _, ok in res if ok)
        upkeep = sum(stats[k]['charged_upkeep'] for k in members)
        key = (-met, upkeep, list(members))
        best_count = max(best_count, met)
        if best is None or key < best[0]:
            best = (key, members, res, met, upkeep)
    return pool, space, best_count, best


# ---------------------------------------------------------------- docket
def match_conference(m, book):
    best = None
    for s, g in sorted(m['gross'].items()):
        if not book.registered(s):
            continue
        if best is None or g > best[1]:
            best = (s, g)
    return (book.conference(best[0]), best[0]) if best else (None, None)


def elect_docket(matches, book, cfg):
    rows = []
    for m in matches:
        conf, holder = match_conference(m, book)
        rows.append({'match_id': m['match_id'], 'logical': m['logical'],
                     'conference': conf, 'conference_holder': holder,
                     'voided_output': sum(m['voided'].values()),
                     'escape_records': m['escape_records'],
                     'review_index': sum(m['voided'].values())
                                     + cfg['review_weight'] * m['escape_records']})
    size, cap = cfg['docket_size'], cfg['docket_cap']
    if size > len(rows):
        size = len(rows)
    # keeping only the top `cap` of each conference cannot lose an optimum
    pool, buckets = [], {}
    for r in sorted(rows, key=lambda r: (-r['review_index'], r['match_id'])):
        if r['conference'] is None:
            pool.append(r)
            continue
        buckets.setdefault(r['conference'], [])
        if len(buckets[r['conference']]) < cap:
            buckets[r['conference']].append(r)
            pool.append(r)
    pool.sort(key=lambda r: (-r['review_index'], r['match_id']))
    best = None
    for combo in itertools.combinations(pool, size):
        counts = {}
        ok = True
        for r in combo:
            if r['conference'] is None:
                continue
            counts[r['conference']] = counts.get(r['conference'], 0) + 1
            if counts[r['conference']] > cap:
                ok = False
                break
        if not ok:
            continue
        total = sum(r['review_index'] for r in combo)
        ids = sorted(r['match_id'] for r in combo)
        key = (-total, ids)
        if best is None or key < best[0]:
            best = (key, combo, total)
    return rows, best


# ---------------------------------------------------------------- reports
def group_report(kind, name, keys, stats, book):
    entries = []
    for k in sorted(keys):
        s = stats[k]
        entries.append({
            'charged_upkeep': s['charged_upkeep'],
            'conference': book.conference(k),
            'division': book.division(k),
            'entrant_id': book.entrant_id(k),
            'gross_output': s['gross_output'],
            'licence_class': book.licence(k),
            'matches': s['matches'],
            'overflow_upkeep': s['overflow_upkeep'],
            'season_score': s['season_score'],
            'segment': book.segment(k),
            'squad_key': k,
            'standing_points': s['standing_points'],
            'streak_credit': s['streak_credit'],
            'tier': s['tier'],
            'voided_output': s['voided_output'],
        })
    counts = {t: 0 for t in ('champion', 'contender', 'provisional', 'qualifier')}
    for e in entries:
        counts[e['tier']] += 1
    total = lambda f: sum(e[f] for e in entries)
    return {
        'charged_upkeep': total('charged_upkeep'),
        'divisions': sorted({e['division'] for e in entries if e['division']}),
        'entrants': len(entries),
        'entries': entries,
        'gross_output': total('gross_output'),
        'group_kind': kind,
        'group_name': name,
        'overflow_upkeep': total('overflow_upkeep'),
        'season_score': total('season_score'),
        'standing_points': total('standing_points'),
        'tier_counts': counts,
        'voided_output': total('voided_output'),
    }


def write_json(path, obj):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write('\n')


# ---------------------------------------------------------------- main
def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: %s DATA_DIR OUT_DIR\n' % os.path.basename(argv[0]))
        return 2
    data_dir, out_dir = os.path.abspath(argv[1]), os.path.abspath(argv[2])
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    docs = scan_docs(data_dir)
    published = parse_controls(docs)
    reg, div, lic = load_registry(data_dir)
    book = Book(reg, div, lic)

    cfg = {
        'match_floor': find_scalar(docs, 'Eligibility match floor', 2),
        'prominence': find_scalar(docs, 'Ladder weight prominence', 4),
        'standing_floor': find_scalar(docs, 'Standing points floor', 3),
        'weight_floor': find_scalar(docs, 'Combined ladder weight floor', 7),
        'gross_floor': find_scalar(docs, 'Combined gross output floor', 900),
        'review_weight': find_scalar(docs, 'Review weight', 25),
        'docket_size': find_scalar(docs, 'Docket size', 5),
        'docket_cap': find_scalar(docs, 'Docket conference cap', 2),
    }
    tag_roster = find_roster(docs, 'Engine tag roster')
    depth_roster = [as_int(x) for x in find_roster(docs, 'Window depth roster')]
    sent_roster = [as_int(x) for x in find_roster(docs, 'Sentinel roster')]
    budget_roster = [as_int(x) for x in find_roster(docs, 'Budget cap roster')]
    thresh_roster = [as_int(x) for x in find_roster(docs, 'Promotion threshold roster')]

    # ---- locate the three captures by their capture identifier
    captures = {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, 'rb') as fh:
            head = fh.read(4)
        if head != b'RJNL':
            continue
        with open(path, 'rb') as fh:
            raw = fh.read()
        env = read_envelope(raw)
        captures.setdefault(env['capture_id'] % 10, []).append((name, raw, env))
    for role in (1, 2, 3):
        if role not in captures:
            raise Bad('no capture with identifier ending in %d' % role)
    ref_name, ref_raw, _ = captures[1][0]
    ctl_name, ctl_raw, _ = captures[2][0]
    sea_name, sea_raw, _ = captures[3][0]

    # ---- pin the guard quantities against the control figures
    best_fail = {q: {} for q in ('engine_tag', 'window_depth', 'sentinel')}
    survivors = []
    combos = 0
    for tag in tag_roster:
        for depth in depth_roster:
            for sent in sent_roster:
                combos += 1
                try:
                    ref = decode_capture(ref_raw, tag, depth, sent, None)
                    rd = roster_dictionary(ref)
                    ctl = decode_capture(ctl_raw, tag, depth, sent, rd)
                    fail = control_check(published, control_reading(ctl, book), GUARD_FIELDS)
                except Bad as exc:
                    ref = rd = ctl = None
                    fail = (0, exc.progress,
                            'no record stream recovers under this reading '
                            '(%s after %d records), so none of %s is reproduced'
                            % (exc, exc.progress,
                               ', '.join(c for c, _f, fl, _v in published
                                         if fl in GUARD_FIELDS)))
                else:
                    if fail is not None:
                        fail = (fail[0], 1 << 30, fail[1])
                if fail is None:
                    survivors.append((tag, depth, sent, ref, rd, ctl))
                for q, val in (('engine_tag', tag), ('window_depth', depth),
                               ('sentinel', sent)):
                    # a value is accepted once any reading carrying it survives;
                    # otherwise keep the falsifier of its furthest-reaching reading
                    cur = best_fail[q].get(val, 'unset')
                    if cur == 'unset' or (cur is not None and
                                          (fail is None or fail[:2] > cur[:2])):
                        best_fail[q][val] = fail
    if not survivors:
        raise Bad('no reading of the guard rosters reproduces the control figures')
    audit_rows = {q: {v: (f[2] if f else None) for v, f in best_fail[q].items()}
                  for q in best_fail}
    tag, depth, sentinel, reference, roster_dict, control = survivors[0]

    # ---- pin the season budget, then the promotion threshold
    budget_audit, budget = {}, None
    for cand in budget_roster:
        fail = control_check(published, control_reading(control, book, cand),
                             BUDGET_FIELDS)
        budget_audit[cand] = fail[1] if fail else None
        if fail is None and budget is None:
            budget = cand
    if budget is None:
        raise Bad('no budget cap reproduces the control figures')

    thresh_audit, threshold = {}, None
    for cand in thresh_roster:
        fail = control_check(published, control_reading(control, book, budget, cand),
                             TIER_FIELDS)
        thresh_audit[cand] = fail[1] if fail else None
        if fail is None and threshold is None:
            threshold = cand
    if threshold is None:
        raise Bad('no promotion threshold reproduces the control figures')

    # ---- reference transcript check
    transcript_check = {'transcript': None, 'checked': False}
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith('.jsonl'):
            continue
        rows = []
        with open(os.path.join(data_dir, name)) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if not rows or 'squad_key' not in rows[0]:
            continue
        mine = [{'kind': r['kind'], 'logical_segment': r['logical_segment'],
                 'match_id': r['match_id'], 'record_ordinal': r['record_ordinal'],
                 'squad_key': r['squad_key'], 'tick': r['tick'], 'unit': r['unit'],
                 'value': r['value']} for r in reference['records']]
        theirs = [{k: r[k] for k in mine[0]} for r in rows]
        transcript_check = {'transcript': name, 'checked': True,
                            'labelled_records': len(theirs),
                            'recovered_records': len(mine),
                            'byte_exact_agreement': mine == theirs,
                            'first_disagreement': next(
                                (i for i, (a, b) in enumerate(zip(mine, theirs)) if a != b),
                                None)}
        break

    # ---- settle the season
    season = decode_capture(sea_raw, tag, depth, sentinel, roster_dict)
    matches = match_tables(season)
    owners, passes, hands, rings = settle_owners(season['records'], roster_dict)
    stats = score_season(matches, book, budget, threshold)
    for k in stats:
        stats[k]['effective_owner'] = owners.get(k, k)
        stats[k]['handed_entry_away'] = owners.get(k, k) != k

    # ---- bracket and docket
    pool, space, best_count, best = elect_bracket(stats, book, owners, hands, cfg)
    rows, docket_best = elect_docket(matches, book, cfg)

    # ---- group reports
    recovered = sorted(stats)
    registered = [k for k in recovered if book.registered(k)]
    unregistered = [k for k in recovered if not book.registered(k)]
    absent = sorted(k for k in reg if k not in stats)

    for conf in book.conferences:
        keys = [k for k in registered if book.conference(k) == conf]
        write_json(os.path.join(out_dir, 'region_%s.json' % conf),
                   group_report('conference', conf, keys, stats, book))
    for seg in book.segments:
        keys = [k for k in registered if book.segment(k) == seg]
        write_json(os.path.join(out_dir, 'segment_%s.json' % seg),
                   group_report('segment', seg, keys, stats, book))

    # ---- summary
    total = lambda f: sum(stats[k][f] for k in stats)
    ranking = sorted(stats.values(),
                     key=lambda s: (-s['season_score'], -s['standing_points'],
                                    s['squad_key']))
    standings = []
    for i, s in enumerate(ranking, 1):
        row = dict(s)
        row['rank'] = i
        row['entrant_id'] = book.entrant_id(s['squad_key'])
        row['division'] = book.division(s['squad_key'])
        row['conference'] = book.conference(s['squad_key'])
        row['licence_class'] = book.licence(s['squad_key'])
        row['segment'] = book.segment(s['squad_key'])
        row['ladder_weight'] = book.weight(s['squad_key'])
        row['levy_rate'] = book.levy(s['squad_key'])
        row['is_ranked'] = book.ranked(s['squad_key'])
        row['registered'] = book.registered(s['squad_key'])
        standings.append(row)

    docket_rows = sorted(docket_best[1], key=lambda r: r['match_id'])
    docket_spread = {}
    for r in docket_rows:
        key = r['conference'] or 'unattributed'
        docket_spread[key] = docket_spread.get(key, 0) + 1

    tier_counts = {t: sum(1 for s in stats.values() if s['tier'] == t)
                   for t in ('champion', 'contender', 'qualifier', 'provisional')}

    summary = {
        'capture': {
            'file': sea_name,
            'capture_id': season['envelope']['capture_id'],
            'guard_register': season['envelope']['guard_register'],
            'guards_active': season['guard_on'],
            'capture_nonce': season['nonce'],
            'base_tick': season['envelope']['base_tick'],
            'segment_step': season['step'],
            'count_bias': season['bias'],
        },
        'resolved_policy': {
            'engine_tag': tag,
            'window_depth': depth,
            'sentinel': '0x%02X' % sentinel,
            'season_budget': budget,
            'promotion_threshold': threshold,
            'roster_dictionary': roster_dict,
        },
        'totals': {
            'matches': len(matches),
            'records': len(season['records']),
            'distinct_squads': len(recovered),
            'registered_squads': len(registered),
            'unregistered_squads': len(unregistered),
            'absent_entrants': len(absent),
            'gross_output': total('gross_output'),
            'voided_output': total('voided_output'),
            'raw_upkeep': total('raw_upkeep'),
            'charged_upkeep': total('charged_upkeep'),
            'overflow_upkeep': total('overflow_upkeep'),
            'season_score': total('season_score'),
            'standing_points': total('standing_points'),
            'streak_credit': total('streak_credit'),
        },
        'tier_counts': tier_counts,
        'attribution_settlement_passes': passes,
        'hand_over_closed_sets': [sorted(r) for r in rings],
        'hand_overs': [{'from': k, 'to': v, 'effective_owner': owners.get(k, k)}
                       for k, v in sorted(hands.items())],
        'standings': standings,
        'conference_totals': {
            c: {f: sum(stats[k][f] for k in registered if book.conference(k) == c)
                for f in ('gross_output', 'voided_output', 'charged_upkeep',
                          'overflow_upkeep', 'season_score', 'standing_points')}
            for c in book.conferences},
        'segment_totals': {
            g: {f: sum(stats[k][f] for k in registered if book.segment(k) == g)
                for f in ('gross_output', 'voided_output', 'charged_upkeep',
                          'overflow_upkeep', 'season_score', 'standing_points')}
            for g in book.segments},
        'finals_bracket': {
            'members': sorted(best[1]),
            'satisfied_constraints': best[3],
            'total_constraints': len(best[2]),
            'constraints': [{'constraint': n, 'met': ok} for n, ok in best[2]],
            'combined_charged_upkeep': best[4],
            'combined_ladder_weight': sum(book.weight(k) for k in best[1]),
            'combined_gross_output': sum(stats[k]['gross_output'] for k in best[1]),
            'member_detail': [{'squad_key': k, 'conference': book.conference(k),
                               'segment': book.segment(k), 'division': book.division(k),
                               'ladder_weight': book.weight(k),
                               'tier': stats[k]['tier'],
                               'standing_points': stats[k]['standing_points'],
                               'season_score': stats[k]['season_score'],
                               'is_ranked': book.ranked(k)} for k in sorted(best[1])],
        },
        'review_docket': {
            'matches': [{'match_id': r['match_id'], 'conference': r['conference'],
                         'conference_holder': r['conference_holder'],
                         'voided_output': r['voided_output'],
                         'escape_records': r['escape_records'],
                         'review_index': r['review_index']} for r in docket_rows],
            'combined_review_index': docket_best[2],
            'conference_spread': docket_spread,
            'docket_size': cfg['docket_size'],
            'conference_cap': cfg['docket_cap'],
        },
        'match_ledger': [{'match_id': r['match_id'], 'logical_position': r['logical'],
                          'conference': r['conference'],
                          'review_index': r['review_index'],
                          'voided_output': r['voided_output'],
                          'escape_records': r['escape_records']} for r in rows],
    }
    write_json(os.path.join(out_dir, 'summary.json'), summary)

    # ---- data quality
    env = season['envelope']
    bias = season['bias']
    kinds = {n: 0 for n in KIND_NAMES.values()}
    for r in season['records']:
        kinds[KIND_NAMES[r['kind']]] += 1
    all_output = sum(r['value'] for r in season['records'] if r['kind'] == 2)
    conf_gross = sum(stats[k]['gross_output'] for k in registered)
    seg_gross = sum(stats[k]['gross_output'] for k in registered if book.segment(k))
    findings = []

    seg_resid = len(season['segments']) - (env['declared_segments'] - bias)
    rec_resid = len(season['records']) - (env['declared_records'] - bias)
    roster_resid = len(registered) - (env['declared_roster'] - bias)
    attr_resid = total('gross_output') - conf_gross
    out_resid = (total('gross_output') + total('voided_output')) - all_output
    up_resid = (total('charged_upkeep') + total('overflow_upkeep')) - total('raw_upkeep')

    if seg_resid:
        findings.append('declared segment count and recovered segments differ by %d' % seg_resid)
    if rec_resid:
        findings.append('declared record count and recovered records differ by %d' % rec_resid)
    if roster_resid:
        findings.append(
            'declared roster size and recovered registered squads differ by %d '
            '(declared %d, recovered %d)'
            % (roster_resid, env['declared_roster'] - bias, len(registered)))
    if unregistered:
        findings.append('squads seen on the wire that the entrant registry never lists: %s'
                        % ', '.join(unregistered))
    if attr_resid:
        findings.append('attribution residual of %d gross output belongs to no conference'
                        % attr_resid)
    if out_resid:
        findings.append('output ledger residual of %d' % out_resid)
    if up_resid:
        findings.append('upkeep ledger residual of %d' % up_resid)
    if season['backward_ticks']:
        findings.append('%d records move the tick backwards inside their segment'
                        % season['backward_ticks'])
    if absent:
        findings.append('absent entrants listed in the registry but seen in no capture: %s'
                        % ', '.join(absent))
    bad_ck = [s['match_id'] for s in season['segments'] if not s['checksum_ok']]
    if bad_ck:
        findings.append('payload checksum disagrees in segments %s' % bad_ck)
    if not env['checksum_ok']:
        findings.append('envelope checksum disagrees')
    if not findings:
        findings.append('every residual is zero')

    dq = {
        'capture': {'file': sea_name, 'capture_id': env['capture_id'],
                    'guard_register': env['guard_register'],
                    'guards_active': season['guard_on'],
                    'count_bias': bias,
                    'envelope_checksum_ok': env['checksum_ok']},
        'declared_versus_recovered': {
            'segments': {'declared_raw': env['declared_segments'],
                         'declared_unbiased': env['declared_segments'] - bias,
                         'recovered': len(season['segments']),
                         'residual': seg_resid},
            'records': {'declared_raw': env['declared_records'],
                        'declared_unbiased': env['declared_records'] - bias,
                        'recovered': len(season['records']),
                        'residual': rec_resid},
            'roster': {'declared_raw': env['declared_roster'],
                       'declared_unbiased': env['declared_roster'] - bias,
                       'registered_squads_recovered': len(registered),
                       'distinct_squads_recovered': len(recovered),
                       'squads_without_an_entry_exposed':
                           len(recovered) - (env['declared_roster'] - bias),
                       'residual': roster_resid},
        },
        'segment_ledger': [
            {'logical_position': s['logical_position'], 'match_id': s['match_id'],
             'storage_slot': s['slot'],
             'declared_raw': s['declared_records'],
             'declared_unbiased': s['declared_records'] - bias,
             'recovered': s['recovered_records'],
             'residual': 0,
             'payload_checksum_ok': s['checksum_ok']}
            for s in season['segments']],
        'kind_ledger': kinds,
        'ledger_residuals': {
            'output': {'gross_output': total('gross_output'),
                       'voided_output': total('voided_output'),
                       'all_output_reported': all_output,
                       'residual': out_resid},
            'upkeep': {'charged_upkeep': total('charged_upkeep'),
                       'overflow_upkeep': total('overflow_upkeep'),
                       'raw_upkeep': total('raw_upkeep'),
                       'residual': up_resid},
        },
        'attribution_residual': {
            'season_gross_output': total('gross_output'),
            'conference_gross_output': conf_gross,
            'segment_gross_output': seg_gross,
            'residual': attr_resid,
            'unregistered_gross_output': sum(stats[k]['gross_output'] for k in unregistered),
        },
        'unregistered_squads': [
            {'squad_key': k, 'gross_output': stats[k]['gross_output'],
             'voided_output': stats[k]['voided_output'],
             'raw_upkeep': stats[k]['raw_upkeep'],
             'charged_upkeep': stats[k]['charged_upkeep'],
             'overflow_upkeep': stats[k]['overflow_upkeep'],
             'season_score': stats[k]['season_score'],
             'standing_points': stats[k]['standing_points'],
             'matches': stats[k]['matches']} for k in unregistered],
        'absent_entrants': [{'squad_key': k, 'entrant_id': reg[k]['entrant_id'],
                             'division': reg[k]['division'],
                             'conference': book.conference(k),
                             'licence_class': reg[k]['licence_class'],
                             'segment': book.segment(k)} for k in absent],
        'backward_tick_events': season['backward_ticks'],
        'reference_transcript_check': transcript_check,
        'findings': findings,
    }
    write_json(os.path.join(out_dir, 'data_quality.json'), dq)

    # ---- policy audit
    def rows_for(name, roster, chosen, verdicts, fmt=str):
        return {
            'roster': [fmt(v) for v in roster],
            'candidate_space': len(roster),
            'resolved': fmt(chosen),
            'candidates': [
                {'value': fmt(v),
                 'verdict': 'accepted' if verdicts.get(v) is None else 'rejected',
                 'falsifier': verdicts.get(v)}
                for v in roster],
        }

    cal = []
    final_got = control_reading(control, book, budget, threshold)
    for cid, figure, field, value in published:
        rep = final_got.get(field)
        cal.append({'control': cid, 'figure': figure,
                    'published_value': value, 'reproduced_value': rep,
                    'agrees': rep == value})

    audit = {
        'captures': {'reference': ref_name, 'control': ctl_name, 'season': sea_name},
        'reference_transcript_check': transcript_check,
        'roster_dictionary': roster_dict,
        'unstated_quantities': {
            'engine_tag': rows_for('engine_tag', tag_roster, tag, audit_rows['engine_tag']),
            'window_depth': rows_for('window_depth', depth_roster, depth,
                                     audit_rows['window_depth'], int),
            'sentinel': rows_for('sentinel', sent_roster, sentinel,
                                 audit_rows['sentinel'], lambda v: '0x%02X' % v),
            'season_budget': rows_for('season_budget', budget_roster, budget,
                                      budget_audit, int),
            'promotion_threshold': rows_for('promotion_threshold', thresh_roster,
                                            threshold, thresh_audit, int),
        },
        'guard_search': {
            'combinations_examined': combos,
            'combinations_surviving': len(survivors),
            'derived_count_bias': control['bias'],
        },
        'attribution_settlement': {
            'passes': passes,
            'closed_sets': [sorted(r) for r in rings],
            'hand_overs': [{'from': k, 'to': v} for k, v in sorted(hands.items())],
            'effective_owners': {k: owners.get(k, k) for k in sorted(stats)
                                 if owners.get(k, k) != k},
        },
        'bracket_search': {
            'eligibility_match_floor': cfg['match_floor'],
            'eligible_pool': pool,
            'candidate_space': space,
            'best_satisfied_constraints': best_count,
            'total_constraints': len(best[2]),
            'elected': sorted(best[1]),
            'elected_satisfied_constraints': best[3],
            'elected_combined_charged_upkeep': best[4],
            'constraints': [{'constraint': n, 'met': ok} for n, ok in best[2]],
        },
        'docket_search': {
            'docket_size': cfg['docket_size'],
            'conference_cap': cfg['docket_cap'],
            'review_weight': cfg['review_weight'],
            'candidate_matches': len(rows),
            'elected': [r['match_id'] for r in docket_rows],
            'combined_review_index': docket_best[2],
        },
        'calibration_comparison': cal,
        'calibration_agrees': all(r['agrees'] for r in cal),
        'policy_constants': cfg,
    }
    write_json(os.path.join(out_dir, 'policy_audit.json'), audit)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
