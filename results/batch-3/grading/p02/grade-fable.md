# Referee report, Problem 2 (minimax rate for the Darcy conductivity)

Reference answer (author's solution): the minimax mean squared L2 error over F(alpha, r), r small, is n^{-2alpha/(2alpha+d+3)} up to logarithmic factors. The mechanism is anisotropic: along the characteristics of grad u the inversion gains one derivative, but across them (angular directions, d-1 dimensional) it is 2-ill-posed, and the angular directions dominate the L2 risk.

## Solution X

**Review.** X claims the exact answer n^{-2alpha/(2alpha+d+2)} (Theorem 1.1), with matching log-free lower and upper bounds. The answer is wrong: the true rate n^{-2alpha/(2alpha+d+3)} is strictly slower, so X's upper bound (Proposition 6.1) is false. The lower bound (Proposition 5.1) is correct but not sharp. Its ingredients are sound: linearization T at f = 1 with Delta(Tg) = g + x.grad g/d (Lemma 2.3), the quadratic remainder bound ||R(h)|| <~ ||h||_inf ||h||_2 (Lemma 2.8), the upper half of the scale estimate ||Tg|| <~ delta ||g|| for a packet of disjoint isotropic bumps (Lemma 4.3), and Fano. This reproduces the easy lower bound that Y and Z also obtain. The fatal error is in the upper bound. Lemma 4.3 is stated and (sketchily) proved only for packets of 4delta-separated bumps with one fixed radial profile; for those the two-sided bound ||Tg|| ~ delta ||g|| is plausible. Lemma 6.2(i) then asserts the inf-sup inequality ||P_delta T h|| >= c delta ||h|| for every element of the sieve F_delta, which is the span of all interior wavelet packets at scales >= delta, by decomposing h into "wavelet pieces at scales 2^{-j}" and invoking Lemma 4.3 per scale plus unproved "almost orthogonality" of the images T h_j (also Remark 4.5). A full wavelet level is not a separated packet, and sums of overlapping bumps can cancel the radial derivative. Explicit counterexample in the sieve: h(x) = a(|x|) b((x/|x| - omega_0)/delta) with a smooth and supported in [1/4, 3/4]; then x.grad h = r a'(r) b(...) = O(||h||), so Lh = O(||h||) and ||Th|| ~ delta^2 ||h||, not delta ||h||. The transport operator L = I + x.grad/d is invertible on L2 (Lemma 2.5, correct), but that does not give a lower bound ||Lg|| >~ delta^{-1} ||g|| on scale-delta functions; X conflates the two. Consequently the risk bound in Section 6(C) (eq. (6.11), "basic") loses a factor delta^{-1} on tangential modes and the claimed rate collapses. The remark in Lemma 6.2 that "the origin is the only degeneracy" is also false: the equatorial directions xi perpendicular to x degenerate at every point. Correct content: Lemma 2.3, Lemma 2.5, Lemma 2.7, Lemma 2.8, the upper half of Lemma 4.3, Proposition 5.1. False content: Theorem 1.1, Lemma 6.2 (inf-sup lower bound), Proposition 6.1, Remark 4.5 (two-sided degree-one claim), the boxed answer.

**Correctness: -1**
**Exposition: 0**
**Attribution: 0**

Exposition: 30 pages of dense notation, constant "absorbed" bookkeeping, forward references, and key steps asserted ("dominated by the diagonal", "almost-orthogonal by Lemma 4.4") rather than proved; followable only with serious effort. Attribution: standard tools are named in text (Fano, Varshamov-Gilbert, Weyl, Tsybakov Theorem 2.5, Cohen-Dahmen-DeVore wavelets) but there is no bibliography and no reference at all to the Darcy inverse problem literature (Richter's inversion formula, Nickl-van de Geer-Wang), for a document claiming to settle a known open problem. No hallucinated references.

## Solution Y

**Review.** Y proves the two-sided non-matching bracket n^{-2alpha/(2alpha+d+2)} <~ R_n <~ (log n)^kappa n^{-2(alpha-1)/(2alpha+d+2)} and states explicitly that the exact rate is open. The true rate n^{-2alpha/(2alpha+d+3)} lies strictly inside this bracket, so no stated bound is wrong. Upper bound (Theorem 1): a wavelet projection estimator of u in H2 at rate n^{-2(alpha-1)/(2alpha+d+2)} (Lemma 4, correct), the plug-in least-squares fit of div(h grad u_hat) = 1 over F(alpha, r), and the stability estimate ||f_1 - f_2||_{L2} <~ ||u_1 - u_2||_{H2} plus residual (Lemma 3, quoted from Nickl-van de Geer-Wang, Lemma 24). I checked the stability estimate for this class: multiplying div(w grad u_0) = residual by w = f_hat - f_0 and integrating gives (1/2) int w^2 Delta u_0 + (1/2) int_{boundary} w^2 d_nu u_0, both nonnegative for small r (Delta u_0 = (1 - grad f.grad u)/f > 0, d_nu u_0 >= 0 by the maximum principle), so the lemma holds without any boundary condition on f. This upper bound is the previously best known rate quoted in the problem's own context, so Y reproduces the literature and does not improve it. Lower bound (Theorem 3): Assouad with isotropic interior bumps and the smoothing bound ||u_f - u_g||_{L2} <~ ||f - g||_{H^{-1}} (Lemma 2). Minor gaps: in Lemma 2 the pairing bounds int (f-g) grad u_g.grad psi by ||f-g||_{H^{-1}} ||grad u_g.grad psi||_{H1}, but grad u_g.grad psi is not in H1_0 (nonzero boundary trace); this is fixed by a cutoff since f - g is interior-supported in the application. In Lemma 5 the inequality (1+|xi|^2)^{-1} <~ 2^{-2j}(1+|2^{-j}xi|^2)^{-1} fails for |xi| << 2^j; one needs a mean-zero bump phi (or d >= 3, with a log loss in d = 2). Both are easily corrected. The lower bound misses the anisotropy (tangential bumps would give the sharp exponent), so it is not sharp, but it is correct. Y answers the question only with "open"; it is a correct, acknowledged partial result.

**Correctness: 0**
**Exposition: 1**
**Attribution: 1**

Attribution: Nickl-van de Geer-Wang (arXiv:1809.08818) is real and correctly credited for the stability idea; Tsybakov, Gilbarg-Trudinger standard. The citation S. Wang, arXiv:2601.09007 (2026), "Lemma 2.2", cannot be verified from this directory; it is plausible but if it does not exist the attribution score should drop to 0. The bracketed "(arxiv.org)" URLs are web-citation artifacts.

## Solution Z

**Review.** Z explicitly scopes itself as partial: it does not determine the minimax rate for the original problem. What it proves, all correctly as far as I can check: (i) the linearization at f = 1, Delta w = -(x.grad h/d + h), the exact radial identity W' = -(rho/d) phi(rho), and injectivity of the linearized map on C^alpha (Propositions 3.1 to 3.3); (ii) the failure of a fixed-anchor quadratic estimate (trivial but correct) and the moving-anchor quadratic remainder ||Phi(f) - Phi(f') - A_{f'}(f - f')||_{L2} <= C ||f - f'||_inf^2 (Theorem 4.2, clean energy argument); (iii) for the frozen periodic sequence model Y_xi = a_beta(xi) theta_xi + n^{-1/2} Z_xi with a_beta(xi) = (1 + i beta xi_d)/|xi|^2 over a Besov ball, the exact minimax rate n^{-2alpha/(2alpha+d+3)}: upper bound by a spectral cutoff with variance sum ~ R^{d+3} (Lemma 5.2, the equatorial slab xi_d = 0 dominates) and bias R^{-2alpha}; lower bound by Assouad on wavelets constant in x_d (Lemmas 5.4 and 5.5, Theorem 5.6); I verified the KL and separation bookkeeping; (iv) the phase-space variance of the genuine variable-coefficient symbol is also of order R^{d+3} (Theorem 6.2, via the angular integral J(t) ~ min(1, 1/t)); (v) a rigorous two-point lower bound for the original problem at the critical point, n^{-(2alpha+d)/(2alpha+d+4)} (Theorem 7.4), correct but weaker than the isotropic Assouad bound that X and Y obtain. Z identifies 2alpha/(2alpha+d+3) as the candidate exponent, which is exactly the author's answer, and identifies the right mechanism (tangential 2-ill-posedness in d-1 dimensions, radial degree-one smoothing). But nothing rigorous is proved about the original problem at that exponent: no estimator, no tangential lower bound, no transfer from the periodic surrogate. Section 8 lists these gaps honestly. The only overstatement is the sentence that the origin "is not the minimax bottleneck", which does not follow from a weaker lower bound there. The "% RECORDS" comments and "certified scope" wording are pipeline artifacts that do not affect the mathematics.

**Correctness: 0**
**Exposition: 1**
**Attribution: 1**

Attribution: Evans, Triebel, Meyer, Daubechies, Tsybakov, all real and used appropriately. No reference to the Darcy inverse problem literature, but Z's results are self-contained and claim no priority.

## Ranking

1. **Z**: every stated result correct; identifies the correct exponent 2alpha/(2alpha+d+3) and the correct anisotropic mechanism, proven for a surrogate model; honest about what is open.
2. **Y**: correct non-matching bracket on the actual problem (lower bound by isotropic Assouad, upper bound equal to the previously known literature rate); honest about openness; no candidate for the exact rate.
3. **X**: wrong final answer; the main theorem and the upper bound rest on a false inf-sup inequality (Lemma 6.2) that is contradicted by tangentially oscillating perturbations; only the non-sharp lower bound survives.

Ranking rule: correct statements first, then how much of the true answer is identified, then how much is rigorously established on the original problem.

| Solution | Correctness | Exposition | Attribution |
|---|---|---|---|
| X | -1 | 0 | 0 |
| Y | 0 | 1 | 1 |
| Z | 0 | 1 | 1 |
