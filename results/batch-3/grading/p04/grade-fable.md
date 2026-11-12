# Referee report, Problem 4 (J-holomorphic local equations for a symplectic snc divisor)

Reference point: the author's solution answers YES. Its content is that the local
normal-bundle model (regularization of Tehrani–McLean–Zinger) only gives the
required coordinates after the normal line bundles of all strata have been given
*compatible pseudo-holomorphic structures*; since generic almost complex manifolds
carry no local holomorphic functions, such structures cannot be chosen locally and
patched, and the author builds them by adding auxiliary Donaldson–Auroux divisors
and inducting over strata. Any correct YES-proof must confront this global
patching problem; any NO-answer must survive the integrable case (a Kähler
manifold with an snc divisor and the integrable J satisfies the requirement
trivially).

## Solution X

**Review.** X claims YES and that J can even be chosen compatible. Section 2 quotes
the regularization theorem of Farajzadeh Tehrani–McLean–Zinger (Thm. 2.13,
Def. 2.12) correctly, and Lemma 3.1 (polar decomposition of ω against a metric)
and Lemma 4.1 (block form of the model symplectic form on ⊕L_k) are correct.
The proof fails at Lemma 4.2(3): X asserts that in a local unitary trivialization
the fiber coordinate w_k has differential vanishing on the horizontal distribution
H of the unitary connection. This is false unless the connection is flat on the
chart: the horizontal lift of v at the point (x,w) is (v, −iβ_k(v)w_k) for the
local connection form β_k, so dw_k(ṽ) = −iβ_k(v)w_k ≠ 0 off the zero section. Hence
w_k is not J_E-holomorphic (for Y horizontal, dw_k(J_E Y) is purely imaginary times
w_k while i·dw_k(Y) is real times w_k), and the almost complex structure produced
by polar decomposition from (ω_E, g_E) does not have the claimed property. Lemma
4.2(3) is the only step that produces J-holomorphic defining functions, so Step 3
of the main proof is unsupported. The deeper gap is Step 4, "the above construction
yields an ω'-compatible almost complex structure in a neighborhood of D": the
construction of Step 3 is performed in a local trivialization over a small W ⊂ D_I,
and X never checks that the structures obtained from different trivializations, or
from the charts of different strata D_I ⊂ D_{I'}, agree on overlaps. They do not in
general: a fiber coordinate is J-holomorphic for a given J only if transition
functions between the trivializations used are J|_{D_I}-holomorphic, which is the
pseudo-holomorphic-line-bundle condition the author's solution spends its Sections
1, 2 and 4 arranging. X's text contains no trace of this difficulty. What is
established: the (correct) reduction to a regularized ω' and the linear-algebra
lemmas. What is claimed but not established: the existence of J, which is the
whole problem. The extension argument of Step 4 (gluing metrics, then polar
decomposition) would be fine if the local structure existed. Minor: the main
theorem refers to "Definition thm:main" (label error).

Correctness: -1

Exposition: 1

Attribution: 1

## Solution Y

**Review.** Y claims NO, with the "counterexample" of a line L in CP^2, via a
claimed obstruction: for an adapted J the normal bundle of every compact
component is flat, so D_α·D_α = 0. The claimed theorem is false, and Y's own
example refutes it: (CP^2, ω_FS) with the standard integrable J and a projective
line admits, in affine coordinates, exactly the required local coordinates with
r = 1 (Y even stresses in Theorem 1.1 and in Section 7(1) that tameness is not
used, so the claim would apply to every complex manifold with a smooth divisor of
nonzero self-intersection). The error is located in Step 2 of the proof of
Theorem 1.1 (Section 6): the transition functions h_ij = z_1^{(j)}/z_1^{(i)} are
J_α-holomorphic on the *open* sets D_α ∩ W_i ∩ W_j, and Y invokes Lemma 5.1 (the
maximum principle) to conclude they are constant. Lemma 5.1 requires either a
compact connected domain or an interior local maximum of |h|; neither holds on an
open overlap. For the line in CP^2 the transition functions of the conormal bundle
are the nonconstant holomorphic functions z_j/z_i. Everything downstream (flat
connection, vanishing Euler class, Corollary 1.2, the deformation-invariance
discussion) collapses. What survives: Lemma 2.1 (strata are J-invariant), the
first part of Lemma 3.1 (ratios are J-holomorphic off the divisor), and Lemma 5.1
itself. A secondary gap: the smooth extension of ζ'/ζ across D_α in Lemma 3.1 is
justified by a "one-variable Taylor argument in the normal direction", but the
normal direction is two-real-dimensional, where ζ' = az + b z̄ gives ζ'/ζ = a +
b z̄/z, which is not smooth without first showing b vanishes along D_α and then
invoking a removable-singularity result (as the author's Lemma 1.1 does). Y's
answer to the problem is wrong, and the mistake is one that the simplest
integrable example exposes.

Correctness: -1

Exposition: 1

Attribution: 1

## Solution Z

**Review.** Z does not answer the question as posed. It splits "symplectic simple
normal crossings divisor" into a "bare reading" (symplectic strata, transverse
crossings, no orientation condition) and the standard reading of Farajzadeh
Tehrani–McLean–Zinger Def. 2.1 (intersection orientations agree with symplectic
orientations), and proves a negative answer only under the bare reading. Under
that reading the argument is correct: Lemma 3.1 (a taming form induces the
complex orientation on every J-invariant subspace, by a convexity path to a
compatible form), Lemma 3.2 (intersection orientation of transverse complex
hyperplanes is the complex orientation), Lemma 3.3 (sign bookkeeping in dimension
four), Proposition 4.1 (adapted coordinates force orientation agreement),
Proposition 4.2 (the agreement sign is constant along a path of forms keeping the
strata symplectic), and the T^4 example in Section 5 (I checked: φ_2^*ω = du∧dv,
the tori meet only at the origin, and the determinant of the 4×4 matrix of tangent
vectors is −1). In dimension four this is positivity of intersections of
J-holomorphic curves, as Z notes in Section 6. However, the orientation condition
is part of the standard definition in the literature the problem draws on (and is
exactly what the author's Theorem 2.1 verifies for the auxiliary divisors), so the
"bare reading" is not a genuine ambiguity of the problem; Z's Section 6 concedes
that under the standard convention "the argument given in this paper does not
settle the existence question." Nothing in Z bears on the actual difficulty
(compatible pseudo-holomorphic normal structures). What is established: a correct
but elementary necessary condition, void under the intended definition. What is
claimed: only that, honestly. No false statements. The leftover "% RECORDS"
comments in the source are harmless. The single reference is to the right arXiv
number but with a garbled title and a wrong author initial ("E. Zinger").

Correctness: 0

Exposition: 1

Attribution: 1

## Ranking

Ordering rule: first by whether the text contains false mathematical claims about
the problem, then by progress on the actual question.

1. **Z** — no false statements; correct elementary obstruction, but explicitly
   only for a nonstandard reading, so no progress on the question as intended.
2. **X** — right answer (YES) and the right starting point (regularization +
   normal-bundle model), but the key lemma is false and the global patching
   problem that constitutes the whole difficulty is never addressed.
3. **Y** — wrong answer (NO) by a false theorem, refuted by the integrable case of
   its own example; the error is a misapplication of the maximum principle on a
   non-compact open set.

| Solution | Correctness | Exposition | Attribution |
|---|---|---|---|
| X | -1 | 1 | 1 |
| Y | -1 | 1 | 1 |
| Z | 0 | 1 | 1 |
