# Referee reports

These reports use only the supplied files. Locations refer to lines in the respective solution files. Attribution assessments concern the statements and references as presented; no external sources were consulted.

## X

This is an explicitly acknowledged partial solution, not a proof of the requested arbitrary-dimensional assertion (abstract, line 35; final section, lines 741–766). It establishes useful results: the flow-box description, finite-dimensionality of the distributional kernel and closedness of the smooth range (Section 4), weighted singular witnesses excluding certain equilibria and periodic orbits (Section 5), and smoothness of invariant measures conditional on a smooth positive invariant density (Section 6). Its entropy argument in Section 7 correctly distinguishes the flow direction's zero exponent from transverse exponents: in dimension three, positive entropy supplies both a positive and a negative transverse exponent, permitting the hyperbolic-flow closing/horseshoe argument. Thus it obtains zero entropy in dimensions at most three under the extra invariant-density hypothesis, and handles dimension one directly. It neither derives that extra hypothesis from global hypoellipticity nor resolves additional transverse zero exponents in higher dimensions. These are substantial missing steps, candidly identified. One additional statement should be deleted or qualified: line 758 identifies the general divergence cohomological obstruction with a “Livšic/de Rham class,” equivalently periodic-orbit integrals. No such general equivalence holds without further dynamical hypotheses; periodic data alone do not generally characterize smooth coboundaries. This ancillary overstatement does not invalidate the proved conditional results. The presentation is lengthy but organized, explicit about conventions, and clear about its limitations. The principal tools are attributed to appropriate standard authors; the references are sometimes less precise than desirable, but there is no central invented theorem. The correctness score reflects the rubric's acknowledged-partial-result category, not completion of the problem.

Correctness: 0

Exposition: 1

Attribution: 1

## Y

The two elementary implications actually proved are sound: a smooth compact-group action preserves an averaged Riemannian metric (lines 155–194), and equicontinuous iterates have zero entropy (lines 223–258). However, the entire connection with the problem's hypothesis rests on the purported Greenfield–Wallach structure theorem at lines 82–115. The general classification asserted there is not the cited theorem: it effectively assumes the toral rigidity conjecture that the supplied author solution explicitly describes as a conjecture. Calling the compact-torus-action consequence weaker does not supply a proof of it. The accompanying explanation involving a circle-valued “first integral” satisfying F(g_t x)=e^{it}F(x) and a “coisotropic structure” is unsupported; even the specified eigenfrequency need not exist for a Diophantine linear flow. The bibliography also misidentifies the Greenfield–Wallach vector-fields paper: the supplied reference gives Topology 12 (1973), 247–254, rather than the American Journal of Mathematics entry here. Consequently Step 1 of the main proof (lines 275–280) fails, and the argument establishes only a conditional result for flows already known to extend to compact-group actions. There are further false claims in the consistency checks (lines 140–148, repeated at 299–306): a nonzero constant vector field on the circle is globally hypoelliptic and has periodic orbits, and compact-group actions certainly can have periodic or fixed orbits. The exposition of the conditional argument is clear, but the unsupported classification is the essential mathematical content, so this approach does not make substantial progress on the general problem. The fabricated theorem and proof narrative warrant the lowest attribution score.

Correctness: -1

Exposition: 1

Attribution: -1

## Z

The solution gives clear elementary arguments about orbit measures, their supports, and conjugating a nowhere-zero circle vector field to a rotation. It claims a complete proof by excluding periodic orbits and then invoking positive entropy to produce a periodic point. The decisive error is the “Katok” theorem at lines 277–285 and its corollary at lines 287–295: positive entropy alone does not imply periodic points for smooth diffeomorphisms in arbitrary dimension. For example, the product of a hyperbolic toral automorphism with an irrational circle rotation has positive entropy and no periodic points. Katok's horseshoe theorem requires a hyperbolic measure, a condition neither stated nor proved here; a nonsingular flow's time-one map already has a zero exponent in the flow direction. Thus lines 317–319 cannot yield the claimed contradiction, and replacing the citation with its correct statement does not repair the proof. There is also an independent convention error at lines 71–94: the operator defined by -u(Xφ) on functionals on functions acts on fΩ as (Xf+f divΩ X)Ω, not as (Xf)Ω. It is the Lie derivative on generalized densities, whereas the problem's scalar operator acts on test densities by duality. Accordingly the direct use of orbit measures and Dirac masses at lines 157–174, 201–202, and 228–234 requires an invariant smooth density or an additional argument relating the operators; the assertion that this is merely an identification is unjustified. The orbit-measure reasoning is valid under the solution's altered hypothesis, and the rotation calculation is useful, but substantial new work is needed to prove the requested result. The prose and organization are easy to follow. The main attribution issue is an incorrect strengthening of a real, relevant Katok theorem, warranting a moderate attribution penalty.

Correctness: -0.5

Exposition: 1

Attribution: 0

## Ranking and scores

**X > Z > Y.** X supplies substantial, honestly delimited partial results; Z retains useful elementary arguments but has a fatal entropy inference and a distributional-convention gap; Y replaces the central difficulty with an unsupported classification theorem.

| Solution | Correctness | Exposition | Attribution |
|---|---:|---:|---:|
| X | 0 | 1 | 1 |
| Y | -1 | 1 | -1 |
| Z | -0.5 | 1 | 0 |
