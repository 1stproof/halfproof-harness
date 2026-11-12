# Referee reports

These reports use only the supplied files. The general answer is **no**: the author's solution constructs a fusion tract with the prescribed ternary relation but without the six-term relation, then completes it to a hyperfield while preserving null relations of length at most seven. Thus proving positive special cases does not answer the competition problem. The rubric's score of 0 applies to sound, explicitly acknowledged partial results; additional substantive mathematical errors can warrant a negative score. External references have not been independently checked, and an unverified reference is not treated as a hallucination merely because its source is unavailable here.

## X

X gives a correct, clearly delimited partial solution for quotient hyperfields. The lifting lemma in Section 2 correctly chooses representatives whose ordinary sum is zero, including when a representative is zero. The classical ring identity then supplies representatives of all six conclusion terms whose sum is zero; projecting this equality proves the theorem in Section 4 (lines 113–154). There is no illicit cancellation or assumption of double distributivity. The final remark (lines 158–165) explicitly recognizes that non-quotient hyperfields exist and that the theorem does not settle the general question. Consequently, the argument is essentially flawless within its stated scope, but it supplies neither a general proof nor the counterexample required for a complete answer; the appropriate competition score is 0 for acknowledged partial progress. The exposition is concise and easy to follow. Krasner's construction and Massouros's non-quotient examples are appropriately credited, consistently with the author's bibliography; the additional axiomatic reference is not needed to bridge any substantive proof gap.

Correctness: 0

Exposition: 1

Attribution: 1

## Y

Y establishes two substantial positive special cases: the quotient-hyperfield theorem in Section 3 follows correctly by lifting representatives and using the cubic identity, and the finite ample theorem in Section 4 follows correctly from the intersection-counting lemma and propagation of all nonzero elements through subsequent sums. Its zero-coordinate argument is also correct. Section 5 gives a valid exhaustive parametrization of small hyperfield tables, but the asserted execution results (especially lines 886–922 and Table 1) are reported without code, tables, or certificates, so the precise enumeration and hypothesis counts are not independently substantiated by this submission. The author's appendix supports the qualitative conclusion for these small orders, but does not verify Y's particular counts or its claimed computation. The repeated statement that the unrestricted problem “remains open” (abstract, introduction, and lines 949–955) is inaccurate in the context of the supplied counterexample: what the submission establishes is that it has not resolved the problem. The invented UNION predicate and its correct counterexample concern a different premise and do not provide a negative answer to the actual question. Overall this is an explicitly partial solution with sound substantive special-case proofs, warranting 0 rather than credit for a complete solution; its unsupported computational details and inaccurate status statement should be corrected. Despite considerable repetition and an unnecessary convention discussion, its mathematical arguments are clearly organized and readable. The counting lemma and classification comparisons are credited to identifiable references. The bibliography is sparse, particularly the bare arXiv entry for ampleness, but that identifier agrees with the Hobby paper listed in the author's bibliography; the permitted evidence does not establish hallucinated attribution.

Correctness: 0

Exposition: 1

Attribution: 1

## Z

Z explicitly acknowledges partial progress and gives valid core arguments for the zero-product case and the singleton-pair-sum case (Sections 2–3). Its binomial expansion gives an inclusion, and the singleton hypothesis correctly makes the mixed-term regrouping exact. However, there are substantive errors beyond incompleteness. In the free-propagation lemma, lines 310–311 incorrectly identify the negative of C ⊞ w with C ⊞ (−w); the correct negative is (−C) ⊞ (−w), leaving the claimed cardinality conclusion for C ⊞ (−w) unproved. Lines 324–325 also wrongly suggest that reversibility requires singleton pair sums, whereas axiom (H4) has no such restriction. The normalization contains readily repairable scaling errors: the product scales by λ³, not λ (the collapse corollary), and line 352 should give −xy/z² = −ab, not −xy = −ab. More seriously, the “zero-trap” discussion in Section 5 does not establish its asserted obstruction. Its claim that the displayed F₁₉/{±1} example has no paired realization (lines 394–402) is false under its own definition: writing [r] for a coset, the target terms are [8], [8], [1], [6], [6], [6]; choose 0 from [8] ⊞ [8], choose [7] from [6] ⊞ [6], and choose [7] from the remaining pair [1] ⊞ [6]. Since −[7] = [7], these choices give precisely a paired realization with t = [7] and u = [1] ⊞ [6]. The Elevation Problem is only a normalization of the unresolved original question, not an independent sufficient lemma that has been proved; the author's counterexample also refutes the submission's final conjectural affirmative answer. Correct special cases survive, but the claimed structural analysis needs substantial correction or removal, justifying −0.5 rather than the clean partial-result score. The later undefined “substitution calculus,” misleading zero classification, and lengthy failed-route commentary make the overall exposition difficult to assess, although the early proofs are readable. No external nonstandard theorem is used without attribution; the uncited numerical experiments are unsupported evidence, not by themselves proof of plagiarism or fabricated references.

Correctness: -0.5

Exposition: 0

Attribution: 1

## Ranking and scores

**Y > X > Z.** Y proves a broader range of sound special cases than X, although X is cleaner and more restrained; neither resolves the problem. Z has valid partial results but also substantive erroneous assertions. The coarse rubric gives Y and X identical numerical scores.

| Solution | Correctness | Exposition | Attribution |
|---|---:|---:|---:|
| X | 0 | 1 | 1 |
| Y | 0 | 1 | 1 |
| Z | -0.5 | 0 | 1 |
