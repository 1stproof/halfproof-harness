# Referee reports

These reports use only the supplied files. Locations refer to source lines and named sections or statements; external references have not been looked up.

## Solution X

This submission explicitly acknowledges that it does not prove the requested theorem (abstract and Section 10, especially lines 716–720). It correctly constructs a smooth descended map, proves the elementary vanishing of the homological pushforward of a compact constant-projection curve, and explains why smooth motion of isolated points need not be constant. Its frontier-propagation argument is also valid as a topological observation. However, the main conditional pinning results depend on the false general assertion in the extended-projection lemma (Section 5.1, lines 231–250) that the product structure of the open stratum canonically determines a projection on every deeper stratum, independently of the contraction to the original graph. For example, a chain of three genus-one components can specialize a separating genus-one/genus-two curve in two ways: smoothing either of its two nodes leaves a different end component as the genus-one factor. The original genus-one/genus-two stratum has no graph symmetry interchanging its factors, yet its projection has different limits along these branches. One must retain a chosen contraction or work on the appropriate normalization; the claimed global map is not supplied by clutching and stabilization. This undermines the conditional proofs as written, quite apart from the unproved extra conditions (F) and (F'). The local moving example in Section 8 is honestly distinguished from a moduli-space counterexample, although its purported strata are nested closed sets rather than disjoint strata; removing the deeper locus repairs that illustrative model. The assertion that the descended map is “not necessarily holomorphic” is not established by observing that a chosen trivialization is merely smooth. Thus the submission contains useful elementary partial observations, but also substantial errors, warranting a deduction below the rubric's score for an otherwise valid acknowledged partial solution. The exposition is clear, though disproportionately long. Attribution has a concrete moderate defect: the BDR bibliography entry (lines 742–745) gives incorrect authors and title for the arXiv identifier; the supplied author's bibliography identifies Frederik Benirschke, Benjamin Dozier, and John Rached, *The boundary of a totally geodesic subvariety of moduli space*.

Correctness: -0.5

Exposition: 1

Attribution: 0

## Solution Y

The principal claim is a counterexample to the problem, but the setup drops the essential assumption that the central surface is totally geodesic (line 41), and the plumbing construction never establishes that assumption for its central fiber. Consequently the proposed family in Section 2 cannot refute the stated theorem. It illustrates only that local product geometry without the Teichmüller-geometric hypothesis allows a moving constant factor. There is a second independent problem: lines 118–123 identify the closure in the entire Deligne–Mumford compactification with the result of allowing only the plumbing parameter to vanish. A small open curve or coordinate disk can also accumulate at the edges of the chosen chart, so this local construction does not establish the asserted global boundary or the hypotheses for every stratum. The sentence that compatibility is automatic does not verify the required global closure equalities. Section 3 gives a reasonable separate argument under the stronger assumption that all fibers are closed totally geodesic surfaces, using the cited finiteness theorem, but this does not address the given deformation. The central counterexample strategy therefore fails at the level of the hypotheses and makes no substantial progress on the requested assertion. The presentation is concise and easy to follow. Its references to plumbing and to Wright's finiteness result are essentially appropriate; the decisive defects are mathematical applicability and the omitted hypothesis, rather than fabricated attribution.

Correctness: -1

Exposition: 1

Attribution: 1

## Solution Z

The submission claims to reduce the theorem to a single additional Input (Mem) (lines 746–758 and Appendix B), but neither the unconditional theorem nor that conditional reduction is established. The acknowledged membership assumption already supplies an essential rigidity assertion absent from the proof, and numerous independent errors remain. The “Mukamel” theorem in Section 2.3 (especially lines 252–260) says that every totally geodesic subvariety of dimension at least two in genus at least three is empty, which is false even for the ambient moduli space itself. The treatment of differentials mixes projectivized and unprojectivized spaces: after quotienting by scaling in the setup, it repeatedly counts a nontrivial scaling fiber; the claimed dimensions and definition of a Teichmüller curve in Section 2.2 are incorrect. The holomorphicity lemma (lines 581–609) falsely infers an analytic total space from smooth triviality of analytic fibers and properness of a projection from closedness of its domain. Smooth families such as points with coordinate conjugate to the parameter disprove the former general assertion, and noncompact fibers disprove the latter. The residue argument (lines 773–855) incorrectly makes cylinder-modulus ratios rational consequences of intersection numbers; even fixed ratios alone do not fix an overall scale. Most decisively, the premise lemma (lines 964–1088) confuses a stratum with its closure and a union of larger strata, asserts that a curve is Zariski-dense in an analytic surface, and claims that a nonempty proper divisor cut on a surface is zero-dimensional. Its displayed identity even uses the closure of the empty set V_0 intersected with a boundary stratum. Hypothesis (ii) propagates an existing central-fiber equality; it does not prove this false central-fiber equality. Appendix A adds another explicit falsehood: a flat constant-degree family of zero-cycles with singleton support need not have fixed support, as the degree-one family consisting of the point t shows. These defects make the approach unsalvageable as submitted, despite the candid admission of Input (Mem). The long dependency structure, inconsistent notation, and contradictory assertions make the exposition followable only with serious effort. Attribution is seriously defective: false rigidity assertions are presented as Mukamel's theorem, and the “Mazor–Wright” reference is used to certify highly specific DM/MD-flow, “central Bers set,” and regeneration results without a credible supporting source. This is pervasive hallucination of theorem content, not merely incomplete bibliographic detail.

Correctness: -1

Exposition: 0

Attribution: -1

## Ranking and score table

**X > Y > Z.** X offers the most valid partial reasoning and clearly acknowledges incompleteness, despite substantive errors. Y is readable but its counterexample omits the essential hypothesis. Z has pervasive mathematical and attribution failures beyond its acknowledged gap.

| Solution | Correctness | Exposition | Attribution |
|---|---:|---:|---:|
| X | -0.5 | 1 | 0 |
| Y | -1 | 1 | 1 |
| Z | -1 | 0 | -1 |
