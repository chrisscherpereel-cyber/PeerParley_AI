# How PeerParley grades — every algorithm explained

All grading lives in `peerparley/grading.py` (`compute`). Defaults are chosen so an
instructor can grade without changing anything. Every input below is optional: with
only the $100 allocation and comments (e.g. a plain Qualtrics export) the engine
still works — the rating/ranking fields are simply left blank.

## Notation
- **n** — team size.
- **received** — the dollars a student was given by *each* teammate.
- **received_avg** — the mean of those.
- **team_avg** — the mean of every member's `received_avg` on that team.
- **team_score** — the grade the team starts at (default **100**).

## 1. Pay grade — the core signal
```
pay_grade = received_avg / team_avg          # 100% = exactly the team average
```
Above 100% → teammates valued this student more than average; below → less. Because
it is measured against the team's **own** average, self-allocations or blank answers
can never push a whole team below 100%.

## Peer-adjustment methods (choose one on the *Grading rules* tab)

Every method produces a **normalized peer factor** — a student's peer score ÷ the
team average, so **1.0 (100%) = average**. The grade adjustment then applies the
same way (§2). These are the established, research-validated approaches; they
differ only in *which* peer signal is used.

| Method | Peer signal used | Basis in the literature |
|---|---|---|
| **Points shared** *(default)* | the $100 allocation | WebPA normalized weighting factor (Loughborough; Loddington & Pond et al., 2009) and the CATME/autorating factor (Ohland et al., 2012) — both = individual score ÷ team average. |
| **Rating factor** | mean of the four 1–7 statements | CATME "adjustment factor" computed from Likert competency ratings (Ohland et al., 2012); Likert ratings are often more reliable than a forced dollar split. |
| **Forced-ranking tiers** | High / Adequate / Low ranking | Rank/tier methods, robust to differences in how strictly individuals rate. |
| **Combined** | average of points + ratings | Triangulating two independent signals reduces the influence of any single noisy one. |

**The weighting fraction.** All methods feed the same formula, which is
algebraically the WebPA/SPARK weighting: `individual = group × [(1 − w) + w ×
factor]`, where **w is the sensitivity B**. So B directly limits how much peers
can move the grade (SPARK: Freeman & McKenzie, 2002).

**Correct for rater leniency (optional).** Ticking this z-scores each evaluator's
allocations before comparing students, removing systematic easy-grader /
hard-grader effects — a standard reliability improvement for peer assessment.
Applies to the points and combined methods.

**All are optional and degrade gracefully:** if a method's signal wasn't
collected (e.g. no ratings on a plain allocation export), the engine falls back to
the points allocation automatically.

## 2. Grade adjustment (the ± %)
```
multiplier      = 1 + B · A · Q · (pay_grade − 1)      # clamped to [1−cap, 1+cap]
individual      = team_score · multiplier
grade_Δ (signed_pct) = individual − team_score         # rounded to your step
```
- **Direction is guaranteed:** `pay_grade > 1` → **bonus (+)**, `< 1` → **deduction
  (−)**, `= 1` → **0**. `A` and `Q` are both ≤ 1, so they can only *shrink* the
  adjustment — never flip its sign.
- **B (sensitivity, default 0.5)** — how strongly peer input moves the grade.
- **cap (default ±15%)** — the most any grade can move (set as *Maximum adjustment*).

*Example:* team average $40; a student averaging $70 received → pay grade 175% → a
bonus; one averaging $20 → 50% → a deduction; one at $40 → 100% → no change.

## 3. Agreement weight A (evaluator consensus)
```
A = 1.00 if SD(received)/expected_share ≤ 10%
    0.75 if ≤ 20%
    0.50 if ≤ 30%
    0.25 otherwise         (expected_share = 100/(n−1))
```
If evaluators strongly **disagree** about a student, the signal is noisier, so their
adjustment is softened. (`A = 1` with fewer than two data points.)

## 4. Comment support Q (do the words back up the dollars?)
From `comments.py`, a 0–1 score for the comments *written about* a student, based on
word count ≥ 25, characters ≥ 150, ≥ 2 sentences, unique-word ratio ≥ 0.45, ≥ 2
contribution keywords, with penalties for repetition and copy-paste across
teammates. **With no comments, Q is neutral (1.0)** so the adjustment isn't
dampened for a reason outside the student's control.

## 5. Feedback points (response quality)
The same comment scorer is applied to the feedback a student **wrote** about their
teammates; `points = round(Q_author × max_points)` (default max **5**). This rewards
writing serious, specific feedback — separate from the grade adjustment.

## 6. Dimension letter grades
Each of the four rating statements (Team Player, Quantity, Quality, Effect) is
averaged and divided by 7 → a 0–1 score → a letter via `GRADE_SCALE`
(F … A). Shown to the student so they see *where* they're strong or weak.

## 7. Performance (High / Adequate / Low)
- **Forced ranking** when the survey collected it: mean of the 1/2/3 ranks a student
  received → `(3 − mean)/2` → **High ≥ 0.8**, **Adequate ≥ 0.4**, else **Low**.
- **Otherwise** the average-based label: above/below the team average by a set band
  (default ±8%). The *agreement guard* softens a forced "Low" to "Expected" when
  evaluators disagreed a lot (`A ≤ 0.5`).

## 8. Self-evaluation
The student's own ratings of themselves are graded the same way and shown next to
the peer grades on their feedback sheet, so they can see the gap.

---
The instructor controls the two settings most people touch — **sensitivity B** and
the **maximum adjustment ±%** — on the *Grading rules* tab, with everything else
under *Advanced options*. What each student *sees* on their PDF is controlled
separately under *Set up survey → What students see in their feedback report*.

## What changed in 2.9.0, and why

Four behaviours here produced numbers that were wrong or disagreed with
themselves. Each was reproduced on synthetic sections before being changed.

**A student nobody evaluated keeps the team score.** An empty list of received
ratings used to average to `0.0`, which is not "no data" but "every teammate
gave this student nothing": multiplier 0.50, −50% on the team score, and a zero
dragged into their team's average, lowering it for everyone else. Such a student
is now excluded from the team average, their multiplier is exactly 1.0, their
performance reads "No data", and tab ④ names them so the instructor can chase
the missing evaluations.

**The $100 goes to teammates, not the whole team.** The expected even share has
always been `100/(n−1)`, so the money was always meant for the other members.
The student form asked for a split across everyone including the student
themselves, required the total to be $100, and then discarded the
self-allocation — which meant a student who kept $40 for themselves distributed
only $60 and lowered every teammate for free. The form no longer offers a box
for yourself.

**Rounding decides the grade, not just the label.** `Round adjustments to 1% /
5%` used to round only the displayed delta while the stored score stayed exact,
so a student could be shown "+5%" against a gradebook figure of 104.44. The
rounded delta is now applied: the report and the score are the same number, and
the cap is re-applied afterwards so nothing can exceed the configured limits.
One consequence is worth planning around — in 5% steps any adjustment smaller
than ±2.5% becomes no change at all, which in a typical section is most
students. Use 1% steps if you want smaller differences to reach the gradebook.

**Correcting for easy and harsh graders stays in dollars.** The old
implementation z-scored each evaluator and then used the standard score where a
ratio was expected, so the adjustment moved onto a different scale entirely: in
testing, one extreme rater swung a teammate from +0% to −35% and reordered the
team. Each rater's allocations are now standardised and mapped back onto the
team's own pooled mean and spread. A rater who splits $40/$35/$25 and one who
splits $34/$33/$33 then carry the same weight, the numbers stay dollars, and the
correction is a no-op when every rater spreads their money the same way. A rater
with no spread at all (everyone the same amount) counts as neutral rather than
as a verdict.

A fifth change is cosmetic but visible: the B−/B boundary was 0.78, which made
B− twice as wide as every other band and B a 0.03 sliver. It is 0.71, restoring
the uniform 0.07 bands. This affects the letter shown beside a rating dimension,
not the grade adjustment.
