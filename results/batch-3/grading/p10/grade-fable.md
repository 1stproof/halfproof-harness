# Referee report, problem 10 (mirror pairs for very general H-polarised Tyurin degenerations)

Reference point for all three reviews: the author's solution proves the statement by (i) classifying the very general H-polarised Tyurin degenerations (three: the D16 case F2 ∪ Bl17 P², and the asymmetric and symmetric E8E8 cases Bl10 P² ∪ Bl8 P², Bl9 P² ∪ Bl9 P²) and computing, for each, the sublattices H and φ_X(K_{V_i}^⊥) of I^⊥/I ≅ H ⊕ E8 ⊕ E8; (ii) on the very general H ⊕ E8 ⊕ E8-quasipolarised K3 surface, taking the two Inose/Clingher–Doran fibrations (2II* 4I1 and I12* 6I1), finding allowable loops by a certified monodromy computation, and computing Γ and φ_Y([r_i(b)]^⊥); (iii) matching the two sets of lattice data by an isometry, which is what [GT24, Definition 5.1 and Proposition 5.3] require. The problem is therefore a genuine computation, not a formal consequence of lattice theory. Note also that the problem is the L = H instance of what [GT24] states as a conjecture (Conjecture 5.10); X and Y both say this, and the structure of the author's proof is consistent with it.

## Solution X

**Review.** X does not attempt a proof. It states (Proposition 3.1) that the problem is the special case L = H, m = 1 of [GT24, Conjecture 5.10], and proves three routine lattice facts: H^⊥ in the K3 lattice is H² ⊕ E8² (Lemma 2.1); a primitive isotropic vector in an even unimodular lattice is 1-admissible, by the standard completion g = x − (x²/2)e (Lemma 2.2); the mirror lattice e^⊥/Ze is H ⊕ E8 ⊕ E8 (Lemma 2.3). Section 4 proves only that a K3 surface with H ⊂ NS is elliptic (citing Huybrechts). Everything proved is correct and the reduction to the conjecture is accurately described, but none of it touches the actual content of the problem: there is no classification of the degenerations, no choice of fibration or loop, no computation of the sublattices H^⊥, φ_X(K_{V_i}^⊥), Γ, φ_Y([r_i(b)]^⊥), and no isometry. The paper says so explicitly (Remark 3.2, Conclusion). One minor misuse: Lemma 2.3 invokes Nikulin Proposition 1.6.1 for transitivity of the orthogonal group on primitive isotropic vectors; the relevant results are Nikulin's Theorem 1.14.4 or the Eichler criterion, though the conclusion is true. Claimed vs established: it claims only a reduction to an open conjecture plus trivial lemmas, and establishes exactly that.

Correctness: 0
Exposition: 1
Attribution: 1

## Solution Y

**Review.** Y, like X, states that the problem is the forward direction of [GT24, Conjecture 5.10] for L = H and that it does not prove it ("Theorem (Status)", Section 1, and the list of open points in Section 7). What it does prove is correct but elementary: Theorem 3.1 shows H^⊥ ≅ II_{2,18}, that every primitive rank-2 isotropic I ⊂ H^⊥ is doubly admissible (correct Gram–Schmidt-type construction of g1, g2), that I^⊥/I ≅ H ⊕ Γ with Γ even unimodular negative definite of rank 16, hence I^⊥/I ≅ H ⊕ E8 ⊕ E8; Proposition 3.2 is uniqueness of the primitive embedding of H ⊕ E8 ⊕ E8 (Nikulin 1.6.1 applies legitimately here). Theorem 5.1 records the Type II weight filtration 0 ⊂ im N ⊂ ker N correctly. Theorem 6.1 is the one piece with some bearing on the real problem: on the 2II* + 4I1 fibration it shows, by subset sums of Euler numbers and a correct trace formula tr(τ_v τ_w) = 2 − (v·w)², that an allowable loop can only have |e(Y1) − 12| ∈ {0, 1, 8, 9}; this is consistent with the author's loops (values ±1 and 0) but does not construct any loop and leaves the residual cases open. The rest is padding: Proposition 5.2 checks numerical d-semistability for a V6 ∪ V12 ansatz which the author's classification shows cannot occur as a very general H-polarised degeneration (Y does not claim it occurs); Proposition 6.2 observes that the clause (A|_{V_i})² > 0 is not quoted in the mirror-side definitions, which is not a mathematical result. The dimension statement in Theorem 4.1 (18 and 2) is correct but is attributed to an unverifiable "Alexeev–Engel, Theorem 4.7, arXiv:2505.22557v4" with no title. I found no false mathematical statement, but nothing proved advances the problem beyond the lattice preliminaries, and Y says so.

Correctness: 0
Exposition: 0
Attribution: 0

Exposition note: the prose is clear sentence by sentence, but a "Theorem (Status)" with a "proof", `% RECORDS` comments, and two sections on irrelevant ansätze mean a reader has to work to isolate the small relevant part. Attribution note: borderline; the GT24 internal references are consistent with the author's usage, but the title-less AE25 entry carrying a theorem number is a moderate defect.

## Solution Z

**Review.** Z claims a complete proof and does not deliver one; the argument rests on misreadings of the basic objects and on results that do not exist in [GT24]. (1) Proposition 4.1(b),(c) asserts that a Tyurin degeneration is Type III with rk N² = 1 and dual complex S². This is false: a Tyurin degeneration has two components glued along a smooth elliptic curve with no triple points, is Type II with N² = 0, and its dual complex is an interval. The whole "pillowcase" matching in Theorem 5.1(b) is built on this error. (2) Z never identifies which degeneration it is treating. The author's classification gives three very general H-polarised Tyurin degenerations with different lattice data (H^⊥ ≅ D16^+ or E8 ⊕ E8; (K_{V_1}², K_{V_2}²) = (−8, 8), (−1, 1), (0, 0)); Z treats "the" degeneration as unique with no computation of H^⊥ or φ_X(K_{V_i}^⊥). (3) The chosen loop (Theorem 5.1), enclosing the four I1 fibres of the 2II* + 4I1 fibration, gives (e(Y1) − 12, e(Y2) − 12) = (−8, 8). Under the mirror relation e(Y_i) − 12 = −K_{V_i}² this could only mirror the D16-type degeneration, whose H^⊥ is D16^+, whereas Γ for this fibration is E8 ⊕ E8; so this pair can never satisfy [GT24, Prop. 5.3]. The author instead uses the I12* 6I1 fibration for that case. Allowability (Theorem 5.1(a)) is "proved" by an order-6 base change and a "Type III cusp" criterion, which is not [GT24, Definition 4.11] (boundary monodromy conjugate to [[1, e(Y_i) − 12],[0, 1]] plus primitivity of r_i(a)); the product of the four I1 monodromies is never computed. (4) Theorem 5.2 "[GT, Theorem C]", with "checkerboard structure", "light-leaf condition", "SYZ mirror diffeomorphism", and "[GT, Definition 5.1(4)]" filtrations E_m ⊂ E_m′, along with "[GT, Example 1.2]", "[GT, Example 4.20(iii)]", "[GT, Construction 4.1, Proposition 4.7]", "[GT, Theorem B]", are fabricated: the actual Definition 5.1 is a lattice-matching condition on I^⊥/I and J^⊥/J (as the author's solution, X and Y all use it), and Z never computes any of the lattices it requires. (5) Theorem 3.1(b) claims the elliptic fibration with section is unique because "NS = U ⊕ E8² forces R = E8²"; this ignores non-saturated trivial lattices and is false, the same surface carrying the I12* 6I1 fibration with trivial lattice H ⊕ D16 of index 2. (6) Minor: "Niemeier's classification in signature (1,1)/(2,2)" (Prop. 2.1) is a misattribution; "Tyurin's comb models" and "Harder–Thompson lattice of essential sections" are not identifiable. What is correct: Lemma 3.2 (Weierstrass model y² = x³ + c t⁴ x + t⁵ β(t), fibres 2II* + 4I1, NS = H ⊕ E8 ⊕ E8 for very general parameters) matches the author's E8E8 fibration, and Lemma 3.3 on the class σ + 2f is essentially right. These are a small fraction of the work and the rest cannot be repaired within Z's framework.

Correctness: -1
Exposition: 0
Attribution: -1

## Ranking

1. X
2. Y
3. Z

Rule: X and Y are both acknowledged partials with no false statements; X ranks above Y because it is accurate, concise and correctly attributed, while Y adds no further progress and carries an attribution defect and heavy padding. Z ranks last: it asserts a proof whose main steps are false or fabricated.

| Solution | Correctness | Exposition | Attribution |
|---|---|---|---|
| X | 0 | 1 | 1 |
| Y | 0 | 0 | 0 |
| Z | -1 | 0 | -1 |
