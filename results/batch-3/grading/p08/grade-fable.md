# Referee report, problem 8 (globally hypoelliptic vector fields have zero entropy)

Context used for all three reviews. The author's proof is genuinely nontrivial: it uses the equivalence GH ⟺ cohomology-free (Forni, Chen–Chi), builds endpoint distributions ξ_x = G^t δ_x with a Lipschitz bound in a negative Sobolev norm, derives Diophantine recurrence and equidistribution bounds, and contradicts an exponential-recurrence lemma for positive-entropy flows via orbit matching. Two facts matter for grading: (i) the statement "a GH vector field is a Diophantine linear flow on a torus" is the open Greenfield–Wallach/Katok conjecture, not a theorem; (ii) absence of periodic orbits does not imply zero entropy for flows in dimension ≥ 4 (the constant-roof suspension of a hyperbolic toral automorphism times an irrational rotation has positive entropy and no periodic orbit).

## Solution X

X is an explicitly acknowledged partial result. Under the correct convention (distributions dual to densities, so that L_X extends the Lie derivative on functions), it proves: the flow-box description of distributional solutions (Prop. 3.1); under (H), the distributional kernel of L_X is finite-dimensional and X has closed range on C^∞, via a closed-graph estimate and Rellich compactness (Thm. 4.1, lines 242–367, correct); (H) excludes trace-zero equilibria and periodic orbits with unit Poincaré determinant, using the weighted orbit current T_γ (Thm. 5.1, correct, including the check that d/ds c(g_s p) = c·div X); the dimension-one case in full (Thm. 5.1(4), correct); an integrating-factor theorem (Thm. 6.1); and zero entropy in dimension ≤ 3 under the extra hypothesis that all invariant probability measures are smooth densities, via Ruelle's inequality applied to g_1 and g_{-1} to force a hyperbolic measure and Katok's horseshoe theorem for flows to produce a periodic orbit (Thm. 7.1, lines 646–716, correct). Section 8 states precisely what is missing: without an integrating factor Xh = div X the divergence term blocks applying (H) to invariant measures, and in dimension ≥ 4 a positive-entropy measure may have transverse zero exponents. Both diagnoses are right, and item 5 correctly observes that excluding periodic orbits cannot suffice. The missing ingredient is exactly Forni's theorem that (H) implies cohomology-free, which would supply the smooth invariant volume; X has the closed-range half but not the codimension-one half, and says so. I found no false statement in the paper. The flow-box section is not needed for anything that follows. Minor bibliographic slip: Ruelle's inequality appeared in Bol. Soc. Brasil. Mat. 9 (1978), not Publ. IHES 49; the Katok 1982 pointer for the flow version of the horseshoe theorem is imprecise but the result is real. The GH literature (Greenfield–Wallach, Katok, Forni) is not cited at all.

Correctness: 0
Exposition: 1
Attribution: 1

## Solution Y

Y's entire proof is Step 1 (lines 275–280): it invokes "Theorem 2.1 (Greenfield–Wallach)" (lines 82–115), asserting that a globally hypoelliptic vector field is a Diophantine one-parameter subgroup of a smooth compact torus action. No such theorem exists. This is the Greenfield–Wallach/Katok conjecture, which the author's own text records as open; Greenfield–Wallach (Topology 12, 1973) posed it as a problem. The cited source "Amer. J. Math. 94 (1972), 428–430" does not match any Greenfield–Wallach paper, and the parenthetical proof sketch (a "smooth circle-valued first integral F with F(g_t x) = e^{it} F(x)" obtained from Treves, then a "coisotropic structure, recursively a T^k-action") is incoherent. Everything after Step 1 is correct but elementary: averaging a metric over a compact group yields isometries (Lemma 2.3), and an equicontinuous map has zero entropy because (n,ε)-separated sets are δ-separated at time zero (Lemma 3.2). Remark 2.2(b) on Diophantine linear flows is correct and standard; Remark 2.2(c) asserts without proof that fields with zeros, periodic orbits or "hyperbolic pieces" are never GH. Nothing in the paper engages with the actual difficulty. The approach cannot be repaired: it assumes the conjecture whose consequence it is asked to prove.

Correctness: -1
Exposition: 1
Attribution: -1

## Solution Z

Z's strategy is: GH excludes periodic orbits in dimension ≥ 2, and positive entropy forces periodic points, so entropy is zero. Both halves have serious problems. First, Z defines distributions as the dual of C^∞(M) and sets (L_X u)(φ) = −u(Xφ) (lines 81–94), then claims this "is exactly the hypothesis in the problem statement." It is not: under Z's identification f ↦ fΩ, Z's operator acts on smooth f as (Xf + f·div_Ω X)Ω, so it is the Lie derivative on densities, not an extension of the Lie derivative on functions. Under the problem's convention (which the author uses), L_X μ_γ = −(div X)μ_γ for a periodic-orbit measure, which is not a priori smooth, so Proposition 3.5 as proved does not follow from the stated hypothesis. The conclusion is true but needs the invariant smooth volume, i.e. Forni's theorem, which Z neither proves nor cites. Second, and fatally, "Theorem 5.2 (Katok)" (lines 277–285) states that every C^{1+α} diffeomorphism of a compact manifold carrying an ergodic measure of positive entropy has a periodic point. This is false: A × R_α on T^3 (A hyperbolic toral automorphism, R_α irrational rotation) is C^∞, Lebesgue is ergodic with entropy log λ > 0, and there are no periodic points. Katok's theorem requires a hyperbolic measure (automatic only on surfaces), and the time-one map of a flow always has a zero exponent, so even the flow version needs an argument Z does not give; in dimension ≥ 4 the needed statement is simply false for flows (the suspension of A × R_α). Hence Corollary 5.3 and the proof of Theorem 6.1 (lines 308–320) collapse for dim M ≥ 3, and the approach cannot be fixed. What survives: the dimension-one case (Prop. 4.1, correct), the completeness lemma and the two support lemmas (correct but routine). The attribution of a false statement to Katok 1980 is the main bibliographic fault; Bowen–Walters, Walters and Lee are real and used appropriately.

Correctness: -1
Exposition: 1
Attribution: 0

## Ranking

X > Z > Y. Ordering rule: amount of correct, relevant mathematics established, with wrong load-bearing statements ranked below honest partial results. X is correct throughout and states its gaps. Z proves a small true piece (dimension one, and the periodic-orbit exclusion under a misread hypothesis) but its key theorem is false. Y proves nothing beyond trivialities and rests on an open conjecture presented as a theorem with a fabricated citation.

| Solution | Correctness | Exposition | Attribution |
|---|---|---|---|
| X | 0 | 1 | 1 |
| Y | -1 | 1 | -1 |
| Z | -1 | 1 | 0 |
