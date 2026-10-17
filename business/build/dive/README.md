# Drillion question bank spec

Drillion is an HSC Business Studies (NSW) study game modelled on **Krillion**, a daily word game.
In Krillion every prompt is an open category ("Name a cheese"). The player types ONE answer.
Any valid answer scores, but common answers score little (10 points) and rare, sophisticated
answers score a lot (up to 100). Each point takes the player 10 m deeper. After answering, the
game reveals every valid answer with its points, so the reveal is where the student learns.

In Drillion the player digs beneath the NESA building. Each topic (Operations, Marketing,
Finance, Human Resources) has its own bank of prompts, one JSON file per topic in this folder.

## Building the game

Edit the bank JSON files in this folder (or the game source in `../drillion_src.html`), then run
from the repo root:

    python3 business/build/build_dive.py

That validates all four banks and writes `business/drillion.html`.

## Source

The banks were written from Luca's Quizlet flashcard exports (one PDF per topic). All answers are
grounded in those flashcards, tidied for wording, with syllabus knowledge used only to fix an
obvious error or add a synonym. Each prompt is tagged with its syllabus section from the `SYL`
list in `../build_biz.py`.

## Output

Write a single JSON file:

```json
{
  "topic": "finance",
  "prompts": [
    {
      "id": "fin-ratios-profit",
      "sec": "P",
      "q": "Name a profitability ratio",
      "hint": "Three of them: gross, net and return on ...",
      "learn": "Profitability ratios show how well the business turns sales and equity into profit. Formulas: GP ÷ sales, NP ÷ sales, NP ÷ total equity.",
      "answers": [
        {"a": "Net profit ratio", "p": 15, "k": ["net profit", "np ratio", "net profit margin"], "why": "Net profit ÷ sales: profit kept from each dollar of sales after all expenses."},
        {"a": "Gross profit ratio", "p": 30, "k": ["gross profit", "gp ratio", "gross profit margin"], "why": "Gross profit ÷ sales: profit after cost of goods sold."},
        {"a": "Return on equity", "p": 75, "k": ["roe", "return on equity ratio", "return on owners equity"], "why": "Net profit ÷ total equity: the return owners earn on their investment."}
      ],
      "near": [
        {"k": ["current ratio", "liquidity ratio"], "p": 5, "msg": "That's a liquidity ratio, not a profitability one."}
      ]
    }
  ]
}
```

Field rules:

- `id`: unique, kebab-case, prefixed with the topic code (`ops-`, `mkt-`, `fin-`, `hr-`).
- `sec`: syllabus section letter — `R` Role, `I` Influences, `P` Processes, `S` Strategies
  (HR also has `E` Effectiveness for the CLAW ABC indicators).
- `q`: the prompt, written as an open category the way Krillion does it, short (ideally under
  70 characters). Good starters: "Name a…", "Give an advantage of…", "Give a disadvantage of…",
  "Name a stakeholder in…", "Name a step in…", "Give an example of…", "Name a strategy to…",
  "Name a feature of…", "Name an effect of…". Every prompt must have SEVERAL valid answers;
  never write a prompt with one right answer (no "What is the formula for X?").
- `hint` (optional but encouraged): a short nudge, ideally the student's own flashcard mnemonic
  (e.g. "CLAW ABC", "BUSFAIL", "CDF CSQ"). Using a hint halves the points, so it can be generous.
- `learn`: 1–2 sentences that teach the category, drawn from the flashcards (definition, formula,
  or the key idea a marker wants). Plain text, no markdown.
- `answers`: 4–10 valid answers (aim for 5–8). Each has:
  - `a`: display name, Title-case first word, concise (under ~45 characters).
  - `p`: points, an integer from 5 to 100 in steps of 5. This is the rarity score: what share of
    Year 12 students would NOT think of this answer. The answer most students type first scores
    10–20. Solid textbook answers 25–50. Sharper, more specific or more sophisticated answers
    55–80. Genuinely rare, Band 6 level answers (specific terminology, less-taught items, precise
    examples from the flashcards) 85–100. Every prompt should have at least one answer at 10–20
    and at least one at 70+. Spread the values; don't give everything 50.
  - `k`: aliases — other ways a student might type the same answer: abbreviations (jit, tqm, whs,
    roe, fwc, ea, csr, sla), synonyms, shorter forms, common phrasings, US/UK spelling variants.
    Lower-case. 2–6 aliases is typical. The display name `a` is always matched too, so don't
    repeat it.
  - `why`: one short sentence (under ~120 characters) explaining or exemplifying the answer, from
    the flashcards. This is shown in the reveal and is where the learning happens.
- `near` (optional): answers that are related but wrong for this prompt — a classic confusion, the
  "other half" of a pair (internal vs external, monetary vs non-monetary, advantage vs
  disadvantage), or the concept next door. They earn 5–15 points and the `msg` corrects the
  misconception in one sentence. Make these count: they are how the game teaches.

## How the game matches typed answers (important for writing aliases)

The matcher lower-cases the text, turns `&` into "and", removes punctuation, drops stopwords
(a an the of to and or in for with on by is are be its it their as at from), strips a trailing
plural `s` (and `ies` → `y`), then an alias matches when EITHER the whole input is within a small
typo distance of the whole alias, OR every word of the alias appears somewhere in the input
(each word allowed a 1-letter typo if 5+ letters long). When several answers match, the one with
the longest matching alias wins.

Consequences:

1. Single-word aliases match any input containing that word, so a single-word alias must be
   distinctive WITHIN THE PROMPT. Never use a generic word ("cost", "staff", "price", "quality",
   "business", "customer") as an alias if another answer in the same prompt also contains it.
2. A `near` alias must not match any real answer in the same prompt.
3. Single-letter aliases are rejected. Two-letter acronyms (ai, pr, ea) are fine, but they only match that exact word.

## Coverage and size

- 36–48 prompts for the topic. Cover every syllabus dot point for the topic that the flashcards
  cover, across all sections (R, I, P, S, and E for HR). Lists in the flashcards (advantages,
  disadvantages, types, steps, examples, effects, influences, mnemonics) make the best prompts.
- Include the interdependence cards (e.g. "Give a way Finance depends on Marketing"), and the
  advantages/disadvantages cards, which are exam favourites.
- Answers within one prompt must be genuinely different from each other, not rewordings.
- Keep all text accurate for the current HSC Business Studies syllabus. Australian spelling.

## Validate after editing

Run:

    python3 business/build/validate_bank.py <your file>

Fix every ERROR (alias collisions, counts, point values) and as many WARNs as sensible. You can also
test how real typing would match:

    python3 business/build/validate_bank.py <file> --try fin-ratios-profit "ROE" fin-ratios-profit "net profit margin"

Try a handful of realistic student answers (abbreviations, typos, partial phrasings) on several
prompts and add aliases where sensible answers fail to match.
