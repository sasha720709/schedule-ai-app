"""Ask about what is actually there, not about what might be.

## The owner's idea, and why it is the right one

Search broadly first, then ask the user questions *based on what came back*,
then narrow.

That inversion is the whole value. A generic clarifying form asks "which
neighbourhood? what hours? full or part time?" -- and half of it is wasted,
because it does not know that every one of today's results is already in Be'er
Sheva, or that not one of them mentions hours. Questions derived from the live
result set can only ever be questions that **discriminate**: they have real
options, real counts, and a visible effect the moment they are answered.

It is a facet filter built from the search results, rather than a survey
written in advance.

## The correction that makes it safe

The obvious next step -- turn the answers into a filter and watch only what
passes -- is the one thing that must not happen.

The answers are derived from **today's** postings. Tomorrow's good job may come
from a company not in today's list, or describe itself differently. A hard
filter built from today would silently exclude it, count zero, and go on
reporting nothing while looking perfectly healthy. That is the single most
persistent bug class in this codebase: it broke the presence kind's text
filter, it is why `COUNT_PROMPT` argues for loose selectors, and removing it is
most of what the jobs registry was for.

So the answers do two different jobs, and the asymmetry is deliberate:

- **On today's list, they filter.** We can see those items, the user can see
  the effect immediately, and a wrong answer is undone by clicking again.
- **On tomorrow's postings, they rank.** They become criteria handed to
  `rank.py`, which scores and only ever drops the outright irrelevant. A future
  job that misses a stated preference is reported with a lower score, not
  hidden.

A preference the user typed is a preference, not a promise about postings that
do not exist yet.

## Why this needs no chat

The roadmap assumed clarifying questions would need multi-turn conversation and
deferred them behind sub-phase 4d. They do not. The plan card already shows
what was found and waits for a confirm; the questions belong on it, and the
answers travel with the confirm call. No new conversational state, no new
endpoint, and nothing is ever required -- confirming without answering behaves
exactly as it did before.

## What it costs

One Haiku call per watch, at plan time, over items already fetched: about half
a cent, paid once. Applying the answers is free -- they are text appended to a
ranking prompt that was going to run anyway.
"""

import cost
import llm

# More than three questions is a form, and a form does not get filled in.
MAX_QUESTIONS = 3
MAX_ITEMS = 30
MAX_TOKENS = 1500

SYSTEM_PROMPT = """You write the few questions worth asking someone about
results that have just been found for them.

You are given their request and a numbered list of what a search returned --
job postings, usually: a title, a company, a location, sometimes a date.

Write at most 3 questions that would help pick between THESE results. For each
option, list the numbers of the items it covers.

THE ONE RULE THAT MATTERS: **only ask what actually splits this list.**

- If every item is in the same city, do not ask about location.
- If no item says anything about hours, do not ask about hours. You cannot
  filter on a fact none of them states.
- An option covering all the items, or none, is not worth offering.
- Prefer the axis that splits the list most evenly. One question that halves it
  beats three that trim one item each.

OPTIONS DESCRIBE THE ITEMS, NOT THE USER'S FLEXIBILITY. Each option is a
property some of the results have: "Be'er Sheva", "Tel Aviv", "Remote",
"Student or intern", "Senior". Never write an option meaning "anywhere",
"doesn't matter", "open to others" or "no preference" -- **not answering
already means that**, and an option like "open to other cities" ends up
carrying only the items that are *not* in the city, so choosing it hides the
local job the person most wanted.

Every option must be something you could point at in the list and say: these
ones are that.

Ask about what a person would actually weigh: seniority, the kind of company,
the specific role when the results mix several, the city when they differ,
on-site versus remote when that is visible.

ASK ABOUT SOMETHING THAT WILL STILL MEAN SOMETHING NEXT MONTH. The answers
become a lasting preference for a watch that keeps running, so ask about
properties of the *job* -- role, level, place, company, on-site or remote --
never about how recently it was posted or where it sits in today's list. "How
recent should the posting be?" splits today's results and is meaningless for a
posting that appears tomorrow.

Write like a person. "Which of these interests you?" not "Please indicate your
preference regarding role classification." Options are short labels, two or
three words, taken from what the items actually say.

If nothing usefully splits the list -- because the results are all much the
same, or too few to choose between -- return an empty list. That is a good
answer, not a failure.

Respond with ONLY a JSON object:
{"questions": [
  {"id": "short_slug",
   "question": "the question, under 12 words",
   "options": [{"value": "short_slug", "label": "short label", "items": [1, 4, 7]}]}
]}

Example of the shape wanted, for a list mixing cities and levels:

{"questions": [
  {"id": "city", "question": "Which city?",
   "options": [{"value": "beer_sheva", "label": "Be'er Sheva", "items": [2]},
               {"value": "tel_aviv", "label": "Tel Aviv", "items": [1, 3]}]},
  {"id": "level", "question": "Which level?",
   "options": [{"value": "student", "label": "Student or junior", "items": [2]},
               {"value": "senior", "label": "Senior", "items": [1, 3]}]}
]}"""


def _prompt_for(request: str, items: list) -> str:
    lines = [f"{n}. {item.get('text', '')[:250]}"
             for n, item in enumerate(items, start=1)]
    return (f"They asked:\n{request}\n\nWhat the search found:\n"
            + "\n".join(lines))


def build(request: str, items: list, *, client=None) -> tuple:
    """Questions grounded in `items`. Returns `(questions, spend_usd)`.

    Never raises. No questions is a perfectly good outcome -- the results may
    genuinely be all of a kind -- and it is also what any failure produces,
    because a watch must be creatable whether or not this step worked.
    """
    if not items or not (request or "").strip():
        return [], 0.0

    batch = items[:MAX_ITEMS]
    try:
        reply = llm.ask(
            client,
            model=llm.READ_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            content=_prompt_for(request, batch),
        )
    except Exception as exc:  # noqa: BLE001 -- never block creating a watch
        print(f"[questions] skipped: {type(exc).__name__}: {exc}")
        return [], 0.0

    spend = cost.questions_cost(len(batch))
    return _clean(reply.get("questions"), batch), spend


def _clean(raw, items: list) -> list:
    """Keep only questions that discriminate, with real item ids attached.

    Everything here arrives from a model, so nothing about its shape is
    guaranteed. Item *numbers* are converted to the stable ids the rest of the
    system uses, so that narrowing the list later is an exact set operation
    rather than re-matching text.
    """
    if not isinstance(raw, list):
        return []

    ids = [item.get("id") for item in items]
    out = []

    for question in raw[:MAX_QUESTIONS]:
        if not isinstance(question, dict):
            continue
        text = str(question.get("question") or "").strip()
        options = _options(question.get("options"), ids)
        # A question whose options cover everything, or nothing, tells the user
        # they have a choice when they do not.
        if not text or len(options) < 2:
            continue
        if all(len(o["items"]) == len(ids) for o in options):
            continue
        out.append({
            "id": str(question.get("id") or f"q{len(out) + 1}")[:40],
            "question": text[:120],
            "options": options,
        })
    return out


def _options(raw, ids: list) -> list:
    if not isinstance(raw, list):
        return []
    out = []
    for option in raw[:4]:
        if not isinstance(option, dict):
            continue
        matched = [ids[n - 1] for n in _numbers(option.get("items"))
                   if 1 <= n <= len(ids) and ids[n - 1]]
        label = str(option.get("label") or "").strip()
        if not label or not matched:
            continue
        out.append({
            "value": str(option.get("value") or label)[:40],
            "label": label[:60],
            "items": matched,
        })
    return out


def _numbers(raw) -> list:
    if not isinstance(raw, list):
        return []
    numbers = []
    for value in raw:
        try:
            numbers.append(int(value))
        except (TypeError, ValueError):
            continue
    return numbers


def as_criteria(questions: list, answers: dict) -> str:
    """The answers, as a sentence for the ranking prompt.

    This is where the asymmetry lives. On today's list the answers filter,
    because the items are in front of us. From here on they are *preferences*
    handed to a ranker that scores rather than excludes -- a future posting
    that misses one is reported with a lower score, never hidden.
    """
    if not questions or not isinstance(answers, dict):
        return ""

    lines = []
    for question in questions:
        chosen = answers.get(question.get("id"))
        if not chosen:
            continue
        wanted = set(chosen if isinstance(chosen, list) else [chosen])
        labels = [o["label"] for o in question.get("options", [])
                  if o.get("value") in wanted]
        if labels:
            lines.append(f"- {question['question']} {' or '.join(labels)}")

    if not lines:
        return ""
    return "They also said:\n" + "\n".join(lines)
