# Referee reports

These assessments use only the supplied files. Scores concern the question as stated, including arbitrary monoids and all higher direct images. An acknowledged, valid partial solution receives correctness 0 under the prescribed rubric.

## X

This is a valid partial solution. The maximal-sieve criterion in §5 correctly proves both the underived unit isomorphism and exactness of restriction: sheafification leaves values at such objects unchanged, and local surjectivity becomes surjectivity on sections. Applying a cover to the identity valuation correctly establishes the criterion when the strata are valuative. The rank-one argument in §6 is sound, including without finite generation, and §7 correctly handles finitely generated valuative monoids. In fact, the proof of Lemma `lem:fg-valuative-strata` never uses finite generation, so the same argument establishes the conclusion for every valuative monoid; the limitation advertised in §8(3) is unnecessary. The face-quotient and filtered Kan-extension preliminaries are also sound. However, nothing establishes the result for general non-valuative monoids, and the abstract and §8 explicitly acknowledge this. The statement that the general problem is “open” should be read only as a limitation of this submission, rather than a claim about the literature (the supplied author solution gives an affirmative answer). The presentation is clear, though longer than necessary, and the attribution of exactness of sheafification to SGA 4 is appropriate; no substantive attribution problem is apparent.

Correctness: 0

Exposition: 1

Attribution: 1

## Y

The submission acknowledges that higher direct images and arbitrary non-finitely-generated bases are unresolved, but its claimed degree-zero theorem for finitely generated bases is not established either. There is some valid formal progress: with the correct face quotient, Proposition `prop:adj` identifies the Kan extension as evaluation at the quotient by the kernel face. The subsequent proof, however, rests on false structural claims. The scope hypothesis in §1 does not permit restricting valuation targets or infinitesimal test monoids to finitely generated ones. Thus Lemma `lem:N` and Corollary `cor:function` cannot replace all valuations by maps to 0 or to N: the nonnegative rationals and the positive cone of lexicographically ordered Z² are permitted valuative monoids. More decisively, Lemma `lem:inj0` (lines 404–417) incorrectly assumes the premise p(y) ≺ 0 when trying to prove group injectivity. Let M = {(n,k) in Z² : n > 0} ∪ {(0,0)} and p(n,k) = n. This is an integral, saturated, sharp monoid, and p is infinitesimal: a positive first coordinate gives membership immediately; if p(x) = 0, the infinitesimal hypotheses force y = 0 and then x = 0. Nevertheless p on group completions has kernel {0} × Z. Consequently the “Atomicity” lemma is false: f(a,b) = a+b from N² to N satisfies its value-group equality at the identity of N, but h(a,b) = (a+b,b) into M has no lift through f, since h(1,0) and h(0,1) differ. This destroys the fold normal form and the descent argument based on it. There are additional independent errors: Proposition `prop:bp` describes a face quotient as a set complement with absorbing zero, rather than the cancellative quotient; Construction `constr:pushout` uses P/Φ_T rather than Q_T as the amalgamation base for a general T; and the descent proof inserts iB(T) as a covering piece although the canonical monoid map goes from P/Φ_T to Q_T, in the opposite direction needed. In §4, Remark `rem:verdier` incorrectly attributes computation of arbitrary sheaf cohomology by ordinary covering nerves to Verdier's hypercover theorem; fiber products alone do not justify replacing hypercovers by ordinary covers. The numerous conflicting direction conventions and unsupported refinement assertions require serious effort to follow. The central collapse approach is untenable, rather than merely incomplete, warranting correctness −1 despite the acknowledged partial scope. The false stated consequence of the cited theorem warrants attribution 0.

Correctness: -1

Exposition: 0

Attribution: 0

## Z

The claimed negative answer rests entirely on an elementary categorical error. The two initial-object lemmas are correct, as is the pointwise Kan-extension formula, but Corollary `cor:Lan-constant` (lines 96–113, especially line 111) confuses initial and terminal objects: a terminal object computes a colimit, whereas an initial object computes a limit. For b in B_P, the comma category (j ↓ j(b)) has terminal object (b,id), so (Lan_j F)(j(b)) = F(b), not F(id_P). For the proposed example with P = N, the diagram on B_P is simply 0 → Z, whose colimit at the zero-monoid object is Z. Thus the asserted zero Kan extension, the zero inverse image, and the counterexample theorem all fail. The correct elementary observations preceding the error provide no substantial progress toward deciding the derived-unit question, and repairing the colimit calculation eliminates the proposed approach. The exposition is clear and easy to audit. The Mac Lane citation is appropriately attached to the correct Kan-extension formula; the subsequent false inference is a correctness error, not evidence of a fabricated reference.

Correctness: -1

Exposition: 1

Attribution: 1

## Ranking and score table

Mathematical ranking: **X > Y > Z**. X establishes genuine special cases. Y retains a useful formal Kan-extension observation but its central argument fails; Z's counterexample disappears upon correcting its sole decisive calculation. Y and Z tie on correctness under the discrete rubric; Z is clearer and has fewer attribution issues.

| Solution | Correctness | Exposition | Attribution |
|---|---:|---:|---:|
| X | 0 | 1 | 1 |
| Y | -1 | 0 | 0 |
| Z | -1 | 1 | 1 |
