# Worked micro example

This is a tiny illustration to fix the conventions. It is not the shipped snapshot
and its numbers do not appear in any answer. It shows two sources feeding one
phase.

Suppose a source has raw amount 41 in the `seg` unit, and the `seg` rational is
1025 over 2. The canonical token count is round half to even of 41 times 1025 over
2, which is round half to even of 42025 over 2. The quotient is 21012 and the
remainder is 1, and twice the remainder equals the denominator, so this is an exact
half and rounds to the even quotient 21012. A conforming run records a rounding tie
for this source. A run that rounds a half upward would write 21013 and diverge
here and in every later stage that consumes this count.

Suppose after scoring two sources in one phase have demand scores 3 and 1 and the
phase budget is 5. The demand sum is 4. The proportional target of the first is
round half to even of 3 times 5 over 4, which is round half to even of 15 over 4,
whose quotient is 3 and remainder 3, and twice 3 exceeds 4 so it rounds up to 4.
The second is round half to even of 1 times 5 over 4, which is round half to even
of 5 over 4, quotient 1 remainder 1, and twice 1 is below 4 so it stays 1. The
targets are 4 and 1. A run that truncates instead of rounding half to even would
write 3 and 1 and diverge.

Suppose a source is critical with a stratum floor of 900 but its proportional
target was 300. Its floored allocation becomes 900. If the phase effective capacity
cannot hold every floored allocation, displacement first reduces auxiliary sources
above their floor, then standard, then critical, each only down to its own floor,
and only if that is still not enough are the floors themselves cut in the same
order. The final matrix, the summaries, the objective tuple and the folded chain
then follow from the rules in the other documents.
