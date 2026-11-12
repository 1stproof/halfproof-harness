# Referee reports

These reports use only the five supplied TeX files. External references and claimed computational checks have not been independently accessed; attribution assessments concern the citations and claims as presented, including their consistency with the supplied reference solution.

## Solution X

The solution describes a plausible FRT/double strategy, but does not establish either requested isomorphism. In Section 5, Definition `def:bcS` substitutes an asserted RLL presentation for the actual reduced skein algebra without proving the geometric identification; moreover, its lower-triangular b and upper-triangular c matrices reverse the triangularities of the specified arcs in the author's solution, without an explicit change of states or conventions. Section 6 then delegates the essential construction and injectivity to unspecified “standard” FRT results. More decisively, Section 7, Proposition `prop:double-quotient`, identifies the two actual Borel torus copies as inverses rather than identifying them with each other. This is incompatible even with the internal negative-Borel relation: if the proposed surviving torus is \(\widehat K_i=(\widehat K_i^-)^{-1}\), then its conjugation on \(F_j\) has the opposite exponent to the required relation in Section 2. The ordinary multiplication map to the quantum group does not kill the proposed ideal: \(\widehat K_i^-\widehat K_i^+-1\) maps to \(\widehat K_i^2-1\). The inverse belongs in the identification of a skein diagonal with a Borel generator, as in the reference solution, not in this quotient of the two already specified Borels. There are further foundational problems: Section 1 reverses both pairing multiplication rules, rather than just one, and its pairing domain does not match the ordering used when defining the Borel double in Section 4; the ground-field claim in the conventions also overlooks the required fractional powers of q. Thus there is a recognizable strategy and relevant background, but substantial corrections and a genuine skein argument are needed. The prose and organization are clear. Attribution is moderately deficient: the decisive presentation and normalization claims are assigned to broad references without precise supporting statements, and the cited standard quotient theorem is used for an incorrect quotient.

Correctness: -0.5

Exposition: 1

Attribution: 0

## Solution Y

This submission claims a complete, computationally verified proof, but its foundational algebra, presentation, and calculations are inconsistent. Already (A2), in Section 1, wrongly gives \(\alpha_r\cdot\alpha_s=-2\delta_{rs}\): for adjacent roots the displayed bilinear form gives +1. Definition `def:H` omits \(\prod_i\widehat K_i=1\), and the error is substantive: Lemma `lem:split` and the final paragraph explicitly assert n independent torus directions, whereas the simply connected SL(n) torus has rank n−1 and the actual loops satisfy \(\prod_i\Omega_i=1\). The pairing table contradicts itself: Lemma `lem:T`(c) first gives \(\langle\widehat K^u F_s,E_s\rangle=\theta\) for every u, then gives \(\theta q^{\mp2}\) for a special such u. In Proposition `prop:EF`, the first channel drops a factor \(q^{-2}\) from the table's value of \(\langle F_s,E_s\mathcal K_s^{-1}\rangle\); even the resulting displayed (EF) does not imply (EF′). Multiplication by a torus unit cannot replace the independent Laurent monomials \(\mathcal K_s^{(-)-1},\mathcal K_s^{(+)}\) by \(\mathcal K_s^{(-)},\mathcal K_s^{(+)-1}\). Equation (KF) also directly reverses the internal relation it purports to use. Section 2's Definition `def:A` declares a different, inadequately specified “Norris / Lauve–Taft” presentation to be the skein algebra, without deriving it; the claimed inverse generator formulas and subsequent exchange computations contain further order/scalar errors. The unitriangularity argument in Theorem `thm:one` supplies neither a valid source basis nor a separating order: terms with the same letter multiset cannot become strictly smaller in an order on those multisets. Some elementary torus-commutation and centrality calculations are useful, but neither main conclusion is established, and the purported proof requires reconstruction. The notation and repetition make it possible to trace the argument only with serious effort. Attribution has blatant problems: a general-n presentation is attributed to Lê's triangular-decomposition work without an applicable statement (the supplied reference identifies that work with n=2), and repeated assertions of exact, “letter-for-letter” verification certify mutually contradictory identities. Those verification claims cannot substantiate the printed proof.

Correctness: -1

Exposition: 0

Attribution: -1

## Solution Z

This is expressly an acknowledged partial solution: the abstract, Section 1, and Section 7 restrict the claimed result to n=2 and list the missing general-n ingredients. Within the cited rank-two presentations, Section 5 correctly checks the explicit map \(K^{1/2}\mapsto x\), \(L^{1/2}\mapsto y\), \(\overline E\mapsto-hux\), \(\overline F\mapsto h^{-1}yv\), including the commutator and surjectivity; injectivity is attributed specifically to Korinman's theorem. Section 6 correctly reduces the skein ideal to \((xy-1)\) and gives mutually inverse presentation maps identifying the normalized quotient with the simply connected SL(2) quantum group. Section 4's abstract torus-centrality criterion is also correctly proved under its stated hypotheses. The main limitation is scope: neither requested general-n statement follows from these results. There is also a convention-checking gap in Section 4.2: the translation between the displayed double generators and the problem's actual Borel generators is asserted by citation and “substitution,” rather than explicitly recording the necessary torus inversions and root normalizations. The determinant of the displayed two-by-two table of torus pairings alone does not prove nondegeneracy of the entire Hopf pairing. These qualifications do not erase the substantial rank-two algebra, but the work should be accepted only as partial progress, giving correctness 0 under the supplied rubric. Exposition is clear, if lengthy, and distinguishes integral from field coefficients and established results from open points. The key external inputs are explicitly attributed to Korinman with theorem and presentation locations, and the other standard constructions are credited; the bibliography's “G. Korinman” rather than Julien Korinman is a minor bibliographic error, not a material attribution failure.

Correctness: 0

Exposition: 1

Attribution: 1

## Ranking and score table

**Z > X > Y.** Z offers coherent, explicitly limited progress; X outlines a relevant strategy but makes major identification errors; Y's claimed complete proof rests on pervasive contradictions and unsupported verification claims.

| Solution | Correctness | Exposition | Attribution |
|---|---:|---:|---:|
| X | -0.5 | 1 | 0 |
| Y | -1 | 0 | -1 |
| Z | 0 | 1 | 1 |
