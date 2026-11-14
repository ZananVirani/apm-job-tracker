"""Strict eligibility classifier for Summer 2027 product internships.

Rules are applied in the documented order and stop at the first REJECT:

    0. role family (core / adjacent / other, judged on the description when needed)
    a. internship, co-op or student programme (plus the start-term check)
    b. education level accepts undergraduates
    c. graduation year compatible with the configured graduation date
    d. location (Canada, US remote, or US with sponsorship) and e. work-authorisation notes
    f. posting still open

Every eligibility decision records the exact text that triggered it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
import re

from .filtering import INTERN_MARKER, SUMMER_MARKER, clean_title, location_evidence, title_category
from .models import Job, normalize_space


ACCEPT = "ACCEPT"
REVIEW = "REVIEW"
REJECT = "REJECT"

MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january", "janvier"),
            ("feb", "february", "fevrier", "février"),
            ("mar", "march", "mars"),
            ("apr", "april", "avril"),
            ("may", "mai"),
            ("jun", "june", "juin"),
            ("jul", "july", "juillet"),
            ("aug", "august", "aout", "août"),
            ("sep", "sept", "september", "septembre"),
            ("oct", "october", "octobre"),
            ("nov", "november", "novembre"),
            ("dec", "december", "decembre", "décembre"),
        ),
        start=1,
    )
    for name in names
}
MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
SEASON_RE = r"(?:spring|summer|fall|autumn|winter)"
SEASON_GRADUATION_MONTH = {"spring": 5, "summer": 7, "fall": 12, "autumn": 12, "winter": 12}


def plain_text(value: str) -> str:
    """Convert HTML (including entity-escaped HTML) into readable plain text."""
    text = value or ""
    for _ in range(2):
        if "&lt;" in text or "&amp;" in text or "&#" in text:
            text = unescape(text)
    text = re.sub(r"<\s*(?:br|/p|/li|/div|/h[1-6]|/tr)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*li\b[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<(?:script|style)\b.*?</(?:script|style)>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text).replace(" ", " ")
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "–": "-", "—": "-"}))
    lines = [normalize_space(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def quote(text: str, start: int, end: int, width: int = 110) -> str:
    """Return the clause around a match, trimmed for display."""
    left = max(text.rfind("\n", 0, start), text.rfind(". ", 0, start), start - width)
    right_candidates = [index for index in (text.find("\n", end), text.find(". ", end)) if index != -1]
    right = min([end + width, *right_candidates]) if right_candidates else min(len(text), end + width)
    snippet = normalize_space(text[max(0, left + 1) : right + 1].strip(" .-"))
    return snippet[:240]


@dataclass
class Classification:
    decision: str = ACCEPT
    category: str = "other"
    category_label: str = ""
    reasons: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    term_start: str = ""
    term_length: str = ""
    priority_location: bool = False
    location_note: str = ""
    remote_status: str = ""
    close_date: str = ""
    status: str = "open"
    description_available: bool = False

    @property
    def reason(self) -> str:
        return self.reasons[0] if self.reasons else ""

    def _flag(self, decision: str, reason: str, quoted: str = "") -> None:
        if decision == REJECT or (decision == REVIEW and self.decision == ACCEPT):
            self.decision = decision
        if decision == ACCEPT:
            self.reasons.append(reason)
        elif decision == REVIEW:
            # Review reasons lead the summary so the digest explains why a human is needed.
            insert_at = sum(1 for item in self.reasons if item.startswith("REVIEW:"))
            self.reasons.insert(insert_at, f"REVIEW: {reason}")
        else:
            self.reasons.insert(0, reason)
        if quoted:
            self.quotes.append(quoted)


# --- Product-orientation signals --------------------------------------------

PM_SIGNALS = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("roadmap", r"\broadmaps?\b"),
        ("PRDs/specs", r"\b(?:prds?|product\s+requirements?|requirements?\s+documents?|(?:write|writing|draft)\w*\s+(?:product\s+|feature\s+|technical\s+)?spec(?:ification)?s?)\b"),
        ("backlog", r"\bbacklogs?\b"),
        ("prioritization", r"\bprioriti[sz](?:e|es|ed|ing|ation)\b"),
        ("user stories", r"\buser\s+stor(?:y|ies)\b"),
        ("discovery", r"\b(?:product|customer|user)\s+discovery\b|\bdiscovery\s+(?:work|research|process)\b"),
        ("customer research", r"\b(?:customer|user)\s+(?:interviews?|research|feedback|needs|pain\s+points)\b"),
        ("engineering + design", r"\b(?:engineer(?:s|ing)?\s*(?:,|and|&)\s*(?:ux\s+)?design(?:ers?)?|design(?:ers)?\s*(?:,|and|&)\s*engineer(?:s|ing)?)\b"),
        ("cross-functional", r"\bcross[- ]functional\b"),
        ("product metrics", r"\b(?:product\s+(?:metrics|analytics|kpis?)|a/b\s+test\w*|experimentation|kpis?)\b"),
        ("feature launches", r"\b(?:launch\w*|ship\w*)\s+(?:new\s+)?(?:features?|products?)\b|\bgo[- ]to[- ]market\b"),
        ("product lifecycle", r"\bproduct\s+(?:lifecycle|life\s+cycle|vision|strategy|management|manager|owner|development)\b"),
    )
)


# Signals that name product-management work itself, as opposed to generic teamwork words.
STRONG_SIGNALS = {"roadmap", "PRDs/specs", "backlog", "user stories", "discovery", "product lifecycle"}


def product_signals(text: str) -> list[str]:
    return [label for label, pattern in PM_SIGNALS if pattern.search(text)]


def is_product_oriented(signals: list[str], minimum: int) -> bool:
    return len(signals) >= minimum and any(signal in STRONG_SIGNALS for signal in signals)


# --- Rule vocabularies --------------------------------------------------------

INTERNSHIP_TEXT = re.compile(
    r"\b(?:this\s+is\s+an?\s+(?:paid\s+)?(?:summer\s+)?(?:internship|co-?op|work\s+term)|"
    r"(?:internship|co-?op)\s+(?:program(?:me)?|position|role|term|placement|opportunity)|"
    r"(?:4|8|12|16|four|eight|twelve|sixteen)[- ]month\s+(?:internship|co-?op|work\s+term|term|placement)|"
    r"(?:10|11|12)[- ]week\s+(?:summer\s+)?(?:internship|program(?:me)?))\b",
    re.IGNORECASE,
)
INTERN_EMPLOYMENT = re.compile(r"\b(?:intern(?:ship)?|co-?op|student|trainee|temporary\s+intern)\b", re.IGNORECASE)
NEW_GRAD_TITLE = re.compile(
    r"\b(?:new[ -]?grad(?:uate)?s?|recent\s+grad(?:uate)?s?|graduate\s+(?:program(?:me)?|rotational|leadership|associate|analyst|scheme)|"
    r"early[ -]career|full[ -]time|entry[ -]level)\b",
    re.IGNORECASE,
)
PART_TIME_DURING_SCHOOL = re.compile(
    r"\bpart[- ]time\b[^.\n]{0,100}\b(?:during\s+(?:the\s+)?(?:school|academic)\s+(?:year|term|semester)|"
    r"while\s+(?:in\s+school|studying|attending)|academic\s+year|school\s+year|year[- ]round)\b|"
    r"\byear[- ]round\s+(?:part[- ]time\s+)?intern(?:ship)?\b",
    re.IGNORECASE,
)

ADVANCED_ONLY_TITLE = re.compile(r"\b(?:mba|m\.?b\.?a\.?|master'?s|masters|ph\.?\s?d|doctoral|graduate\s+student)\b", re.IGNORECASE)
ADVANCED_REQUIRED = re.compile(
    r"\b(?:(?:currently\s+)?(?:enrolled|pursuing|enrol(?:l)?ment|candidates?\s+for|students?\s+in|working\s+towards?|completing|"
    r"must\s+be\s+(?:in|pursuing))\s+(?:in\s+)?(?:an?\s+|your\s+)?(?:full[- ]time\s+|accredited\s+|second[- ]year\s+|first[- ]year\s+)?"
    r"(?:mba|m\.b\.a\.?|master'?s|masters|ph\.?\s?d|doctoral|graduate)(?:\s+(?:degree|program(?:me)?|studies|school))?|"
    r"\bmba\s+(?:candidates?|students?|interns?|program(?:me)?|summer\s+associates?)|"
    r"(?:requires?|required|must\s+have)\s+(?:an?\s+)?(?:master'?s|graduate|ph\.?\s?d|mba)\s+degree|"
    r"(?:mba|graduate|master'?s|ph\.?\s?d)\s+students?\s+only)\b",
    re.IGNORECASE,
)
UNDERGRAD_ALLOWED = re.compile(
    r"\b(?:bachelor'?s?|baccalaur\w*|undergrad(?:uate)?s?|b\.?\s?sc?\.?|b\.?\s?a\.?|b\.?\s?comm?\.?|bba|"
    r"college\s+students?|university\s+students?|post[- ]secondary)\b",
    re.IGNORECASE,
)

GRADUATION_CONTEXT = re.compile(
    r"\b(?:graduat\w*|class\s+of|grad\s+date|degree\s+completion|expected\s+(?:to\s+)?(?:complete|finish)|completion\s+date)\b",
    re.IGNORECASE,
)
RETURNING_STUDENT = re.compile(
    r"\b(?:return(?:ing)?\s+to\s+(?:full[- ]time\s+)?(?:school|studies|university|college|your\s+(?:studies|program(?:me)?|degree))|"
    r"penultimate|pre[- ]final\s+year|rising\s+(?:sophomore|junior|senior)s?|"
    r"at\s+least\s+one\s+(?:more\s+|additional\s+)?(?:semester|term|year|academic\s+term)\s+(?:remaining|left|of\s+(?:study|school))|"
    r"currently\s+enrolled\s+in\s+(?:an?\s+)?(?:bachelor'?s?|undergraduate|post[- ]secondary|university|college))\b",
    re.IGNORECASE,
)
FINAL_YEAR = re.compile(
    r"\b(?:final[- ]year\s+(?:students?|undergraduates?|of\s+(?:study|your\s+degree))|in\s+(?:your|their)\s+(?:final|last)\s+year|"
    r"recent\s+graduates?|graduated\s+within|have\s+graduated|"
    r"graduating\s+(?:students?|seniors?)\s+(?:only|in\s+20(?:2[0-7])))\b",
    re.IGNORECASE,
)

_NEGATION = (
    r"(?:will\s+not|won'?t|does\s+not|doesn'?t|do\s+not|don'?t|cannot|can\s*not|can'?t|is\s+not|are\s+not|"
    r"(?:is|are)?\s*unable\s+to|(?:is|are)?\s*not\s+able\s+to|not\s+in\s+a\s+position\s+to|"
    r"(?:is|are)?\s*not\s+(?:currently\s+)?(?:offering|providing|sponsoring|supporting))"
)
_SPONSORSHIP_KIND = r"(?:visa|immigration|employment|work|h-?1b|work\s+authori[sz]ation|employer)"
US_SPONSORSHIP_DENIED = re.compile(
    r"\b(?:not\s+(?:be\s+)?eligible\s+for\s+(?:\w+\s+){0,3}(?:visa|immigration|work\s+permit)?\s*sponsorship|"
    + _NEGATION
    + r"\s+(?:currently\s+|now\s+)?(?:provide|offer|sponsor|support|pursue|consider|accommodate)\w*\s+(?:[\w/-]+\s+){0,5}?"
    r"(?:" + _SPONSORSHIP_KIND + r"\s+)?(?:sponsorship|visas?|work\s+permits?)|"
    r"(?:unable|not\s+able)\s+to\s+sponsor|cannot\s+sponsor|(?:do|does|will)\s+not\s+sponsor|"
    r"no\s+(?:" + _SPONSORSHIP_KIND + r"\s+)?sponsorship|"
    r"(?:" + _SPONSORSHIP_KIND + r"\s+)?sponsorship\s+(?:is\s+|will\s+)?(?:not|un)\s*(?:be\s+)?(?:available|provided|offered|supported|considered)|"
    r"(?:not\s+)?(?:requir(?:e|es|ing)|need(?:s|ing)?)\s+(?:\w+\s+){0,6}?sponsorship\s+(?:\w+\s+){0,8}?(?:will\s+not|are\s+not|is\s+not|cannot|won'?t|not)\s+(?:be\s+)?(?:considered|eligible)|"
    r"must\s+not\s+(?:now\s+or\s+in\s+the\s+future\s+)?require\s+(?:\w+\s+){0,3}sponsorship|"
    r"without\s+(?:the\s+)?(?:need\s+(?:for|of)\s+|requiring\s+|needing\s+|require\s+)?(?:current\s+or\s+future\s+|now\s+or\s+in\s+the\s+future\s+|future\s+|any\s+|an?\s+)?(?:" + _SPONSORSHIP_KIND + r"\s+)?sponsorship|"
    r"(?:must|required\s+to)\s+be\s+(?:a\s+)?u\.?s\.?\s+(?:citizens?|persons?|nationals?)|u\.?s\.?\s+citizenship\s+(?:is\s+)?required|"
    r"requires?\s+u\.?s\.?\s+citizenship|(?:must|required\s+to)\s+(?:be|have)\s+(?:a\s+)?(?:u\.?s\.?\s+)?(?:citizen|permanent\s+resident|green\s+card)|"
    r"(?:open|available|limited)\s+(?:only\s+)?to\s+(?:u\.?s\.?\s+)?citizens|"
    r"(?:active|current|ability\s+to\s+obtain(?:\s+and\s+maintain)?\s+an?)\s+(?:\w+\s+){0,2}security\s+clearance|"
    r"does\s+not\s+offer\s+sponsorship|citizenship\s+is\s+required|itar|export\s+control\w*\s+(?:regulations\s+)?requir\w*)\b",
    re.IGNORECASE,
)
US_SPONSORSHIP_OFFERED = re.compile(
    r"\b(?:(?:visa|immigration|h-?1b)\s+sponsorship\s+(?:is\s+)?(?:available|provided|offered|may\s+be\s+(?:available|provided))|"
    r"(?:will|can|may)\s+(?:provide|offer|sponsor|support)\s+(?:\w+\s+){0,3}(?:visa|immigration)?\s*sponsorship|"
    r"offers?\s+sponsorship|sponsorship\s+(?:is\s+)?available|cpt|(?:stem\s+)?opt(?![- ](?:in|out))\b|curricular\s+practical\s+training|"
    r"international\s+students\s+(?:are\s+)?(?:welcome|eligible|encouraged))\b",
    re.IGNORECASE,
)
US_AUTHORIZATION_ONLY = re.compile(
    r"\b(?:must\s+be\s+(?:legally\s+)?(?:authori[sz]ed|eligible)\s+to\s+work\s+in\s+the\s+(?:u\.?s\.?|united\s+states))\b",
    re.IGNORECASE,
)
CANADA_AUTHORIZATION = re.compile(
    r"\b(?:(?:legally\s+)?(?:authori[sz]ed|eligible|entitled)\s+to\s+work\s+in\s+canada|canadian\s+work\s+(?:permit|authori[sz]ation)|"
    r"based\s+in\s+canada\s+for\s+the\s+duration)\b",
    re.IGNORECASE,
)
DESCRIPTION_COUNTRY_REMOTE = re.compile(
    r"\bremote\b[^.\n]{0,40}\b(?:in|within|across|from)\s+(?:the\s+)?(canada|united\s+states|u\.?s\.?)\b|"
    r"\b(canada|united\s+states|u\.?s\.?)[- ]?(?:based\s+)?remote\b|"
    r"\bmust\s+(?:reside|live|be\s+located|be\s+based)\s+in\s+(?:the\s+)?(canada|united\s+states|u\.?s\.?)\b",
    re.IGNORECASE,
)

CLOSED_TEXT = re.compile(
    r"\b(?:no\s+longer\s+(?:accepting|available|open)|(?:this\s+)?(?:job|position|posting|requisition|role)\s+(?:is|has\s+been)\s+(?:closed|filled|expired|cancelled)|"
    r"applications?\s+(?:are|is|have)\s+(?:now\s+)?closed|posting\s+has\s+expired|job\s+not\s+found)\b",
    re.IGNORECASE,
)
DATE_RE = (
    rf"(?:{MONTH_RE}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d{{2}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH_RE}\.?,?\s+20\d{{2}}|"
    r"20\d{2}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/20\d{2})"
)
CLOSE_DATE_TEXT = re.compile(
    rf"\b(?:apply\s+by|applications?\s+(?:close|closes|due|deadline|will\s+be\s+accepted\s+until)|closing\s+date|"
    rf"(?:application\s+)?deadline(?:\s+to\s+apply)?|posting\s+(?:end|close)s?\s+(?:date|on)?|job\s+posting\s+end\s+date|"
    rf"date\s+de\s+fin\s+d'affichage)\s*(?:is|on|:)?\s*(?:[A-Za-z]+day,?\s+)?({DATE_RE})",
    re.IGNORECASE,
)


def parse_date(value: str) -> date | None:
    value = normalize_space(value).strip(" ,.").replace(",", "")
    if not value:
        return None
    iso = re.match(r"^(20\d{2})-(\d{2})-(\d{2})", value)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    slash = re.match(r"^(\d{1,2})/(\d{1,2})/(20\d{2})$", value)
    if slash:
        first, second, year = int(slash.group(1)), int(slash.group(2)), int(slash.group(3))
        month, day = (first, second) if first <= 12 else (second, first)
        try:
            return date(year, month, day)
        except ValueError:
            return None
    words = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value, flags=re.IGNORECASE).split()
    month = day = year = None
    for word in words:
        lowered = word.casefold().rstrip(".")
        if lowered in MONTHS:
            month = MONTHS[lowered]
        elif re.fullmatch(r"20\d{2}", word):
            year = int(word)
        elif re.fullmatch(r"\d{1,2}", word):
            day = int(word)
    if month and day and year:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _timestamp_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d{10,13}", value):
        seconds = int(value) / (1000 if len(value) == 13 else 1)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
    return parse_date(value[:10]) or parse_date(value)


# --- Term detection ------------------------------------------------------------

SEASON_YEAR = re.compile(
    rf"\b(?P<s1>{SEASON_RE})(?:\s*(?:/|&|and|or|-|\+)\s*(?P<s2>{SEASON_RE}))?\s*(?:term\s+|of\s+|semester\s+)?['’]?(?P<y>20\d{{2}}|\d{{2}})\b|"
    rf"\b(?P<y2>20\d{{2}})\s+(?P<s3>{SEASON_RE})(?:\s*(?:/|&|and|or|-)\s*(?P<s4>{SEASON_RE}))?\b",
    re.IGNORECASE,
)
START_PHRASE = re.compile(
    rf"\b(?:start(?:s|ing)?(?:\s+date)?|begin(?:s|ning)?|commenc\w+|term\s*(?:dates?)?:?|(?:internship|co-?op)\s+(?:period|dates?):?|"
    rf"available\s+to\s+start)\b[^.\n]{{0,40}}?\b(?P<m>{MONTH_RE})\.?\s*(?:\d{{1,2}}(?!\d)(?:st|nd|rd|th)?,?\s*)?(?P<y>20\d{{2}})?",
    re.IGNORECASE,
)
MONTH_RANGE = re.compile(
    rf"\b(?P<m1>{MONTH_RE})\.?\s*(?:\d{{1,2}}(?!\d)(?:st|nd|rd|th)?,?\s*)?(?P<y1>20\d{{2}})?\s*(?:-|to|through|until|thru)\s*"
    rf"(?P<m2>{MONTH_RE})\.?\s*(?:\d{{1,2}}(?!\d)(?:st|nd|rd|th)?,?\s*)?(?P<y2>20\d{{2}})",
    re.IGNORECASE,
)
TERM_LENGTH = re.compile(
    r"\b(?P<n>(?:4|8|12|16|four|eight|twelve|sixteen)(?:\s*(?:,|or|/|-|and)\s*(?:4|8|12|16))*)[- ]?months?\b|"
    r"\b(?P<w>\d{1,2})[- ]?weeks?\b",
    re.IGNORECASE,
)
WORD_NUMBERS = {"four": "4", "eight": "8", "twelve": "12", "sixteen": "16"}


def _year(token: str) -> int:
    value = int(token)
    return value + 2000 if value < 100 else value


@dataclass(frozen=True)
class TermFinding:
    verdict: str  # ok | reject | review | unknown
    label: str
    quoted: str = ""


def _evaluate_season_mentions(text: str, target_year: int) -> list[tuple[str, str, int, str]]:
    found: list[tuple[str, str, int, str]] = []
    for match in SEASON_YEAR.finditer(text):
        year = _year(match.group("y") or match.group("y2"))
        if year < 2020 or year > 2035:
            continue
        seasons = [s.casefold() for s in (match.group("s1"), match.group("s2"), match.group("s3"), match.group("s4")) if s]
        quoted = quote(text, match.start(), match.end())
        for season in seasons:
            found.append((season, normalize_space(match.group(0)), year, quoted))
    return found


def _season_verdict(season: str, year: int, target_year: int) -> str:
    if year < target_year:
        return "reject"
    if year > target_year:
        return "reject"
    if season == "summer":
        return "ok"
    if season == "spring":
        return "review"
    return "reject"  # winter (Jan start) or fall/autumn (Sept start)


def _month_verdict(month: int, year: int, target_year: int) -> str:
    if year != target_year:
        return "reject"
    if 5 <= month <= 8:
        return "ok"
    if month == 4:
        return "review"
    return "reject"


def detect_term(title: str, text: str, term_hint: str, target_year: int) -> TermFinding:
    title_mentions = _evaluate_season_mentions(f"{title} {term_hint}", target_year)
    if title_mentions:
        verdicts = {(_season_verdict(season, year, target_year)) for season, _, year, _ in title_mentions}
        label = ", ".join(dict.fromkeys(raw for _, raw, _, _ in title_mentions))
        quoted = title_mentions[0][3]
        if "ok" in verdicts:
            return TermFinding("ok", label, quoted)
        if "review" in verdicts:
            return TermFinding("review", label, quoted)
        return TermFinding("reject", label, quoted)

    title_years = {int(value) for value in re.findall(r"\b20\d{2}\b", title)}
    if title_years and target_year in title_years and SUMMER_MARKER.search(title):
        return TermFinding("ok", f"Summer {target_year}", title)

    def about_graduation(start: int, end: int) -> bool:
        window = text[max(0, start - 80) : end + 40]
        return bool(GRADUATION_CONTEXT.search(window) or re.search(r"\b(?:degree|enrol\w*|class\s+of)\b", window, re.IGNORECASE))

    def soften(verdict: str, year: int, start: int, end: int) -> str:
        # Past-year dates reject only when the sentence is about the internship term itself;
        # otherwise (application windows, events) a human should look.
        window = text[max(0, start - 80) : end + 40]
        about_term = re.search(r"\b(?:internship|co-?op|work\s+term|placement|term)\b", window, re.IGNORECASE)
        return "review" if verdict == "reject" and year < target_year and not about_term else verdict

    for match in START_PHRASE.finditer(text):
        if about_graduation(match.start(), match.end()):
            continue
        month = MONTHS.get(match.group("m").casefold().rstrip("."))
        year = int(match.group("y")) if match.group("y") else None
        if not month:
            continue
        if year is None:
            following = re.search(r"\b20\d{2}\b", text[match.end() : match.end() + 60])
            year = int(following.group(0)) if following else None
        if year is None:
            continue
        verdict = soften(_month_verdict(month, year, target_year), year, match.start(), match.end())
        return TermFinding(verdict, f"{match.group('m').title()} {year} start", quote(text, match.start(), match.end()))

    for match in MONTH_RANGE.finditer(text):
        if about_graduation(match.start(), match.end()):
            continue
        m1 = MONTHS.get(match.group("m1").casefold().rstrip("."))
        m2 = MONTHS.get(match.group("m2").casefold().rstrip("."))
        y2 = int(match.group("y2"))
        y1 = int(match.group("y1")) if match.group("y1") else (y2 if m1 and m2 and m1 <= m2 else y2 - 1)
        if not m1:
            continue
        verdict = soften(_month_verdict(m1, y1, target_year), y1, match.start(), match.end())
        return TermFinding(verdict, f"{match.group('m1').title()} {y1} - {match.group('m2').title()} {y2}", quote(text, match.start(), match.end()))

    description_mentions = [
        mention for mention in _evaluate_season_mentions(text, target_year)
        if not GRADUATION_CONTEXT.search(mention[3])
    ]
    if description_mentions:
        verdicts = [(_season_verdict(season, year, target_year), raw, quoted) for season, raw, year, quoted in description_mentions]
        for verdict, raw, quoted in verdicts:
            if verdict == "ok":
                return TermFinding("ok", raw, quoted)
        for verdict, raw, quoted in verdicts:
            if verdict == "review":
                return TermFinding("review", raw, quoted)
        # Descriptions often mention other cohorts in passing; only reject when every
        # season mentioned is outside the window and at least one is a forward date.
        forward = [item for item in verdicts if re.search(rf"\b{target_year}|\b20(?:2[8-9]|3\d)", item[1])]
        if forward:
            return TermFinding("reject", forward[0][1], forward[0][2])
    return TermFinding("unknown", "")


def detect_term_length(title: str, text: str) -> str:
    for source in (title, text):
        match = TERM_LENGTH.search(source)
        if not match:
            continue
        if match.group("n"):
            value = match.group("n").casefold()
            for word, number in WORD_NUMBERS.items():
                value = value.replace(word, number)
            value = re.sub(r"\s*(?:,|or|and|-)\s*", "/", value).replace(" ", "")
            return f"{value} months"
        weeks = int(match.group("w"))
        if 6 <= weeks <= 16:
            return f"{weeks} weeks"
    return ""


# --- Graduation year ---------------------------------------------------------------

GRAD_DATE = re.compile(
    rf"(?P<season>{SEASON_RE})?\s*(?P<month>{MONTH_RE})?\.?\s*(?:\d{{1,2}}(?!\d)(?:st|nd|rd|th)?,?\s*)?(?P<year>20\d{{2}})",
    re.IGNORECASE,
)


def _grad_points(clause: str) -> list[tuple[int, int, int, int]]:
    points: list[tuple[int, int, int, int]] = []
    for match in GRAD_DATE.finditer(clause):
        year = int(match.group("year"))
        month_token = (match.group("month") or "").casefold().rstrip(".")
        season = (match.group("season") or "").casefold()
        month = MONTHS.get(month_token) or SEASON_GRADUATION_MONTH.get(season) or 0
        points.append((year, month, match.start(), match.end()))
    return points


def evaluate_graduation(text: str, grad_year: int, grad_month: int) -> tuple[str, str, str]:
    """Return (verdict, reason, quote): verdict ok | reject | review | none."""
    returning = RETURNING_STUDENT.search(text)
    final = FINAL_YEAR.search(text)
    target = grad_year * 12 + grad_month

    for context in GRADUATION_CONTEXT.finditer(text):
        window_start = max(0, context.start() - 60)
        window_end = min(len(text), context.end() + 140)
        boundary = text.find("\n", context.end())
        if boundary != -1:
            window_end = min(window_end, boundary)
        clause = text[window_start:window_end]
        points = _grad_points(clause)
        if not points:
            continue
        quoted = quote(text, context.start(), context.end())
        values = [year * 12 + (month or (grad_month if year == grad_year else 6)) for year, month, _, _ in points]
        lowered = clause.casefold()
        if len(points) >= 2 and re.search(r"\bbetween\b|\bfrom\b|-|\bto\b|\bthrough\b|\band\b", lowered):
            low, high = min(values), max(values)
            if low - 1 <= target <= high + 1:
                return "ok", "graduation window includes your date", quoted
            return "reject", "graduation window excludes your graduation date", quoted
        point_year, point_month, _, point_end = points[-1]
        after = clause[point_end : point_end + 25].casefold()
        before = clause[max(0, points[-1][2] - 30) : points[-1][2]].casefold()
        value = values[-1]
        if re.search(r"or\s+(?:later|after)|and\s+(?:beyond|later)|onwards|\+", after) or re.search(r"\b(?:after|no\s+earlier\s+than)\b", before):
            if value <= target + 1:
                return "ok", "accepts graduates from your year onward", quoted
            return "reject", "requires a graduation date later than yours", quoted
        if re.search(r"\b(?:by|before|no\s+later\s+than|prior\s+to|or\s+earlier)\b", before + after):
            if value >= target - 1:
                return "ok", "graduation deadline is after your date", quoted
            return "reject", "requires graduating before your graduation date", quoted
        years = {year for year, _, _, _ in points}
        if grad_year in years:
            return "ok", f"graduation year {grad_year} is accepted", quoted
        if all(year < grad_year for year in years):
            if returning:
                return "review", "graduation year conflicts with returning-student language", quoted
            return "reject", f"requires graduating in {', '.join(map(str, sorted(years)))}", quoted
        if all(year > grad_year for year in years):
            return "reject", f"requires graduating in {', '.join(map(str, sorted(years)))}", quoted
        return "review", "graduation requirement is ambiguous", quoted

    if returning:
        return "ok", "open to returning students", quote(text, returning.start(), returning.end())
    if final:
        return "reject", "requires final-year students or recent graduates", quote(text, final.start(), final.end())
    return "none", "graduation year not stated", ""


# --- Main entry point ---------------------------------------------------------------


# Adjacent titles the user explicitly wants from specific companies, even when the
# description does not read as product work (company name -> category labels).
REQUESTED_ADJACENT: dict[str, frozenset[str]] = {
    "teck resources limited": frozenset({"Business Analyst"}),
}


def classify(
    job: Job,
    today: date | None = None,
    target_year: int = 2027,
    grad_year: int = 2028,
    grad_month: int = 4,
    cycle_start: date = date(2026, 8, 1),
) -> Classification:
    today = today or datetime.now(timezone.utc).date()
    title = clean_title(job.title)
    text = plain_text(job.full_text)
    result = Classification(description_available=len(text) >= 200)
    result.term_length = detect_term_length(title, text)

    # 0. Role family ---------------------------------------------------------
    category = title_category(title)
    signals = product_signals(text) if text else []
    signal_note = f" (product signals: {', '.join(signals[:5])})" if signals else ""
    result.category = category.category
    result.category_label = category.label
    if category.category == "excluded":
        if result.description_available and is_product_oriented(signals, 5) and re.search(r"product\s+manag", text, re.IGNORECASE):
            result.category = "adjacent"
            result._flag(ACCEPT, f"{category.label} title, but the description is product-management work{signal_note}")
        else:
            result._flag(REJECT, f"Not a product role: {category.label}", title)
            return result
    elif category.label in REQUESTED_ADJACENT.get(normalize_space(job.company).casefold(), ()):
        result._flag(ACCEPT, f"{category.label} title (requested for {job.company})")
    elif category.category == "other" or category.needs_product_description:
        threshold = 4 if category.category == "other" else 2
        if not result.description_available:
            if category.category == "other":
                # Kept for audit; a generic title alone is not evidence of product work.
                result._flag(REJECT, "Generic title and no readable description to show product work", title)
                return result
            result._flag(REVIEW, f"{category.label} title; description could not be read to confirm product focus")
        elif is_product_oriented(signals, threshold):
            if category.category == "other":
                result.category = "adjacent"
                result.category_label = "Product-oriented (judged on description)"
            result._flag(ACCEPT, f"description is product-oriented{signal_note}")
        else:
            label = f"{category.label} title" if category.label else "Generic title"
            result._flag(REJECT, f"Not product-focused: {label} and only {len(signals)} product signal(s) in the description")
            return result
    else:
        result._flag(ACCEPT, f"{'Core PM' if category.category == 'core' else category.label} title")

    # a. Internship / co-op / student -------------------------------------------
    title_intern = INTERN_MARKER.search(title)
    employment_intern = job.employment_type and INTERN_EMPLOYMENT.search(job.employment_type)
    text_intern = INTERNSHIP_TEXT.search(text) if text else None
    if NEW_GRAD_TITLE.search(title) and not title_intern:
        result._flag(REJECT, "Full-time / new-grad role, not an internship", title)
        return result
    if not (title_intern or employment_intern or text_intern or job.term_hint):
        evidence = f"{title}" + (f" [{job.employment_type}]" if job.employment_type else "")
        result._flag(REJECT, "Not an internship, co-op or student role", evidence)
        return result
    if text_intern and not (title_intern or employment_intern):
        result._flag(ACCEPT, "description says it is an internship", quote(text, text_intern.start(), text_intern.end()))
    part_time = PART_TIME_DURING_SCHOOL.search(f"{title}\n{text}")
    if part_time and not SUMMER_MARKER.search(title):
        haystack = f"{title}\n{text}"
        result._flag(REJECT, "Part-time during the school year", quote(haystack, part_time.start(), part_time.end()))
        return result

    term = detect_term(title, text, job.term_hint, target_year)
    result.term_start = term.label
    if term.verdict == "reject":
        result._flag(REJECT, f"Term outside the Summer {target_year} window ({term.label})", term.quoted)
        return result
    if term.verdict == "review":
        result._flag(REVIEW, f"term may not start in May-Aug {target_year} ({term.label})", term.quoted)
    elif term.verdict == "unknown":
        posted = _timestamp_date(job.posted_date)
        if posted and posted < cycle_start:
            result._flag(REVIEW, f"term not stated and posted {posted.isoformat()}, before the Summer {target_year} cycle")
        else:
            result.term_start = f"Summer {target_year} (assumed)"
    else:
        result._flag(ACCEPT, f"term {term.label}")

    # b. Education level ----------------------------------------------------------
    if ADVANCED_ONLY_TITLE.search(title) and not UNDERGRAD_ALLOWED.search(title):
        result._flag(REJECT, "Graduate/MBA-only role (title)", title)
        return result
    if text:
        advanced = ADVANCED_REQUIRED.search(text)
        if advanced:
            # An undergraduate alternative only counts when it is offered in the same
            # requirement ("Bachelor's or Master's"), not anywhere in the posting.
            window = text[max(0, advanced.start() - 150) : advanced.end() + 150]
            undergrad = UNDERGRAD_ALLOWED.search(window)
            quoted = quote(text, advanced.start(), advanced.end())
            if undergrad:
                result._flag(ACCEPT, "accepts undergraduate and graduate students", quoted)
            else:
                result._flag(REJECT, "Requires MBA / Master's / PhD enrolment", quoted)
                return result
    elif not result.description_available:
        pass

    # c. Graduation year ------------------------------------------------------------
    if text:
        verdict, reason, quoted = evaluate_graduation(text, grad_year, grad_month)
        if verdict == "reject":
            result._flag(REJECT, f"Graduation year: {reason}", quoted)
            return result
        if verdict == "review":
            result._flag(REVIEW, f"graduation year: {reason}", quoted)
        elif verdict == "ok":
            result._flag(ACCEPT, reason, quoted)

    # d/e. Location, sponsorship and work authorisation ------------------------------
    evidence = location_evidence(job.location, job.country_codes, job.remote)
    countries = set(evidence.countries)
    if not countries and text:
        remote_country = DESCRIPTION_COUNTRY_REMOTE.search(text)
        if remote_country:
            named = next(group for group in remote_country.groups() if group)
            countries.add("CA" if named.casefold().startswith("can") else "US")
            result.quotes.append(quote(text, remote_country.start(), remote_country.end()))
    result.remote_status = "remote" if evidence.remote else ("onsite/hybrid" if job.location else "unknown")
    result.priority_location = evidence.gta
    if evidence.waterloo and not evidence.gta:
        result.location_note = "Waterloo Region (not GTA)"
    location_label = job.location or "no location"
    if "CA" in countries:
        result._flag(ACCEPT, f"Canada ({'GTA' if evidence.gta else location_label})")
        authorization = CANADA_AUTHORIZATION.search(text) if text else None
        if authorization:
            result.quotes.append(quote(text, authorization.start(), authorization.end()))
    elif (sponsorship_text := "\n".join(filter(None, (job.sponsorship_hint, text)))) and (
        denied := US_SPONSORSHIP_DENIED.search(sponsorship_text)
    ):
        # Any non-Canadian role (US or unstated location) that denies sponsorship is out.
        result._flag(REJECT, "Role without visa sponsorship / citizenship required", quote(sponsorship_text, denied.start(), denied.end()))
        return result
    elif "US" in countries:
        offered = US_SPONSORSHIP_OFFERED.search(sponsorship_text)
        if evidence.remote:
            result._flag(ACCEPT, "US remote")
        elif offered:
            result._flag(ACCEPT, "US role that supports sponsorship/CPT/OPT", quote(sponsorship_text, offered.start(), offered.end()))
        else:
            authorization_only = US_AUTHORIZATION_ONLY.search(sponsorship_text)
            if authorization_only:
                result._flag(REVIEW, "US onsite; requires US work authorization (sponsorship unclear)", quote(sponsorship_text, authorization_only.start(), authorization_only.end()))
            else:
                result._flag(REVIEW, "US onsite role; sponsorship not mentioned")
    elif evidence.foreign_only:
        result._flag(REJECT, f"Location outside Canada/US ({location_label})", location_label)
        return result
    elif evidence.remote:
        result._flag(REVIEW, "remote, but the eligible country is not stated", location_label)
    else:
        result._flag(REVIEW, f"location not stated ({location_label})")

    # f. Open / closed ------------------------------------------------------------------
    closed = CLOSED_TEXT.search(text) if text else None
    if closed:
        result.status = "closed"
        result._flag(REJECT, "Posting is closed", quote(text, closed.start(), closed.end()))
        return result
    close_date = _timestamp_date(job.close_date)
    if close_date is None and text:
        match = CLOSE_DATE_TEXT.search(text)
        if match:
            close_date = parse_date(match.group(1))
            if close_date:
                result.quotes.append(quote(text, match.start(), match.end()))
    if close_date:
        result.close_date = close_date.isoformat()
        if close_date < today:
            result.status = "closed"
            result._flag(REJECT, f"Application deadline passed ({close_date.isoformat()})")
            return result
    return result
