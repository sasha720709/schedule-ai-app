# The interface: how not to make it look generated

Written 2026-08-05, because the owner named the problem precisely: *"I don't
want it to look AI-ish."* That is a real risk and it deserves a real answer
rather than a style guide.

This document is an argument, not a set of rules. Read it before opening a
component.

---

## 1. Start by admitting what the failure looks like

"AI slop" is not vague. It is a short list of specific, nameable habits, and
naming them is most of the defence. Left to my own devices I will produce all
of them, because they are the statistical centre of every interface ever
written down.

**Defaults, everywhere.** The shadcn card, the indigo-to-purple gradient,
`rounded-xl shadow-lg`, a three-column feature grid. The visual signature of
*no decision having been made*.

**Flat hierarchy.** Every element the same size, weight and colour, so nothing
leads and the eye has nowhere to land. Real design has one thing that
dominates each screen and is unembarrassed about it.

**Decoration standing in for content.** Icons beside labels that were already
clear. A hero section on a tool. Glassmorphism. Ornament arrives exactly where
there is nothing to say.

**Uniform spacing.** Everything sixteen pixels apart. Real layouts group
related things tightly and push unrelated things far apart; even spacing means
nobody decided what belongs together.

**Copy written by nobody.** "Welcome to your dashboard." "Get started."
Emoji in headings. Placeholder voice for a product that has a voice.

**Indifference to the data.** Every watch rendered as an identical card,
whatever is interesting about it. A share watch and a job watch are not the
same object and should not look the same.

---

## 2. This project's unfair advantage, and it is not a design skill

**This app has something true and specific to say on every screen**, and an
entire day went into making those sentences precise. That is rarer than it
sounds, and it is the whole lever.

Things the backend can already state, exactly, today:

    read $306.49 just now — this is what will be watched
    5% below the $333.43 read at the previous close
    next check 16:00, in 16 h
    1899 ILS  ivory  (free delivery, read just now)
    2400 ILS  bug    (before delivery, read 45 min ago)
    not watching amazon — prices in USD, and this watch is about ILS
    47 listed here today, 3 of which match
    a shop stopped letting us look
    measured across every shop below — the cheapest offer is what this
      watch calls the price

Not one of those is filler. Each was added after a real failure, and several
exist because a watch was silently wrong until someone made it say what it
knew. **A screen assembled from sentences like these cannot be generic**,
because no other product has them.

So the method is: **design outward from the true sentences.** Decide what each
screen is *for*, write the words first, then decide typography and spacing to
serve them. Never start from a component library — a component library is a
set of answers to questions this product has not asked.

---

## 3. On Figma, honestly

The owner's instinct — *connect AI to Figma and the design gets better* — is
half right, and the half that is wrong matters.

**What Figma will not do.** It will not supply taste. Pointing a model at
Figma produces the same generic output as pointing it at React, in a different
file format. The slop moves; it does not disappear. There is no tool that
converts "make it look good" into good.

**What Figma genuinely gives.**

- **Decisions before code.** The single biggest cause of generated-looking UI
  is that layout gets decided while typing components, one `div` at a time. A
  canvas forces the whole screen to be looked at before any of it is real.
- **A system you can actually hold to.** Tokens — type scale, spacing scale,
  colour with meaning — defined once and referenced. Once they exist, nothing
  generated can wander outside them, and *that constraint is what stops slop*.
- **Code generated from your design instead of my defaults.** This is the real
  prize. With the Figma MCP integration, components can be built from the file
  you drew rather than from what a model thinks a dashboard looks like. The
  design becomes the source of truth and I become the typist.
- **Cheap iteration.** Ten variants of the plan card in an afternoon, none of
  which touch the repository.

**Verdict: use it, and use it for the tokens and one screen first.** Not for
everything, and not expecting it to design for you.

---

## 4. Constrain me, explicitly

I am the most likely source of slop in this project and the cheapest thing to
fix. Constraints work; taste-by-request does not. A brief that says *"make it
clean and modern"* returns the statistical average of every dashboard on the
internet. A brief with bans in it returns something else.

A starting set, to be argued with rather than accepted:

- **No gradients. No glassmorphism. No drop shadows** except where something
  genuinely floats above something else.
- **No icon** unless it carries information a word would not. No decorative
  icons beside labels.
- **No cards by default.** A card is a fence, and most of this data does not
  need fencing. Use rules, spacing and alignment first.
- **One accent colour**, and it means *something* — "this needs your
  attention" — never "this is a button".
- **Monospace for every number that was measured.** Prices, times,
  thresholds, counts. It is honest about what they are and it aligns.
- **No emoji.**
- **No placeholder copy.** If a screen has nothing to say, it should be
  smaller, not padded.

---

## 5. What this thing should feel like

An opinion, offered to be disagreed with, because a project with no stated
direction gets whatever the model averages.

This is **an instrument, not a dashboard.** It watches things closely and
reports exactly, including when it is unsure — the whole day of 2026-08-05
went into `unavailable` vs `failed` vs `blocked`, into "read 45 min ago", into
"delivery not included". The interface should carry that seriousness.

That suggests something closer to a **well-set document or a trading terminal**
than to a SaaS product page: dense rather than airy, typographic rather than
illustrated, quiet in colour, unafraid of showing a lot of true information at
once. Restraint reads as confidence, and it happens to be much harder to make
generic than decoration is.

The nearest reference points worth stealing from, and *what* to steal:

| | what to take |
|---|---|
| Linear | typographic hierarchy and restraint; keyboard-first density |
| Stripe's dashboard | how a lot of numbers can be legible without cards |
| A financial terminal | monospace figures, tight rows, colour used only for meaning |
| A good newspaper table | alignment and rules doing the work of boxes |

**The owner should replace this table with their own before any design work
starts.** References are the highest-value input available and the only one I
cannot generate: two or three products you actually like, with a sentence each
on *what specifically* to take. Without them I will average everything.

---

## 6. The order of work

Deliberately not "build the app, then style it".

1. **Inventory the sentences.** They already exist, in `notifier/handler.py`,
   in the plan card in `App.tsx`, in the emails. Collect them. That list *is*
   the content design, and it is already written.
2. **The owner picks references and bans.** §4 and §5, replaced with their own
   opinions. This is the step that cannot be delegated and the one that
   decides whether the result looks generated.
3. **Tokens, in Figma.** One typeface (two at most), a 5–6 step type scale,
   ~6 spacing steps, four colours with meanings, one radius. Small enough to
   memorise. Written down means never re-decided.
4. **Design one screen properly: the plan card.** Not the list, not the
   layout, not sign-in. The plan card is the heart of the product — the moment
   the system says what it found and what it is about to do, and the user says
   yes. If that screen is good the rest follows; if it is generic, nothing
   downstream saves it.
5. **Iterate that one screen until it is actually good.** Ten variants. This
   is where the time should go, and it is the step most likely to be skipped.
6. **Only then generate code**, from the design, via the Figma integration.
7. **Extend outward** — list, detail, sign-in — reusing decisions rather than
   making new ones.

---

## 7. Two traps specific to this codebase

**The plan card is not a form.** It is the system showing its work: what it
read, from where, what it will cost, what it is unsure about, and the
questions it wants answered before it starts. Designing it as a settings form
would throw away the most interesting thing the product does.

**Statuses are not decoration.** `planning`, `proposed`, `active`, `paused`,
`triggered`, `degraded`, `expired`, `blocked` — each one exists because
collapsing it into another caused a real bug. They must stay visually
distinguishable, and `degraded` and `blocked` in particular must not look like
ordinary failure: one means the page changed, the other means a shop shut us
out, and the user can act on the second.
