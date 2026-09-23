# Scale factors, quanta and folding constants

These fixed constants back the arithmetic in `rules.md`.

## Length to micrometres

A declared length is multiplied by the factor for its unit code to reach micrometres.

- MM one thousand
- CM ten thousand
- IN twenty five thousand four hundred

Bay interior dimensions, crate interior dimensions and handling class margins are all
declared in millimetres and so use the millimetre factor of one thousand before any
comparison against a micrometre quantity.

## Mass to grams

A declared mass is multiplied by the factor for its mass unit code to reach grams.

- G one
- KG one thousand

## Money to cents

Declared value is multiplied by one hundred to reach cents. Insurance in cents is the
round half up value of value in cents times the lender rate numerator over the lender
rate denominator.

## Setup and days

Setup seconds are the round half up value of the handling class setup base times the
object mass in grams over the setup quantum of seven hundred. The install window offers
three thousand six hundred seconds of setup capacity per available day, and the number of
days an object requires is its setup seconds divided by that per day capacity, rounded up.

## Rounding

Every rational quantity is reduced to lowest terms, and every quantity that a stage
rounds is rounded half up: the quotient is taken, and when twice the remainder is greater
than or equal to the divisor the quotient is increased by one. A denominator of zero in a
ratio yields the sentinel numerator zero over denominator one.

## Chain fold

The final state chain starts from a seed string of sixty four zero characters. For each
manifest entry in sorted name order the chain becomes the sha256 hex digest of the
current chain string concatenated with that entry's digest string. The hashed manifest
set is every jsonl artifact together with the penalty summary, the objective tuple and the
program file; the integrity manifest and the final state are not themselves hashed into
the manifest.
