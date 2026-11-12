# Referee report: problem p05 (cubic relation in hyperfields)

Reference point: the author's solution answers the question **negatively**. It builds a tract with fusion in which `x+y+z` is null but `x^3+y^3+z^3-xyz-xyz-xyz` is not, then closes it under fission (adding new elements) while proving that no new null posinomial of length at most 7 is created, and finally invokes Souza's theorem (tract with fusion and fission = hyperfield). The author also records that the implication *does* hold for every quotient hyperfield `F/G`, for every perfect hyperfield, and for every hyperfield of size at most 13. So any submission that proves the implication in a special class is consistent with the truth, but any submission that argues the answer is "yes" in general is wrong, and none of the three submissions reaches the actual answer.

---

## Solution X

**What is proved.** X proves the implication for Krasner quotient hyperfields `K/T` (`K` a field, `T ≤ K^×`). The argument is correct and short: `0 ∈ x ⊞ y ⊞ z` gives `-z ∈ x ⊞ y` (uniqueness of inverses), which unwinds to representatives `a', b', c'` of the three cosets with `a'+b'+c'=0` in `K` (Lemma lem:lift); the field identity `a'^3+b'^3+c'^3-3a'b'c' = 0` then holds, and choosing multiplier `t=1` at every step of the six-fold hyperaddition shows the class of that ordinary sum, namely `0`, lies in the hypersum (Theorem thm:quotientcase). The claim that `[a_1]⊞...⊞[a_n]` contains `[a_1+...+a_n]` is used without a separate lemma but is immediate from the definition and associativity.

**What is claimed vs. established.** The abstract and the final Remark state explicitly that non-quotient hyperfields exist (Massouros) and that the theorem "does not, by itself, settle the implication for arbitrary hyperfields." No answer to the posed question is offered. This is an acknowledged partial result covering exactly the class the author's appendix lists as a non-counterexample class. No false statements found. The side remark that the sign and tropical hyperfields are quotient hyperfields is correct.

**Correctness: 0**
**Exposition: 1**
**Attribution: 1**

---

## Solution Y

**What is proved.** Y proves three positive special cases under a "left-fold" reading of iterated hypersums (which, given associativity, is the standard reading, so the convention discussion is a non-issue): (1) the implication for every quotient hyperfield `F/G` (Theorem thm:quotient), by the same lifting argument as X, preceded by a long but correct verification that `F/G` is a hyperfield and an unfolding lemma (Lemma lem:quotient-unfolding) that `nest([w_1],...,[w_n]) = {[Σ h_i w_i] : h_i ∈ G}`; (2) the implication for finite "ample" hyperfields, defined by `m + k > |H^×|` where `m = min |(w⊞1)\{0}|` and `k = min |{u : w ∈ u⊞1}|` (Theorem thm:ample). The counting lemma `H^× ⊆ (x⊞y)⊞z` for nonzero `x,y,z` (Lemma lem:counting) and the propagation lemma `H^× ⊆ H^× ⊞ c` are correct, and the deduction `S_2 ∋ ab`, hence `0 ∈ S_2 ⊞ (-ab)`, is correct; the zero-coordinate case is handled separately and correctly. (3) An asserted exhaustive computer enumeration of all hyperfields of order ≤ 7 (334 isomorphism classes, 51176 hypothesis triples, zero failures; Theorem thm:small-orders). Section 2 also contains a digression (Proposition prop:union-fails) showing that a non-standard "pairwise overlap" premise would make the implication false; this is a strawman and adds nothing.

**Gaps.** The enumeration in Section 5 is presented as a theorem but no code, no tables of hyperfields, and no independent check are given; the reader cannot verify the class counts (2, 5, 7, 27, 16, 277) or the "zero failures" claim from the text. It is consistent with the author's own computation (positive answer for all sizes ≤ 13), so I do not treat it as a wrong statement, but it is not a proof as written. The abstract and Section 6 state plainly that "the unrestricted problem for arbitrary commutative hyperfields remains open." No answer to the posed question. No false mathematical statements found in the proved parts.

**Attribution issues.** The bibliography is deficient: entry `[ample]` is just "arXiv:2507.18908v1" with no author or title (the author's own bibliography shows this number is Hobby, "Blocks in finite hyperfields"), and `[Linzi]` is "Linzi, arXiv:2608.03625v1" with no title. "Lemma 3.1 of [ample]" and "agrees with the independent count in [Linzi]" cannot be checked from the text. The Liu citation is a real paper and is used appropriately.

**Correctness: 0**
**Exposition: 1**
**Attribution: 0**

---

## Solution Z

**What is proved.** Working purely from the hyperfield axioms (H1)–(H4), Z proves: (a) the degenerate case `xyz = 0` (Proposition prop:degen), correctly; (b) a scalar-invariance lemma (Lemma lem:scale), correctly; (c) the "cubing a pair sum" inclusion `(a⊞b)^{·3} ⊆ a^3 ⊞ (a^2 b)^{⊞3} ⊞ (ab^2)^{⊞3} ⊞ b^3` (Lemma lem:atoms (A2)), correct and the one genuinely useful tool in the paper; (d) the "collapse case": if one of `x⊞y`, `x⊞z`, `y⊞z` is a singleton then the implication holds (Lemma lem:collapse, Corollary cor:collapse). The collapse proof is correct: after rescaling to `X ⊞ Y = {1}`, the six mixed terms regroup as `(X^2Y ⊞ XY^2)^{⊞3} = {XY}^{⊞3}`, so the witness `u` produced by (A2) lies in `(XY)⊞(XY)⊞(XY)` and `0 ∈ X^3 ⊞ Y^3 ⊞ (-1) ⊞ (XY)^{⊞3}` follows. This is a small but real unconditional result not contained in X or Y.

**What is claimed vs. established.** The remaining "free case" is restated as an "Elevation Problem" (E): `1 ∈ a⊞b ⇒ 0 ∈ a^3 ⊞ b^3 ⊞ (-1) ⊞ (ab)^{⊞3}`. Z presents the equivalence of (E) with the free case as a reduction (Theorem thm:main, "conditional form"), but (E) is literally the original question with `z = -1` after the scaling every submission uses; the "reduction" has no content, and the paper's framing obscures this. Z states that it "does not assert the unrestricted answer 'yes' as proved," but in Remark rem:honest(f) records "the author's judgement ... is that it is true in every hyperfield." That judgement is wrong: the author's solution exhibits a hyperfield where the implication fails. Since it is flagged as a non-proof opinion I do not dock correctness for it beyond the partial-result score, but it points the reader in the wrong direction. Sections 4–5 (Remark rem:gap, Remark rem:zerotrap, the "atlas of failed routes") are narrative about unsuccessful attempts, finite-model experiments, and a "review history" of an earlier withdrawn draft; none of this is mathematics that a referee can assess, and the finite-model claims (e.g. the `F_19/{±1}` computations) are offered explicitly as not-proof-steps. Spot-checking: the `F_19/{±1}` example (`8+11 = 19`, `2^3 = 3^3 = 8 mod 19`, `17+3 = 1`) and the `F_5` counterexample to the "H4-collapse" conditional (`2+4 = 1`, `1-3 = 3 ≠ 4`) are both arithmetically correct. No false proved statement found; one minor inconsistency (Remark prob:elevation is cited as "Proposition").

**Exposition.** The proved parts are clear, but roughly half of the document is meta-commentary ("Problem 5 -- [system]" title, "this round," "review history," withdrawn claims, lists of failed routes), and the central "reduction" to (E) is presented as structural progress when it is a change of variables. A reader has to do real work to separate what is established (degenerate and collapse cases) from what is rhetoric.

**Correctness: 0**
**Exposition: 0**
**Attribution: 1**

---

## Ranking

Ranking rule: first by how much of the question is settled correctly (none settle it; all are acknowledged partial positives), then by cleanliness and verifiability of what is proved, then by attribution.

1. **X** — smallest scope (quotient hyperfields only) but every line is correct, verifiable, and the limits of the result are stated honestly with correct references.
2. **Y** — widest scope (quotient, finite ample, computer-checked orders ≤ 7) and the proved parts are correct, but the enumeration is unverifiable from the text and two of the three bibliography entries lack author or title.
3. **Z** — proves a genuine small new case (collapse) from the axioms, but dresses a change of variables as a reduction, fills half the document with accounts of failed attempts, and voices the wrong conjecture ("true in every hyperfield").

X and Y are close; Y proves strictly more but reports it less verifiably.

| Solution | Correctness | Exposition | Attribution |
|---|---|---|---|
| X | 0 | 1 | 1 |
| Y | 0 | 1 | 0 |
| Z | 0 | 0 | 1 |
