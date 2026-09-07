# Roster continuity

The roster dictionary is the list of distinct attribution keys as written on the wire in
the reference capture, kept in the order in which each key is first met when that capture
is read in logical order. It is a property of the corpus rather than of any single capture,
and the sealed season and the control slice both resolve their dictionary positions against
it.

An entry hand over is recorded as an event of kind 6. Its value field holds the position of
the receiving squad in the roster dictionary, reduced modulo the size of the dictionary.
When a squad records more than one hand over, the one that is read last in logical order is
the one that stands.

Following the hand overs to their end gives the effective owner of every squad. A squad
that never hands its entry away owns itself. When a set of hand overs closes on itself so
that following them returns to where it began, every squad on that closed set settles on
the squad of that set whose key sorts first.

The settlement is reached by repeated sweeps. A sweep replaces the current owner of every
squad by the squad that the current owner hands to, and the settlement is reached on the
first sweep that changes nothing. The number of sweeps taken, counting the sweep that
changed nothing, is a reported figure.

A squad that has handed its entry away is no longer its own effective owner and stands
outside the pool from which the finals bracket is elected.
