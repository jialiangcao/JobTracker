"""Non-regex filters applied after the DB rule set: posting age and US location.

These live in code rather than in `filter_rules` because neither is expressible as a
regex over one field. Age needs arithmetic against the current date, and location needs
to reconcile the wildly inconsistent strings boards actually emit — "Chicago, IL",
"Chicago, United States", "US / Canada", "Toronto, New York, San Francisco", and (for
boards that put the work model where the city belongs) "In-Office", with the real city
only in the title.

Both are fail-open: a posting with no date, or with no recognizable geography anywhere,
is kept. Missing a real internship costs more than showing one extra.
"""

import re
from datetime import UTC, datetime, timedelta

from jobtrack.schema import JobPosting

# State abbreviations only count when comma- or delimiter-preceded ("Portland, OR"), so
# the "OR" in "Dublin OR London" doesn't read as Oregon. Case-sensitive: postings write
# them uppercase, and lowercase "us"/"in" appear constantly in prose.
_US_ABBREV = re.compile(
    r"(?:^|[,;/•|])\s*(?:A[LKZR]|C[AOT]|D[CE]|FL|GA|HI|I[DLAN]|K[SY]|LA|M[DAIENSOT]"
    r"|N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY])(?=$|[,;/•|)\s])"
    r"|\bU\.?S\.?A?\.?(?=$|[^\w.])"
)

_US_NAME = re.compile(
    r"united states"
    r"|\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware"
    r"|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana"
    r"|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana"
    r"|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina"
    r"|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina"
    r"|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia"
    r"|wisconsin|wyoming)\b"
    r"|\b(?:san francisco|nyc|new york city|brooklyn|seattle|austin|chicago|boston"
    r"|los angeles|atlanta|denver|boulder|dallas|houston|philadelphia|phoenix"
    r"|san diego|san mateo|mountain view|palo alto|menlo park|sunnyvale|santa clara"
    r"|bellevue|redmond|cupertino|arlington|mclean|pittsburgh|detroit|ann arbor"
    r"|minneapolis|madison|miami|portland|salt lake city|raleigh|durham|charlotte"
    r"|nashville|columbus|kansas city|st\.? louis|las vegas|sacramento|irvine"
    r"|jersey city|hoboken|stamford|princeton|bentonville"
    r"|remote\s*[-\u2013\u2014(]*\s*us)\b",
    re.IGNORECASE,
)

# Deliberately omits names that are also US places — Cambridge, Manchester, Birmingham,
# Ontario, Athens, Lima, Valencia, Wellington — to avoid excluding US postings.
_NON_US = re.compile(
    r"\b(?:canada|toronto|vancouver|montreal|ottawa|calgary|waterloo"
    r"|united kingdom|england|scotland|wales|london|edinburgh|glasgow|belfast|bristol"
    r"|ireland|dublin|cork"
    r"|germany|berlin|munich|münchen|hamburg|frankfurt|cologne|stuttgart"
    r"|france|paris|lyon|toulouse|nantes"
    r"|netherlands|amsterdam|rotterdam|utrecht|eindhoven"
    r"|spain|madrid|barcelona|italy|rome|milan|turin|bologna"
    r"|sweden|stockholm|denmark|copenhagen|norway|oslo|finland|helsinki"
    r"|poland|warsaw|kraków|krakow|wrocław|wroclaw|gdansk"
    r"|czech|czechia|prague|hungary|budapest|romania|bucharest|cluj"
    r"|bulgaria|sofia|serbia|belgrade|croatia|zagreb|slovakia|bratislava|slovenia"
    r"|portugal|lisbon|porto|greece|switzerland|zurich|zürich|geneva|lausanne"
    r"|austria|vienna|belgium|brussels|ghent|luxembourg|iceland|reykjavik"
    r"|ukraine|kyiv|kiev|lithuania|vilnius|latvia|riga|estonia|tallinn"
    r"|israel|tel aviv|jerusalem|haifa|turkey|istanbul|ankara"
    r"|india|bengaluru|bangalore|mumbai|new delhi|gurugram|gurgaon|noida|hyderabad"
    r"|pune|chennai|kolkata|ahmedabad|jaipur"
    r"|singapore|japan|tokyo|osaka|kyoto|china|beijing|shanghai|shenzhen|guangzhou"
    r"|hong kong|taiwan|taipei|korea|seoul|vietnam|hanoi|ho chi minh"
    r"|thailand|bangkok|indonesia|jakarta|philippines|manila|malaysia|kuala lumpur"
    r"|pakistan|karachi|lahore|islamabad|bangladesh|dhaka|sri lanka|colombo"
    r"|australia|sydney|melbourne|brisbane|canberra|adelaide|new zealand|auckland"
    r"|brazil|são paulo|sao paulo|rio de janeiro|belo horizonte"
    r"|mexico|guadalajara|monterrey|argentina|buenos aires|chile|santiago"
    r"|colombia|bogotá|bogota|medellín|medellin|peru|uruguay|montevideo|costa rica"
    r"|united arab emirates|uae|dubai|abu dhabi|saudi arabia|riyadh|qatar|doha"
    r"|egypt|cairo|nigeria|lagos|kenya|nairobi|south africa|cape town|johannesburg"
    r"|morocco|tunisia|ghana|accra"
    r"|emea|apac|latam|anz)\b",
    re.IGNORECASE,
)


def is_recent(job: JobPosting, max_age_days: int, now: datetime | None = None) -> bool:
    """False only when the posting has a date and that date is older than the window."""
    if max_age_days <= 0 or job.posted_at is None:
        return True
    return job.posted_at >= (now or datetime.now(UTC)) - timedelta(days=max_age_days)


def is_us_location(job: JobPosting) -> bool:
    """Location first; fall back to the title for boards that put the work model
    ("In-Office", "Hybrid") in the location field and the city in the title.

    A US signal wins over a non-US one in the same string, so multi-site listings like
    "Toronto, New York, San Francisco" are kept. The cost is that a US city name inside
    a foreign location ("San José, Costa Rica") reads as US.
    """
    for text in (job.location or "", job.title):
        if not text:
            continue
        if _US_ABBREV.search(text) or _US_NAME.search(text):
            return True
        if _NON_US.search(text):
            return False
    return True


def is_eligible(
    job: JobPosting, *, max_age_days: int, us_only: bool, now: datetime | None = None
) -> bool:
    if not is_recent(job, max_age_days, now):
        return False
    return not (us_only and not is_us_location(job))
