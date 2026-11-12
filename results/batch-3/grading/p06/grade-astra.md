# Referee reports

The intended convention is that an L-space knot lies in $S^3$, as in the author's solution. These reports use only the supplied files; external references have not been consulted. Locations refer to source-file line numbers. None of the three submissions supplies a valid complete proof of the requested affirmative answer.

## Solution X

The submission gives substantial relevant partial results: the nonintegral and cable-surgery cases, the Hom–Lidman–Zufelt restriction to slope $p=2g-1$, and the reduction of a hypothetical counterexample to two lens spaces and an integer homology sphere. Its principal error is the repeated assertion that the requested result is open (abstract, lines 24–28; final remark, lines 239–245). This is not merely an acknowledged incomplete proof: it incorrectly asserts that the remaining case cannot presently be settled. The missing argument is precisely the essential step of the supplied author's solution. If the three factors are $L(m,s),L(n,t),Q$, then $p=mn=2g-1$; fiberedness bounds the generator rank of the surgery group's commutator subgroup by $2g=p+1$, whereas the universal abelian cover and Grushko's theorem give rank at least $(m-1)(n-1)+2p>p+1$. Thus the alleged open configuration is impossible, without proving the Cabling Conjecture. There are additional defects: the proof of the three-summand theorem (lines 111–117) does not follow from its stated inputs, since bounds on lens-space and homology-sphere factors do not exclude other rational-homology-sphere factors. The conclusion is a legitimate external result, but this purported synthesis omits a necessary structural input. The cable corollary (lines 177–184) also fails to establish that the companion filling is nontrivial; as stated, taking the companion to be the unknot and $q=1$ gives an $S^3$ factor and only one nontrivial prime summand. Finally, the unrestricted reducibility statements at lines 78–100 overlook zero surgery on the unknot, which gives $S^1\times S^2$. The exposition is clear and organized despite these mathematical defects. Attribution is extensive and correctly identifies the crucial HLZ result, but the unsupported attribution of the three-summand synthesis and failure to identify the essential fibered-knot rank argument warrant a moderate penalty. A substantial new final argument is required, so the correctness score is below the rubric's category for a merely acknowledged partial result.

Correctness: -0.5

Exposition: 1

Attribution: 0

## Solution Y

The answer “yes” is correct, but the proof rests on a fabricated stronger input: the claimed “cabling conjecture for L-space knots; Ni–Wu, completed by Z. Wu” (lines 136–140, with attribution at 150–158). This input already supplies the entire difficult classification and is invoked at the decisive steps of the main proof (lines 348–360). The submission therefore does not establish either the requested bound or its stronger assertion that every reducible surgery on a nontrivial L-space knot is a cabling surgery (lines 61–67). The purported explanatory mechanism makes the problem especially evident. The Schubert–Scharlemann statement at lines 397–405 posits a closed surface representing a nonzero class in $H_2(E(K);\mathbb Z)$, but this homology group is zero for every knot exterior in $S^3$, independently of the L-space condition. That alternative would make the claimed reduction prove the full Cabling Conjecture immediately. The “Ni–Wu genus bound” at lines 440–450 is directly false: the submission's own right-handed trefoil example has genus $1$ and reducible slope $6$, so its asserted inequality becomes $1/2>5/2$. The grading-shift discussion at lines 453–471 neither defines the needed quantities nor provides a valid deduction; a strict contradiction is even described as forcing equality. Calling this sketch optional does not validate the central externally asserted theorem. There is nevertheless substantial correct partial mathematics: the homology calculation, treatment of nonintegral surgery, fibered zero surgery, and cable-surgery decomposition support the bound in those special cases. These merits justify -0.5 rather than dismissing the entire submission as making no progress, but repairing the main proof requires replacing its central argument. Minor additional problems include identifying orientation reversal of a knot with mirroring (lines 94–95), and stating the positive L-space slope characterization without the nontrivial-knot exception (lines 97–99). The main case analysis is readable and well organized, so exposition receives credit independently of correctness. Attribution receives the lowest score because a decisive nonexistent theorem and demonstrably false supporting theorems are explicitly presented as established work of named authors.

Correctness: -0.5

Exposition: 1

Attribution: -1

## Solution Z

The submission establishes useful conditional and special-case results: assuming the Cabling Conjecture, the cable decomposition gives at most two summands; its positive trefoil-cable examples correctly demonstrate sharpness; and it records the relevant three-summand structure and HLZ slope restriction. The discussions of local knots and knots in other ambient manifolds are explicitly scoped but do not answer the intended $S^3$ question. For that question the submission stops at a conditional theorem and incorrectly treats the three-summand configuration as a remaining open possibility (abstract, lines 29 and 51; “Open points,” lines 574–576). The missing obstruction is substantial: fiberedness gives commutator generator rank at most $2g$, while a splitting $L(m,s)\#L(n,t)\#Q$ at $mn=2g-1$ forces rank at least $(m-1)(n-1)+2mn>2g$. This rules out exactly the configuration that Section 7 leaves unresolved. Several quoted results also have incorrect hypotheses. The Bodish–DeYeso lemma (lines 327–339) allows $Z=S^3$; positive $5$-surgery on the genus-one trefoil, explicitly used elsewhere in this submission, then contradicts $r\le g(K)$. A nontriviality hypothesis is indispensable, though the later application does have a nontrivial remaining factor. The Howie bridge lemma (lines 343–351) is false for surgery merely containing two lens summands: $6$-surgery on the trefoil has lens orders $2,3$ and bridge number $2$, contradicting $2+3\le2+1$. A hypothesis concerning a three-summand situation cannot be omitted. The Hedden lemma (lines 221–226) likewise needs a positive L-space surgery assumption on the companion, rather than an unspecified lens-space surgery: mirroring the trefoil exposes the missing sign condition. Finally, Property R alone does not assert irreducibility of all nontrivial-knot zero surgeries, as claimed at line 212; the stronger consequence of Gabai's work requires the appropriate statement. The writing is lengthy but clear, and it carefully labels the conditional ambient example. Attribution is broadly present, but multiple incorrectly stated cited theorems and bibliographic errors (including the initials of Short and Zufelt) merit a moderate penalty. These are not merely missing details in an otherwise complete solution.

Correctness: -0.5

Exposition: 1

Attribution: 0

## Ranking and score table

**X > Z > Y.** X gives the most direct relevant reduction with fewer extraneous claims. Z contains useful additional partial results but more misstated inputs and substantial material outside the intended scope. Y ranks last because its claimed complete proof depends on a fabricated theorem and its supporting account contains explicit contradictions. Equal numerical scores for X and Z reflect the rubric's coarse categories.

| Solution | Correctness | Exposition | Attribution |
|---|---:|---:|---:|
| X | -0.5 | 1 | 0 |
| Y | -0.5 | 1 | -1 |
| Z | -0.5 | 1 | 0 |
