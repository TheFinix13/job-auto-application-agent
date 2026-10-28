"""Reject any claim that is not traceable to the facts file (hard rule 3).

Two entry points:

* ``check_draft(text, facts)``: free text is checked against every usable fact.
* ``check_claims(claims, facts)``: each claim names the fact ids it rests on,
  and may only use values found in those facts.

What counts as a violation:

* a number (digits or number words such as "three") that no usable fact contains;
* a date or a month and year pair that no usable fact contains;
* a capitalised name (employer, school, place) that no usable fact contains;
* a tool from the tool list that no usable fact contains, in any letter case;
* an email address or other contact detail that no usable fact contains;
* any of the above that only appears in a fact marked unknown or not claimed.

A usable fact has status V or B, or an id the user has listed as resolved.

Limits. The check is lexical. It cannot tell that a reworded sentence changes
the meaning of a fact, so it is the first of two checks: a second reader
still looks at the draft. It also rejects some harmless drafts, for example a
sentence-initial word it does not recognise, which is the safe direction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from job_agent.facts import Fact

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip

NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
    "thousand": 1000, "million": 10**6, "billion": 10**9, "dozen": 12,
}  # fmt: skip
# "one" is left out: it is far more often a pronoun than a count.

TOOLS = {
    "python", "java", "javascript", "typescript", "sql", "scala", "kotlin", "c++", "c#", "matlab",
    "bash", "kubernetes", "docker", "terraform", "ansible", "jenkins", "aws", "azure", "gcp",
    "pyspark", "hadoop", "kafka", "tensorflow", "pytorch", "keras", "scikit-learn", "sklearn",
    "pandas", "numpy", "scipy", "matplotlib", "seaborn", "tableau", "powerbi", "power bi", "git",
    "github", "gitlab", "linux", "mongodb", "postgresql", "postgres", "mysql", "sqlite", "redis",
    "snowflake", "databricks", "dbt", "mlflow", "fastapi", "django", "nodejs", "node.js",
    "langchain", "jira", "spss", "selenium", "playwright", "xgboost", "lightgbm", "opencv",
    "huggingface", "hugging face", "spacy", "nltk", "stable-baselines3", "gymnasium", "docker-compose",
}  # fmt: skip
# Words that are also ordinary English (excel, spring, react, spark, go, r, rust, swift) are left
# out of the list; they are still caught when capitalised, by the name check.

GENERIC_WORDS = {
    "i", "cv", "uk", "british", "english", "dear", "sincerely", "yours", "faithfully", "regards",
    "kind", "best", "thank", "thanks", "mr", "ms", "mrs", "dr", "the", "a", "an", "and", "in",
    "at", "for", "with", "to", "of", "on", "my", "as", "by", "from", "this", "that", "it",
    "during", "after", "before", "while", "through", "across", "using", "alongside",
}  # fmt: skip

SENTENCE_STARTERS = {
    "built", "led", "ran", "wrote", "made", "won", "set", "cut", "grew", "took", "gave", "held",
    "taught", "began", "chose", "drew", "found", "kept", "met", "sent", "spoke", "wrote",
    "my", "our", "his", "her", "their", "the", "a", "an", "this", "these", "i", "we", "with", "as",
    "after", "before", "during", "also", "currently", "recently", "previously", "alongside",
    "seeking", "keen", "experienced", "skilled", "motivated", "graduate", "junior", "recent",
    "strong", "familiar", "responsible", "proficient", "working", "interested", "eager", "dear",
    "yours", "sincerely", "thank", "thanks", "kind", "best", "regards", "please", "email", "phone",
    "contact", "address",
}  # fmt: skip

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_MONTH_YEAR_NUM = re.compile(r"\b(\d{1,2})/(\d{4})\b")
_MONTH_YEAR_NAME = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{4})\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-']*[A-Za-z0-9+#]|[A-Za-z]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_LEADING_MARK = re.compile(r"^[\s\-*\u2022\d.)]+")


@dataclass(frozen=True)
class Violation:
    kind: str  # number, date, entity, tool, contact, unknown_fact, no_fact_id, bad_fact_id
    text: str
    detail: str = ""


@dataclass
class FactCheckResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class Claim:
    text: str
    fact_ids: list[str]


@dataclass
class _Corpus:
    numbers: set[int | float] = field(default_factory=set)
    months: set[tuple[int, int]] = field(default_factory=set)
    month_numbers: set[int] = field(default_factory=set)
    words: set[str] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)
    contacts: set[str] = field(default_factory=set)


def _number_value(token: str) -> int | float | None:
    token = token.replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return None
    return int(value) if value == int(value) else value


def _numbers_in(text: str) -> list[tuple[str, int | float]]:
    found = []
    for match in _NUMBER.finditer(text):
        value = _number_value(match.group())
        if value is not None:
            found.append((match.group(), value))
    for match in re.finditer(r"[A-Za-z]+", text):
        word = match.group().lower()
        if word in NUMBER_WORDS:
            found.append((match.group(), NUMBER_WORDS[word]))
    return found


def _month_pairs(text: str) -> list[tuple[str, tuple[int, int]]]:
    pairs = [(m.group(), (int(m.group(1)), int(m.group(2)))) for m in _MONTH_YEAR_NUM.finditer(text)
             if 1 <= int(m.group(1)) <= 12]  # fmt: skip
    for m in _MONTH_YEAR_NAME.finditer(text):
        month = MONTHS.get(m.group(1).lower())
        if month:
            pairs.append((m.group(), (month, int(m.group(2)))))
    return pairs


def _tools_in(text: str) -> list[str]:
    found = []
    lowered = text.lower()
    for tool in TOOLS:
        pattern = r"(?<![\w+#.-])" + re.escape(tool) + r"(?![\w+#-])"
        for match in re.finditer(pattern, lowered):
            found.append(text[match.start() : match.end()])
    return found


def _words_in(text: str) -> list[str]:
    return [w.rstrip(".'-") for w in _WORD.findall(text)]


def _corpus_of(texts: Iterable[str]) -> _Corpus:
    corpus = _Corpus()
    for text in texts:
        corpus.numbers.update(v for _, v in _numbers_in(text))
        for _, pair in _month_pairs(text):
            corpus.months.add(pair)
            corpus.month_numbers.add(pair[0])
        corpus.words.update(w.lower() for w in _words_in(text))
        corpus.tools.update(t.lower() for t in _tools_in(text))
        corpus.contacts.update(e.lower() for e in _EMAIL.findall(text))
    return corpus


def _is_sentence_start_exempt(word: str, corpus_words: set[str]) -> bool:
    low = word.lower()
    return (
        low in SENTENCE_STARTERS
        or low in GENERIC_WORDS
        or low in corpus_words
        or low.endswith(("ed", "ing"))
    )


def _name_candidates(text: str, corpus_words: set[str]) -> list[str]:
    """Capitalised words that must be backed by a fact."""
    candidates = []
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = _LEADING_MARK.sub("", sentence)
        for index, word in enumerate(_words_in(sentence)):
            if len(word) < 2 or word.lower() in GENERIC_WORDS or word.lower() in MONTHS:
                continue
            capitalised = word[0].isupper()
            inner_caps = any(c.isupper() for c in word[1:])
            if not (capitalised or inner_caps):
                continue
            if index == 0 and not inner_caps and _is_sentence_start_exempt(word, corpus_words):
                continue
            candidates.append(word)
    return candidates


def _classify(kind: str, text: str, token_in_blocked: str | None) -> Violation:
    if token_in_blocked:
        return Violation("unknown_fact", text, token_in_blocked)
    return Violation(kind, text)


def _check_text(
    text: str,
    allowed: _Corpus,
    blocked: list[tuple[Fact, _Corpus]],
    extra_words: set[str],
) -> list[Violation]:
    violations: list[Violation] = []

    def blocked_by(predicate) -> str | None:
        for fact, corpus in blocked:
            if predicate(corpus):
                return f"{fact.id} (status {fact.status})"
        return None

    pair_years = set()
    for raw, pair in _month_pairs(text):
        pair_years.add(pair[1])
        if pair not in allowed.months:
            violations.append(_classify("date", raw, blocked_by(lambda c, p=pair: p in c.months)))

    for raw, value in _numbers_in(text):
        if value in allowed.numbers:
            continue
        is_year = isinstance(value, int) and 1900 <= value <= 2100
        kind = "date" if is_year else "number"
        violations.append(_classify(kind, raw, blocked_by(lambda c, v=value: v in c.numbers)))

    for tool in _tools_in(text):
        if tool.lower() not in allowed.tools:
            violations.append(_classify("tool", tool, blocked_by(lambda c, t=tool.lower(): t in c.tools)))

    tool_words = {w.lower() for tool in _tools_in(text) for w in _words_in(tool)}
    for email in _EMAIL.findall(text):
        if email.lower() not in allowed.contacts:
            violations.append(_classify("contact", email, blocked_by(lambda c, e=email.lower(): e in c.contacts)))

    email_words = {w.lower() for email in _EMAIL.findall(text) for w in _words_in(email)}
    known = allowed.words | extra_words
    for word in _name_candidates(text, allowed.words):
        low = word.lower()
        if low in known or low in tool_words or low in email_words:
            continue
        violations.append(_classify("entity", word, blocked_by(lambda c, w=low: w in c.words)))

    return _dedupe(violations)


def _dedupe(violations: list[Violation]) -> list[Violation]:
    seen, result = set(), []
    for v in violations:
        key = (v.kind, v.text.lower())
        if key not in seen:
            seen.add(key)
            result.append(v)
    return result


def _split_facts(facts: Iterable[Fact], resolved_ids: Iterable[str]) -> tuple[list[Fact], list[Fact]]:
    resolved = set(resolved_ids)
    usable, blocked = [], []
    for fact in facts:
        (usable if fact.usable or fact.id in resolved else blocked).append(fact)
    return usable, blocked


def check_draft(
    text: str,
    facts: Iterable[Fact],
    resolved_ids: Iterable[str] = (),
    extra_terms: Iterable[str] = (),
) -> FactCheckResult:
    """Check free text against all usable facts.

    ``extra_terms`` are names taken from the job advert (the company and role
    title). They are allowed as names only; numbers and dates are never
    taken from an advert.
    """
    usable, blocked = _split_facts(list(facts), resolved_ids)
    allowed = _corpus_of(f.text for f in usable)
    blocked_corpora = [(f, _corpus_of([f.text])) for f in blocked]
    extra = {w.lower() for term in extra_terms for w in _words_in(term)}
    return FactCheckResult(_check_text(text, allowed, blocked_corpora, extra))


def check_claims(
    claims: Iterable[Claim],
    facts: Iterable[Fact],
    resolved_ids: Iterable[str] = (),
    extra_terms: Iterable[str] = (),
) -> FactCheckResult:
    """Check that each claim cites facts, and uses only values found in them."""
    facts = list(facts)
    by_id = {f.id: f for f in facts}
    resolved = set(resolved_ids)
    extra = {w.lower() for term in extra_terms for w in _words_in(term)}
    violations: list[Violation] = []
    for claim in claims:
        if not claim.fact_ids:
            violations.append(Violation("no_fact_id", claim.text, "claim cites no fact id"))
            continue
        cited_usable, ok = [], True
        for fact_id in claim.fact_ids:
            fact = by_id.get(fact_id)
            if fact is None:
                violations.append(Violation("bad_fact_id", fact_id, f"not in the facts file: {claim.text!r}"))
                ok = False
            elif fact.usable or fact_id in resolved:
                cited_usable.append(fact)
            else:
                violations.append(
                    Violation("unknown_fact", fact_id, f"status {fact.status}, not resolved: {claim.text!r}")
                )
                ok = False
        if not ok:
            continue
        allowed = _corpus_of(f.text for f in cited_usable)
        violations.extend(_check_text(claim.text, allowed, [], extra))
    return FactCheckResult(_dedupe(violations))
