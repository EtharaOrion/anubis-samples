# A tiny worked case

This walks one invented object through the early stages so the arithmetic is concrete.
The numbers here are illustrative and are not from the shipped snapshot.

Take an object declared in centimetres as width forty, height sixty, depth thirty, with
rotation allowed and tipping not allowed, made of a material whose handling class has a
clearance width margin of eight millimetres, a clearance depth margin of eight
millimetres and a clearance height margin of twelve millimetres.

Stage one converts the dimensions to micrometres by the centimetre factor of ten
thousand, giving width four hundred thousand, height six hundred thousand and depth three
hundred thousand.

Stage two adds the margins, converted from millimetres by the factor of one thousand. The
clearance width is four hundred thousand plus two times eight thousand, which is four
hundred sixteen thousand. The clearance depth is three hundred thousand plus two times
eight thousand, which is three hundred sixteen thousand. The clearance height is six
hundred thousand plus twelve thousand, which is six hundred twelve thousand. The aspect
ratio is height over width, six hundred thousand over four hundred thousand, which reduces
to three over two.

Stage three keeps the base orientation and the rotation about the vertical, because
rotation is allowed, but not the tipped orientations, because tipping is not.

Stage four tests a bay whose interior is two thousand by two thousand six hundred by one
thousand five hundred millimetres, that is two million by two million six hundred thousand
by one million five hundred thousand micrometres. In the base orientation the footprint
width four hundred sixteen thousand is less than or equal to the interior width, the
footprint depth three hundred sixteen thousand is strictly less than the interior depth,
and the vertical extent six hundred twelve thousand is less than or equal to the interior
height, so the object fits.

If a second object had a clearance depth exactly equal to the interior depth it would not
fit that bay, because the depth axis is exclusive, even though a clearance width exactly
equal to the interior width would still fit, because the width axis is inclusive.

For the rounding rule, suppose a setup base of fifty five and a mass of seventy grams with
a quantum of seven hundred. Fifty five times seventy is three thousand eight hundred fifty,
and three thousand eight hundred fifty over seven hundred is five and one half exactly, so
the round half up setup is six, not five.
