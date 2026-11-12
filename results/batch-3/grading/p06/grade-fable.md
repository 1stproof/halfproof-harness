# Referee report, problem 6

**Problem.** Does a 3-manifold obtained by Dehn surgery on an L-space knot have at most two prime summands?

**Reference answer (author).** Yes. Reducible surgery forces an integral slope and a hyperbolic or cable knot; cables give at most two summands; three summands force the shape `L(m,s) # L(n,t) # Q` with `Q` a homology sphere and `p = mn`. For a hyperbolic L-space knot Hom–Lidman–Zufelt force `p = 2g-1`. The universal abelian cover of `L(m,s) # L(n,t) # Q` is `#_{(m-1)(n-1)} S^1xS^2 # (#_{mn} Q)`, whose fundamental group has rank at least `(m-1)(n-1) + 2mn` by Grushko. L-space knots are fibered (Ni), so the commutator subgroup of the knot group is free of rank `2g = mn+1` and surjects onto that group; `(m-1)(n-1) + 2mn <= mn+1` is impossible for `m, n >= 2`. I checked each step of the author's argument and it is correct.

The crux of the problem is therefore the hyperbolic case. None of the three submissions resolves it.

---

## Solution X

**Review.** X states the question and asserts that it is open for hyperbolic L-space knots, calling it a special case of the "Two-Summands Conjecture" and declining to attempt it. What X actually proves: (a) non-integral surgeries are prime (Gordon–Luecke); (b) any reducible surgery has at most three prime summands, and three summands force `L_1 # L_2 # Y` with `Y` a homology sphere (quoted from Sayari, Valdez-Sánchez, Howie); (c) every surgery on a torus or satellite L-space knot has at most two prime summands (Moser, Scharlemann, and the cabling-slope splitting `L # S^3_{q/p}(J)`); (d) for a hyperbolic L-space knot of genus `g` any surgery with more than two summands is `S^3_{2g-1}(K) = L_1 # L_2 # Y` (Hom–Lidman–Zufelt). Items (a)–(d) are correct and correctly argued, but they are the routine reduction; X stops exactly where the problem starts and makes no attempt at the rank/covering argument that finishes it. The statement in the abstract and in Remark 5.2 that the hyperbolic case "is currently open" is wrong: the author's proof uses only results available in the literature. The mathematics X does write contains no error. One attribution error: the lens-space-summand theorem (Lemma 2.2) is cited to Gordon–Luecke's "Knots are determined by their complements" (JAMS 1989), which is a different paper; the correct source is Gordon–Luecke, "Reducible manifolds and Dehn surgery", Topology 35 (1996). Gabai and Krcatovich appear in the bibliography but are never cited.

Correctness: 0
Exposition: 1
Attribution: 0

---

## Solution Y

**Review.** Y claims a complete affirmative answer. The structure is: non-integral slopes are prime (Gordon–Luecke); 0-surgery on a fibered knot is a surface bundle, hence prime; for an integral slope, a reducible surgery forces `K` to be a cable at its cabling slope, and the cabling-slope splitting `S^3_{uv}(C_{u,v}(C)) = S^3_{v/u}(C) # L(u,·)` (Lemma 2.4, proved correctly and carefully) gives exactly two summands. The whole argument rests on "Theorem 1.6 (The cabling conjecture for L-space knots; Ni–Wu, completed by Z. Wu (2014))": every reducible surgery on an L-space knot is a cabling surgery. No such theorem exists. The cabling conjecture is open for hyperbolic L-space knots; Ni–Wu's "Heegaard Floer correction terms and rational genus bounds" (Adv. Math. 267, 2014) is a real paper but proves nothing of the kind, and the "completion by Z. Wu (2014)" is invented. With this input removed, Y has proved only the non-hyperbolic case, i.e. the same reduction as X, and the approach cannot be repaired without proving an open conjecture, so it is unsalvageable as a route to the hyperbolic case. Section 5, offered as the "mechanism" behind Theorem 1.6, contains further false statements: Theorem 5.1 (attributed to Schubert and Scharlemann) asserts that a non-cabled reducible surgery forces a closed essential surface `F` with `0 != [F] in H_2(E(K); Z)`, but `H_2(E(K); Z) = 0` for every knot exterior in `S^3` by Alexander duality, so the stated alternative is vacuous and the "theorem" would prove the cabling conjecture outright; the "Ni–Wu genus bound" (Theorem 5.2) and the inequality chain in Remark 5.3 are not meaningful statements. "Calegari–Gordon proved that the infimum is attained and equals `g - 1/2`" is also garbled. The correctly proved parts (Lemmas 2.1–2.4, Cases 1–2) are clearly written, with hypotheses stated where used, and the splitting lemma's identification of the closed summand via the torus-knot case is a nice touch; but the paper's main claim is false as a proof. There is no bibliography; sources are named inline only.

Correctness: -1
Exposition: 1
Attribution: -1

---

## Solution Z

**Review.** Z does not answer the question; it scopes it into three "readings" and treats each. Reading B (local knots in reducible ambient manifolds, Prop. 3.1) and Reading C (a regular fiber of the Poincaré sphere, using Caudell's arXiv:2101.01256 surgery to `L(2,1) # L(3,2) # L(5,4)`, Thm. 8.3) are outside the question as normally understood, and Z says so in §9(4); they are correct but irrelevant padding. For the actual question (Reading A, knots in `S^3`) Z proves: at most two summands assuming the Cabling Conjecture (Thm. 4.2, with a correct treatment of the companion summand and of 0-surgery via Gabai); sharpness via Hedden's cabling theorem applied to cables of the trefoil (Thm. 5.3, correct); the unconditional bound three with the shape `L(l_1,q_1) # L(l_2,q_2) # Y` (Howie); and two residual constraints on a hypothetical hyperbolic counterexample: the reducing slope `2g-1` is not a prime power (Thm. 7.5, via a cyclic-homology argument plus a quoted bound "fibered `K`, `S^3_r(K) = L(r,a) # Z` with `Z` a homology sphere implies `r <= g`", attributed to Bodish–DeYeso), and the three-summand shape has `l_1 l_2 = 2g-1` with bridge number at least `l_1 + l_2 - 1 >= 7` (Thm. 7.6, via Howie's bridge-number bound). All of these are correctly argued from the quoted inputs. The quoted `r <= g` bound is exactly the González-Acuña–Short universal-abelian-cover estimate specialized to one lens-space summand; applying the same estimate to the two-lens-space shape in Thm. 7.6 finishes the problem in three lines, so Z assembled every ingredient of the author's proof and did not take the last step. Minor attribution faults: "W. Short" (should be H. Short), "M. Zufelt" (N. Zufelt) in two entries, several references given only by arXiv number without title, the cabling-slope splitting attributed to Howie rather than to Gordon, and the Bodish–DeYeso and Meier citations could not be checked from the files provided (the statements attributed to them are true). The dependence on Caudell's abstract is flagged explicitly, which is good practice.

Correctness: 0
Exposition: 1
Attribution: 1

---

## Ranking

1. **Z** — correct conditional theorem plus genuine unconditional constraints on the hyperbolic case; honest about what is open; attributions essentially right.
2. **X** — correct but minimal reduction; wrongly declares the hyperbolic case open; one misattributed key citation.
3. **Y** — claims a full proof resting on a fabricated theorem equivalent to an open conjecture, plus a false "Scharlemann" theorem.

Ranking rule: by how much of the hyperbolic case is established, then by the number of false statements.

| Solution | Correctness | Exposition | Attribution |
|---|---|---|---|
| X | 0 | 1 | 0 |
| Y | -1 | 1 | -1 |
| Z | 0 | 1 | 1 |
