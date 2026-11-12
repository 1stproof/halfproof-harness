# Referee reports

The requested loss is **mean squared** L² loss. The benchmark in the author's detailed risk calculation is n^(-2α/(2α+d+3)), up to logarithmic factors; the exponent α/(2α+d+3) in the author's opening theorem refers to the unsquared scale. The reports below assess each submission's actual arguments, rather than merely comparing its answer with that benchmark. Only the supplied local files were consulted. External references were not opened, so an unverified bibliographic detail is not treated as evidence of fabrication.

## Solution X

X claims a complete, log-free minimax theorem with rate n^(-2α/(2α+d+2)), which is incorrect. It contains useful ingredients: the radial transport inverse, an energy-based quadratic remainder estimate (after correcting the linearization's sign), and a conventional interior-bump Fano construction supporting a weaker lower bound. Its upper bound, however, has several independent fatal defects. In §5(A), lines 1109–1143, the sieve fixes the conductivity to 1 on fixed neighborhoods of the origin and boundary, yet claims approximation error O(δ^α) for the entire class. A fixed small smooth perturbation supported in either excluded region gives an error bounded away from zero, disproving that assertion and consistency of this estimator. The transport inverse's L² boundedness does not imply uniform degree-one inversion: tangential oscillations remain substantially harder. The purported packet lower-bound proof uses an upper almost-orthogonality estimate in the lower direction without proving diagonal dominance (lines 804–830); its harmonic-correction subtraction also introduces an extra factor δ not supplied by the preceding bound (lines 884–900). The multiscale inf–sup statement even claims an upper bound Cδ‖h‖ for all retained coarse modes, which fails for any fixed nonzero coarse mode as δ tends to zero. In §5(C), lines 1373–1385, the equation for the derivative at m incorrectly replaces div(m∇·) by Δ, and describes the difference of two linear operators as having a nonzero value at zero. Lines 1449–1457 incorrectly bound the sum over scales 2^(-j) ≥ δ by O(δ^α), although its coarse terms are O(1). Lines 1535–1538 further assert δ^(α+1/4) ≲ δ^(α+1). Earlier errors include inconsistent signs in the linearization, the false H⁻¹-to-H¹ bound at line 186, and barriers that omit ∇f·∇u₀ and reverse their ordering. These are structural failures, requiring a different upper-bound argument and a different claimed rate, not minor repairs. The lengthy, repetitive presentation and inconsistent operator conventions make verification laborious, though the intended strategy can be followed. Standard tools are named and Fano is attributed to Tsybakov; the erroneous new analytic claims are presented as the author's own arguments, rather than attributed to fabricated sources.

Correctness: -0.5

Exposition: 0

Attribution: 1

## Solution Y

Y explicitly supplies an acknowledged partial solution. Sections 3–4 correctly develop the linearization, radial identity, injectivity, a counterexample to a uniform fixed-anchor quadratic estimate, and a moving-anchor remainder bound. Section 5 establishes the rate n^(-2α/(2α+d+3)) for its explicitly defined frozen periodic model: the variance sum is of order R^(d+3), and the equatorial wavelet hypercube gives the matching lower bound. Section 6 correctly calculates the corresponding phase-space integral, and §7 gives a genuine, weaker lower bound n^(-(2α+d)/(2α+d+4)) for the original nonlinear model using radial perturbations at the origin. The essential missing work is correctly listed in §8: neither an estimator attaining the proposed rate for the original ball problem nor a matching nonlinear lower bound is established. Consequently the frozen-model theorem does not answer the competition question. The introductory assertion that the weaker origin lower bound “shows” the origin is not the bottleneck overstates what a lower bound alone establishes; likewise, saying the original problem “remains open” should be read as an admission that this submission has not solved it, not a conclusion about the supplied problem. Minor constant slips include omitting |D|^(1/2) in the energy proof in §2 and writing ‖h‖∞ ≤ a in §7 without normalizing ‖ψ‖∞ ≤ 1; both are absorbed by fixed constants and do not affect the results. The exposition clearly separates the surrogate problem from the original one and explains its limitations, despite excessive length and repeated “certified” terminology. The standard PDE, function-space, wavelet, and testing tools receive appropriate named references. The score of 0 follows the rubric's explicit category for acknowledged partial results.

Correctness: 0

Exposition: 1

Attribution: 1

## Solution Z

Z acknowledges that its proposed bounds do not match: it claims a lower bound n^(-2α/(2α+d+2)) and an upper bound n^(-2(α−1)/(2α+d+2)), rather than determining the requested minimax rate. The H² wavelet bias–variance calculation in §3 is useful and correct, and a residual-minimization approach can yield a nonoptimal upper bound with a suitable stability argument. However, the claimed lower-bound proof rests on false assertions. In §2.2, line 109, ∇u_g·∇ψ need not belong to H¹₀ merely because u_g and ψ vanish on the boundary: its trace is generally the product of their nonzero normal derivatives. Thus the duality with the explicitly defined H⁻¹=(H¹₀)' is invalid, and the stated uniform smoothing lemma is false without an appropriate change of norm or support restriction. In §4.1, lines 244–255, the scaling estimate for an arbitrary compactly supported bump is false in d=2 when its integral is nonzero: the squared H⁻¹ norm has an additional factor of order j. The cited “effective support” argument does not eliminate low frequencies. The hypercube in §4.2 imposes no cancellation, and line 275 also requires an impossible simultaneous normalization: a function supported in a cube of volume less than 1 cannot have supremum norm at most 1 and L² norm 1. A repaired lower-bound construction could use mean-zero bumps in a fixed interior region and a valid localized duality estimate, but these changes are not supplied. Proposition 1 also incorrectly treats α−1 as belonging to (0,1] for every α>1 and conflates integer Zygmund regularity with classical endpoint Hölder regularity. Finally, the upper-bound proof invokes a generalized residual stability theorem without stating its hypotheses or checking them for the projected potential (lines 226–229). These defects warrant a negative correctness score beyond mere acknowledged incompleteness. The organization and calculations are otherwise clear. Relevant tools and related work are attributed; the supplied files do not establish that the external references are fabricated, and no such allegation is made. The claim that this particular minimax problem is open is not justified by pointing to gaps in earlier, broader settings.

Correctness: -0.5

Exposition: 1

Attribution: 1

## Ranking and scores

**Y > Z > X.** Y has the strongest sound partial analysis and clearest account of its limitations. Z offers a useful nonoptimal estimation route but flawed lower-bound details. X asserts a false complete theorem and its proposed estimator fails to approximate the parameter class.

| Solution | Correctness | Exposition | Attribution |
|---|---:|---:|---:|
| X | -0.5 | 0 | 1 |
| Y | 0 | 1 | 1 |
| Z | -0.5 | 1 | 1 |
