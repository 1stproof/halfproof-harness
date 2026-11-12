# Referee reports

These reports use the symplectic SNC convention with positive, orientation-compatible intersections, as in the supplied author's solution and its regularization theorem. The problem does not spell out this convention; Z's explicit discussion of the weaker convention is therefore relevant, but does not resolve the intended problem. Locations below refer to source lines in the respective solution files. Only the supplied local files were consulted.

## Solution X

X claims the affirmative result, even with compatibility rather than tameness, and correctly identifies regularization by a cohomologous deformation as a useful first step. Its block decomposition of the model symplectic form and most of its polar-decomposition argument are valid. However, the decisive assertion in the lemma “Compatible J preserving the splitting and making fiber coordinates holomorphic” is false: lines 229–231 assert that a fiber coordinate in a unitary trivialization has differential vanishing on the connection's horizontal distribution. With connection form `dθ + β`, the horizontal lift of a base vector `v` instead satisfies `dw(v^H) = -i β(v) w`, which need not vanish. Preserving the horizontal/vertical splitting and acting by `i` vertically consequently does not make that coordinate holomorphic. This is a substantive obstruction to the proposed proof, not a choice-of-sign issue: arbitrary unitary connections and frames do not supply the requisite pseudo-holomorphic normal-bundle structures. Independently, Step 4 (lines 291–298) asserts that the separately constructed local structures already give a structure near the whole divisor, without proving agreement on overlaps or between strata. Averaging metrics cannot generally preserve their different local holomorphic defining functions. The author’s construction addresses precisely such additional normal-bundle and compatibility data; X has not established them. There is also a minor missing minus sign in line 153: the displayed expression should be `-g(B^{-1}A^2v,w)`. Thus X establishes useful preliminary geometry but neither the requested global structure nor its stronger compatibility claim. The organization and explanations are clear despite these mathematical failures. The regularization input is appropriately attributed to Farajzadeh Tehrani–McLean–Zinger, and the matrix construction has an appropriate named reference; the central false lemma is presented as X's own argument, rather than falsely attributed to that source.

Correctness: -0.5

Exposition: 1

Attribution: 1

## Solution Y

Y claims that local holomorphic defining functions force every normal line bundle to be flat, and hence that a projective line in complex projective two-space is a counterexample. This approach is unsalvageable: the standard integrable complex structure on projective two-space is tamed by the Fubini–Study form and has ordinary holomorphic coordinates defining the line, whose normal bundle is indeed `O(1)`. Thus the very example proposed in lines 93–113 directly disproves Y's obstruction theorem. The fatal step is lines 276–291: the transition functions are defined only on overlaps `D_α ∩ W_i ∩ W_j`, not on the entire compact component. Compactness of `D_α` does not force holomorphic functions on these generally noncompact open overlaps to be constant. Nonconstant holomorphic transition functions are entirely consistent with a nontrivial holomorphic normal bundle. There is another gap in the ratio lemma (lines 191–198): transverse vanishing of smooth complex-valued functions does not by itself permit division by a complex normal coordinate using the stated one-real-variable Taylor argument. The ratio conclusion can be justified using holomorphicity and a removable-singularity argument, but that would not repair the flatness claim. Also, the numerical pairing of an Euler class of degree two with `[D_α]` in the general-dimensional theorem (lines 77–85 and 313–316) is only a numerical self-intersection when `D_α` is a surface. The elementary assertion that strata are J-invariant is correct, but it does not provide substantial progress toward the existence question once the false obstruction is removed. The exposition is readable and systematically organized. Nijenhuis–Woolf and Chern–Weil theory are explicitly credited; the principal failure is a misuse of the maximum principle, not a fabricated external theorem or reference.

Correctness: -1

Exposition: 1

Attribution: 1

## Solution Z

Z correctly proves a necessary orientation-agreement condition, its persistence under symplectic deformation, and a counterexample for the explicitly weakened “bare” crossing condition. In particular, for the two tori parametrized in lines 408–413, both pullbacks of the symplectic form are `du ∧ dv`, the intersection consists of one point, and the tangent-basis determinant in lines 497–506 is `-1`. Tameness and invariance of the branch tangent planes would force positive intersection, so the claimed obstruction for this pair is valid, including under the permitted deformations. The limitation is that a negative crossing is excluded from the symplectic SNC hypothesis in the intended convention. Z explicitly acknowledges this in lines 33–46 and 536: it does not settle the existence question for orientation-agreeing divisors. Accordingly this is an acknowledged partial result/analysis of a weaker formulation, scored 0 rather than a complete negative solution or an erroneous claimed proof of the intended result. The argument is clear, self-contained, and somewhat repetitive, with the conditional scope stated prominently. Attribution has a moderate bibliographic problem: its sole reference (lines 540–543) gives “E. Zinger” and the title “Normal crossings divisors and configurations for symplectic topology,” whereas the supplied author's bibliography identifies A. Zinger and “Normal crossings singularities for symplectic topology.” The intended work is recognizable and the orientation convention is credited, so this is not a case of wholesale invented attribution, but the reference as written is materially inaccurate.

Correctness: 0

Exposition: 1

Attribution: 0

## Ranking and score table

**Z > X > Y.** Z gives a sound, explicitly limited result; X gives relevant preliminary constructions but fails at the essential holomorphic-coordinate and gluing steps; Y's central obstruction is contradicted by its own proposed example. This ranking prioritizes mathematical validity and accurately stated scope over the sum of the three scores.

| Solution | Correctness | Exposition | Attribution |
|---|---:|---:|---:|
| X | -0.5 | 1 | 1 |
| Y | -1 | 1 | 1 |
| Z | 0 | 1 | 0 |
