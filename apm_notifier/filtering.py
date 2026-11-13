"""Title and location vocabulary shared by the extractors and the classifier.

The title gate here is deliberately recall-oriented: it only decides whether a
posting is worth classifying and storing. Eligibility (internship status,
education, graduation year, location and sponsorship) is decided strictly in
``classify.py`` against the full posting text.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .models import normalize_space


def clean_title(title: str) -> str:
    return re.sub(r"[‐-―]", "-", normalize_space(title))


# --- Title vocabulary -------------------------------------------------------

INTERN_MARKER = re.compile(
    r"\b(?:intern(?:s|ship|ships)?|co[ -]?op|co-operative|students?|stagiaire|stage\s+(?:en|d[e'])|"
    r"summer\s+(?:analyst|associate|program(?:me)?|scholar)|work[ -]terms?|placements?|pey|"
    r"internship\s+program(?:me)?)\b",
    re.IGNORECASE,
)
SUMMER_MARKER = re.compile(r"\bsummer\b", re.IGNORECASE)
STRONG_INTERN_MARKER = re.compile(r"\b(?:intern(?:s|ship)?|co[ -]?op|stagiaire)\b", re.IGNORECASE)

CORE_TITLE = re.compile(
    r"\b(?:product\s+manag(?:er|ers|ement)|associate\s+product\s+manager|apm|pm|"
    r"(?:ai|technical|growth|platform|data)\s+product\s+manager|gestionnaire\s+de\s+produits?|"
    r"chef\s+de\s+produit)\b",
    re.IGNORECASE,
)

# (label, pattern, needs_product_description)
ADJACENT_TITLES: tuple[tuple[str, re.Pattern[str], bool], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE), conditional)
    for label, pattern, conditional in (
        ("Product Analyst", r"\bproduct\s+(?:data\s+)?analy(?:st|tics)\b", False),
        ("Product Owner", r"\bproduct\s+owner\b", False),
        ("Product Coordinator", r"\bproduct\s+coordinat(?:or|ion)\b", False),
        ("Product Associate", r"\bproduct\s+associate\b", False),
        ("Product Operations", r"\bproduct\s+(?:operations?|ops)\b", False),
        ("Product Strategy", r"\bproduct\s+strateg(?:y|ist)\b", False),
        ("Product Marketing", r"\bproduct\s+marketing\b", False),
        ("Product Specialist", r"\bproduct\s+specialist\b", False),
        ("Product Growth", r"\bproduct\s+growth\b|\bgrowth\s+(?:intern|co-?op|analyst|associate|strategy|strategist|marketing|operations|product|pm)\b|^growth\b", False),
        ("Product Research", r"\bproduct\s+(?:research|insights?)\b", False),
        ("Product Innovation", r"\bproduct\s+(?:innovation|development\s+program)\b", False),
        ("Digital Product/Strategy", r"\bdigital\s+(?:operations?|products?|strategy|transformation)\b", False),
        ("Program Manager", r"\b(?:technical\s+)?program(?:me)?\s+manag(?:er|ement)\b|\btpm\b", False),
        ("Strategy & Operations", r"\bstrategy\s*(?:&|and)\s*operations\b|\bbiz\s*ops\b|\bbusiness\s+operations\b", False),
        ("Innovation/Venture", r"\b(?:innovation|ventures?|incubat(?:ion|or))\b", True),
        ("Technology/AI Strategy", r"\b(?:technology|tech|ai|digital)\s+strategy\b", False),
        ("Product Engineer", r"\bproduct\s+engineer(?:ing)?\b", True),
        ("Product Builder", r"\bproduct\s+builder\b", True),
        ("Forward Deployed/Solutions", r"\bforward[ -]deployed\b|\bsolutions?\s+(?:engineer|consultant|architect|analyst|specialist)\b", True),
        ("Project Manager", r"\bproject\s+(?:manag(?:er|ement)|coordinator)\b", True),
        ("Business Analyst", r"\bbusiness\s+(?:systems?\s+)?analyst\b", True),
        ("Customer Insights", r"\b(?:customer|consumer|user)\s+(?:insights?|research)\b", True),
    )
)

# Pure functions the user does not want, unless the description is clearly PM work.
EXCLUDED_FUNCTION = re.compile(
    r"\b(?:software|frontend|front[ -]end|backend|back[ -]end|full[ -]?stack|mobile|ios|android|devops|"
    r"site\s+reliability|sre|qa|quality\s+assurance|test|firmware|hardware|embedded|network|security|cyber|"
    r"cloud|data|ml|machine\s+learning|ai|research|silicon|asic|verification|electrical|mechanical|civil|"
    r"chemical|manufacturing|systems?|platform|infrastructure)\s+(?:engineer(?:ing)?|developer|development)\b|"
    r"\bswe\b|\bsde\b|\bdeveloper\b|\bprogrammer\b|\bdata\s+scien(?:ce|tist)\b|\bmachine\s+learning\b|"
    r"\bresearch\s+scientist\b|\bapplied\s+scientist\b|\bscientist\b|"
    r"\b(?:ux|ui|visual|graphic|product|interaction|industrial|motion|brand|system)\s+design(?:er)?\b|\bdesigner\b|"
    r"\bdesign\s+(?:intern(?:ship)?|co-?op)\b|\b(?:controller|treasury|actuarial)\b|"
    r"\bsales\b|\bsdr\b|\bbdr\b|\baccount\s+executive\b|\bbusiness\s+development\s+representative\b|"
    r"\baccount(?:ant|ing)\b|\baudit(?:or|ing)?\b|\btax\b|\bhr\b|\bhuman\s+resources\b|\bpeople\s+(?:operations|partner)\b|"
    r"\btalent\s+acquisition\b|\brecruit(?:er|ing|ment)\b|\blegal\b|\bparalegal\b|\bcounsel\b|\blaw\s+(?:clerk|student)\b|"
    r"\badministrative\b|\badmin\s+assistant\b|\breceptionist\b|\bengineers?\b",
    re.IGNORECASE,
)


SENIORITY = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|director|head|lead|vice\s+president|vp|manager,\s+product\s+management|"
    r"group\s+product\s+manager|gpm|[2-9]\+?\s+years|ii|iii|iv)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TitleCategory:
    category: str  # core | adjacent | other | excluded
    label: str = ""
    needs_product_description: bool = False


def title_category(title: str) -> TitleCategory:
    """Categorize the role itself; team names in brackets only count when the role is generic."""
    candidate = clean_title(title)
    role = normalize_space(re.sub(r"[(\[（][^)\]）]*[)\]）]", " ", candidate))
    if role and role != candidate:
        category = _categorize(role)
        if category.category != "other":
            return category
    return _categorize(candidate)


# Broad keywords that describe a team or theme more often than the role itself;
# an explicit excluded function ("Software Engineer ... Innovation") wins over them.
LOOSE_ADJACENT = {"Product Growth", "Innovation/Venture", "Technology/AI Strategy", "Strategy & Operations"}


def _categorize(candidate: str) -> TitleCategory:
    product_design = re.search(r"\bproduct\s+design(?:er)?\b", candidate, re.IGNORECASE)
    if CORE_TITLE.search(candidate) and not product_design:
        return TitleCategory("core", "Product Manager")
    loose: TitleCategory | None = None
    for label, pattern, conditional in ADJACENT_TITLES:
        if pattern.search(candidate) and not product_design:
            if label not in LOOSE_ADJACENT:
                return TitleCategory("adjacent", label, conditional)
            loose = loose or TitleCategory("adjacent", label, conditional)
    match = EXCLUDED_FUNCTION.search(candidate)
    if match:
        return TitleCategory("excluded", normalize_space(match.group(0)))
    return loose or TitleCategory("other")


def title_years(title: str) -> set[int]:
    return {int(value) for value in re.findall(r"\b20\d{2}\b", title)}


class RoleFilter:
    """Recall-oriented gate deciding which postings are classified and stored."""

    def __init__(self, target_year: int = 2027) -> None:
        self.target_year = target_year

    def matches(self, title: str) -> bool:
        candidate = clean_title(title)
        if len(candidate) < 4 or len(candidate) > 220 or re.search(r"\w_\w", candidate):
            return False  # Icon names and template keys are not job titles.
        years = title_years(candidate)
        # A title naming only earlier cohorts ("Summer 2026") can never qualify.
        if years and max(years) < self.target_year:
            return False
        senior = SENIORITY.search(candidate)
        if INTERN_MARKER.search(candidate) and not (senior and not STRONG_INTERN_MARKER.search(candidate)):
            return True
        # Without an intern marker only product-family titles are kept (stored as audited
        # rejects unless the description shows an internship); senior titles never qualify.
        if senior:
            return False
        category = title_category(candidate)
        return category.category in {"core", "adjacent"}


# --- Location vocabulary ----------------------------------------------------

US_TERMS = (
    "united states", "united states of america", "usa", "u.s.", "alabama", "alaska", "arizona", "arkansas",
    "california", "colorado", "connecticut", "delaware", "district of columbia", "florida", "georgia, us",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee",
    "texas", "utah", "vermont", "virginia", "washington state", "west virginia", "wisconsin", "wyoming",
    "atlanta", "austin", "bellevue", "boston", "boulder", "brooklyn", "cambridge, ma", "charlotte", "chicago",
    "cupertino", "dallas", "denver", "detroit", "houston", "irvine", "jersey city", "kirkland", "los angeles",
    "los gatos", "menlo park", "miami", "minneapolis", "mountain view", "nashville", "new york city", "nyc",
    "palo alto", "philadelphia", "phoenix", "pittsburgh", "plano", "portland", "raleigh", "redmond",
    "redwood city", "salt lake city", "san diego", "san francisco", "san jose", "san mateo", "santa clara",
    "santa monica", "seattle", "sunnyvale", "washington dc", "washington, dc", "washington, d.c.",
    "mount laurel", "wilmington", "columbus", "st. louis", "kansas city", "lehi", "el segundo", "culver city",
    "northbrook", "tempe", "scottsdale", "durham", "reston", "mclean", "arlington",
    "sf", "la", "nyc", "dc", "silicon valley", "bay area", "sf bay area", "remote in usa", "us remote",
)
CA_TERMS = (
    "canada", "alberta", "british columbia", "manitoba", "new brunswick", "newfoundland", "nova scotia",
    "nunavut", "ontario", "prince edward island", "quebec", "québec", "saskatchewan", "yukon",
    "northwest territories", "burnaby", "calgary", "edmonton", "halifax", "hamilton, on", "kanata",
    "kitchener", "laval", "london, on", "london, ontario", "markham", "mississauga", "montreal", "montréal",
    "oakville", "ottawa", "quebec city", "regina", "richmond hill", "saskatoon", "surrey, bc", "toronto",
    "vancouver", "vaughan", "victoria, bc", "waterloo", "winnipeg", "brampton", "north york", "etobicoke",
    "scarborough", "burlington, on", "guelph", "fredericton", "moncton", "st. john's", "red deer", "gatineau",
)
GTA_TERMS = (
    "toronto", "gta", "greater toronto", "north york", "scarborough", "etobicoke", "markham", "mississauga",
    "brampton", "vaughan", "richmond hill", "oakville", "pickering", "ajax", "whitby", "oshawa", "milton",
    "newmarket", "aurora", "concord, on", "thornhill", "woodbridge",
)
WATERLOO_TERMS = ("waterloo", "kitchener", "cambridge, on", "cambridge, ontario")


def _term_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![a-z])(?:{alternatives})(?![a-z])", re.IGNORECASE)


US_PATTERN = _term_pattern(US_TERMS)
CA_PATTERN = _term_pattern(CA_TERMS)
GTA_PATTERN = _term_pattern(GTA_TERMS)
WATERLOO_PATTERN = _term_pattern(WATERLOO_TERMS)
US_STATE_ABBREVIATION = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)"
    r"(?![A-Za-z])",
)
CA_PROVINCE_ABBREVIATION = re.compile(
    r"(?:,|\s-)\s*(?:ON|BC|QC|AB|MB|NS|NB|SK|NL|PE|PEI)(?![A-Za-z])",
)
US_CODE = re.compile(r"(?<![A-Za-z])(?:US|USA)(?![A-Za-z])")
CA_CODE = re.compile(r"(?<![A-Za-z])CAN(?![A-Za-z])")
REMOTE = re.compile(r"\b(?:remote|work\s+from\s+home|home[ -]based|virtual|distributed|anywhere)\b", re.IGNORECASE)
GENERIC_LOCATION = re.compile(
    r"^(?:\d+\s+locations?|multiple\s+locations?|various(?:\s+locations?)?|remote|hybrid|flexible|"
    r"n/?a|tbd|anywhere|global|worldwide|[\s,;/|-]*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LocationEvidence:
    countries: frozenset[str]
    remote: bool
    generic: bool  # Empty or uninformative ("2 Locations", "Remote").
    gta: bool
    waterloo: bool

    @property
    def foreign_only(self) -> bool:
        return not self.countries and not self.generic


def location_evidence(location: str, country_codes: tuple[str, ...] = (), remote: bool | None = None) -> LocationEvidence:
    text = normalize_space(location)
    countries: set[str] = set()
    for code in country_codes:
        code = code.strip().upper()
        if code in {"US", "USA"}:
            countries.add("US")
        elif code in {"CA", "CAN"}:
            countries.add("CA")
    if CA_PATTERN.search(text) or CA_PROVINCE_ABBREVIATION.search(text) or CA_CODE.search(text):
        countries.add("CA")
    if US_PATTERN.search(text) or US_CODE.search(text):
        countries.add("US")
    if US_STATE_ABBREVIATION.search(text):
        # ", CA" usually means California, but Canadian feeds also emit "Toronto, ON, CA".
        stripped = re.sub(r",\s*CA(?![A-Za-z])", "", text) if "CA" in countries else text
        if US_STATE_ABBREVIATION.search(stripped) or "CA" not in countries:
            countries.add("US")
    pieces = [piece for piece in re.split(r"[;|]", text) if piece.strip()]
    generic = not pieces or all(GENERIC_LOCATION.match(piece.strip()) for piece in pieces)
    if country_codes and not countries:
        generic = False  # An explicit non-US/CA country code is decisive.
    is_remote = bool(remote) or bool(REMOTE.search(text))
    return LocationEvidence(
        countries=frozenset(countries),
        remote=is_remote,
        generic=generic,
        gta=bool(GTA_PATTERN.search(text)),
        waterloo=bool(WATERLOO_PATTERN.search(text)),
    )
